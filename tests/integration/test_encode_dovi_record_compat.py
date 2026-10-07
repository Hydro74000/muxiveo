"""Intégration : record DOVI reconstruit après un muxage qui l'a perdu (L5-A03).

Une piste P8.4 / P8.2 réencodée avec copie DV garde sa compatibilité d'image de
base (HLG / SDR) quand le record Matroska est recréé : patch après muxage FFmpeg
(``metadata_inject``) et assainissement de la sortie NVEncC (``sanitize_dovi_mkv``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.matroska.editors.dovi import MatroskaDoviBlockAdditionEditor, sanitize_dovi_mkv
from core.workflows.encode.dovi_policy import dovi_output_compat_id_for
from core.workflows.encode.models import VideoEncodeSettings
from core.workflows.encode.runtime.metadata_inject import _build_dovi_record_from_rpu, _resolve_dovi_compat_id

_TOOLS = ("ffmpeg", "ffprobe", "dovi_tool")

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in _TOOLS),
    reason="FFmpeg, ffprobe et dovi_tool requis",
)


@pytest.fixture(scope="module")
def mkv_without_record(tmp_path_factory) -> tuple[Path, Path]:
    """HEVC + RPU P8.4 muxé sans record DOVI, et son RPU."""
    work = tmp_path_factory.mktemp("dovi_record")
    config = work / "p84.json"
    config.write_text(json.dumps({"cm_version": "V40", "profile": "8.4", "length": 8}), encoding="utf-8")
    rpu = work / "p84.bin"
    subprocess.run(["dovi_tool", "generate", "-j", str(config), "-o", str(rpu)], check=True, capture_output=True)
    base = work / "bl.hevc"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=256x144:r=24",
         "-frames:v", "8", "-pix_fmt", "yuv420p10le", "-c:v", "libx265",
         "-x265-params", "log-level=0:bframes=0:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
         "-f", "hevc", str(base)],
        check=True, capture_output=True,
    )
    injected = work / "dv.hevc"
    subprocess.run(["dovi_tool", "inject-rpu", "-i", str(base), "-r", str(rpu), "-o", str(injected)],
                   check=True, capture_output=True)
    mkv = work / "no_record.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts", "-r", "24",
         "-i", str(injected), "-c", "copy", str(mkv)],
        check=True, capture_output=True,
    )
    assert _compat(mkv) is None
    return mkv, rpu


def _compat(path: Path) -> int | None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    for side in json.loads(out)["streams"][0].get("side_data_list") or []:
        if side.get("side_data_type") == "DOVI configuration record":
            return side.get("dv_bl_signal_compatibility_id")
    return None


def _video(sub_profile: str) -> VideoEncodeSettings:
    return VideoEncodeSettings(codec="libx265", copy_dv=True, dovi_profile="0", dovi_source_profile=sub_profile)


@pytest.mark.parametrize(("sub_profile", "expected"), [("p8_4", 4), ("p8_2", 2), ("p8_1", 1)])
def test_post_mux_record_keeps_base_layer_compat(mkv_without_record, tmp_path, sub_profile, expected):
    mkv, rpu = mkv_without_record
    candidate = tmp_path / "candidate.mkv"
    shutil.copy(mkv, candidate)
    record = _build_dovi_record_from_rpu(
        rpu_bin=rpu,
        dovi_tool_bin="dovi_tool",
        forced_compat_id=_resolve_dovi_compat_id(
            p7_router_decision=None, user_dovi_profile="0", video=_video(sub_profile),
        ),
        min_level=None,
    )
    assert record is not None
    MatroskaDoviBlockAdditionEditor().patch(candidate, record=record)
    assert _compat(candidate) == expected


@pytest.mark.parametrize(("sub_profile", "expected"), [("p8_4", 4), ("p8_2", 2)])
def test_nvencc_sanitize_keeps_base_layer_compat(mkv_without_record, tmp_path, sub_profile, expected):
    mkv = mkv_without_record[0]
    candidate = tmp_path / "nvencc.mkv"
    shutil.copy(mkv, candidate)
    video = _video(sub_profile)
    sanitize_dovi_mkv(candidate, fps=24, target_compat_id=dovi_output_compat_id_for(video))
    assert _compat(candidate) == expected
