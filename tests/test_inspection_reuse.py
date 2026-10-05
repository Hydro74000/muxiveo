"""tests/test_inspection_reuse.py — Les données de l'inspection évitent toute relecture de la source."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

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
    payload: dict[str, object] = {"streams": [{"index": 0, "codec_type": "video"}], "format": {}}
    service = HdrMetadataProbeService(ffmpeg_bin=lambda: "ffmpeg", tool_bin=lambda name: name)
    service.remember_ffprobe_payload(source, payload, source_key=service.source_cache_key(source))
    with patch("subprocess.run", side_effect=AssertionError("ffprobe relancé")):
        assert service.ffprobe_streams_payload(source) is payload


def test_remember_ignores_payload_without_streams(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    service = HdrMetadataProbeService(ffmpeg_bin=lambda: "ffmpeg", tool_bin=lambda name: name)
    source_key = service.source_cache_key(source)
    service.remember_ffprobe_payload(source, None, source_key=source_key)
    service.remember_ffprobe_payload(source, {"format": {}}, source_key=source_key)
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert service.ffprobe_streams_payload(source) is None


def test_remember_ignores_payload_without_inspection_signature(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    payload: dict[str, object] = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}
    service = HdrMetadataProbeService(ffmpeg_bin=lambda: "ffmpeg", tool_bin=lambda name: name)
    service.remember_ffprobe_payload(source, payload)
    with patch("subprocess.run", side_effect=FileNotFoundError) as probe:
        assert service.ffprobe_streams_payload(source) is None
        probe.assert_called_once()


@pytest.mark.parametrize("change", ["size", "mtime"])
def test_old_inspection_cannot_overwrite_probe_for_modified_source(tmp_path: Path, change: str) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    old_payload: dict[str, object] = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}
    new_payload = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}]}
    service = HdrMetadataProbeService(ffmpeg_bin=lambda: "ffmpeg", tool_bin=lambda name: name)
    inspected_key = service.source_cache_key(source)
    service.remember_ffprobe_payload(source, old_payload, source_key=inspected_key)
    st = source.stat()
    if change == "size":
        source.write_bytes(b"changed")
        os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns))
    else:
        os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    service.remember_ffprobe_payload(source, old_payload, source_key=inspected_key)
    result = subprocess.CompletedProcess([], 0, json.dumps(new_payload), "")
    with patch("subprocess.run", return_value=result) as probe:
        assert service.ffprobe_streams_payload(source) == new_payload
        # Un rafraîchissement UI ultérieur ne doit pas écraser ce probe récent.
        service.remember_ffprobe_payload(source, old_payload, source_key=inspected_key)
        assert service.ffprobe_streams_payload(source) == new_payload
        probe.assert_called_once()
