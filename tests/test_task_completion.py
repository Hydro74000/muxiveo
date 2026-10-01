from __future__ import annotations

import io
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PySide6.QtCore import QEventLoop, QTimer, QObject, Signal, QThread

from core.runner import TaskSignals
from cli import runtime
from cli.constants import EXIT_OK, EXIT_WORKFLOW
from cli.logging import Logger
from cli.options import CommonOptions


@pytest.mark.parametrize("kind", ["finished", "failed", "cancelled"])
@pytest.mark.parametrize("early", [True, False])
def test_terminal_delivery_once_in_qt_thread(qt_app, kind, early):
    signals = TaskSignals()
    args = {"finished": ("ok",), "failed": ("boom", ValueError("boom")), "cancelled": ()}[kind]
    seen = []

    def emit():
        getattr(signals, kind).emit(*args)

    def subscribe():
        callbacks: dict[str, Any] = {kind: lambda *values: seen.append((values, QThread.currentThread()))}
        signals.connect_terminal(**callbacks)

    if not early:
        subscribe()
    worker = threading.Thread(target=emit)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    if early:
        qt_app.processEvents()  # La notification initiale a déjà été consommée.
        subscribe()
    assert seen == []
    qt_app.processEvents()
    qt_app.processEvents()
    assert seen == [(args, qt_app.thread())]
    emit()
    qt_app.processEvents()
    assert len(seen) == 1
    subscribe()  # Un second observateur peut arriver après le premier.
    qt_app.processEvents()
    qt_app.processEvents()
    assert seen == [(args, qt_app.thread()), (args, qt_app.thread())]


@pytest.mark.parametrize("kind,expected", [("finished", EXIT_OK), ("failed", EXIT_WORKFLOW), ("cancelled", EXIT_WORKFLOW)])
def test_cli_receives_completion_before_run_returns(qt_app, tmp_path, monkeypatch, kind, expected):
    class ImmediateWorkflow(QObject):
        log_message = Signal(str, str)
        backend_event = Signal(object)

        def run(self, _config):
            signals = TaskSignals()
            if kind == "finished":
                signals.finished.emit("done")
            elif kind == "failed":
                signals.failed.emit("early failure", RuntimeError("early failure"))
            else:
                signals.cancelled.emit()
            return signals

    wf = ImmediateWorkflow()
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timed_out = []
    timer.timeout.connect(lambda: (timed_out.append(True), loop.quit()))
    monkeypatch.setattr(runtime, "workflow", lambda *_args: wf)
    monkeypatch.setattr(runtime, "QEventLoop", lambda: loop)
    timer.start(1000)
    try:
        result = runtime.run_remux_config(
            cast(Any, SimpleNamespace()), CommonOptions(), Logger(stream=io.StringIO()),
            cast(Any, SimpleNamespace(output=tmp_path / "out.mkv")),
        )
    finally:
        timer.stop()
    assert not timed_out
    assert result == expected


def test_cli_transmits_sync_preferences(monkeypatch):
    captured = {}
    config = SimpleNamespace(
        tool_ffmpeg="ffmpeg", tool_ffprobe="ffprobe", tool_mediainfo="mediainfo",
        ffmpeg_threads=2, generate_nfo=False, sync_rewrite_enabled=True,
        sync_advanced_audio_rewrite_enabled=True,
        aac_bitrate_per_channel_kbps=80, eac3_bitrate_per_channel_kbps=112,
    )
    monkeypatch.setattr(runtime, "RemuxWorkflow", lambda **kwargs: captured.update(kwargs))
    runtime.workflow(cast(Any, config), CommonOptions(), Logger())
    assert captured["sync_rewrite_enabled"] is True
    assert captured["sync_advanced_audio_rewrite_enabled"] is True
    assert captured["aac_bitrate_per_channel_kbps"] == 80
    assert captured["eac3_bitrate_per_channel_kbps"] == 112


def test_remux_temp_cleanup_runs_before_deferred_terminal_notification(qt_app, tmp_path):
    """Le work_dir est nettoyé avant que l'attente (boucle CLI) soit notifiée de la fin."""
    import threading

    from PySide6.QtCore import QCoreApplication

    from core.runner import TaskSignals
    from core.workflows.remux_sync import bind_temp_cleanup

    process_dir = tmp_path / "job"
    (process_dir / "attachments").mkdir(parents=True)
    signals = TaskSignals()
    bind_temp_cleanup(signals, [process_dir])
    seen: list[bool] = []
    signals.connect_terminal(finished=lambda _r: seen.append(process_dir.exists()))

    worker = threading.Thread(target=lambda: signals.finished.emit("ok"))
    worker.start()
    worker.join()
    for _ in range(20):
        QCoreApplication.processEvents()

    assert seen == [False]
