"""Lot 6 : profondeur effectivement écrite, vérifiée par ffprobe."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from core.workflows.encode import EncodeConfig, EncodeWorkflow, VideoEncodeSettings
from core.workflows.encode.catalog import default_preset_for_codec
from tests.integration._synth import ffprobe_json, wait_task

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg/ffprobe requis")


@pytest.fixture(params=[8, 10])
def depth_source(request, tmp_path):
    depth = request.param
    path = tmp_path / "source.mkv"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        # Une seconde : NVEncC/NVDEC se bloque parfois avant la première
        # image sur un HEVC synthétique de seulement trois images.
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24", "-frames:v", "24",
        "-pix_fmt", "yuv420p10le" if depth == 10 else "yuv420p",
        "-c:v", "libx265", "-preset", "ultrafast",
        "-x265-params", "log-level=error:pools=1:frame-threads=1", str(path),
    ], capture_output=True, check=True, timeout=30)
    return path, depth


def _encode_and_check(qt_app, tmp_path, depth_source, codec, choice):
    source, depth = depth_source
    output = tmp_path / "out.mkv"
    workflow = EncodeWorkflow(
        ffmpeg_bin="ffmpeg", nvencc_bin=shutil.which("nvencc"),
        ram_buffer_enabled=False, ffmpeg_threads=2, generate_nfo=False,
    )
    video = VideoEncodeSettings(codec=codec, bit_depth=choice, preset=default_preset_for_codec(codec))
    config = EncodeConfig(source=source, output=output, video=video, copy_subtitles=False, keep_chapters=False, mux_backend="ffmpeg")
    assert workflow.validate(config) == []
    resolved = workflow.resolve_source_color_transfer(config)
    assert resolved.video.source_bit_depth == depth
    if codec == "hevc_nvenc" and choice == "auto":
        assert "-hwaccel" in workflow.build_command_single(config)
    state = wait_task(workflow.run(config), timeout=60)
    failure = "\n".join(str(part) for part in state["failed"]) if state["failed"] else ""
    (tmp_path / "workflow.log").write_text(failure, encoding="utf-8")
    assert not failure, failure
    assert state["finished"] and not state["cancelled"]
    expected = (8 if codec in {"h264_nvenc", "nvencc_h264"} else depth) if choice == "auto" else int(choice)
    stream = next(s for s in ffprobe_json(output)["streams"] if s["codec_type"] == "video")
    assert stream["pix_fmt"] == ("yuv420p10le" if expected == 10 else "yuv420p")
    if codec == "libx264" and expected == 10:
        assert stream["profile"] == "High 10"


@pytest.mark.parametrize("codec", ["libx264", "libx265", "libsvtav1"])
@pytest.mark.parametrize("choice", ["auto", "8", "10"])
def test_lot6_software_depth(qt_app, tmp_path, depth_source, codec, choice):
    _encode_and_check(qt_app, tmp_path, depth_source, codec, choice)


@pytest.mark.skipif(not {"nvenc", "nvencc"} <= set(os.environ.get("MUXIVEO_TEST_HW", "").split(",")), reason="MUXIVEO_TEST_HW=nvenc,nvencc requis")
@pytest.mark.parametrize("codec", ["h264_nvenc", "hevc_nvenc", "av1_nvenc", "nvencc_h264", "nvencc_hevc", "nvencc_av1"])
@pytest.mark.parametrize("choice", ["auto", "8", "10"])
def test_lot6_nvidia_depth(qt_app, tmp_path, depth_source, codec, choice):
    if choice == "10" and codec in {"h264_nvenc", "nvencc_h264"}:
        pytest.skip("H.264 10 bits non garanti sur le GPU de test")
    _encode_and_check(qt_app, tmp_path, depth_source, codec, choice)


@pytest.mark.skipif("nvenc" not in os.environ.get("MUXIVEO_TEST_HW", "").split(","), reason="MUXIVEO_TEST_HW=nvenc requis")
@pytest.mark.parametrize("codec", ["h264_nvenc", "hevc_nvenc"])
def test_lot6_high10_to_hardware_8bit(qt_app, tmp_path, codec):
    source = tmp_path / "high10.mkv"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24", "-frames:v", "24",
        "-pix_fmt", "yuv420p10le", "-c:v", "libx264", "-preset", "ultrafast", str(source),
    ], capture_output=True, check=True, timeout=30)
    _encode_and_check(qt_app, tmp_path, (source, 10), codec, "8")
