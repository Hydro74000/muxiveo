"""Real dovi_tool/FFmpeg checks: scene timing and non-geometric RPU data survive."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.dovi_profile_detector import DoviProfileDetector
from core.workflows.encode.runtime.crop_detector import detect_black_bars_ffmpeg
from core.workflows.encode.runtime.dovi_geometry import align_dovi_rpu_geometry, extract_dovi_rpu

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe", "dovi_tool")),
    reason="FFmpeg, ffprobe and dovi_tool are required",
)


def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=45)


@pytest.fixture
def variable_rpu(tmp_path):
    config = {
        "cm_version": "V40", "length": 300,
        "level6": {"max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
                   "max_content_light_level": 1000, "max_frame_average_light_level": 400},
        "default_metadata_blocks": [{"Level1": {"min_pq": 0, "max_pq": 3079, "avg_pq": 1500}}],
    }
    generator = tmp_path / "generator.json"
    generator.write_text(json.dumps(config))
    raw = tmp_path / "generated.bin"
    run(["dovi_tool", "generate", "-j", str(generator), "-o", str(raw)])
    edit = tmp_path / "scenes.json"
    edit.write_text(json.dumps({"active_area": {
        "presets": [{"id": 0, "top": 16, "bottom": 16, "left": 0, "right": 0},
                    {"id": 1, "top": 0, "bottom": 0, "left": 0, "right": 0}],
        "edits": {"0-99": 0, "100-199": 1, "200-299": 0},
    }}))
    output = tmp_path / "variable.bin"
    run(["dovi_tool", "editor", "-i", str(raw), "-j", str(edit), "-o", str(output)])
    return output


def export_all(rpu: Path, output: Path):
    run(["dovi_tool", "export", "-i", str(rpu), "-d", f"all={output}"])
    return json.loads(output.read_text())


def without_geometry(value):
    if isinstance(value, dict):
        return {key: without_geometry(item) for key, item in value.items()
                if key not in {"Level5", "rpu_data_crc32"}}
    if isinstance(value, list):
        return [without_geometry(item) for item in value]
    return value


@pytest.mark.parametrize("crop,pad", [((0, 0, 0, 0), (0, 2, 0, 4)),
                                      ((0, 8, 0, 10), (0, 0, 0, 0))])
def test_real_rpu_editor_preserves_other_metadata_and_frame_ranges(tmp_path, variable_rpu, crop, pad):
    before = export_all(variable_rpu, tmp_path / "before.json")
    edited = align_dovi_rpu_geometry(
        dovi_tool_bin="dovi_tool", rpu_input=variable_rpu, output_rpu=tmp_path / "edited.bin",
        crop_offsets=crop, pad_offsets=pad, work_dir=tmp_path,
    )
    after = export_all(edited, tmp_path / "after.json")
    assert len(before) == len(after) == 300
    assert without_geometry(before) == without_geometry(after)
    l5_json = tmp_path / "after_l5.json"
    run(["dovi_tool", "export", "-i", str(edited), "-d", f"level5={l5_json}"])
    l5 = json.loads(l5_json.read_text())
    presets = {p["id"]: p for p in l5["presets"]}
    for frame_range, preset_id in l5["edits"].items():
        start = int(frame_range.split("-")[0])
        old_top = 0 if 100 <= start <= 199 else 16
        assert presets[preset_id]["top"] == max(0, old_top - crop[1]) + pad[1]
        assert presets[preset_id]["bottom"] == max(0, old_top - crop[3]) + pad[3]
    assert set(l5["edits"]) == {"0-99", "100-199", "200-299"}


@pytest.fixture
def variable_mp4(tmp_path, variable_rpu):
    base = tmp_path / "base.hevc"
    run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25",
         "-frames:v", "300", "-c:v", "libx265", "-pix_fmt", "yuv420p10le",
         "-x265-params", "log-level=error:pools=1:bframes=0:keyint=25", str(base)])
    injected = tmp_path / "injected.hevc"
    run(["dovi_tool", "inject-rpu", "-i", str(base), "--rpu-in", str(variable_rpu), "-o", str(injected)])
    mp4 = tmp_path / "source.mp4"
    run(["ffmpeg", "-v", "error", "-fflags", "+genpts", "-r", "25", "-i", str(injected),
         "-c:v", "copy", "-strict", "unofficial", str(mp4)])
    return mp4


def test_real_mp4_extraction_and_seek_sampling(tmp_path, variable_rpu, variable_mp4):
    mp4 = variable_mp4
    cleanup = []
    raw = extract_dovi_rpu(source=mp4, stream_index=0, ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool",
                           output_rpu=tmp_path / "extracted.bin", work_dir=tmp_path,
                           cleanup_paths=cleanup, run_cmd=run)
    assert export_all(raw, tmp_path / "mp4_rpu.json") == export_all(variable_rpu, tmp_path / "original.json")
    assert not (tmp_path / "source_meta.hevc").exists()
    assert DoviProfileDetector().probe_l5_offsets(mp4) == (0, 0, 0, 0)


def test_real_short_clip_cropdetect(tmp_path):
    source = tmp_path / "short.mkv"
    run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=white:s=320x160:r=5:d=2",
         "-vf", "pad=320:240:0:40:black", "-c:v", "ffv1", str(source)])
    assert detect_black_bars_ffmpeg(source, dimensions=(320, 240), duration_s=2) == (40, 40, 0, 0)


@pytest.mark.parametrize("operation,expected_size", [("pad", (320, 192)), ("crop", (320, 160)),
                                                     ("resize", (160, 90))])
def test_real_nvencc_preserves_rpu_after_geometry(tmp_path, variable_mp4, operation, expected_size):
    from core.workflows.encode.models import VideoCropSettings, VideoEncodeSettings, VideoResizeSettings
    from core.workflows.encode.runtime.dovi_geometry import align_nvencc_dovi_geometry
    from core.workflows.encode.runtime.nvencc import build_nvencc_command, detect_nvencc_available

    nvencc = shutil.which("nvencc")
    if not nvencc or not detect_nvencc_available(nvencc)[0]:
        pytest.skip("NVEncC with an accessible GPU is required")
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, dovi_profile="8.1")
    if operation == "crop":
        video.crop = VideoCropSettings(enabled=True, top=2, bottom=2)
    if operation == "resize":
        video.resize = VideoResizeSettings(enabled=True, mode="size", width=160, height=90)
    geometry = align_nvencc_dovi_geometry(video, (320, 180), (0, 0, 0, 0))
    rpu = None
    if geometry.needs_rpu_alignment:
        raw = extract_dovi_rpu(source=variable_mp4, stream_index=0, ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool",
                               output_rpu=tmp_path / "extracted.bin", work_dir=tmp_path,
                               cleanup_paths=[], run_cmd=run)
        rpu = align_dovi_rpu_geometry(
            dovi_tool_bin="dovi_tool", rpu_input=raw, output_rpu=tmp_path / "aligned.bin", work_dir=tmp_path,
            crop_offsets=geometry.crop_offsets or (0, 0, 0, 0), pad_offsets=geometry.pad_offsets or (0, 0, 0, 0),
        )
    output = tmp_path / "encoded.mkv"
    cmd = build_nvencc_command(nvencc, geometry.video, output, input_path=variable_mp4,
                               input_reader="avsw", dovi_rpu=rpu, vpp_pad=geometry.vpp_pad, source_fps="25")
    run(cmd)
    streams = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]).stdout)["streams"]
    assert (streams[0]["width"], streams[0]["height"]) == expected_size
    if operation == "resize":
        assert not any(item.get("side_data_type") == "DOVI configuration record"
                       for item in streams[0].get("side_data_list", []))
    else:
        output_rpu = tmp_path / "output.bin"
        run(["dovi_tool", "extract-rpu", "-i", str(output), "-o", str(output_rpu)])
        # NVEncC must insert the edited RPU in the original display order.
        assert rpu is not None
        assert export_all(output_rpu, tmp_path / "output.json") == export_all(rpu, tmp_path / "aligned.json")
