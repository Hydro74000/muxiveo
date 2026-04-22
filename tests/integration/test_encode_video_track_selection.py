"""Intégration : sélection indépendante des pistes vidéo côté encode.

Ces tests génèrent de vrais samples MKV multi-vidéo minuscules avec ffmpeg,
lancent EncodeWorkflow, puis vérifient la sortie avec ffprobe. Ils couvrent le
cas critique où le panel encode choisit une piste vidéo qui n'est pas 0:v:0.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.workflows.encode import (
    EncodeConfig,
    EncodeWorkflow,
    QualityMode,
    VideoEncodeSettings,
)

from tests.integration._synth import (
    ffprobe_json,
    make_multi_video_mkv,
    streams_of_type,
    wait_task,
)


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis pour les tests d'intégration encode vidéo",
)


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    return qt_app


def _workflow() -> EncodeWorkflow:
    return EncodeWorkflow(
        ffmpeg_bin="ffmpeg",
        dovi_tool_bin="dovi_tool",
        hdr10plus_bin="hdr10plus_tool",
        mediainfo_bin="mediainfo",
        ram_buffer_enabled=False,
        ffmpeg_threads=1,
        generate_nfo=False,
    )


def _video_dims(path: Path) -> list[tuple[int, int]]:
    probe = ffprobe_json(path)
    return [
        (int(stream.get("width") or 0), int(stream.get("height") or 0))
        for stream in streams_of_type(probe, "video")
    ]


def _run_encode_copy(
    source: Path,
    output: Path,
    *,
    video_source: Path,
    stream_index: int,
) -> dict:
    cfg = EncodeConfig(
        source=source,
        output=output,
        video=VideoEncodeSettings(
            codec="copy",
            source_path=video_source,
            stream_index=stream_index,
            track_entry_id=f"video-{stream_index}",
        ),
        audio_tracks=[],
        copy_subtitles=False,
        keep_chapters=False,
        duration_s=1.0,
    )
    wf = _workflow()
    return wait_task(wf.run(cfg), timeout=60.0)


def test_encode_copy_keeps_selected_video_stream_not_first(tmp_path: Path) -> None:
    """Dans un MKV multi-vidéo, stream_index=1 doit produire la 2e vidéo."""
    src = tmp_path / "multi.mkv"
    make_multi_video_mkv(
        src,
        videos=[
            (64, 48, "red"),
            (96, 72, "blue"),
        ],
    )
    assert _video_dims(src) == [(64, 48), (96, 72)]

    out = tmp_path / "selected-second.mkv"
    state = _run_encode_copy(src, out, video_source=src, stream_index=1)

    assert state["failed"] is None, f"Encode failed: {state['failed']}"
    assert out.exists() and out.stat().st_size > 0
    assert _video_dims(out) == [(96, 72)]


def test_encode_copy_can_take_video_from_second_source(tmp_path: Path) -> None:
    """La piste vidéo encode peut venir d'une autre source que config.source."""
    primary = tmp_path / "primary.mkv"
    alt = tmp_path / "alt.mkv"
    make_multi_video_mkv(primary, videos=[(64, 48, "red")])
    make_multi_video_mkv(alt, videos=[(112, 80, "green")])

    out = tmp_path / "selected-alt-source.mkv"
    state = _run_encode_copy(primary, out, video_source=alt, stream_index=0)

    assert state["failed"] is None, f"Encode failed: {state['failed']}"
    assert out.exists() and out.stat().st_size > 0
    assert _video_dims(out) == [(112, 80)]


def test_encode_transcode_uses_selected_video_stream(tmp_path: Path) -> None:
    """Le réencodage vidéo mappe aussi la piste sélectionnée, pas 0:v:0."""
    src = tmp_path / "multi-transcode.mkv"
    make_multi_video_mkv(
        src,
        videos=[
            (64, 48, "red"),
            (128, 96, "blue"),
        ],
    )

    out = tmp_path / "transcoded-second.mkv"
    cfg = EncodeConfig(
        source=src,
        output=out,
        video=VideoEncodeSettings(
            codec="libx264",
            quality_mode=QualityMode.CRF,
            crf=35,
            preset="ultrafast",
            source_path=src,
            stream_index=1,
            track_entry_id="video-second",
        ),
        audio_tracks=[],
        copy_subtitles=False,
        keep_chapters=False,
        duration_s=1.0,
    )

    wf = _workflow()
    state = wait_task(wf.run(cfg), timeout=60.0)

    assert state["failed"] is None, f"Encode failed: {state['failed']}"
    assert out.exists() and out.stat().st_size > 0
    assert _video_dims(out) == [(128, 96)]
