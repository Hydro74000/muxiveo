"""Intégration (outils réels) : HDR10+ généré depuis le Dolby Vision, en Copy (flux patché) et en réencodage.

Source synthétique : HEVC PQ 24 images, RPU P8.1 à deux plans (L1 différents) généré par ``dovi_tool``, muxé en
MKV. Extension mvo-fel isolée (``tests/conftest.py``) : repli L1 du RPU. Vérifie sur la sortie : une entrée HDR10+ par
image, plans du RPU, RPU et record Dolby Vision conservés en Copy, aucun HDR10+ dans la source.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.matroska.editors.dovi import sanitize_dovi_mkv
from core.workflows.encode import EncodeConfig, EncodeWorkflow, VideoEncodeSettings
from tests.integration._synth import wait_task

_TOOLS = ("ffmpeg", "ffprobe", "dovi_tool", "hdr10plus_tool", "mediainfo")
pytestmark = pytest.mark.skipif(not all(shutil.which(tool) for tool in _TOOLS),
                                reason="FFmpeg, ffprobe, MediaInfo, dovi_tool et hdr10plus_tool requis")

_FRAMES = 24


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    return qt_app


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


@pytest.fixture(scope="module")
def dv_source(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("hdr10plus_generation")
    config = work / "p81.json"
    level1 = [{"Level1": {"min_pq": 7, "max_pq": 3079, "avg_pq": 1229}},
              {"Level1": {"min_pq": 0, "max_pq": 2081, "avg_pq": 819}}]
    config.write_text(json.dumps({
        "cm_version": "V40", "profile": "8.1", "length": _FRAMES,
        "level6": {"max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
                   "max_content_light_level": 1000, "max_frame_average_light_level": 400},
        "shots": [{"start": 0, "duration": 12, "metadata_blocks": [level1[0]]},
                  {"start": 12, "duration": 12, "metadata_blocks": [level1[1]]}],
    }), encoding="utf-8")
    rpu = work / "p81.bin"
    _run(["dovi_tool", "generate", "-j", str(config), "-o", str(rpu)])
    base = work / "bl.hevc"
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=256x144:r=24",
          "-frames:v", str(_FRAMES), "-pix_fmt", "yuv420p10le", "-c:v", "libx265",
          "-x265-params", "log-level=0:bframes=0:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
          "-f", "hevc", str(base)])
    injected = work / "dv.hevc"
    _run(["dovi_tool", "inject-rpu", "-i", str(base), "-r", str(rpu), "-o", str(injected)])
    mkv = work / "dv.mkv"
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts", "-r", "24",
          "-i", str(injected), "-c", "copy", str(mkv)])
    sanitize_dovi_mkv(mkv, fps=24, target_compat_id=1)  # record DOVI P8.1, absent d'un muxage FFmpeg du brut
    assert _has_dovi_record(mkv)
    return mkv


def _encode(source: Path, output: Path, **video: object) -> dict:
    settings = VideoEncodeSettings(stream_index=0, source_path=source, generate_hdr10plus=True,
                                   dovi_source_profile="p8_1", source_color_transfer="smpte2084", **video)
    config = EncodeConfig(source=source, output=output, video=settings, audio_tracks=[], copy_subtitles=False,
                          duration_s=1.0, work_dir=output.parent / "work")
    workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg", ffmpeg_threads=1, generate_nfo=False)
    return wait_task(workflow.run(config), timeout=180.0)


def _hdr10plus(path: Path, tmp_path: Path) -> dict:
    target = tmp_path / f"{path.stem}.hdr10plus.json"
    _run(["hdr10plus_tool", "extract", str(path), "-o", str(target)])
    return json.loads(target.read_text(encoding="utf-8"))


def _rpu_frames(path: Path, tmp_path: Path) -> int:
    rpu = tmp_path / f"{path.stem}.rpu.bin"
    _run(["dovi_tool", "extract-rpu", "-i", str(path), "-o", str(rpu)])
    summary = _run(["dovi_tool", "info", "-i", str(rpu), "--summary"]).stdout
    return int(next(line for line in summary.splitlines() if "Frames" in line).split(":")[1])


def _has_dovi_record(path: Path) -> bool:
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(path)]).stdout
    return any(side.get("side_data_type") == "DOVI configuration record"
               for side in json.loads(out)["streams"][0].get("side_data_list") or [])


def test_copy_patches_the_stream_and_keeps_dolby_vision(dv_source, tmp_path):
    output = tmp_path / "copy.mkv"
    state = _encode(dv_source, output, codec="copy", copy_dv=True)
    assert state.get("finished"), state
    document = _hdr10plus(output, tmp_path)
    assert len(document["SceneInfo"]) == _FRAMES
    assert document["SceneInfoSummary"]["SceneFirstFrameIndex"] == [0, 12]
    first, second = (document["SceneInfo"][index]["LuminanceParameters"]["MaxScl"][0] for index in (0, 12))
    assert first > second  # plans du RPU : L1 max différents
    assert _rpu_frames(output, tmp_path) == _FRAMES and _has_dovi_record(output)


def test_reencode_generates_without_copying_dolby_vision(dv_source, tmp_path):
    output = tmp_path / "x265.mkv"
    state = _encode(dv_source, output, codec="libx265", crf=28, preset="ultrafast")
    assert state.get("finished"), state
    assert len(_hdr10plus(output, tmp_path)["SceneInfo"]) == _FRAMES
    assert not _has_dovi_record(output)


def test_generation_replaces_an_existing_hdr10plus(dv_source, tmp_path):
    from core.fel.hdr10plus import FrameRecord, FrameStatistics, SceneRecord, write_hdr10plus_json

    flat = FrameRecord(True, (999.0, 999.0, 999.0), 50.0, 999.0, 0.5, (1.0, 5.0, 10.0, 20.0, 50.0, 80.0, 999.0))
    source_json = tmp_path / "source_hdr10plus.json"
    write_hdr10plus_json(FrameStatistics(0, 0, (flat,) * _FRAMES, (SceneRecord(0, _FRAMES),)), source_json)
    annexb, with_plus, source = tmp_path / "src.hevc", tmp_path / "src_plus.hevc", tmp_path / "dv_plus.mkv"
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(dv_source), "-c", "copy",
          "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(annexb)])
    _run(["hdr10plus_tool", "inject", "-i", str(annexb), "-j", str(source_json), "-o", str(with_plus)])
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts", "-r", "24",
          "-i", str(with_plus), "-c", "copy", str(source)])
    assert _hdr10plus(source, tmp_path)["SceneInfo"][0]["LuminanceParameters"]["MaxScl"][0] == 9990
    output = tmp_path / "replaced.mkv"
    state = _encode(source, output, codec="copy", copy_dv=True)
    assert state.get("finished"), state
    document = _hdr10plus(output, tmp_path)
    assert len(document["SceneInfo"]) == _FRAMES  # une seule entrée par image : celle de la source retirée
    assert document["SceneInfoSummary"]["SceneFirstFrameIndex"] == [0, 12]
    assert all(entry["LuminanceParameters"]["MaxScl"][0] != 9990 for entry in document["SceneInfo"])
