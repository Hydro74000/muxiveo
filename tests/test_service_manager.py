"""Gestionnaire de jobs : admission, files par classe, barrière exclusive, annulation, drainage, arrêt."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
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
def _update(request: _Request, _ctx: ExecutionContext) -> _Result:
    return _Result(name=request.name, restart_required=True)


_CATALOG = Catalog([_make("t.analysis"), _make("t.job", queue="job"), _make("t.exclusive", queue="exclusive"),
                    _make("t.secret", queue="exclusive", privileged=True), _update])


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
