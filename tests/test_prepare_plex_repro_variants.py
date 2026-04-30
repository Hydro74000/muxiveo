from __future__ import annotations

from pathlib import Path

from scripts.prepare_plex_repro_variants import (
    VARIANTS,
    build_clip_output_name,
    build_extract_clip_cmd,
    build_output_name,
    build_remux_cmd,
)


def test_build_output_name_appends_variant_suffix():
    source = Path("/tmp/example.mkv")
    assert build_output_name(source, VARIANTS[0]) == "example.dv_hdr10.mkv"


def test_variants_cover_expected_matrix():
    matrix = {(variant.keep_dovi, variant.keep_hdr10plus) for variant in VARIANTS}
    assert matrix == {
        (True, False),
        (False, True),
        (False, False),
    }


def test_build_clip_output_name_includes_clip_window():
    source = Path("/tmp/example.mkv")
    result = build_clip_output_name(
        source,
        VARIANTS[0],
        clip_start="00:30:00",
        clip_duration="00:02:00",
    )
    assert result == "example.clip_00-30-00_00-02-00.dv_hdr10.mkv"


def test_extract_clip_cmd_builds_minimal_av_clip_without_subtitles():
    cmd = build_extract_clip_cmd(
        "ffmpeg",
        source=Path("/tmp/source.mkv"),
        dest=Path("/tmp/clip.mkv"),
        clip_start="00:30:00",
        clip_duration="00:02:00",
    )
    assert "-map" in cmd
    assert "0:v:0" in cmd
    assert "0:a?" in cmd
    assert "-sn" in cmd
    assert "-dn" in cmd
    assert "0:s?" not in cmd
    assert "0:t?" not in cmd


def test_remux_cmd_keeps_video_and_audio_only():
    cmd = build_remux_cmd(
        "ffmpeg",
        wrapped_video_mkv=Path("/tmp/video.mkv"),
        source_mkv=Path("/tmp/source_clip.mkv"),
        output_mkv=Path("/tmp/out.mkv"),
    )
    assert "0:v:0" in cmd
    assert "1:a?" in cmd
    assert "-sn" in cmd
    assert "-dn" in cmd
    assert "1:s?" not in cmd
