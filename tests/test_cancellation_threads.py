"""
tests/test_cancellation_threads.py — Annulations et fils (N11, N13, N10 ; plan de remédiation 5.2.0, lot 10).

Test de survie FEL interruptible (deux étapes FFmpeg, délai maximal) ; sonde d'origine des flux annulée sans être
prise pour une origine illisible ; échec du démarrage d'un fil du gestionnaire de jobs sans blocage.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from core.hdr_inspection.fel_analyzer import Frame, SurvivalCancelled, encode_round_trip
from core.runner import TaskCancelledError

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="outil factice en script POSIX")


def _tool(tmp_path: Path, body: str) -> str:
    tool = tmp_path / "ffmpeg-factice"
    tool.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    tool.chmod(0o755)
    return str(tool)


def _frames(count: int = 3) -> list[Frame]:
    y = np.full((16, 16), 512, np.uint16)
    c = np.full((8, 8), 512, np.uint16)
    return [Frame(y, c, c) for _ in range(count)]


@posix_only
@pytest.mark.parametrize("stage", ["encode", "decode"])
def test_survival_round_trip_is_cancelled_in_both_steps(tmp_path, stage):
    # Encodage (« -c:v » présent) ou décodage sans fin selon l'étape visée.
    body = ('case "$*" in *-c:v*) sleep 30;; *) cat >/dev/null; sleep 30;; esac' if stage == "encode" else
            'case "$*" in *-c:v*) cat >/dev/null; exit 0;; *) sleep 30;; esac')
    ffmpeg = _tool(tmp_path, body)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(SurvivalCancelled):
        encode_round_trip(_frames(), ffmpeg_bin=ffmpeg, encoder="libx265", bitrate_kbps=1000, frame_rate="24/1",
                          cancel=cancel)
    assert time.monotonic() - started < 2.0  # processus tué, fils rejoints


@posix_only
def test_survival_round_trip_has_a_time_limit(tmp_path):
    ffmpeg = _tool(tmp_path, "sleep 30")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        encode_round_trip(_frames(), ffmpeg_bin=ffmpeg, encoder="libx265", bitrate_kbps=1000, frame_rate="24/1",
                          timeout=0.3)
    assert time.monotonic() - started < 2.0


def test_survival_callers_forward_the_cancellation(monkeypatch):
    """Les deux appelants (``hdr.inspect`` et HDR Studio) transmettent leur annulation au test de survie."""
    import inspect

    import core.service.ops.hdr as hdr_ops
    import ui.panels.hdr_studio.panel as panel_module

    assert "cancel=cancel)" in inspect.getsource(hdr_ops.fel_analysis).split("run_survival(", 1)[1].split("\n\n")[0]
    source = inspect.getsource(panel_module.HdrStudio.test_fel_survival)
    assert "cancel=cancel" in source.split("run_survival(", 1)[1]


@posix_only
def test_origin_probe_cancellation_is_never_an_unreadable_origin(tmp_path):
    from core.workflows.common.native_stream_inputs import probe_start_time

    ffprobe = _tool(tmp_path, "sleep 30")
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(TaskCancelledError):
        probe_start_time(tmp_path / "source.mp4", ffprobe, cancel.is_set)
    assert time.monotonic() - started < 3.0
    with pytest.raises(RuntimeError, match="illisible"):  # sonde en échec ordinaire : origine illisible
        probe_start_time(tmp_path / "source.mp4", _tool(tmp_path, "exit 1"))


def test_failed_thread_start_finishes_the_job_and_keeps_scheduling(monkeypatch):
    from core.service import ListSink
    from core.service import manager as manager_module
    from core.service.events import EventType
    from core.service.manager import JobManager
    from tests.test_service_manager import _CATALOG, _GATES

    sink = ListSink()
    manager = JobManager(_CATALOG, lambda: object(), sink, max_jobs=1)
    original = threading.Thread.start
    failures = {"left": 1}

    def start(self):  # type: ignore[no-untyped-def]
        if self.name.startswith("job-") and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("can't start new thread")
        return original(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    admitted: list[str] = []
    first = manager.submit("t.exclusive", {"name": "x1"}, on_admitted=admitted.append)
    assert len(admitted) == 1  # une seule réponse à l'admission
    deadline = time.monotonic() + 5
    while manager.get(first).state.value != "failed" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.get(first).state.value == "failed"
    _GATES.setdefault("x2", threading.Event()).set()
    second = manager.submit("t.job", {"name": "x2"})  # la file repart après l'échec d'un job exclusif
    deadline = time.monotonic() + 5
    while manager.get(second).state.value != "succeeded" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.get(second).state.value == "succeeded"
    assert any(event.type is EventType.ERROR and event.job == first for event in sink.events)
    manager.shutdown(timeout=5)  # rend la main
    assert manager_module is not None
