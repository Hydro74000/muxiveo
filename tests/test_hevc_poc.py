"""Ordre d'affichage d'un HEVC brut (POC) et images sans RPU (audit D01)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.matroska.hevc.access_units import iter_hevc_access_units
from core.matroska.hevc.poc import iter_pictures
from core.workflows.encode.runtime.dovi_rpu_gaps import scan_annexb_rpu_gaps, scan_rpu_gaps

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe requis")

GOPS = {
    "b_frames_open_gop": "keyint=12:min-keyint=12:scenecut=0:bframes=4:b-pyramid=1:open-gop=1",
    "closed_gop_idr": "keyint=8:min-keyint=8:scenecut=0:bframes=3:open-gop=0",
    "long_gop_short_poc": "keyint=40:min-keyint=40:scenecut=0:bframes=3:log2-max-poc-lsb=4",
}


def _encode(tmp_path: Path, params: str, frames: int = 60) -> tuple[Path, Path]:
    """MKV x265 (horodatages exacts du muxer) et son flux Annex B."""
    mkv, raw = tmp_path / "video.mkv", tmp_path / "video.hevc"
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=128x72:rate=25:duration={frames / 25}", "-c:v", "libx265",
                    "-x265-params", f"{params}:log-level=error", str(mkv)], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(mkv), "-map", "0:v:0", "-c", "copy",
                    "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(raw)], check=True, capture_output=True)
    return mkv, raw


def _display_ranks_from_timestamps(mkv: Path) -> list[int]:
    """Rang d'affichage de chaque image en ordre de décodage, d'après les PTS du conteneur."""
    packets = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                         "packet=pts", "-of", "json", str(mkv)], check=True, capture_output=True,
                                        text=True).stdout)["packets"]
    pts = [int(packet["pts"]) for packet in packets]
    order = sorted(range(len(pts)), key=lambda index: pts[index])
    rank = {decode: display for display, decode in enumerate(order)}
    return [rank[index] for index in range(len(pts))]


def _display_ranks_from_poc(raw: Path) -> list[int]:
    keys = [key for key, _kinds in iter_pictures(raw)]
    order = sorted(range(len(keys)), key=lambda index: (keys[index], index))
    rank = {decode: display for display, decode in enumerate(order)}
    return [rank[index] for index in range(len(keys))]


@pytest.mark.parametrize("gop", sorted(GOPS))
def test_poc_order_matches_container_timestamps(tmp_path, gop):
    mkv, raw = _encode(tmp_path, GOPS[gop])
    assert _display_ranks_from_poc(raw) == _display_ranks_from_timestamps(mkv)


def _with_rpu_except(raw: Path, target: Path, missing_decode: set[int]) -> None:
    """Ajoute un faux RPU (NAL 62, suffixe) à chaque image sauf celles de ``missing_decode`` (ordre de décodage)."""
    rpu = b"\x00\x00\x00\x01" + bytes([62 << 1, 1]) + b"\x01\x02\x03"
    with target.open("wb") as out:
        for index, unit in enumerate(iter_hevc_access_units(raw)):
            out.write(unit.payload + (b"" if index in missing_decode else rpu))


@pytest.mark.parametrize("gop", sorted(GOPS))
def test_gaps_are_reported_at_display_ranks(tmp_path, gop):
    mkv, raw = _encode(tmp_path, GOPS[gop])
    ranks = _display_ranks_from_timestamps(mkv)
    chosen = {3, 17, len(ranks) - 2}
    target = tmp_path / "with_rpu.hevc"
    _with_rpu_except(raw, target, chosen)
    gaps = scan_annexb_rpu_gaps(target)
    assert gaps.frames == len(ranks)
    assert gaps.missing == tuple(sorted(ranks[index] for index in chosen))
    assert gaps.trailing is False


def test_corpus_annexb_matches_matroska_scan(tmp_path):
    source = ROOT / "samples" / "BL_EL.mkv"
    if not source.is_file():
        pytest.skip("samples/BL_EL.mkv absent")
    raw = tmp_path / "bl_el.hevc"
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(source), "-map", "0:v:0", "-c", "copy",
                    "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(raw)], check=True, capture_output=True)
    assert scan_annexb_rpu_gaps(raw) == scan_rpu_gaps(source, 0)
    assert scan_rpu_gaps(source, 0).missing == (2875, 2876)
