"""Runtime de job : attente d'une tâche Qt, nettoyage, annulation, Ctrl+C ; opérations media.* du remux."""

from __future__ import annotations

import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal

from cli.interrupts import interrupt_guard
from core.jobs import runtime
from core.jobs.options import CommonOptions
from core.runner import TaskSignals
from core.service import ApiError, CancelToken, ErrorCode, EventType, ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.jobs import await_task
from core.service.ops import media


class _Poster(QObject):
    posted = Signal()


def _start(work, *, after=None) -> TaskSignals:
    """Tâche : ``work(signals)`` émet la fin, puis ``after()`` s'exécute dans le finally du worker."""
    signals = TaskSignals()
    executor = ThreadPoolExecutor(max_workers=1)

    def run() -> None:
        try:
            work(signals)
        finally:
            if after is not None:
                after()

    signals.watch_future(executor.submit(run))
    executor.shutdown(wait=False)
    return signals


def test_await_task_returns_after_cleanup_and_posted_slots(qapp):
    seen: list[str] = []
    poster = _Poster()
    poster.posted.connect(lambda: seen.append("slot"))

    def after() -> None:
        time.sleep(0.1)  # nettoyage après le signal terminal
        seen.append("cleanup")
        poster.posted.emit()  # slot posté vers le thread Qt pendant le nettoyage

    signals = _start(lambda s: (time.sleep(0.05), s.finished.emit("fini")), after=after)
    outcome = await_task(signals, CancelToken())
    assert outcome.status == "finished" and outcome.result == "fini"
    assert seen == ["cleanup", "slot"]


def test_await_task_failed_carries_message_and_exception(qapp):
    error = RuntimeError("boum")
    outcome = await_task(_start(lambda s: s.failed.emit("échec", error)), CancelToken())
    assert outcome.status == "failed" and outcome.message == "échec" and outcome.exception is error


def test_cancel_token_cancels_running_task(qapp):
    def work(signals: TaskSignals) -> None:
        while not signals._cancel_event.is_set():
            time.sleep(0.01)
        signals.cancelled.emit()

    token = CancelToken()
    threading.Timer(0.1, token.cancel).start()
    assert await_task(_start(work), token).status == "cancelled"


def test_task_already_cancelled_is_stopped_at_once(qapp):
    token = CancelToken()
    token.cancel()

    def work(signals: TaskSignals) -> None:
        time.sleep(0.05)
        signals.cancelled.emit() if signals._cancel_event.is_set() else signals.finished.emit("trop tard")

    assert await_task(_start(work), token).status == "cancelled"


def test_await_task_outside_qt_thread_uses_blocking_wait(qapp):
    result: dict[str, Any] = {}

    def waiter() -> None:
        result["outcome"] = await_task(_start(lambda s: s.finished.emit("ok")), CancelToken())

    thread = threading.Thread(target=waiter)
    thread.start()
    thread.join(10)
    assert result["outcome"].status == "finished"


def test_progress_is_forwarded(qapp):
    lines: list[str] = []
    pct: list[int] = []

    def work(signals: TaskSignals) -> None:
        time.sleep(0.05)  # abonnement à la progression après le lancement, comme le workflow
        signals.progress.emit("frame=1")
        signals.progress_pct.emit(50)
        signals.finished.emit("")

    await_task(_start(work), CancelToken(), on_progress=lines.append, on_progress_pct=pct.append)
    assert lines == ["frame=1"] and pct == [50]


@pytest.mark.skipif(os.name == "nt", reason="signal POSIX envoyé au processus")
def test_interrupt_guard_turns_sigint_into_cancellation():
    previous = signal.getsignal(signal.SIGINT)
    with interrupt_guard(CancelToken()) as token:
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)
        assert token.cancelled
    assert signal.getsignal(signal.SIGINT) is previous


@pytest.mark.skipif(os.name == "nt", reason="signal POSIX envoyé au processus")
def test_interrupt_guard_never_takes_the_token_lock_in_the_signal_handler():
    """Ctrl+C pendant que le fil principal tient le verrou du jeton (``on_cancel``) : le gestionnaire ne fait que
    noter la demande, l'annulation suit dès la libération (sinon le fil principal s'attendait lui-même)."""
    with interrupt_guard(CancelToken()) as token:
        with token._lock:
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)  # gestionnaire exécuté ici, verrou tenu
            assert not token.cancelled
        deadline = time.monotonic() + 5
        while not token.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        assert token.cancelled


