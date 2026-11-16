"""Aperçus d'un job encodé (encode.preview_capture), presets d'encodage (encode_preset.*), gabarit avec encode."""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.encode_commands import _preset_from_args
from cli.encode_options import add_encode_options
from core.inspector import FileInfo, HDRType, VideoTrack
from core.jobs.errors import JobError
from core.jobs.inspection import encode_template_from_info
from core.runner import TaskSignals
from core.service import ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.ops import encode as encode_ops
from core.service.schemas.encode import EncodeBlock


def _ctx(tmp_path: Path) -> ExecutionContext:
    return ExecutionContext.create(SimpleNamespace(app_data_dir=tmp_path / "data", tool_ffmpeg="ffmpeg"), ListSink())


# --- Presets -------------------------------------------------------------------------------

def test_preset_lifecycle(tmp_path):
    preset = {"name": "x265 rapide", "codec": "libx265", "crf": 22, "preset": "fast",
              "restoration": {"enabled": True, "level": "moyen"}}
    assert call(CATALOG, "encode_preset.save", {"preset": preset}, _ctx(tmp_path)).name == "x265 rapide"
    with pytest.raises(JobError) as caught:
        call(CATALOG, "encode_preset.save", {"preset": preset}, _ctx(tmp_path))
    assert caught.value.exit_code == 5
    listed = call(CATALOG, "encode_preset.list", {}, _ctx(tmp_path)).presets
    assert listed == ({"name": "x265 rapide", "description": "", "codec": "libx265", "preset": "fast"},)
    shown = call(CATALOG, "encode_preset.show", {"name": "x265 rapide"}, _ctx(tmp_path)).preset
    assert shown["crf"] == 22 and shown["restoration"]["level"] == "moyen"
    exported = tmp_path / "p.json"
    call(CATALOG, "encode_preset.export", {"name": "x265 rapide", "file": str(exported)}, _ctx(tmp_path))
    call(CATALOG, "encode_preset.import", {"file": str(exported), "name": "Copie"}, _ctx(tmp_path))
    assert {p["name"] for p in call(CATALOG, "encode_preset.list", {}, _ctx(tmp_path)).presets} == \
        {"x265 rapide", "Copie"}
    call(CATALOG, "encode_preset.delete", {"name": "Copie"}, _ctx(tmp_path))
    with pytest.raises(JobError):
        call(CATALOG, "encode_preset.show", {"name": "Copie"}, _ctx(tmp_path))


def test_preset_fields_are_checked(tmp_path):
    with pytest.raises(JobError, match="inconnu"):
        call(CATALOG, "encode_preset.save", {"preset": {"name": "x", "speed": 1}}, _ctx(tmp_path))
    with pytest.raises(JobError, match="name"):
        call(CATALOG, "encode_preset.save", {"preset": {"codec": "libx265"}}, _ctx(tmp_path))


def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--description", default="")
    add_encode_options(parser)
    return parser.parse_args(list(argv))


def test_preset_from_cli_options_keeps_encoder_settings_only():
    preset = _preset_from_args(_args("P", "--video-codec", "hevc_nvenc", "--cq", "24", "--encoder-preset", "p5",
                                     "--resize", "1080p", "--bake-fel", "--tonemap", "mobius"))
    assert preset == {"name": "P", "description": "", "codec": "hevc_nvenc", "cq": 24, "preset": "p5",
                      "resize": {"enabled": True, "mode": "preset", "preset": "1080p"}, "bake_dovi_fel": True,
                      "tonemap_algorithm": "mobius"}
    with pytest.raises(JobError, match="propres à la source"):
        _preset_from_args(_args("P", "--hdr10plus", "off"))


# --- Gabarit ---------------------------------------------------------------------------------

def test_template_encode_block_mirrors_source_hdr():
    video = VideoTrack(index=0, codec="hevc", codec_long="hevc", width=3840, height=2160, frame_rate="24",
                       bit_depth=10, color_space=None, color_primaries=None, color_transfer="smpte2084",
                       color_matrix=None, hdr_type=HDRType.DOLBY_VISION_HDR10PLUS, raw={})
    info = FileInfo(path=Path("/a.mkv"), format="matroska", duration_s=1.0, size_bytes=1, bit_rate=1,
                    video_tracks=[video], hdr_type=HDRType.DOLBY_VISION_HDR10PLUS)
    block = encode_template_from_info(info)
    assert block["video"]["default"] == {"codec": "copy", "dolby_vision": {"mode": "copy"},
                                         "hdr10plus": {"mode": "copy"}, "static_hdr": {"mode": "source"}}
    EncodeBlock.from_dict(block)


# --- Aperçus ---------------------------------------------------------------------------------

class _PreviewWorkflow:
    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir

    def run_preview(self, config, request):
        previews = self.work_dir / "previews"
        previews.mkdir(parents=True, exist_ok=True)
        image = previews / "scene.png"
        image.write_bytes(b"png")
        other = previews / "autre-job.png"  # fichier d'un autre aperçu : jamais touché
        other.write_bytes(b"x")
        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def go():
            time.sleep(0.02)
            signals.finished.emit(json.dumps({"mode": request.mode, "captures": [
                {"image_path": str(image), "scene_time_s": 12.0, "label": "scène"}], "video_path": None,
                "warning": ""}))

        signals.watch_future(executor.submit(go))
        executor.shutdown(wait=False)
        return signals


def test_preview_capture_copies_only_its_files(qapp, tmp_path, monkeypatch):
    work = tmp_path / "work"
    resolved = SimpleNamespace(config=SimpleNamespace(output=tmp_path / "film.mkv", video_tracks=[]))
    monkeypatch.setattr(encode_ops, "_build_cached", lambda request, ctx: ("remux", SimpleNamespace(ffmpeg=None), None))
    monkeypatch.setattr(encode_ops, "resolve_job_encode", lambda *a: resolved)
    monkeypatch.setattr(encode_ops, "encode_workflow", lambda config, options: _PreviewWorkflow(work))
    monkeypatch.setattr(encode_ops, "prepare_vulkan", lambda *a: None)
    out = tmp_path / "apercus"
    result: Any = call(CATALOG, "encode.preview_capture", {"job": {}, "output_dir": str(out), "timecode_s": 12},
                       _ctx(tmp_path))
    assert result.captures[0]["path"] == str(out / "film.apercu-12.0s.png")
    assert (out / "film.apercu-12.0s.png").read_bytes() == b"png"
    assert not (work / "previews" / "scene.png").exists() and (work / "previews" / "autre-job.png").exists()


def test_preview_refuses_a_job_without_encoding(tmp_path, monkeypatch):
    monkeypatch.setattr(encode_ops, "_build_cached", lambda request, ctx: ("remux", SimpleNamespace(), None))
    monkeypatch.setattr(encode_ops, "resolve_job_encode", lambda *a: None)
    with pytest.raises(JobError, match="n'encode rien"):
        call(CATALOG, "encode.preview_capture", {"job": {}}, _ctx(tmp_path), base_dir=tmp_path)
