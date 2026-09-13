"""Vérifie les tags et titres de chapitres dans les sorties réelles FFmpeg."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.workflows.encode import EncodeConfig, EncodeWorkflow, VideoEncodeSettings
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg et ffprobe requis",
)


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _probe(path: Path) -> dict:
    return json.loads(_run([
        "ffprobe", "-v", "error", "-show_format", "-show_chapters",
        "-show_streams", "-of", "json", str(path),
    ]))


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("container-metadata")
    metadata = root / "source.ffmetadata"
    metadata.write_text(
        ";FFMETADATA1\nIMDB=tt-source\nGENRE=Old\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=400\ntitle=Intro\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=400\nEND=1000\ntitle=Suite\n",
        encoding="utf-8",
    )
    path = root / "source.mkv"
    _run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "color=size=64x64:rate=5:duration=1", "-i", str(metadata),
        "-map", "0:v", "-map_metadata", "1", "-map_chapters", "1",
        "-metadata:s:v:0", "comment=Track note",
        "-c:v", "libx264", "-preset", "ultrafast", str(path),
    ])
    assert [c["tags"]["title"] for c in _probe(path)["chapters"]] == ["Intro", "Suite"]
    return path


@pytest.mark.parametrize("workflow", ["remux", "encode-copy", "encode-h264"])
@pytest.mark.parametrize("tag_mode", ["copy", "remove", "replace", "unchecked"])
@pytest.mark.parametrize("keep_chapters", [True, False])
def test_global_tags_do_not_control_chapter_or_stream_metadata(
    source, tmp_path, qt_app, workflow, tag_mode, keep_chapters,
):
    output = tmp_path / "output.mkv"
    overrides = {"GENRE": "New"} if tag_mode == "replace" else {}
    if tag_mode == "copy" or (workflow == "remux" and tag_mode == "unchecked"):
        overrides = None

    if workflow == "remux":
        config = RemuxConfig(
            sources=[SourceInput(
                path=source, file_index=0,
                tracks=[TrackEntry(
                    mkv_tid=0, track_type="video", codec="COPY", display_info="",
                    language="", title="",
                )],
                copy_tags=tag_mode == "copy", has_chapters=True,
            )],
            output=output, track_order=[(0, 0)],
            keep_chapters=keep_chapters, tag_overrides=overrides,
        )
        command = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").build_command(config)
    else:
        config = EncodeConfig(
            source=source, output=output,
            video=VideoEncodeSettings(
                codec="copy" if workflow == "encode-copy" else "libx264",
                preset="ultrafast",
            ),
            copy_subtitles=False, keep_chapters=keep_chapters,
            tag_overrides=overrides, duration_s=1,
        )
        command = EncodeWorkflow(
            ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool",
            hdr10plus_bin="hdr10plus_tool", ffmpeg_threads=1,
        ).build_command_single(config)

    _run(command)
    probe = _probe(output)
    tags = {key.upper(): value for key, value in probe["format"].get("tags", {}).items()}
    assert tags.get("IMDB") == ("tt-source" if tag_mode == "copy" else None)
    assert tags.get("GENRE") == {"copy": "Old", "replace": "New"}.get(tag_mode)
    assert probe["streams"][0]["tags"]["COMMENT"] == "Track note"
    chapters = probe.get("chapters", [])
    if keep_chapters:
        assert [(c["start_time"], c["end_time"], c.get("tags", {}).get("title")) for c in chapters] == [
            ("0.000000", "0.400000", "Intro"),
            ("0.400000", "1.000000", "Suite"),
        ]
    else:
        assert chapters == []