@pytest.mark.skipif(os.name == "nt", reason="signal POSIX envoyé au processus")
def test_interrupt_guard_survives_a_burst_of_signals_while_the_token_is_locked():
    """Rafale de Ctrl+C, verrou du jeton tenu : demandes regroupées, le gestionnaire n'écrit jamais en bloquant."""
    with interrupt_guard(CancelToken()) as token:
        with token._lock:
            for _ in range(70_000):  # au-delà de la capacité d'un tube (64 Kio sous Linux)
                os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
        deadline = time.monotonic() + 5
        while not token.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        assert token.cancelled


@pytest.mark.skipif(os.name == "nt", reason="signal POSIX envoyé au processus")
def test_nested_guards_are_all_cancelled_before_the_inner_one_closes():
    """Le lot est annulé avant que le job rende la main (sinon le job suivant pouvait démarrer)."""
    class _SlowToken(CancelToken):
        def cancel(self) -> None:
            time.sleep(0.2)  # annulation du lot lente à démarrer
            super().cancel()

    with interrupt_guard(_SlowToken()) as batch:
        with interrupt_guard(CancelToken()) as job:
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.01)
        assert job.cancelled and batch.cancelled


@pytest.mark.skipif(os.name == "nt", reason="signal POSIX envoyé au processus")
def test_a_second_ctrl_c_while_a_guard_closes_stays_deferred():
    """Sortie de garde : demandes traitées avant de rétablir le gestionnaire d'origine (un second Ctrl+C pendant
    l'attente ne lève pas KeyboardInterrupt au milieu de l'annulation)."""
    finished = threading.Event()
    with interrupt_guard(CancelToken()) as token:
        token.on_cancel(lambda: (time.sleep(0.3), finished.set()))
        os.kill(os.getpid(), signal.SIGINT)
        threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGINT)).start()  # pendant l'attente de sortie
    assert finished.is_set() and token.cancelled


def test_every_signal_keeps_its_own_targets():
    """Deux signaux avant que la veille ne lise : le jeton intérieur retiré entre les deux reste annulé."""
    from cli.interrupts import _Interrupts

    state = _Interrupts()
    wake, read = state.write_fd, state.read_fd
    paused_read, state.write_fd = os.pipe()  # veille suspendue : les octets vont ailleurs
    try:
        outer, inner = CancelToken(), CancelToken()
        state.tokens = [outer, inner]
        state.handler(signal.SIGINT, None)
        state.tokens.remove(inner)
        state.signalled = False
        state.handler(signal.SIGINT, None)
        os.write(wake, b"c")  # reprise de la veille
        with state.done:
            assert state.done.wait_for(lambda: state.handled >= state.requested, timeout=5)
        assert outer.cancelled and inner.cancelled
    finally:  # fin de fichier : la veille de cette instance s'arrête ; tous les descripteurs fermés
        for descriptor in (wake, paused_read, state.write_fd):
            os.close(descriptor)
        state.thread.join(5)
        assert not state.thread.is_alive()
        os.close(read)


def _open_descriptors() -> set[tuple[int, int, int]]:
    """Descripteurs ouverts du processus, identifiés par numéro et objet ouvert (périphérique, inode)."""
    found = set()
    for entry in Path("/proc/self/fd").iterdir():
        try:
            status = os.fstat(int(entry.name))
        except OSError:  # descripteur de l'itération elle-même, ou fermé entre-temps
            continue
        found.add((int(entry.name), status.st_dev, status.st_ino))
    return found


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="descripteurs listés par /proc")
def test_interrupt_guards_leak_neither_descriptors_nor_threads():
    """Aucun descripteur ni fil laissé par les gardes : seuls ceux apparus pendant les gardes comptent (les
    ressources d'autres tests libérées entre-temps, par le ramasse-miettes ou la fin de leurs fils, ne faussent rien)."""
    previous = signal.getsignal(signal.SIGINT)
    with interrupt_guard(CancelToken()):
        pass
    descriptors = _open_descriptors()
    threads = set(threading.enumerate())
    for _ in range(50):
        with interrupt_guard(CancelToken()):
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.002)
    assert _open_descriptors() - descriptors == set()
    assert set(threading.enumerate()) - threads == set()
    assert signal.getsignal(signal.SIGINT) is previous


# --- run_remux : fin, échec, annulation, course annulation / publication -----------------

class _Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def emit(self, level: str, message: str, **fields: Any) -> None:
        self.records.append((level, message, fields))


class _FakeWorkflow(QObject):
    log_message = Signal(str, str)
    backend_event = Signal(object)

    def __init__(self, work) -> None:
        super().__init__()
        self._work = work

    def run(self, _config) -> TaskSignals:
        self.backend_event.emit({"selected": "native"})
        return _start(self._work)


