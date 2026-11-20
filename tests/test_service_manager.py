"""Gestionnaire de jobs : admission, files par classe, barrière exclusive, annulation, drainage, arrêt."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.service import ApiError, ErrorCode, EventType, ExecutionContext, ListSink, Model, operation
from core.service import manager as manager_module
from core.service.events import Event, JobState
from core.service.manager import JobManager
from core.service.operations import Catalog

_GATES: dict[str, threading.Event] = {}
_STARTED: list[str] = []


@dataclass(frozen=True)
class _Request(Model):
    name: str = ""


@dataclass(frozen=True)
class _Result(Model):
    name: str
    config: str = ""
    restart_required: bool = False


def _blocking(request: _Request, ctx: ExecutionContext) -> _Result:
    _STARTED.append(request.name)
    gate = _GATES.setdefault(request.name, threading.Event())
    while not gate.wait(0.01):
        ctx.cancel.raise_if_cancelled()
    ctx.events.progress(pct=100.0)
    return _Result(name=request.name, config=str(getattr(ctx.config, "marker", "")))


def _make(name: str, **kwargs: Any) -> Any:
    return operation(name, request=_Request, result=_Result, summary=name, **kwargs)(_blocking)


@operation("t.update", request=_Request, result=_Result, summary="Mise à jour", queue="exclusive")
def _update(request: _Request, ctx: ExecutionContext) -> _Result:
    gate = _GATES.setdefault(request.name, threading.Event())  # libérée par le test : file remplie avant la fin
    while not gate.wait(0.01):
        ctx.cancel.raise_if_cancelled()
    return _Result(name=request.name, restart_required=True)


@operation("t.cross", request=_Request, result=_Result, summary="Annulation croisée")
def _cross(request: _Request, ctx: ExecutionContext) -> _Result:
    ctx.cancel.on_cancel(lambda: ctx.events.log("info", "annulation reçue"))  # rappel qui émet
    _STARTED.append(request.name)
    _GATES.setdefault("cross", threading.Event()).wait(5)
    ctx.events.progress(pct=50.0)  # la destination annule l'autre job pendant cette livraison
    ctx.cancel.wait(5)
    ctx.cancel.raise_if_cancelled()
    return _Result(name=request.name)


_SIDE: dict[str, threading.Event] = {}


@operation("t.side", request=_Request, result=_Result, summary="Livraison par un autre fil")
def _side(request: _Request, ctx: ExecutionContext) -> _Result:
    """Un fil auxiliaire livre une progression (destination bloquée) : la fin du job est livrée par lui, plus tard."""
    threading.Thread(target=lambda: ctx.events.progress(pct=10.0), daemon=True).start()
    assert _SIDE["blocked"].wait(5)
    return _Result(name=request.name)


@operation("t.watch", request=_Request, result=_Result, summary="Annulation journalisée")
def _watch(request: _Request, ctx: ExecutionContext) -> _Result:
    ctx.cancel.on_cancel(lambda: ctx.events.log("info", f"annulation {request.name}"))
    _STARTED.append(request.name)
    ctx.cancel.wait(10)
    ctx.cancel.raise_if_cancelled()
    return _Result(name=request.name)


_HELD: dict[str, threading.Event] = {}


@operation("t.held", request=_Request, result=_Result, summary="Rappel d'annulation suspendu avant d'émettre")
def _held(request: _Request, ctx: ExecutionContext) -> _Result:
    def callback() -> None:
        _HELD["inside"].set()
        _HELD["resume"].wait(10)
        ctx.events.log("info", "rappel tardif")

    ctx.cancel.on_cancel(callback)
    _STARTED.append(request.name)
    ctx.cancel.wait(10)
    ctx.cancel.raise_if_cancelled()
    return _Result(name=request.name)


_CATALOG = Catalog([_make("t.analysis"), _make("t.job", queue="job"), _make("t.exclusive", queue="exclusive"),
                    _make("t.secret", queue="exclusive", privileged=True), _update, _cross, _side, _watch, _held])


@pytest.fixture(autouse=True)
def _reset():
    _GATES.clear()
    _STARTED.clear()
    yield
    for gate in _GATES.values():
        gate.set()


def _release(name: str) -> None:
    _GATES.setdefault(name, threading.Event()).set()


def _until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "délai dépassé"
        time.sleep(0.005)


def _manager(sink: Any = None, **kwargs: Any) -> tuple[JobManager, ListSink]:
    sink = sink if sink is not None else ListSink()
    configs = iter(range(1, 1000))
    return JobManager(_CATALOG, lambda: SimpleNamespace(marker=f"c{next(configs)}"), sink, **kwargs), sink


def _state(manager: JobManager, job: str) -> JobState:
    return manager.get(job).state


def _done(manager: JobManager, *jobs: str) -> None:
    _until(lambda: all(_state(manager, job) in (JobState.SUCCEEDED, JobState.CANCELLED, JobState.FAILED)
                       for job in jobs))


def test_quotas_per_class_and_fifo():
    manager, _ = _manager(max_jobs=1, max_analyses=2)
    jobs = [manager.submit("t.job", {"name": f"j{i}"}) for i in range(2)]
    analyses = [manager.submit("t.analysis", {"name": f"a{i}"}) for i in range(3)]
    _until(lambda: sorted(_STARTED) == ["a0", "a1", "j0"])
    assert _state(manager, jobs[1]) is JobState.QUEUED and _state(manager, analyses[2]) is JobState.QUEUED
    _release("a1")
    _until(lambda: "a2" in _STARTED)
    _release("j0")
    _until(lambda: "j1" in _STARTED)
    for name in ("a0", "a2", "j1"):
        _release(name)
    _done(manager, *jobs, *analyses)
    manager.shutdown()


def test_exclusive_waits_for_everything_and_is_a_barrier():
    manager, _ = _manager(max_jobs=1, max_analyses=2)
    job = manager.submit("t.job", {"name": "j"})
    _until(lambda: _STARTED == ["j"])
    exclusive = manager.submit("t.exclusive", {"name": "x"})
    later = manager.submit("t.analysis", {"name": "a"})  # soumise après l'exclusive : attend derrière elle
    time.sleep(0.05)
    assert _STARTED == ["j"] and _state(manager, exclusive) is JobState.QUEUED
    _release("j")
    _until(lambda: _STARTED == ["j", "x"])
    time.sleep(0.05)
    assert _state(manager, later) is JobState.QUEUED  # rien ne démarre pendant l'exclusive
    _release("x")
    _until(lambda: _STARTED == ["j", "x", "a"])
    _release("a")
    _done(manager, job, exclusive, later)
    manager.shutdown()


def test_events_are_sequenced_from_queued_and_announced_after_admission():
    sink = ListSink()
    seen_at_admission: list[int] = []
    manager, _ = _manager(sink)
    _release("q")
    job = manager.submit("t.analysis", {"name": "q"},
                         on_admitted=lambda job_id: seen_at_admission.append(
                             sum(event.job == job_id for event in sink.events)))
    _done(manager, job)
    events = [event for event in sink.events if event.job == job]
    assert seen_at_admission == [0]
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert events[0].data["state"] == "queued" and events[-1].data["state"] == "succeeded"
    info = manager.get(job)
    assert info.result == {"name": "q", "config": "c1", "restart_required": False} and info.progress == {"pct": 100.0}
    manager.shutdown()


def test_cancel_queued_running_and_during_admission():
    sink = ListSink()
    manager, _ = _manager(sink, max_jobs=1)
    running = manager.submit("t.job", {"name": "r"})
    queued = manager.submit("t.job", {"name": "q"})
    _until(lambda: _STARTED == ["r"])
    assert manager.cancel(queued) and _state(manager, queued) is JobState.CANCELLED
    assert [e.type for e in sink.events if e.job == queued][-2:] == [EventType.ERROR, EventType.STATE]
    assert manager.cancel(running)
    _done(manager, running)
    assert _state(manager, running) is JobState.CANCELLED and not manager.cancel(running)
    early = manager.submit("t.job", {"name": "e"}, on_admitted=manager.cancel)
    assert _state(manager, early) is JobState.CANCELLED and "e" not in _STARTED
    assert [e.data.get("state") for e in sink.events if e.job == early] == ["queued", None, "cancelled"]
    manager.shutdown()


def test_a_sink_may_call_back_the_manager():
    holder: dict[str, JobManager] = {}

    class _Reentrant(ListSink):
        def emit(self, event: Event) -> None:
            super().emit(event)
            holder["m"].get(event.job)
            holder["m"].list()

    manager, _ = _manager(_Reentrant())
    holder["m"] = manager
    _release("z")
    job = manager.submit("t.analysis", {"name": "z"})
    _done(manager, job)
    manager.cancel(job)
    manager.shutdown()


def test_admission_refusals_create_no_job(tmp_path):
    manager, _ = _manager()
    refusals = [(("t.secret", {}), ErrorCode.FORBIDDEN), (("t.nope", {}), ErrorCode.UNKNOWN_OPERATION),
                (("t.analysis", {"bogus": 1}), ErrorCode.INVALID_REQUEST)]
    for (operation_name, payload), code in refusals:
        with pytest.raises(ApiError) as caught:
            manager.submit(operation_name, payload)
        assert caught.value.code is code
    with pytest.raises(ApiError):
        manager.submit("t.analysis", {}, base_dir="relatif")  # type: ignore[arg-type]
    assert manager.list() == ()
    privileged, _ = _manager(allow_privileged=True)
    _release("s")
    _done(privileged, privileged.submit("t.secret", {"name": "s"}))
    privileged.shutdown()


def test_queue_limit(monkeypatch):
    monkeypatch.setattr(manager_module, "QUEUE_LIMIT", 2)
    manager, _ = _manager(max_jobs=1)
    manager.submit("t.job", {"name": "a"})
    _until(lambda: _STARTED == ["a"])
    manager.submit("t.job", {"name": "b"})
    manager.submit("t.job", {"name": "c"})
    with pytest.raises(ApiError) as caught:
        manager.submit("t.job", {"name": "d"})
    assert caught.value.code is ErrorCode.BUSY
    manager.shutdown()


def test_configuration_is_renewed_after_an_exclusive_job():
    manager, _ = _manager()
    for name in ("a", "x", "b"):
        _release(name)
    first = manager.submit("t.analysis", {"name": "a"})
    _done(manager, first)
    _done(manager, manager.submit("t.exclusive", {"name": "x"}))
    after = manager.submit("t.analysis", {"name": "b"})
    _done(manager, after)
    assert manager.get(first).result["config"] == "c1"  # type: ignore[index]
    assert manager.get(after).result["config"] == "c2"  # type: ignore[index]
    manager.shutdown()


def test_restart_required_drains_the_manager():
    manager, sink = _manager(max_jobs=1)
    update = manager.submit("t.update", {"name": "u"})
    waiting = manager.submit("t.job", {"name": "w"})  # derrière l'exclusive : annulé par le drainage
    _release("u")
    _done(manager, update, waiting)
    assert manager.draining and _state(manager, waiting) is JobState.CANCELLED and "w" not in _STARTED
    with pytest.raises(ApiError) as caught:
        manager.submit("t.analysis", {"name": "n"})
    assert caught.value.code is ErrorCode.BUSY
    manager.shutdown()


def test_history_is_bounded(monkeypatch):
    monkeypatch.setattr(manager_module, "HISTORY_LIMIT", 3)
    manager, _ = _manager()
    jobs = []
    for index in range(5):
        _release(f"h{index}")
        jobs.append(manager.submit("t.analysis", {"name": f"h{index}"}))
        _done(manager, jobs[-1])
    # Un job terminé reste connu jusqu'à la publication de ses événements finaux, qui suit son état terminal :
    # l'historique ne revient à sa borne qu'après cette publication.
    _until(lambda: len(manager.list()) <= 3)
    assert [item.job for item in manager.list()] == jobs[-3:]
    with pytest.raises(ApiError):
        manager.get(jobs[0])
    manager.shutdown()


def test_shutdown_cancels_and_waits():
    manager, _ = _manager(max_jobs=1)
    running = manager.submit("t.job", {"name": "r"})
    queued = manager.submit("t.job", {"name": "q"})
    _until(lambda: _STARTED == ["r"])
    manager.shutdown(timeout=5)
    assert _state(manager, running) is JobState.CANCELLED and _state(manager, queued) is JobState.CANCELLED
    assert not any(thread.name.startswith("job-t.") and thread.is_alive() for thread in threading.enumerate())
    with pytest.raises(ApiError) as caught:
        manager.submit("t.analysis", {"name": "x"})
    assert caught.value.code is ErrorCode.BUSY


def test_job_states_are_published():
    schema = manager_module.JobInfo.json_schema()
    assert schema["properties"]["state"]["enum"] == [state.value for state in JobState]


def test_cross_cancellations_from_sinks_never_deadlock():
    jobs: list[str] = []
    meeting = threading.Barrier(2)
    holder: dict[str, JobManager] = {}

    class _Crossing(ListSink):
        def emit(self, event: Event) -> None:
            super().emit(event)
            if event.type is EventType.PROGRESS:  # les deux livraisons en cours en même temps
                try:
                    meeting.wait(5)
                except threading.BrokenBarrierError:
                    pass
                holder["m"].cancel(jobs[1 - jobs.index(event.job)])

    manager, sink = _manager(_Crossing(), max_analyses=2)
    holder["m"] = manager
    jobs.extend(manager.submit("t.cross", {"name": name}) for name in ("a", "b"))
    _until(lambda: sorted(_STARTED) == ["a", "b"])
    _release("cross")
    _until(lambda: all(_state(manager, job) is JobState.CANCELLED for job in jobs), timeout=10)
    _until(lambda: sum(e.type is EventType.LOG for e in sink.events) == 2)
    manager.shutdown(timeout=5)


def test_a_submission_during_an_announcement_respects_the_barrier():
    manager, _ = _manager(max_analyses=2)
    inner: dict[str, str] = {}
    exclusive = manager.submit("t.exclusive", {"name": "x"}, on_admitted=lambda _job: inner.setdefault(
        "after", manager.submit("t.analysis", {"name": "after"})))
    _until(lambda: _STARTED == ["x"])
    assert _state(manager, inner["after"]) is JobState.QUEUED
    _release("x")
    _until(lambda: _STARTED == ["x", "after"])
    _release("after")
    _done(manager, exclusive, inner["after"])
    manager.shutdown()


def test_queue_limit_counts_jobs_being_announced(monkeypatch):
    monkeypatch.setattr(manager_module, "QUEUE_LIMIT", 1)
    manager, _ = _manager(max_jobs=1)
    manager.submit("t.job", {"name": "a"})
    _until(lambda: _STARTED == ["a"])
    codes: list[ErrorCode] = []

    def nested(_job: str) -> None:
        with pytest.raises(ApiError) as caught:
            manager.submit("t.job", {"name": "c"})
        codes.append(caught.value.code)

    manager.submit("t.job", {"name": "b"}, on_admitted=nested)
    assert codes == [ErrorCode.BUSY] and len(manager.list()) == 2
    manager.shutdown()


def test_terminal_events_follow_the_recorded_end():
    seen: list[tuple[str, str | None, bool]] = []
    holder: dict[str, JobManager] = {}

    class _Watching(ListSink):
        def emit(self, event: Event) -> None:
            super().emit(event)
            if event.type in (EventType.RESULT, EventType.ERROR) or (
                    event.type is EventType.STATE and event.data["state"] in ("succeeded", "cancelled")):
                info = holder["m"].get(event.job)
                seen.append((info.state.value, event.data.get("state"), info.result is not None or
                             info.error is not None))

    manager, _ = _manager(_Watching(), max_jobs=1)
    holder["m"] = manager
    _release("ok")
    _done(manager, manager.submit("t.job", {"name": "ok"}))
    cancelled = manager.submit("t.job", {"name": "stop"})
    _until(lambda: "stop" in _STARTED)
    manager.cancel(cancelled)
    _done(manager, cancelled)
    _until(lambda: len(seen) == 4)
    assert seen == [("succeeded", None, True), ("succeeded", "succeeded", True), ("cancelled", None, True),
                    ("cancelled", "cancelled", True)]
    manager.shutdown()


@pytest.mark.parametrize("ending", ["shutdown", "drain"])
def test_cancellations_beyond_the_history_keep_every_job_known_until_published(monkeypatch, ending):
    monkeypatch.setattr(manager_module, "HISTORY_LIMIT", 3)
    holder: dict[str, JobManager] = {}

    class _Asking(ListSink):
        def emit(self, event: Event) -> None:
            super().emit(event)
            holder["m"].get(event.job)  # « Job inconnu » si le job a quitté l'historique avant ses événements

    manager, sink = _manager(_Asking(), max_jobs=1)
    holder["m"] = manager
    first = manager.submit("t.update" if ending == "drain" else "t.job", {"name": "r"})
    queued = [manager.submit("t.job", {"name": f"q{index}"}) for index in range(6)]
    if ending == "drain":
        _release("r")  # mise à jour terminée une fois la file remplie ; suivie ensuite par ses événements
        _until(lambda: any(e.job == first and e.data.get("state") == "succeeded" for e in sink.events))
        _until(lambda: sum(e.type is EventType.STATE and e.data["state"] == "cancelled" for e in sink.events) == 6)
    else:
        _until(lambda: _STARTED == ["r"])
    manager.shutdown(timeout=5)
    cancelled = {e.job for e in sink.events if e.type is EventType.STATE and e.data["state"] == "cancelled"}
    assert set(queued) <= cancelled and len(manager.list()) == 3
    assert not any(thread.name.startswith("job-t.") and thread.is_alive() for thread in threading.enumerate())


def test_a_job_stays_known_until_its_end_is_delivered_by_another_thread(monkeypatch):
    monkeypatch.setattr(manager_module, "HISTORY_LIMIT", 1)
    _SIDE["blocked"], _SIDE["open"] = threading.Event(), threading.Event()
    holder: dict[str, JobManager] = {}
    failures: list[BaseException] = []

    class _Slow(ListSink):
        def emit(self, event: Event) -> None:
            super().emit(event)
            if event.type is EventType.PROGRESS and event.data.get("pct") == 10.0:
                _SIDE["blocked"].set()
                _SIDE["open"].wait(10)
            try:
                holder["m"].get(event.job)
            except ApiError as exc:
                failures.append(exc)

    manager, sink = _manager(_Slow(), max_analyses=2)
    holder["m"] = manager
    slow = manager.submit("t.side", {"name": "s"})
    _until(lambda: _SIDE["blocked"].is_set())
    _release("other")
    other = manager.submit("t.analysis", {"name": "other"})
    _until(lambda: any(e.job == other and e.data.get("state") == "succeeded" for e in sink.events))
    stopping = threading.Thread(target=manager.shutdown, kwargs={"timeout": 10})
    stopping.start()
    time.sleep(0.2)
    assert stopping.is_alive()  # l'arrêt attend la livraison de la fin du job lent
    _SIDE["open"].set()
    stopping.join(10)
    assert not stopping.is_alive() and failures == []
    assert [e.data.get("state") for e in sink.events if e.job == slow and e.type is EventType.STATE][-1] == \
        "succeeded"


def test_a_failed_queued_announcement_leaves_no_reservation():
    failed = {"done": False}

    class _OnceFailing(ListSink):
        def emit(self, event: Event) -> None:
            if event.data.get("state") == "queued" and not failed["done"]:
                failed["done"] = True
                raise RuntimeError("destination indisponible")
            super().emit(event)

    manager, _ = _manager(_OnceFailing(), max_analyses=2)
    exclusive = manager.submit("t.exclusive", {"name": "x"})  # identifiant rendu : le job existe, en échec
    assert _state(manager, exclusive) is JobState.FAILED
    _release("a")
    _done(manager, manager.submit("t.analysis", {"name": "a"}))  # aucune barrière laissée par l'exclusive
    manager.shutdown(timeout=5)
    assert "x" not in _STARTED


def test_emitter_keeps_delivering_after_a_failing_sink():
    from core.service.events import Emitter

    gate, entered = threading.Event(), threading.Event()
    delivered: list[int] = []

    class _Failing:
        def emit(self, event: Event) -> None:
            if event.seq == 1:
                entered.set()
                gate.wait(5)
                raise RuntimeError("destination en échec")
            delivered.append(event.seq)

    emitter = Emitter(_Failing())
    first = threading.Thread(target=lambda: pytest.raises(RuntimeError, emitter.log, "info", "un"))
    first.start()
    assert entered.wait(5)
    emitter.state(JobState.SUCCEEDED)  # mis en file : livré par le premier fil malgré son échec
    gate.set()
    first.join(5)
    assert delivered == [2]


def test_shutdown_waits_for_an_announcement_in_progress():
    admitted, resume = threading.Event(), threading.Event()
    manager, sink = _manager()
    holder: dict[str, str] = {}

    def announce(job: str) -> None:
        holder["job"] = job
        admitted.set()
        resume.wait(10)

    submitting = threading.Thread(target=lambda: manager.submit("t.analysis", {"name": "late"}, on_admitted=announce))
    submitting.start()
    assert admitted.wait(5)
    stopping = threading.Thread(target=manager.shutdown, kwargs={"timeout": 10})
    stopping.start()
    time.sleep(0.2)
    assert stopping.is_alive()
    resume.set()
    submitting.join(5)
    stopping.join(10)
    assert not stopping.is_alive() and _state(manager, holder["job"]) is JobState.CANCELLED
    assert [e.type for e in sink.events if e.job == holder["job"]] == [EventType.STATE, EventType.ERROR,
                                                                        EventType.STATE]
    assert "late" not in _STARTED


def test_shutdown_cancels_every_job_despite_a_failing_cancel_callback():
    failed = {"done": False}

    class _FailingLog(ListSink):
        def emit(self, event: Event) -> None:
            super().emit(event)
            if event.type is EventType.LOG and not failed["done"]:
                failed["done"] = True
                raise RuntimeError("journal indisponible")

    manager, _ = _manager(_FailingLog(), max_analyses=2)
    jobs = [manager.submit("t.watch", {"name": name}) for name in ("a", "b")]
    _until(lambda: sorted(_STARTED) == ["a", "b"])
    with pytest.raises(RuntimeError, match="journal"):
        manager.shutdown(timeout=10)
    assert all(_state(manager, job) is JobState.CANCELLED for job in jobs)
    assert not any(thread.name.startswith("job-t.") and thread.is_alive() for thread in threading.enumerate())


@pytest.mark.parametrize("refused", [EventType.ERROR, EventType.RESULT])
def test_the_final_state_is_delivered_even_when_the_previous_event_fails(refused):
    class _Refusing(ListSink):
        def emit(self, event: Event) -> None:
            super().emit(event)
            if event.type is refused:
                raise RuntimeError("destination en échec")

    manager, sink = _manager(_Refusing(), max_jobs=1)
    _release("ok")
    first = manager.submit("t.job", {"name": "ok"})  # résultat refusé
    blocked = manager.submit("t.job", {"name": "blocked"})
    queued = manager.submit("t.job", {"name": "queued"})  # annulé à l'arrêt : erreur refusée
    _until(lambda: "blocked" in _STARTED)
    try:
        assert manager.shutdown(timeout=5)
    except RuntimeError:
        pass
    finals = {e.job: e.data["state"] for e in sink.events if e.type is EventType.STATE
              and e.data["state"] not in ("queued", "running")}
    assert set(finals) == {first, blocked, queued}


def test_shutdown_waits_for_cancel_callbacks_still_to_emit():
    _HELD["inside"], _HELD["resume"] = threading.Event(), threading.Event()
    manager, sink = _manager()
    job = manager.submit("t.held", {"name": "h"})
    _until(lambda: _STARTED == ["h"])
    canceller = threading.Thread(target=manager.cancel, args=(job,))
    canceller.start()
    assert _HELD["inside"].wait(5)  # rappel suspendu avant son émission ; le job, réveillé, se termine
    _until(lambda: any(e.job == job and e.data.get("state") == "cancelled" for e in sink.events))
    assert not manager.shutdown(timeout=0.2)  # l'arrêt attend le rappel
    _HELD["resume"].set()
    assert manager.shutdown(timeout=5)
    canceller.join(5)
    assert sink.events[-1].data.get("message") == "rappel tardif"


def test_every_cancel_callback_runs_despite_a_failing_one():
    from core.service.context import CancelToken

    token, released = CancelToken(), threading.Event()
    token.on_cancel(lambda: (_ for _ in ()).throw(RuntimeError("rappel en échec")))
    token.on_cancel(released.set)
    with pytest.raises(RuntimeError, match="rappel"):
        token.cancel()
    assert released.is_set() and token.cancelled


def test_shutdown_timeout_is_kept_with_a_blocked_sink():
    gate = threading.Event()

    class _Blocking(ListSink):
        def emit(self, event: Event) -> None:
            super().emit(event)
            if event.data.get("state") == "cancelled":
                gate.wait(10)

    manager, _ = _manager(_Blocking(), max_jobs=1)
    manager.submit("t.job", {"name": "r"})
    manager.submit("t.job", {"name": "q"})
    _until(lambda: _STARTED == ["r"])
    started = time.monotonic()
    assert manager.shutdown(timeout=0.2) is False and time.monotonic() - started < 2
    gate.set()
    assert manager.shutdown(timeout=10)


def test_a_cancellation_in_progress_holds_the_shutdown_even_if_the_job_ends_alone():
    """Annulation en cours (jeton pas encore levé) pendant que le job finit seul : l'arrêt attend ses rappels.
    Réservation prise dans la même section verrouillée que la sélection du job (aucune fenêtre entre les deux)."""
    gate = threading.Event()
    manager, _ = _manager(max_jobs=1)
    job = manager.submit("t.job", {"name": "r"})
    _until(lambda: _STARTED == ["r"])
    token = manager._jobs[job].cancel
    original = token.cancel

    def suspended() -> None:
        _release("r")  # le job se termine pendant que l'annulation attend
        gate.wait(10)
        original()

    token.cancel = suspended  # type: ignore[method-assign]
    canceller = threading.Thread(target=manager.cancel, args=(job,))
    canceller.start()
    _done(manager, job)
    assert manager.shutdown(timeout=0.3) is False
    gate.set()
    assert manager.shutdown(timeout=5)
    canceller.join(5)


def test_shutdown_requested_from_a_sink_or_a_callback_never_waits_for_itself():
    holder: dict[str, JobManager] = {}
    answers: list[bool] = []

    class _Stopping(ListSink):
        def emit(self, event: Event) -> None:
            super().emit(event)
            if event.data.get("state") == "succeeded":
                answers.append(holder["m"].shutdown())  # fil du job, en livraison : déclenche sans attendre

    manager, _ = _manager(_Stopping())
    holder["m"] = manager
    _release("s")
    job = manager.submit("t.analysis", {"name": "s"})
    _until(lambda: answers == [False])
    assert manager.shutdown(timeout=5) and _state(manager, job) is JobState.SUCCEEDED

    second, _ = _manager()
    job = second.submit("t.watch", {"name": "w"})
    _until(lambda: "w" in _STARTED)
    second._jobs[job].cancel.on_cancel(lambda: answers.append(second.shutdown()))
    stopper = threading.Thread(target=second.cancel, args=(job,))
    stopper.start()
    stopper.join(5)
    assert not stopper.is_alive() and answers == [False, False]
    assert second.shutdown(timeout=5)


def test_direct_calls_always_try_the_final_state():
    from core.service import call

    class _Refusing(ListSink):
        def __init__(self, refused: Callable[[Event], bool]) -> None:
            super().__init__()
            self._refused = refused

        def emit(self, event: Event) -> None:
            super().emit(event)
            if self._refused(event):
                raise RuntimeError("destination en échec")

    for refused, name, payload in ((lambda e: e.type is EventType.ERROR, "t.secret", {}),
                                   (lambda e: e.data.get("state") == "running", "t.analysis", {"name": "z"})):
        _release("z")
        sink = _Refusing(refused)
        with pytest.raises((ApiError, RuntimeError)):
            call(_CATALOG, name, payload, ExecutionContext.create(SimpleNamespace(), sink))
        assert sink.events[-1].type is EventType.STATE and sink.events[-1].data["state"] == "failed"


def test_shutdown_requested_while_announcing_never_waits_for_itself():
    manager, _ = _manager()
    answers: list[bool] = []
    worker = threading.Thread(target=lambda: manager.submit("t.analysis", {"name": "n"},
                                                            on_admitted=lambda _job: answers.append(manager.shutdown())))
    worker.start()
    worker.join(5)
    assert not worker.is_alive() and answers == [False] and "n" not in _STARTED
    assert manager.shutdown(timeout=5)


def test_a_refused_warning_still_publishes_the_final_state():
    from core.service import call

    class _NoWarnings(ListSink):
        def emit(self, event: Event) -> None:
            super().emit(event)
            if event.type is EventType.WARNING:
                raise RuntimeError("destination en échec")

    sink = _NoWarnings()
    with pytest.raises(RuntimeError):
        call(_CATALOG, "t.analysis", {"name": "w", "bogus": 1}, ExecutionContext.create(SimpleNamespace(), sink),
             strict=False)
    assert sink.events[-1].data.get("state") == "failed" and "w" not in _STARTED


@pytest.mark.parametrize("raised, final", [(KeyboardInterrupt, "cancelled"),
                                           (lambda: ApiError(ErrorCode.CANCELLED, "annulé"), "cancelled")])
def test_an_interrupted_warning_still_publishes_a_consistent_final_state(raised, final):
    from core.service import call

    class _Interrupting(ListSink):
        def emit(self, event: Event) -> None:
            super().emit(event)
            if event.type is EventType.WARNING:
                raise raised()

    sink = _Interrupting()
    with pytest.raises((KeyboardInterrupt, ApiError)):
        call(_CATALOG, "t.analysis", {"name": "i", "bogus": 1}, ExecutionContext.create(SimpleNamespace(), sink),
             strict=False)
    assert [e.data.get("state") for e in sink.events if e.type is EventType.STATE] == [final]


def test_a_nul_in_base_dir_is_refused_at_admission():
    manager, _ = _manager()
    with pytest.raises(ApiError) as caught:
        manager.submit("t.analysis", {"name": "n"}, base_dir=Path("/media\0bad"))
    assert caught.value.code is ErrorCode.INVALID_REQUEST and manager.list() == ()
    manager.shutdown()


# --- Branches d'erreur restées sans exécution (couverture résiduelle de l'audit 4effdd1) -----------------------


def test_failing_admission_callback_leaves_no_job_and_frees_the_queue():
    manager, _ = _manager(max_jobs=1)
    running = manager.submit("t.job", {"name": "r"})

    def refuse(_job: str) -> None:
        raise RuntimeError("client parti")

    with pytest.raises(RuntimeError):
        manager.submit("t.job", {"name": "never"}, on_admitted=refuse)
    assert len(manager.list()) == 1  # l'identifiant jamais annoncé n'a pas existé
    after = manager.submit("t.job", {"name": "n"})
    _release("r")
    _release("n")
    _done(manager, running, after)
    assert _state(manager, after) is JobState.SUCCEEDED and "never" not in _STARTED
    manager.shutdown()


def test_failure_before_execution_is_recorded_and_published(monkeypatch):
    manager, sink = _manager()

    def broken(*_args: Any, **_kwargs: Any) -> ExecutionContext:
        raise RuntimeError("contexte impossible")

    monkeypatch.setattr(manager_module.ExecutionContext, "create", broken)
    job = manager.submit("t.analysis", {"name": "f"})
    _done(manager, job)
    assert _state(manager, job) is JobState.FAILED
    _until(lambda: any(event.type is EventType.STATE and event.data.get("state") == "failed"
                       for event in sink.events if event.job == job))
    manager.shutdown()


def test_unreadable_configuration_after_exclusive_keeps_the_previous_one():
    calls = iter([SimpleNamespace(marker="c1")])

    def factory() -> Any:
        try:
            return next(calls)
        except StopIteration:
            raise ValueError("config.ini illisible") from None

    manager = JobManager(_CATALOG, factory, ListSink())
    for name in ("x", "b"):
        _release(name)
    _done(manager, manager.submit("t.exclusive", {"name": "x"}))
    after = manager.submit("t.analysis", {"name": "b"})
    _done(manager, after)
    assert manager.get(after).result["config"] == "c1"  # type: ignore[index]
    manager.shutdown()
