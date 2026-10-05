"""Intégration : HDR10 statique et VUI d'une sortie HDR (lot 3 de l'audit onglet Video).

Source HEVC 10 bits PQ dont le MDCV / MaxCLL est porté par des SEI x265 :
ffmpeg les recopie dans la sortie (images et conteneur) tant qu'ils ne sont pas
retirés explicitement.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.workflows.encode import EncodeConfig, EncodeWorkflow, QualityMode, VideoEncodeSettings

from tests.integration._synth import wait_task


def _has_libx265() -> bool:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return False
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=False,
    ).stdout
    return "libx265" in encoders


pytestmark = pytest.mark.skipif(not _has_libx265(), reason="ffmpeg avec libx265 requis")

_SOURCE_MD = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
_EDITED_MD = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(40000000,50)"


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    return qt_app


def _make_hdr10_source(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24", "-t", "1",
            "-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc",
            "-pix_fmt", "yuv420p10le", "-c:v", "libx265",
            "-x265-params", f"log-level=error:master-display={_SOURCE_MD}:max-cll=1000,400",
            str(path),
        ],
        check=True,
    )


def _video_hdr(path: Path) -> tuple[str, str]:
    """(color_transfer, luminance max du MDCV de la 1re image ou "")."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
            "-show_entries", "stream=color_transfer:frame_side_data", "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    transfer = str((data.get("streams") or [{}])[0].get("color_transfer") or "")
    side_data = (data.get("frames") or [{}])[0].get("side_data_list") or []
    mdcv = next(
        (str(item.get("max_luminance")) for item in side_data
         if item.get("side_data_type") == "Mastering display metadata"),
        "",
    )
    return transfer, mdcv


def _encode(tmp_path: Path, src: Path, name: str, **video_kw) -> Path:
    out = tmp_path / f"{name}.mkv"
    video = VideoEncodeSettings(
        codec="libx265", quality_mode=QualityMode.CRF, crf=32, preset="ultrafast",
        force_10bit=True, source_path=src, **video_kw,
    )
    cfg = EncodeConfig(source=src, output=out, video=video, audio_tracks=[], copy_subtitles=False,
                       keep_chapters=False, duration_s=1.0, work_dir=tmp_path)
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", ram_buffer_enabled=False, ffmpeg_threads=1, generate_nfo=False)
    assert wf.validate(cfg) == []
    state = wait_task(wf.run(cfg), timeout=120.0)
    assert state["failed"] is None, state["failed"]
    return out


def test_unchecked_static_hdr_keeps_pq_without_source_mdcv(tmp_path: Path) -> None:
    """V09 / V10 : case HDR10 décochée → sortie PQ, sans le MDCV de la source."""
    src = tmp_path / "src.mkv"
    _make_hdr10_source(src)
    assert _video_hdr(src) == ("smpte2084", "10000000/10000")
    # Transfert source non renseigné : résolu par la préparation (payload ffprobe).
    out = _encode(tmp_path, src, "hdr10_off", inject_hdr_meta=False)
    assert _video_hdr(out) == ("smpte2084", "")


def test_edited_static_hdr_replaces_source_values(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    _make_hdr10_source(src)
    out = _encode(tmp_path, src, "hdr10_edited", inject_hdr_meta=True,
                  master_display=_EDITED_MD, max_cll="4000,800")
    assert _video_hdr(out) == ("smpte2084", "40000000/10000")