def _run_remux(monkeypatch, tmp_path: Path, work, cancel: CancelToken | None = None) -> tuple[_Logger, Any]:
    monkeypatch.setattr(runtime, "remux_workflow", lambda config, options: _FakeWorkflow(work))
    logger = _Logger()
    config = SimpleNamespace(output=tmp_path / "out.mkv")
    try:
        result = runtime.run_remux(config, config=SimpleNamespace(), options=CommonOptions(), logger=logger,
                                   cancel=cancel or CancelToken())
    except ApiError as error:
        result = error
    return logger, result


def test_run_remux_success_logs_backend_and_message(qapp, monkeypatch, tmp_path):
    logger, result = _run_remux(monkeypatch, tmp_path, lambda s: s.finished.emit("Remux terminé"))
    assert result == "Remux terminé"
    assert logger.records[0] == ("info", "Sélection du backend de remuxage.", {"event": "mux_backend",
                                                                               "selected": "native"})
    assert logger.records[-1] == ("info", "Remux terminé", {})


def test_run_remux_failure_is_logged_once_then_raised(qapp, monkeypatch, tmp_path):
    exc = RuntimeError("ffmpeg")
    logger, result = _run_remux(monkeypatch, tmp_path, lambda s: s.failed.emit("Échec du remux", exc))
    assert isinstance(result, ApiError) and result.logged and result.code is ErrorCode.WORKFLOW_FAILED
    assert logger.records[-1] == ("error", "Échec du remux", {"exception": repr(exc)})


def test_run_remux_cancel_too_late_keeps_success(qapp, monkeypatch, tmp_path):
    token = CancelToken()

    def work(signals: TaskSignals) -> None:
        signals.finished.emit("publié")
        token.cancel()

    logger, result = _run_remux(monkeypatch, tmp_path, work, token)
    assert result == "publié"
    assert ("warning", "Annulation trop tardive : la sortie était déjà publiée.", {"code": "cancel_too_late"}) \
        in logger.records


def _in_job_thread(target) -> dict[str, Any]:
    """Exécute ``target`` dans un fil sans boucle Qt (job d'un service) ; renvoie son résultat ou son erreur."""
    outcome: dict[str, Any] = {}

    def job() -> None:
        try:
            outcome["result"] = target()
        except BaseException as exc:  # noqa: BLE001 - restitué au test
            outcome["error"] = exc

    thread = threading.Thread(target=job)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    return outcome


def test_run_remux_off_qt_thread_delivers_workflow_logs(qapp, monkeypatch, tmp_path):
    """Workflow créé dans un fil de job, journal émis depuis son fil d'exécution : livré (relais direct)."""
    created: dict[str, _FakeWorkflow] = {}

    def work(signals: TaskSignals) -> None:
        created["wf"].log_message.emit("INFO", "étape 1")
        signals.finished.emit("ok")

    def factory(_config, _options) -> _FakeWorkflow:
        created["wf"] = _FakeWorkflow(work)
        return created["wf"]

    monkeypatch.setattr(runtime, "remux_workflow", factory)
    logger = _Logger()
    outcome = _in_job_thread(lambda: runtime.run_remux(
        SimpleNamespace(output=tmp_path / "out.mkv"), config=SimpleNamespace(), options=CommonOptions(),
        logger=logger, cancel=CancelToken()))
    assert outcome == {"result": "ok"}
    assert ("INFO", "étape 1", {}) in logger.records
    assert logger.records[0][2]["event"] == "mux_backend"


class _FakeEncodeWorkflow(QObject):
    log_message = Signal(str, str)
    failed_work_dir_retained = Signal(object)

    def __init__(self, work) -> None:
        super().__init__()
        self._work = work

    def validate(self, _config) -> list[str]:
        return []

    def parse_progress(self, _config, _line):
        return None

    def run(self, _config, *, validate: bool, retain_failed_work_dir: bool) -> TaskSignals:
        return _start(self._work)


def test_run_encode_off_qt_thread_delivers_workflow_logs(qapp, monkeypatch, tmp_path):
    from core.jobs import encode_runtime

    created: dict[str, _FakeEncodeWorkflow] = {}

    def work(signals: TaskSignals) -> None:
        created["wf"].log_message.emit("WARN", "repli")
        signals.finished.emit("Encodage terminé")

    def factory() -> _FakeEncodeWorkflow:
        created["wf"] = _FakeEncodeWorkflow(work)
        return created["wf"]

    monkeypatch.setattr(encode_runtime, "prepare_vulkan", lambda *a, **k: None)
    monkeypatch.setattr(encode_runtime, "align_dovi_nvenc", lambda config, *a, **k: config)
    logger = _Logger()
    outcome = _in_job_thread(lambda: encode_runtime.run_encode(
        SimpleNamespace(output=tmp_path / "out.mkv"), config=SimpleNamespace(tool_ffmpeg="ffmpeg"),
        options=CommonOptions(), logger=logger, cancel=CancelToken(),
        workflow_factory=factory))  # type: ignore[arg-type]
    assert outcome == {"result": "Encodage terminé"}
    assert ("WARN", "repli", {}) in logger.records


