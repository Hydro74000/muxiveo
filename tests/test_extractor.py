from pathlib import Path

from core.extractor import TrackExtractor


def test_subtitle_command_accepts_ffmpeg_progress_args() -> None:
    cmd = TrackExtractor.build_subtitle_command(
        "ffmpeg",
        Path("/in.mkv"),
        3,
        "subrip",
        Path("/out.srt"),
        progress_args=["-progress", "pipe:1", "-nostats"],
    )

    assert cmd[:5] == ["ffmpeg", "-hide_banner", "-y", "-progress", "pipe:1"]
    assert "-nostats" in cmd
    assert cmd[cmd.index("-map") + 1] == "0:3"
    assert cmd[cmd.index("-c:s") + 1] == "copy"


def test_subtitle_command_converts_mov_text_to_srt() -> None:
    cmd = TrackExtractor.build_subtitle_command(
        "ffmpeg",
        Path("/in.mp4"),
        2,
        "mov_text",
        Path("/out.srt"),
    )

    assert cmd[cmd.index("-c:s") + 1] == "srt"
