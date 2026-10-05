"""tests/test_inspection_reuse.py — Les données de l'inspection évitent toute relecture de la source."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from core.inspector import FileInfo
from core.workflows.encode.runtime.hdr_metadata import HdrMetadataProbeService
from core.workflows.remux_models import _native_enabled_flags


def test_native_enabled_flags_reuse_inspection_without_reading(tmp_path: Path) -> None:
    path = tmp_path / "source.mkv"
    path.write_bytes(b"pas un mkv")
    info = FileInfo(path=path, format="matroska,webm", duration_s=None, size_bytes=None, bit_rate=None,
                    track_enabled={0: True, 1: False})
    with patch("core.matroska.reader.MatroskaReader.tracks", side_effect=AssertionError("relecture")):
        assert _native_enabled_flags(info) == {0: True, 1: False}


def test_remembered_ffprobe_payload_skips_new_probe(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    payload = {"streams": [{"index": 0, "codec_type": "video"}], "format": {}}
    service = HdrMetadataProbeService(ffmpeg_bin=lambda: "ffmpeg", tool_bin=lambda name: name)
    service.remember_ffprobe_payload(source, payload)
    with patch("subprocess.run", side_effect=AssertionError("ffprobe relancé")):
        assert service.ffprobe_streams_payload(source) is payload


def test_remember_ignores_payload_without_streams(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    service = HdrMetadataProbeService(ffmpeg_bin=lambda: "ffmpeg", tool_bin=lambda name: name)
    service.remember_ffprobe_payload(source, None)
    service.remember_ffprobe_payload(source, {"format": {}})
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert service.ffprobe_streams_payload(source) is None
