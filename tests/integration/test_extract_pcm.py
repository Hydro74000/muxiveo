"""Extraction des PCM sans conversion (audit B03) : conteneur choisi selon le codec exact, codec relu identique,
refus explicite quand aucun conteneur ne le reçoit tel quel."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.extractor import TrackExtractor
from tests.integration._synth import ffprobe_json
from tests.integration.test_cli_extract import _cli, pytestmark  # noqa: F401  (ffmpeg/ffprobe requis)

EXTRACTABLE = sorted(TrackExtractor._PCM_WAV | TrackExtractor._PCM_AIFF)
REFUSED = ["pcm_s16le_planar", "pcm_s64be", "pcm_u16le", "pcm_u24be", "pcm_u32le"]


def _source(tmp_path: Path, codec: str) -> Path:
    source = tmp_path / f"{codec}.nut"
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.2", "-ac", "2", "-c:a", codec, str(source)],
                   check=True, capture_output=True)
    return source


@pytest.mark.parametrize("codec", EXTRACTABLE)
def test_pcm_extracted_without_conversion(tmp_path, codec):
    source = _source(tmp_path, codec)
    done = _cli(tmp_path, "extract", "-i", str(source), "--track", "a:0", "--output-dir", str(tmp_path / "out"))
    assert done.returncode == 0, done.stderr
    outputs = [path for path in (tmp_path / "out").iterdir() if not path.name.endswith(".partial")]
    assert len(outputs) == 1
    expected = ".aiff" if codec in TrackExtractor._PCM_AIFF else ".wav"
    assert outputs[0].suffix == expected
    assert ffprobe_json(outputs[0])["streams"][0]["codec_name"] == codec


@pytest.mark.parametrize("codec", REFUSED)
def test_pcm_without_lossless_container_is_refused(tmp_path, codec):
    source = _source(tmp_path, codec)
    done = _cli(tmp_path, "extract", "-i", str(source), "--track", "a:0", "--output-dir", str(tmp_path / "out"))
    assert done.returncode == 3, done.stderr  # validation : rien d'écrit, aucune conversion implicite
    assert "sans conversion" in done.stderr
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


@pytest.mark.parametrize("codec", ["pcm_f32be", "pcm_f64be", "pcm_s8"])
def test_matroska_refused_for_pcm_it_cannot_carry(tmp_path, codec):
    source = _source(tmp_path, codec)
    done = _cli(tmp_path, "extract", "-i", str(source), "--track", "a:0", "--format", "mkv",
                "--output-dir", str(tmp_path / "out"))
    assert done.returncode == 3, done.stderr
    assert "Matroska" in done.stderr


@pytest.mark.parametrize("codec", sorted(TrackExtractor._PCM_MATROSKA))
def test_matroska_keeps_pcm_codec(tmp_path, codec):
    source = _source(tmp_path, codec)
    done = _cli(tmp_path, "extract", "-i", str(source), "--track", "a:0", "--format", "mkv",
                "--output-dir", str(tmp_path / "out"))
    assert done.returncode == 0, done.stderr
    output, = [path for path in (tmp_path / "out").iterdir() if not path.name.endswith(".partial")]
    assert ffprobe_json(output)["streams"][0]["codec_name"] == codec
