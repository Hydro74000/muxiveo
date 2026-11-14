"""Forme d'onde d'une piste audio sur une plage (données d'un client graphique du Synchro Studio)."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from core.subprocess_utils import ProbeCancelledError, run_cancellable_capture, subprocess_windows_no_window_kwargs

#: Fréquence de décodage (Hz) : assez pour des pics à la milliseconde, sans décoder la pleine résolution.
DECODE_RATE = 8000


class WaveformError(RuntimeError):
    """Décodage impossible."""


def stream_map(stream: str | int) -> str:
    """Flux FFmpeg : index ffprobe (« 3 ») ou spécificateur (« 0:a:1 », « a:1 »)."""
    text = str(stream).strip()
    if text.isdigit():
        return f"0:{text}"
    return text if text.startswith("0:") else f"0:{text}"


def track_peaks(ffmpeg: str, path: Path, stream: str | int, *, start_s: float, duration_s: float,
                resolution_ms: int = 10, cancel: threading.Event | None = None) -> np.ndarray:
    """Pic d'amplitude (mono, 0–1) de chaque tranche de ``resolution_ms`` sur [start, start + durée]."""
    command = [ffmpeg, "-nostdin", "-v", "error", "-ss", f"{max(0.0, start_s):.3f}", "-t", f"{duration_s:.3f}",
               "-i", str(path), "-map", stream_map(stream), "-ac", "1", "-ar", str(DECODE_RATE), "-f", "f32le",
               "pipe:1"]

    def check_cancelled() -> None:
        if cancel is not None and cancel.is_set():
            raise ProbeCancelledError("Forme d'onde annulée.")

    result = run_cancellable_capture(command, cancel_cb=cancel.is_set if cancel is not None else None,
                                     check_cancelled=check_cancelled, timeout=max(60.0, duration_s * 2),
                                     **subprocess_windows_no_window_kwargs())
    if result.returncode:
        raise WaveformError((result.stderr or b"").decode("utf-8", errors="replace").strip()[-400:])
    samples = np.abs(np.frombuffer(result.stdout, dtype="<f4"))
    bucket = max(1, DECODE_RATE * int(resolution_ms) // 1000)
    count = -(-samples.size // bucket)
    padded = np.zeros(count * bucket, dtype=np.float32)
    padded[:samples.size] = samples
    return np.minimum(padded.reshape(count, bucket).max(axis=1), 1.0) if count else np.zeros(0, dtype=np.float32)