def test_run_remux_refuses_existing_output(qapp, monkeypatch, tmp_path):
    (tmp_path / "out.mkv").write_bytes(b"x")
    _logger, result = _run_remux(monkeypatch, tmp_path, lambda s: s.finished.emit(""))
    assert isinstance(result, ApiError) and result.code is ErrorCode.OUTPUT_EXISTS


# --- Opérations media.validate / preview / run ------------------------------------------

class _Wf:
    def validate(self, _config):
        return []

    def build_command(self, _config):
        return ["ffmpeg", "-i", "a.mkv"]

    def preview_command(self, _config):
        return "ffmpeg -i a.mkv"

    def execution_preview(self, _config):
        return {"selected_backend": "native", "plan_version": 1, "preparation_commands": [],
                "native_diagnostics": []}


def _ctx() -> tuple[ExecutionContext, ListSink]:
    sink = ListSink()
    return ExecutionContext.create(SimpleNamespace(), sink), sink


def test_media_preview_and_validate_share_the_job_resolution(monkeypatch, tmp_path):
    built: list[dict] = []

    def build(job, config, options, logger, **_kwargs):
        built.append(job)
        return SimpleNamespace(output=tmp_path / "out.mkv")

    monkeypatch.setattr(media, "build_remux_config", build)
    monkeypatch.setattr(media, "remux_workflow", lambda config, options: _Wf())
    monkeypatch.setattr(media, "serialize_remux_config", lambda config: {"output": str(config.output)})
    job = {"sources": ["a.mkv"], "output": "out.mkv"}
    ctx, _ = _ctx()
    preview = call(CATALOG, "media.preview", {"job": job}, ctx, base_dir=tmp_path)
    assert preview.valid and preview.command_text == "ffmpeg -i a.mkv"
    assert preview.execution["selected_backend"] == "native"
    assert built[0]["sources"] == [str(tmp_path / "a.mkv")] and job["sources"] == ["a.mkv"]
    ctx, _ = _ctx()
    validate = call(CATALOG, "media.validate", {"job": job, "options": {"sync_mode": "physical"}}, ctx,
                    base_dir=tmp_path)
    assert validate.to_dict() == {"valid": True, "errors": [], "config": {"output": str(tmp_path / "out.mkv")},
                                  "encode": None}


def test_media_job_options_are_strict():
    ctx, _ = _ctx()
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "media.validate", {"job": {}, "options": {"sync_mode": "x", "nope": 1}}, ctx)
    assert {d.path for d in caught.value.details} == {"options.sync_mode", "options.nope"}


def test_media_run_reports_output_artifact(monkeypatch, tmp_path):
    output = tmp_path / "out.mkv"
    monkeypatch.setattr(media, "build_remux_config", lambda job, config, options, logger, **_k: SimpleNamespace(output=output))
    seen: dict[str, Any] = {}

    def run(remux_config, *, config, options, logger, cancel, force, progress=None):
        seen.update(force=force, cancel=cancel)
        logger.emit("info", "STEP 1")
        return "fini"

    monkeypatch.setattr(media, "run_remux", run)
    ctx, sink = _ctx()
    result = call(CATALOG, "media.run", {"job": {}, "force": True}, ctx)
    assert result.to_dict() == {"status": "completed", "output": str(output), "message": "fini"}
    assert seen == {"force": True, "cancel": ctx.cancel}
    kinds = [e.type for e in sink.events]
    assert EventType.ARTIFACT in kinds and EventType.LOG in kinds


def test_job_options_round_trip_common_options():
    options = CommonOptions(ffprobe="/x/ffprobe", sync_mode="physical", calibration="c.json", drift_threshold_ms=40,
                            cadence_method="atempo", verbose=True)
    payload = media.JobOptions.from_common(options)
    parsed = media.JobOptions.from_dict(payload, allow_relative=True)
    from core.service.context import Overrides

    ctx = ExecutionContext.create(SimpleNamespace(), ListSink(), overrides=Overrides(ffprobe="/x/ffprobe"), api=False,
                                  base_dir=Path("/jobs"))
    rebuilt = parsed.to_common(ctx)
    assert rebuilt == CommonOptions(ffprobe="/x/ffprobe", sync_mode="physical", calibration="c.json",
                                    drift_threshold_ms=40, cadence_method="atempo", verbose=True, api=False,
                                    base_dir=Path("/jobs"))
    api = parsed.to_common(ExecutionContext.create(SimpleNamespace(), ListSink()))  # requête de l'API : aucun dossier implicite
    assert api.api is True and api.base_dir is None
