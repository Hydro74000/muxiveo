"""Intégration : interpolation d'images RIFE (muxiveo-rife) dans EncodeWorkflow.

Génère des sources A/V synthétiques, dont 130 s avec DoVi/HDR10+, puis vérifie
cadence, décodage intégral, images animées et métadonnées. Nécessite le binaire
``muxiveo-rife`` (``MUXIVEO_RIFE_BIN`` ou PATH), ses modèles et un
périphérique Vulkan (llvmpipe accepté).
Cas longs : ``MUXIVEO_TEST_LONG_INTERPOLATION=1`` ; encodeurs matériels :
``MUXIVEO_TEST_HW=nvenc,nvencc``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from fractions import Fraction
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

from core.workflows.encode.interpolation import RIFE_HYBRID_MIN_VERSION, rife_version
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
_MASTER_DISPLAY = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"
_MAX_CLL = "1000,400"

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


@pytest.mark.skipif(
    not RIFE_BIN or (rife_version(RIFE_BIN) or (0, 0, 0)) < RIFE_HYBRID_MIN_VERSION,
    reason="moteur hybride : muxiveo-rife ≥ 1.3.0 requis",
)
def test_encode_interpolation_hybrid_preset(tmp_path: Path) -> None:
    """Préréglage Équilibré (défaut) : moteur hybride, cadence et nombre d'images exacts."""
    src = tmp_path / "src.mkv"
    make_av_container(src, duration=1.0)
    src_frames = _frame_count(src)
    out = tmp_path / "hybrid.mkv"
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
        duration_s=1.0,
    )
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", ram_buffer_enabled=False, ffmpeg_threads=1, generate_nfo=False,
                        rife_bin=RIFE_BIN)
    assert wf.validate(cfg) == []
    state = wait_task(wf.run(cfg), timeout=180.0)
    assert state["failed"] is None, f"Encode failed: {state['failed']}"
    assert _frame_count(out) == 2 * src_frames
    assert any("moteur hybrid" in str(line) for line in state["progress"])


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
            interpolation=FrameInterpolationSettings(enabled=True, factor=2, quality="fast"),
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


