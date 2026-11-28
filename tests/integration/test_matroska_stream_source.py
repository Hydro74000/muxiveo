"""Flux Matroska d'un vrai FFmpeg sur pipe : mêmes blocs que le même flux écrit en fichier."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from core.matroska.reader import MatroskaReader
from core.matroska.stream_source import open_stream_reader
from core.workflows.common.ffmpeg_runtime import matroska_output_args

from ._synth import make_av_container

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg requis")


def _copy_command(source: Path, stream_index: int, output: str) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", f"0:{stream_index}", "-c", "copy",
        *matroska_output_args("ffmpeg"), "-write_crc32", "0", "-f", "matroska", output,
    ]


def _summary(blocks) -> list[tuple]:
    return [(b.timestamp_ns, b.duration_ns, b.references_ns, b.payload) for b in blocks]


@pytest.mark.parametrize("container", ["mp4", "mkv"])
@pytest.mark.parametrize("stream_index", [0, 1])
def test_pipe_stream_equals_file(tmp_path: Path, container: str, stream_index: int) -> None:
    source = tmp_path / f"source.{container}"
    make_av_container(source, duration=3.0)
    reference = tmp_path / "reference.mkv"
    subprocess.run(_copy_command(source, stream_index, str(reference)), check=True)

    process = subprocess.Popen(_copy_command(source, stream_index, "pipe:1"), stdout=subprocess.PIPE)
    try:
        assert process.stdout is not None
        reader = open_stream_reader(process.stdout, tmp_path / "header.mkv")
        streamed = _summary(reader.blocks(first_lace_only=True))
        tracks = reader.tracks()
    finally:
        if process.stdout is not None:
            process.stdout.close()
        assert process.wait(timeout=60) == 0

    expected = MatroskaReader(reference)
    assert [(t.codec_id, t.codec_private) for t in tracks] == [
        (t.codec_id, t.codec_private) for t in expected.tracks()
    ]
    assert streamed == _summary(expected.blocks(first_lace_only=True))
    assert len(streamed) > 10
