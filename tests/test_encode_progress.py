"""
tests/test_encode_progress.py — Helpers de parsing pour la progression FFmpeg.
"""

from ui.panels.encode_panel.theme import ffmpeg_progress_seconds


def test_ffmpeg_progress_seconds_parses_classic_time_line() -> None:
    line = "frame= 148 fps=42.0 q=28.0 size=1024kB time=00:01:02.50 bitrate=134.2kbits/s"
    assert ffmpeg_progress_seconds(line) == 62.5


def test_ffmpeg_progress_seconds_parses_machine_out_time_line() -> None:
    line = "out_time=00:03:04.250000"
    assert ffmpeg_progress_seconds(line) == 184.25


def test_ffmpeg_progress_seconds_parses_tick_fallback() -> None:
    line = "out_time_ms=1500000"
    assert ffmpeg_progress_seconds(line) == 1.5
