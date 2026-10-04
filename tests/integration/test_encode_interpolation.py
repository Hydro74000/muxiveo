"""Intégration : interpolation RIFE/MVTools avec FFmpeg et NVEncC.

Génère une source A/V synthétique, encode avec interpolation x2 puis vérifie
cadence, nombre de trames, audio et offsets. Chaque moteur/encodeur disponible
est testé indépendamment ; RIFE nécessite Vulkan et NVEncC un GPU NVENC.
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
from core.workflows.encode.runtime.nvencc import detect_nvencc_available

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
    return str(Path(candidate).resolve()) if gpus else None


RIFE_BIN = _rife_bin()

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis",
)


@pytest.fixture(params=["rife", "mvtools-standard", "mvtools-uhd"])
def interpolation_engine(request):
    """Chaque moteur est contrôlé indépendamment, sans repli."""
    if request.param == "rife":
        if not RIFE_BIN:
            pytest.skip("muxiveo-rife/Vulkan requis")
        return {}, {"rife_bin": RIFE_BIN}, "muxiveo-rife"
    binary = os.environ.get("MUXIVEO_MVTOOLS_BIN") or shutil.which("muxiveo-mvtools")
    if not binary:
        pytest.skip("MUXIVEO_MVTOOLS_BIN requis")
    mode = request.param.split("-", 1)[1]
    return {"backend": "mvtools", "mvtools_mode": mode}, {"mvtools_bin": str(Path(binary).resolve())}, "muxiveo-mvtools"


@pytest.fixture(autouse=True)
def _qt_app(qt_app):
    return qt_app


@pytest.fixture(params=["libx264", "nvencc_hevc"])
def encode_backend(request):
    if request.param == "libx264":
        return "libx264", "ultrafast", {}
    binary = shutil.which("nvencc") or shutil.which("NVEncC")
    if not binary or not detect_nvencc_available(binary)[0]:
        pytest.skip("NVEncC et GPU NVENC requis")
    return "nvencc_hevc", "performance", {"nvencc_bin": binary}


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
def test_encode_interpolation_doubles_frame_rate(tmp_path: Path, mux_backend: str, interpolation_engine, encode_backend) -> None:
    src = tmp_path / "src.mkv"
    make_av_container(src, duration=1.0)
    src_frames = _frame_count(src)

    out = tmp_path / f"out-{mux_backend}.mkv"
    cfg = EncodeConfig(
        source=src,
        output=out,
        video=VideoEncodeSettings(
            codec=encode_backend[0],
            quality_mode=QualityMode.CRF,
            crf=30,
            preset=encode_backend[1],
            interpolation=FrameInterpolationSettings(enabled=True, factor=2, quality="fast", **interpolation_engine[0]),
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
        **interpolation_engine[1],
        **encode_backend[2],
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
    assert any(interpolation_engine[2] in str(line) for line in state["progress"])


@pytest.mark.parametrize("mux_backend", ["ffmpeg", "native"])
def test_encode_interpolation_keeps_video_delay(tmp_path: Path, mux_backend: str, interpolation_engine, encode_backend) -> None:
    """Un retard vidéo (+400 ms) survit à l'encode interpolé et à l'assemblage final."""
    src = tmp_path / "src.mkv"
    make_av_container(src, duration=2.0)
    out = tmp_path / f"delay-{mux_backend}.mkv"
    cfg = EncodeConfig(
        source=src,
        output=out,
        video=VideoEncodeSettings(
            codec=encode_backend[0], quality_mode=QualityMode.CRF, crf=30, preset=encode_backend[1],
            interpolation=FrameInterpolationSettings(enabled=True, factor=2, **interpolation_engine[0]),
        ),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")],
        copy_subtitles=False,
        keep_chapters=False,
        duration_s=2.0,
        mux_backend=mux_backend,
        track_time_offsets=[TrackTimeOffset(track_type="video", source_path=src, stream_index=0, offset_ms=400)],
    )
    wf = EncodeWorkflow(
        ffmpeg_bin="ffmpeg", ram_buffer_enabled=False, ffmpeg_threads=1, generate_nfo=False,
        **interpolation_engine[1], **encode_backend[2],
    )
    assert wf.validate(cfg) == []
    state = wait_task(wf.run(cfg), timeout=180.0)
    assert state["failed"] is None, f"Encode failed: {state['failed']}"
    starts = {s["codec_type"]: float(s.get("start_time") or 0.0) for s in ffprobe_json(out)["streams"]}
    assert abs((starts["video"] - starts["audio"]) - 0.4) < 0.05
