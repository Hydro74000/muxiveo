"""Intégration : interpolation d'images RIFE (muxiveo-rife) dans EncodeWorkflow.

Génère une source A/V synthétique, encode avec interpolation x2 puis vérifie
cadence, nombre de trames et présence de l'audio. Nécessite le binaire
``muxiveo-rife`` (``MUXIVEO_RIFE_BIN`` ou PATH), ses modèles et un
périphérique Vulkan (llvmpipe accepté).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.workflows.common import TrackTimeOffset
from core.workflows.encode import (
    AudioTrackSettings,
    EncodeConfig,
    EncodeWorkflow,
    FrameInterpolationSettings,
    QualityMode,
    VideoEncodeSettings,
)

from tests.integration._synth import ffprobe_json, make_av_container, streams_of_type, wait_task


def _rife_bin() -> str | None:
    candidate = os.environ.get("MUXIVEO_RIFE_BIN") or shutil.which("muxiveo-rife")
    if not candidate or not Path(candidate).is_file():
        return None
    try:
        probe = subprocess.run([candidate, "--list-gpus"], capture_output=True, text=True, timeout=60, check=False)
        gpus = json.loads(probe.stdout or "{}").get("gpus") or []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return candidate if gpus else None


RIFE_BIN = _rife_bin()

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None or RIFE_BIN is None,
    reason="ffmpeg/ffprobe, muxiveo-rife et un périphérique Vulkan requis",
)


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    return qt_app


def _frame_count(path: Path) -> int:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
            "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


@pytest.mark.parametrize("mux_backend", ["ffmpeg", "native"])
def test_encode_interpolation_doubles_frame_rate(tmp_path: Path, mux_backend: str) -> None:
    src = tmp_path / "src.mkv"
    make_av_container(src, duration=1.0)
    src_frames = _frame_count(src)

    out = tmp_path / f"out-{mux_backend}.mkv"
    cfg = EncodeConfig(
        source=src,
        output=out,
        video=VideoEncodeSettings(
            codec="libx264",
            quality_mode=QualityMode.CRF,
            crf=30,
            preset="ultrafast",
            interpolation=FrameInterpolationSettings(enabled=True, factor=2, quality="fast"),
        ),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")],
        copy_subtitles=False,
        keep_chapters=False,
        duration_s=1.0,
        mux_backend=mux_backend,
    )
    wf = EncodeWorkflow(
        ffmpeg_bin="ffmpeg",
        ram_buffer_enabled=False,
        ffmpeg_threads=1,
        generate_nfo=False,
        rife_bin=RIFE_BIN,
    )
    assert wf.validate(cfg) == []
    state = wait_task(wf.run(cfg), timeout=180.0)

    assert state["failed"] is None, f"Encode failed: {state['failed']}"
    probe = ffprobe_json(out)
    video = streams_of_type(probe, "video")
    assert len(video) == 1
    assert video[0].get("avg_frame_rate") in {"50/1", "50"}
    assert _frame_count(out) == 2 * src_frames
    assert len(streams_of_type(probe, "audio")) == 1
    assert abs(float(probe["format"]["duration"]) - 1.0) < 0.15
    assert any("muxiveo-rife" in str(line) for line in state["progress"])


@pytest.mark.parametrize("mux_backend", ["ffmpeg", "native"])
def test_encode_interpolation_keeps_video_delay(tmp_path: Path, mux_backend: str) -> None:
    """Un retard vidéo (+400 ms) survit à l'encode interpolé et à l'assemblage final."""
    src = tmp_path / "src.mkv"
    make_av_container(src, duration=2.0)
    out = tmp_path / f"delay-{mux_backend}.mkv"
    cfg = EncodeConfig(
        source=src,
        output=out,
        video=VideoEncodeSettings(
            codec="libx264", quality_mode=QualityMode.CRF, crf=30, preset="ultrafast",
            interpolation=FrameInterpolationSettings(enabled=True, factor=2),
        ),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")],
        copy_subtitles=False,
        keep_chapters=False,
        duration_s=2.0,
        mux_backend=mux_backend,
        track_time_offsets=[TrackTimeOffset(track_type="video", source_path=src, stream_index=0, offset_ms=400)],
    )
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", ram_buffer_enabled=False, ffmpeg_threads=1, generate_nfo=False,
                        rife_bin=RIFE_BIN)
    assert wf.validate(cfg) == []
    state = wait_task(wf.run(cfg), timeout=180.0)
    assert state["failed"] is None, f"Encode failed: {state['failed']}"
    starts = {s["codec_type"]: float(s.get("start_time") or 0.0) for s in ffprobe_json(out)["streams"]}
    assert abs((starts["video"] - starts["audio"]) - 0.4) < 0.05
