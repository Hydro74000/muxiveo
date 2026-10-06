"""
tests/test_probe_lifecycle.py — Sondes bornées et annulables, fermeture coopérative (A08, A18).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core.inspector import FileInspector, InspectionCancelled, InspectionError
from core.subprocess_utils import ProbeCancelledError, run_probe

SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]


def _fake_tool(tmp_path: Path, name: str, body: str) -> str:
    """Outil factice exécutable (script Python) imitant ffprobe/mediainfo."""
    script = tmp_path / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        launcher = tmp_path / f"{name}.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        return str(launcher)
    launcher = tmp_path / name
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    return str(launcher)


def test_run_probe_times_out_and_kills_process() -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_probe(SLEEPER, timeout=0.5)
    assert time.monotonic() - started < 10


def test_run_probe_cancellation_stops_silent_process() -> None:
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(ProbeCancelledError):
        run_probe(SLEEPER, timeout=60, cancel_event=cancel)
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("cancellable", [False, True])
def test_run_probe_large_output_without_deadlock(cancellable: bool) -> None:
    cmd = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5_000_000); sys.stderr.write('e' * 200_000)"]
    result = run_probe(cmd, timeout=60, cancel_event=threading.Event() if cancellable else None)
    assert result.returncode == 0
    assert len(result.stdout) == 5_000_000 and len(result.stderr) == 200_000


def test_run_probe_missing_tool_is_distinct() -> None:
    with pytest.raises(FileNotFoundError):
        run_probe(["/nonexistent/muxiveo-ffprobe"], timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="Lanceur .cmd : le délai seul ne tue pas l'arbre de processus")
def test_inspector_reports_hung_ffprobe_as_timeout(tmp_path: Path) -> None:
    ffprobe = _fake_tool(tmp_path, "ffprobe", "import time; time.sleep(30)\n")
    media = tmp_path / "film.mkv"
    media.write_bytes(b"x")
    inspector = FileInspector(ffprobe_bin=ffprobe, mediainfo_bin=ffprobe, probe_timeout_s=0.5)
    started = time.monotonic()
    with pytest.raises(InspectionError, match="sans réponse"):
        inspector._run_ffprobe(media)
    assert time.monotonic() - started < 10
    # MediaInfo bloqué : sonde abandonnée, inspection non bloquée.
    assert inspector._run_mediainfo_json(media) is None


def test_inspector_cancellation(tmp_path: Path) -> None:
    ffprobe = _fake_tool(tmp_path, "ffprobe", "import time; time.sleep(30)\n")
    media = tmp_path / "film.mkv"
    media.write_bytes(b"x")
    cancel = threading.Event()
    inspector = FileInspector(ffprobe_bin=ffprobe, mediainfo_bin=ffprobe, cancel_event=cancel)
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(InspectionCancelled):
        inspector._run_ffprobe(media)
    assert time.monotonic() - started < 10


# ---------------------------------------------------------------------------
# MediaManager (A18)
# ---------------------------------------------------------------------------

def test_mediamanager_probe_stops_on_interruption() -> None:
    import mediamanager

    interrupted = threading.Event()
    threading.Timer(0.3, interrupted.set).start()
    started = time.monotonic()
    assert mediamanager._run_interruptible(SLEEPER, interrupted.is_set) is None
    assert time.monotonic() - started < 10


def test_mediamanager_close_waits_for_threads_without_blocking(qt_app, tmp_path: Path, monkeypatch) -> None:
    import mediamanager
    from PySide6.QtCore import QCoreApplication

    monkeypatch.setattr(mediamanager, "_resolve_mediainfo_command", lambda: list(SLEEPER))
    movie = tmp_path / "Matrix.1999.1080p.mkv"
    movie.write_bytes(b"x" * 10)
    win = mediamanager.MediaManager(startup_dir=str(tmp_path))
    assert win.scanner is not None and win.scanner.wait(5000)
    thread = mediamanager.MediaInfoThread(str(movie))
    emitted: list[object] = []
    thread.info_ready.connect(emitted.append)
    win._threads.append(thread)
    thread.start()
    time.sleep(0.2)
    assert thread.isRunning()
    win.close()
    # Fermeture différée : la sonde est interrompue, la boucle Qt reste active.
    deadline = time.monotonic() + 10
    while thread.isRunning() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.02)
    assert not thread.isRunning()
    assert emitted == []
    for _ in range(10):
        QCoreApplication.processEvents()
        time.sleep(0.06)
    assert not win.isVisible()
