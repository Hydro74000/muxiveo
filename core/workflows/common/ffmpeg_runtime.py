from __future__ import annotations

import os
from pathlib import Path


def cli_path(path: Path | str) -> str:
    if isinstance(path, str):
        return path
    text = str(path)
    if text.startswith("\\\\.\\pipe\\"):
        return text
    return path.as_posix()


def default_ffmpeg_thread_count() -> int:
    """Default FFmpeg thread count: logical CPU count x 0.75, rounded up."""
    cpu_count = os.cpu_count() or 1
    return max(1, (cpu_count * 3 + 3) // 4)


def normalize_ffmpeg_thread_count(value: int | None) -> int:
    """Return a safe FFmpeg thread count, preserving 0 as ffmpeg auto mode."""
    if value is None or value < 0:
        return default_ffmpeg_thread_count()
    return value


def normalize_max_parallel_video_encodes(value: int | None) -> int:
    """Return a safe per-workflow parallelism value for multi-video preparation."""
    if value is None:
        return 1
    return max(1, int(value))


def ffmpeg_progress_args() -> list[str]:
    """Force a stable machine-readable ffmpeg progress output."""
    return ["-progress", "pipe:1", "-nostats"]


def ffmpeg_thread_args(thread_count: int) -> list[str]:
    """Formate `-threads N` (N=0 → ffmpeg auto). Le caller a déjà normalisé `thread_count`."""
    return ["-threads", str(max(0, int(thread_count)))]
