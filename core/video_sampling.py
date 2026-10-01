"""Bounded, seek-based sampling shared by video geometry probes."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

from core.subprocess_utils import subprocess_text_kwargs


def video_sample_times(duration_s: float | None) -> list[float]:
    """Include the opening and spread a few samples over the known duration."""
    if duration_s is None or not math.isfinite(duration_s) or duration_s <= 0:
        return [0.0]
    fractions = (0.0, 0.1, 0.3, 0.5, 0.7, 0.9) if duration_s > 30 else (0.0, 0.35, 0.7)
    # Leave enough room for several frames, including on very short clips.
    last_start = max(0.0, duration_s - 0.5)
    return sorted({min(duration_s * fraction, last_start) for fraction in fractions})


def probe_video_duration(source: Path, *, ffprobe_bin: str = "ffprobe") -> float | None:
    try:
        # argv séparés, sans shell ; ffprobe provient de la configuration locale.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        result = subprocess.run(  # nosec B603
            [ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
            capture_output=True, check=True, timeout=10, **subprocess_text_kwargs(),
        )
        duration = float(result.stdout.strip())
        return duration if math.isfinite(duration) and duration > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