def _run_media_command(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def long_dynamic_hdr_source(tmp_path_factory) -> Path:
    """130 s d'images animées en HEVC 10 bits PQ, avec RPU et HDR10+ par image."""
    if not all(shutil.which(tool) for tool in ("dovi_tool", "hdr10plus_tool")):
        pytest.skip("dovi_tool et hdr10plus_tool requis")
    root = tmp_path_factory.mktemp("interpolation-long-hdr")
    frames = 3120
    base = root / "base.hevc"
    _run_media_command([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=256x144:r=24000/1001",
        "-frames:v", str(frames), "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", "ultrafast",
        "-x265-params", "log-level=none:pools=1:frame-threads=1:bframes=0:keyint=48:"
        "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
        f"master-display={_MASTER_DISPLAY}:max-cll={_MAX_CLL}",
        "-f", "hevc", str(base),
    ])
    config = root / "dovi.json"
    config.write_text(json.dumps({"cm_version": "V40", "profile": "8.1", "length": frames}), encoding="utf-8")
    rpu = root / "source.bin"
    _run_media_command(["dovi_tool", "generate", "-j", str(config), "-o", str(rpu)])
    dovi = root / "dovi.hevc"
    _run_media_command(["dovi_tool", "inject-rpu", "-i", str(base), "-r", str(rpu), "-o", str(dovi)])
    hdr_json = root / "hdr10plus.json"
    hdr_json.write_text(json.dumps({
        "JSONInfo": {"HDR10plusProfile": "B", "Version": "1.0"},
        "ToolInfo": {"Tool": "hdr10plus_tool", "Version": "1.7.1"},
        "SceneInfoSummary": {"SceneFirstFrameIndex": list(range(0, frames, 48)),
                             "SceneFrameNumbers": [48] * (frames // 48)},
        "SceneInfo": [{
            "BezierCurveData": {"Anchors": [256, 512, 768, 1023, 1023, 1023, 1023, 1023, 1023],
                                "KneePointX": 0, "KneePointY": 0},
            "LuminanceParameters": {
                "AverageRGB": 1000 + index % 100,
                "LuminanceDistributions": {
                    "DistributionIndex": [1, 5, 10, 25, 50, 75, 90, 95, 99],
                    "DistributionValues": [10, 50, 100, 500, 1000, 2000, 3000, 4000, 5000],
                },
                "MaxScl": [5000, 5000, 5000],
            },
            "NumberOfWindows": 1, "TargetedSystemDisplayMaximumLuminance": 400,
            "SceneFrameIndex": index % 48, "SceneId": index // 48, "SequenceFrameIndex": index,
        } for index in range(frames)],
    }), encoding="utf-8")
    combined = root / "combined.hevc"
    _run_media_command(["hdr10plus_tool", "inject", "-i", str(dovi), "-j", str(hdr_json), "-o", str(combined)])
    source = root / "source.mkv"
    _run_media_command([
        "ffmpeg", "-v", "error", "-y", "-r", "24000/1001", "-i", str(combined),
        "-f", "lavfi", "-i", "sine=f=440:r=48000:d=130.13", "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "flac", str(source),
    ])
    assert _frame_count(source) == frames
    return source


@pytest.mark.skipif(
    os.environ.get("MUXIVEO_TEST_LONG_INTERPOLATION") != "1",
    reason="130 s d'interpolation réelle : activer MUXIVEO_TEST_LONG_INTERPOLATION=1",
)
@pytest.mark.parametrize("mux_backend", ["ffmpeg", "native"])
@pytest.mark.parametrize(("codec", "quality"), [
    ("libx265", "fast"), ("hevc_nvenc", "balanced"), ("nvencc_hevc", "balanced"),
])
def test_interpolation_dynamic_hdr_decodes_past_two_minutes(
    tmp_path: Path, long_dynamic_hdr_source: Path, mux_backend: str, codec: str, quality: str,
) -> None:
    """La sortie reste décodable et animée après 1:50/2:00, avec métadonnées alignées."""
    hardware = set(os.environ.get("MUXIVEO_TEST_HW", "").split(","))
    family = "nvencc" if codec == "nvencc_hevc" else "nvenc"
    if codec != "libx265" and family not in hardware:
        pytest.skip(f"Activer MUXIVEO_TEST_HW={family} pour cet encodeur GPU")
    if codec == "nvencc_hevc" and not shutil.which("nvencc"):
        pytest.skip("NVEncC requis")
    if quality == "balanced" and (rife_version(RIFE_BIN) or (0, 0, 0)) < RIFE_HYBRID_MIN_VERSION:
        pytest.skip("moteur hybride : muxiveo-rife ≥ 1.3.0 requis")
    out = tmp_path / "long-hdr.mkv"
    cfg = EncodeConfig(
        source=long_dynamic_hdr_source, output=out,
        video=VideoEncodeSettings(
            codec=codec, quality_mode=QualityMode.CRF if codec == "libx265" else QualityMode.CQ,
            crf=28, cq=28, preset={"libx265": "ultrafast", "hevc_nvenc": "p1", "nvencc_hevc": "P1"}[codec],
            bit_depth="10", copy_dv=True, copy_hdr10plus=True, dovi_source_profile="p8_1",
            source_color_transfer="smpte2084",
            inject_hdr_meta=True, master_display=_MASTER_DISPLAY, max_cll=_MAX_CLL,
            interpolation=FrameInterpolationSettings(enabled=True, target_fps="60000/1001", quality=quality),
        ),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")],
        copy_subtitles=False, keep_chapters=False, duration_s=130.13,
        mux_backend=mux_backend, work_dir=tmp_path / "work",
    )
    wf = EncodeWorkflow(
        ffmpeg_bin="ffmpeg", nvencc_bin=shutil.which("nvencc"), rife_bin=RIFE_BIN,
        ram_buffer_enabled=False, ffmpeg_threads=1, generate_nfo=False,
    )
    logs: list[str] = []
    wf.log_message.connect(lambda level, message: logs.append(f"{level}: {message}"))
    assert wf.validate(cfg) == []
    signals = wf.run(cfg)
    state = wait_task(signals, timeout=240)
    if not any((state["finished"], state["failed"], state["cancelled"])):
        signals.cancel()
        wait_task(signals, timeout=10)
    (tmp_path / "workflow.log").write_text("\n".join(logs + state["progress"]), encoding="utf-8")
    assert state["failed"] is None, state["failed"]
    assert state["finished"] and not state["cancelled"], state
    probe = ffprobe_json(out)
    video = streams_of_type(probe, "video")[0]
    # Les timestamps Matroska à la milliseconde arrondissent légèrement la moyenne ffprobe.
    assert float(Fraction(video["avg_frame_rate"])) == pytest.approx(60000 / 1001, abs=0.001)
    assert video["pix_fmt"] == "yuv420p10le" and video["color_transfer"] == "smpte2084"
    assert any(s.get("dv_profile") == 8 for s in video.get("side_data_list", []))
    assert len(streams_of_type(probe, "audio")) == 1
    assert float(probe["format"]["duration"]) == pytest.approx(130.13, abs=0.1)
    first_frame = json.loads(_run_media_command([
        "ffprobe", "-v", "error", "-read_intervals", "%+#1", "-select_streams", "v:0",
        "-show_frames", "-of", "json", str(out),
    ]))["frames"][0]
    side_data_types = {side["side_data_type"] for side in first_frame.get("side_data_list", [])}
    assert {"Mastering display metadata", "Content light level metadata"} <= side_data_types

    # Décodage complet : un simple compte des paquets ne détecte pas une vidéo figée/corrompue.
    decoded = _run_media_command([
        "ffmpeg", "-v", "error", "-xerror", "-err_detect", "explode", "-i", str(out),
        "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "framemd5", "-",
    ])
    rows = [line.split(",") for line in decoded.splitlines() if line and not line.startswith("#")]
    expected_frames = 7800  # 3120 × 2,5 : 23,976 → 59,94 i/s.
    assert len(rows) == expected_frames
    pts = [int(row[2]) for row in rows]
    assert all(right - left == 1 for left, right in zip(pts, pts[1:]))
    # Chaque seconde après 1:50 doit encore contenir plusieurs images différentes.
    for start in range(6600, expected_frames, 60):
        assert len({row[-1].strip() for row in rows[start:start + 60]}) > 20, start

    rpu = tmp_path / "output.bin"
    _run_media_command(["dovi_tool", "extract-rpu", "-i", str(out), "-o", str(rpu)])
    hdr = tmp_path / "output-hdr10plus.json"
    _run_media_command(["hdr10plus_tool", "extract", "-i", str(out), "-o", str(hdr)])
    from core.workflows.encode.runtime.frame_count_guard import FrameCountGuard

    audit = FrameCountGuard().audit(
        source=long_dynamic_hdr_source, encoded=out, rpu_bin=rpu, hdr10p_json=hdr, frame_ratio=Fraction(5, 2),
    )
    assert audit.source == audit.encoded == audit.rpu == audit.hdr10p == expected_frames, audit
    scenes = json.loads(hdr.read_text(encoding="utf-8"))["SceneInfo"]
    assert [scene["LuminanceParameters"]["AverageRGB"] for scene in scenes] == [
        1000 + (index * 2 // 5) % 100 for index in range(expected_frames)
    ]
