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


@pytest.mark.parametrize("mode", ["timeout", "cancel", "exited-parent"])
def test_descendant_holding_stdout_cannot_keep_probe_alive(tmp_path: Path, mode: str) -> None:
    pidfile = tmp_path / "child.pid"
    child = "import time; time.sleep(30)"
    body = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"Path({str(pidfile)!r}).write_text(str(p.pid)); "
        + ("time.sleep(30)" if mode != "exited-parent" else "sys.exit(0)")
    )
    cancel = threading.Event()
    started = time.monotonic()
    if mode == "cancel":
        threading.Timer(0.5, cancel.set).start()
    try:
        expected = ProbeCancelledError if mode == "cancel" else subprocess.TimeoutExpired
        with pytest.raises(expected):
            run_probe([sys.executable, "-c", body], timeout=0.8,
                      cancel_event=cancel if mode == "cancel" else None)
        assert time.monotonic() - started < 5
        if sys.platform.startswith("linux") and pidfile.exists():
            status = Path("/proc") / pidfile.read_text() / "status"
            deadline = time.monotonic() + 2
            while status.exists():
                try:
                    state = status.read_text()
                except FileNotFoundError:
                    break
                # SIGKILL peut précéder de quelques millisecondes l'arrêt du
                # descendant. Un zombie ne peut plus retenir les pipes.
                if "State:\tZ" in state:
                    break
                assert time.monotonic() < deadline, "descendant encore vivant"
                time.sleep(0.02)
    finally:
        # Nettoyage du descendant si une régression fait échouer le test.
        if pidfile.exists():
            import os
            from contextlib import suppress
            with suppress(OSError):
                os.kill(int(pidfile.read_text()), 9)
