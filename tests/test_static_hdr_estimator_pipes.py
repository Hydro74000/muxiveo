"""Analyse HDR10 estimée : tubes vidés en continu, annulation hors des lectures bloquantes."""
from __future__ import annotations

import sys
import threading
import time

import pytest

from core.workflows.encode.runtime.static_hdr_estimator import StaticHdrEstimateService

# Image linéaire 2×2 en float32 : 16 octets.
_FRAME = 16


def _service() -> StaticHdrEstimateService:
    return StaticHdrEstimateService(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool")


def test_verbose_stderr_never_blocks_frame_reading():
    script = ("import sys; sys.stderr.write('x' * 1_048_576); sys.stderr.flush(); "
              f"sys.stdout.buffer.write(b'\\0' * {_FRAME * 2}); sys.stdout.flush()")
    frames = _service()._analyze_linear_frames([sys.executable, "-c", script], width=2, height=2)
    assert len(frames) == 2


def test_cancellation_stops_a_tool_stuck_on_its_pipes():
    """Audit Codex G1 : stderr plein puis stdout silencieux ; l'annulation doit aboutir."""
    script = ("import sys, time; sys.stderr.write('x' * 262_144); sys.stderr.flush(); "
              f"sys.stdout.buffer.write(b'\\0' * {_FRAME}); sys.stdout.flush(); time.sleep(60)")
    cancel = threading.Event()
    outcome: list[BaseException | object] = []

    def run() -> None:
        try:
            outcome.append(_service()._analyze_linear_frames(
                [sys.executable, "-c", script], width=2, height=2, cancel_cb=cancel.is_set,
            ))
        except BaseException as exc:  # noqa: BLE001 — résultat observé par le test
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(1.0)
    cancel.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert isinstance(outcome[0], RuntimeError) and "annulée" in str(outcome[0])


def test_failing_tool_reports_the_end_of_its_stderr():
    script = "import sys; sys.stderr.write('a' * 200_000 + 'FIN_ERREUR'); sys.exit(3)"
    with pytest.raises(RuntimeError, match="FIN_ERREUR"):
        _service()._analyze_linear_frames([sys.executable, "-c", script], width=2, height=2)
