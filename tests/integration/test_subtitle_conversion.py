"""Intégration : conversion automatique des codecs de sous-titres non
copyables vers MKV (cas 1 du routage subtitle_codec).

Scénarios :
  - MKV source avec srt → sortie MKV doit avoir subrip copié tel quel
  - MP4 source avec mov_text → sortie MKV doit contenir subrip (converti)
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.inspector import FileInspector
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, tracks_from_file_info

from tests.integration._synth import (
    ffprobe_json,
    make_mkv_with_srt,
    make_mp4_with_mov_text,
    streams_of_type,
    wait_task,
)


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis pour les tests d'intégration subtitle",
)


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    return qt_app


def _remux_all_tracks(src: Path, out: Path) -> dict:
    """Remuxe toutes les pistes (v/a/s) du src vers out.mkv et retourne l'état."""
    info = FileInspector().inspect(src)
    tracks = tracks_from_file_info(info, file_id="src-0")
    assert tracks, f"{src.name} : aucune piste détectée"

    wf = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    cfg = RemuxConfig(
        sources=[SourceInput(path=src, file_index=0, tracks=tracks)],
        output=out,
        track_order=[(0, t.mkv_tid) for t in tracks],
        keep_chapters=False,
    )
    return wait_task(wf.run(cfg), timeout=60.0)


def test_mkv_srt_copied_as_subrip(tmp_path: Path) -> None:
    """Le codec srt (subrip) passe en copy : le stream est préservé à
    l'identique côté Matroska."""
    src = tmp_path / "src.mkv"
    make_mkv_with_srt(src)

    out = tmp_path / "out.mkv"
    state = _remux_all_tracks(src, out)
    assert state["failed"] is None, f"Remux failed: {state['failed']}"
    assert out.exists()

    probe = ffprobe_json(out)
    subs = streams_of_type(probe, "subtitle")
    assert subs, "Piste sous-titre absente de la sortie"
    assert subs[0].get("codec_name") == "subrip"


def test_mp4_mov_text_converted_to_srt(tmp_path: Path) -> None:
    """mov_text (MP4/MOV) n'est pas copyable en MKV : le workflow doit
    automatiquement convertir vers subrip via ``-c:s:N srt``."""
    src = tmp_path / "src.mp4"
    make_mp4_with_mov_text(src)

    # Sanity : la source a bien du mov_text
    src_probe = ffprobe_json(src)
    src_subs = streams_of_type(src_probe, "subtitle")
    assert src_subs and src_subs[0].get("codec_name") == "mov_text"

    out = tmp_path / "out.mkv"
    state = _remux_all_tracks(src, out)
    assert state["failed"] is None, f"Remux failed: {state['failed']}"
    assert out.exists()

    probe = ffprobe_json(out)
    subs = streams_of_type(probe, "subtitle")
    assert subs, "Piste sous-titre absente après conversion mov_text → srt"
    assert subs[0].get("codec_name") == "subrip", (
        f"Codec attendu subrip, obtenu : {subs[0].get('codec_name')}"
    )
