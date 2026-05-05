"""Backend-specific progress parsers."""

from __future__ import annotations

import re

from core.workflows.encode.backends.models import ProgressEvent


_FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_FFMPEG_OUT_TIME_RE = re.compile(r"\bout_time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_FFMPEG_OUT_TIME_TICKS_RE = re.compile(r"\bout_time_(?:ms|us)=(\d+)")
_FFMPEG_FPS_RE = re.compile(r"\bfps=\s*([\d.]+)")
_FFMPEG_FRAME_RE = re.compile(r"\bframe=\s*(\d+)")
_FFMPEG_PROGRESS_PREFIXES: tuple[str, ...] = (
    "frame=",
    "fps=",
    "stream_",
    "bitrate=",
    "total_size=",
    "out_time=",
    "out_time_ms=",
    "out_time_us=",
    "dup_frames=",
    "drop_frames=",
    "speed=",
    "progress=",
)
_NVENCC_PROGRESS_WITH_PERCENT_RE = re.compile(
    r"^\[(?P<pct>\d{1,3}(?:\.\d+)?)%\]\s+"
    r"(?P<frame>\d+)\s+frames:\s+"
    r"(?P<fps>[\d.]+)\s+fps,"
    r"(?:.*?\bremain\s+(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2}))?",
    re.IGNORECASE,
)
_NVENCC_PROGRESS_PLAIN_RE = re.compile(
    r"^(?P<frame>\d+)\s+frames:\s+"
    r"(?P<fps>[\d.]+)\s+fps,",
    re.IGNORECASE,
)
_NVENCC_ENCODED_SUMMARY_RE = re.compile(
    r"^encoded\s+(?P<frame>\d+)\s+frames,\s+"
    r"(?P<fps>[\d.]+)\s+fps,",
    re.IGNORECASE,
)


def _hms_to_seconds(hours: str, minutes: str, seconds: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_ffmpeg_progress_seconds(line: str) -> float | None:
    match = _FFMPEG_TIME_RE.search(line)
    if match:
        return _hms_to_seconds(match.group(1), match.group(2), match.group(3))

    match = _FFMPEG_OUT_TIME_RE.search(line)
    if match:
        return _hms_to_seconds(match.group(1), match.group(2), match.group(3))

    match = _FFMPEG_OUT_TIME_TICKS_RE.search(line)
    if match:
        return int(match.group(1)) / 1_000_000.0

    return None


def parse_ffmpeg_progress(line: str) -> ProgressEvent | None:
    elapsed_seconds = parse_ffmpeg_progress_seconds(line)

    fps: float | None = None
    fps_match = _FFMPEG_FPS_RE.search(line)
    if fps_match:
        try:
            fps = float(fps_match.group(1))
        except ValueError:
            fps = None

    frame: int | None = None
    frame_match = _FFMPEG_FRAME_RE.search(line)
    if frame_match:
        try:
            frame = int(frame_match.group(1))
        except ValueError:
            frame = None

    if (
        elapsed_seconds is None
        and fps is None
        and frame is None
        and not any(line.startswith(prefix) for prefix in _FFMPEG_PROGRESS_PREFIXES)
    ):
        return None

    return ProgressEvent(
        raw_line=line,
        frame=frame,
        fps=fps,
        elapsed_seconds=elapsed_seconds,
        should_log=False,
    )


def parse_nvencc_progress(line: str) -> ProgressEvent | None:
    stripped = line.strip()

    percent: float | None = None
    frame: int | None = None
    fps: float | None = None
    eta_seconds: float | None = None

    match = _NVENCC_PROGRESS_WITH_PERCENT_RE.search(stripped)
    if match is not None:
        try:
            percent = float(match.group("pct"))
        except ValueError:
            percent = None
        try:
            frame = int(match.group("frame"))
        except ValueError:
            frame = None
        try:
            fps = float(match.group("fps"))
        except ValueError:
            fps = None
        hours = match.group("h")
        minutes = match.group("m")
        seconds = match.group("s")
        if hours is not None and minutes is not None and seconds is not None:
            eta_seconds = _hms_to_seconds(hours, minutes, seconds)
    else:
        match = _NVENCC_PROGRESS_PLAIN_RE.search(stripped)
        if match is not None:
            try:
                frame = int(match.group("frame"))
            except ValueError:
                frame = None
            try:
                fps = float(match.group("fps"))
            except ValueError:
                fps = None
        else:
            match = _NVENCC_ENCODED_SUMMARY_RE.search(stripped)
            if match is None:
                return None
            try:
                frame = int(match.group("frame"))
            except ValueError:
                frame = None
            try:
                fps = float(match.group("fps"))
            except ValueError:
                fps = None

    return ProgressEvent(
        raw_line=line,
        percent=percent,
        frame=frame,
        fps=fps,
        eta_seconds=eta_seconds,
        should_log=False,
    )
