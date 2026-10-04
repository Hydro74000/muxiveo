"""Encodages GPU réels, activés explicitement par MUXIVEO_TEST_HW.

Exemple : MUXIVEO_TEST_HW=nvenc,nvencc,vaapi QT_QPA_PLATFORM=offscreen
python -m pytest tests/integration/test_linux_hardware_encode.py -v
Un backend demandé mais indisponible échoue : il n'est pas ignoré silencieusement.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast
import shutil
import subprocess
import sys
import time

import pytest

from core.workflows.encode import EncodeConfig, EncodeWorkflow, QualityMode, VideoEncodeSettings
from core.workflows.common import TrackTimeOffset
from core.workflows.encode.models import FrameInterpolationSettings
from core.workflows.encode.hardware import HardwareEncoderDetector
from core.workflows.encode.hw_devices import select_linux_hwaccel_device
from core.workflows.encode.models import AudioTrackSettings, VideoResizeSettings
from tests.integration._synth import _run_ffmpeg, ffprobe_json, wait_task


FAMILIES = {
    "nvenc": ("h264_nvenc", "hevc_nvenc", "av1_nvenc"),
    "nvencc": ("nvencc_h264", "nvencc_hevc", "nvencc_av1"),
    "vaapi": ("h264_vaapi", "hevc_vaapi"),
}
REQUESTED = set(filter(None, os.environ.get("MUXIVEO_TEST_HW", "").split(",")))
CODECS = [codec for family, codecs in FAMILIES.items() if family in REQUESTED for codec in codecs]
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not REQUESTED,
    reason="Encodages GPU Linux : activer MUXIVEO_TEST_HW=nvenc,nvencc,vaapi",
)


def hardware_preset(codec):
    if codec.endswith("_vaapi"):
        return "0"
    return "default" if codec.startswith("nvencc") else "p4"


@pytest.mark.skipif("nvencc" not in REQUESTED, reason="Activer MUXIVEO_TEST_HW=nvencc")
@pytest.mark.parametrize("mux_backend", ["ffmpeg", "native"])
@pytest.mark.parametrize("interpolate", [False, True])
@pytest.mark.parametrize("offset_ms", [0, 200])
def test_nvencc_keeps_source_delay_and_p3(tmp_path, hardware, qt_app, mux_backend, interpolate, offset_ms):
    """Le départ source s'ajoute au retard demandé ; le pipe conserve le marquage P3."""
    ffmpeg, nvencc, available = hardware
    assert "nvencc_h264" in available
    rife = os.environ.get("MUXIVEO_RIFE_BIN") or shutil.which("muxiveo-rife")
    if interpolate and not rife:
        pytest.skip("muxiveo-rife requis pour le cas interpolé")
    src = tmp_path / "source.mkv"
    subprocess.run([
        ffmpeg, "-v", "error", "-y",
        "-itsoffset", "0.4", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2.4",
        "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
        "-vf", "setparams=color_primaries=smpte432:color_trc=iec61966-2-1:colorspace=bt709",
        "-c:a", "flac", "-fps_mode", "passthrough", str(src),
    ], check=True)
    original = {s["codec_type"]: s for s in ffprobe_json(src)["streams"]}
    assert float(original["video"]["start_time"]) - float(original["audio"]["start_time"]) == pytest.approx(0.4)
    assert original["video"]["color_primaries"] == "smpte432"
    config = EncodeConfig(
        source=src, output=tmp_path / "result.mkv",
        video=VideoEncodeSettings(
            codec="nvencc_h264", preset="P1",
            interpolation=FrameInterpolationSettings(enabled=interpolate, factor=2),
        ),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")],
        copy_subtitles=False, keep_chapters=False, duration_s=2.4,
        mux_backend=mux_backend, work_dir=tmp_path,
        track_time_offsets=[TrackTimeOffset(track_type="video", source_path=src, stream_index=0, offset_ms=offset_ms)],
    )
    workflow = EncodeWorkflow(
        ffmpeg_bin=ffmpeg, nvencc_bin=nvencc, rife_bin=rife,
        ffmpeg_threads=1, ram_buffer_enabled=False, generate_nfo=False,
    )
    assert workflow.validate(config) == []
    state = wait_task(workflow.run(config), timeout=90)
    (tmp_path / "workflow.log").write_text("\n".join(map(str, state["progress"])) + "\n" + str(state["failed"]), encoding="utf-8")
    assert state["failed"] is None, state["failed"]
    assert state["finished"] and not state["cancelled"], state
    streams = {s["codec_type"]: s for s in ffprobe_json(config.output)["streams"]}
    assert float(streams["video"]["start_time"]) - float(streams["audio"]["start_time"]) == pytest.approx(
        0.4 + offset_ms / 1000, abs=0.025,
    )
    assert streams["video"]["color_primaries"] == "smpte432"
    assert streams["video"]["color_transfer"] == "iec61966-2-1"
    assert streams["video"]["color_space"] == "bt709"


@pytest.fixture(scope="module")
def hardware():
    assert not REQUESTED.difference(FAMILIES), f"Familles inconnues : {REQUESTED.difference(FAMILIES)}"
    nvencc = shutil.which("nvencc") or shutil.which("NVEncC")
    available, ffmpeg = HardwareEncoderDetector().detect("ffmpeg", nvencc_bin=nvencc)
    return ffmpeg, nvencc, available


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    root = tmp_path_factory.mktemp("linux_hw_source")
    path = root / "Été source.mkv"
    subs = root / "fr.srt"
    subs.write_text("1\n00:00:00,000 --> 00:00:02,500\nEssai GPU été\n", encoding="utf-8")
    attachment = root / "audit.txt"
    attachment.write_bytes(b"muxiveo hardware attachment\n")
    _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-i", str(subs),
        "-t", "3", "-map", "0:v", "-map", "1:a", "-map", "2:s",
        "-c:v", "libx264", "-preset", "ultrafast", "-threads", "2",
        "-c:a", "aac", "-c:s", "srt", "-metadata:s:a:0", "language=fra",
        "-metadata:s:s:0", "language=fra", "-attach", str(attachment),
        "-metadata:s:t:0", "mimetype=text/plain", str(path),
    ])
    return path


def execute_case(source, tmp_path, hardware, video, backend, *, expected_size=(640, 360), ten_bit=False):
    ffmpeg, nvencc, available = hardware
    assert video.codec in available, f"Encodeur indisponible : {video.codec}"
    if video.codec.endswith("_vaapi"):
        assert select_linux_hwaccel_device(video.codec) is not None
    output = tmp_path / "Été résultat.mkv"
    cfg = EncodeConfig(
        source=source, output=output, video=video,
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="aac", source_path=source, bitrate_kbps=96)],
        copy_subtitles=False, subtitle_tracks=[(source, 2)], attachment_streams=[(source, 3)],
        keep_chapters=False, duration_s=3, mux_backend=backend,
        file_title="Audit GPU été", work_dir=tmp_path / "work",
    )
    workflow = EncodeWorkflow(
        ffmpeg_bin=ffmpeg, nvencc_bin=nvencc, ffmpeg_threads=2,
        ram_buffer_enabled=False, generate_nfo=False,
    )
    logs = []
    workflow.log_message.connect(lambda level, message: logs.append(f"{level}: {message}"))
    signals = workflow.run(cfg, validate=False)
    state = wait_task(signals, timeout=90)
    if not any((state["finished"], state["failed"], state["cancelled"])):
        signals.cancel()
        wait_task(signals, timeout=10)
    (tmp_path / "workflow.log").write_text("\n".join(logs + state["progress"]), encoding="utf-8")
    assert state["failed"] is None, state["failed"]
    assert state["finished"] is not None and not state["cancelled"], state
    assert output.is_file() and output.stat().st_size > 0

    probe = ffprobe_json(output)
    (tmp_path / "probe.json").write_text(json.dumps(probe, indent=2), encoding="utf-8")
    streams = probe["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    expected_codec = next(c for c in ("h264", "hevc", "av1") if c in video.codec)
    assert v["codec_name"] == expected_codec
    assert (v["width"], v["height"]) == expected_size
    assert v["pix_fmt"] == ("yuv420p10le" if ten_bit else "yuv420p")
    assert int(v["tags"]["NUMBER_OF_FRAMES"]) == 75
    assert sum(s["codec_type"] == "audio" for s in streams) == 1
    assert sum(s["codec_type"] == "subtitle" for s in streams) == 1
    assert sum(s["codec_type"] == "attachment" for s in streams) == 1
    tags = {key.casefold(): value for key, value in probe["format"]["tags"].items()}
    assert tags["title"] == "Audit GPU été"

    decoded = subprocess.run([
        ffmpeg, "-v", "error", "-xerror", "-i", str(output), "-map", "0:v:0", "-map", "0:a:0",
        "-progress", "pipe:1", "-f", "null", "-",
    ], capture_output=True, text=True, timeout=30)
    (tmp_path / "decode.log").write_text(decoded.stdout + decoded.stderr, encoding="utf-8")
    assert decoded.returncode == 0, decoded.stderr
    frames = [int(line.split("=", 1)[1]) for line in decoded.stdout.splitlines() if line.startswith("frame=")]
    assert frames and frames[-1] == 75, decoded.stdout
    extracted = tmp_path / "attachment.txt"
    dump = subprocess.run([
        ffmpeg, "-v", "error", "-dump_attachment:t:0", str(extracted), "-i", str(output),
        "-map", "0:v:0", "-c", "copy", "-t", "0", "-f", "null", "-",
    ], capture_output=True, text=True, timeout=30)
    assert dump.returncode == 0, dump.stderr
    assert extracted.read_bytes() == b"muxiveo hardware attachment\n"


@pytest.mark.parametrize("codec", CODECS)
@pytest.mark.parametrize("mode", ["cq", "bitrate", "resize"])
def test_gpu_workflow_sdr(qt_app, source, tmp_path, hardware, codec, mode):
    video = VideoEncodeSettings(
        codec=codec, source_path=source, quality_mode=QualityMode.BITRATE if mode == "bitrate" else QualityMode.CQ,
        cq=26, bitrate_kbps=1200, preset=hardware_preset(codec),
        resize=VideoResizeSettings(enabled=mode == "resize", mode="size", width=320, height=180),
    )
    execute_case(source, tmp_path, hardware, video, "ffmpeg", expected_size=(320, 180) if mode == "resize" else (640, 360))


@pytest.mark.parametrize("codec", [c for c in CODECS if "hevc" in c])
@pytest.mark.parametrize("backend", ["ffmpeg", "native"])
def test_gpu_workflow_hevc_10bit(qt_app, source, tmp_path, hardware, codec, backend):
    video = VideoEncodeSettings(
        codec=codec, source_path=source, quality_mode=QualityMode.CQ, cq=26, force_10bit=True,
        preset=hardware_preset(codec),
    )
    execute_case(source, tmp_path, hardware, video, backend, ten_bit=True)


@pytest.mark.parametrize("codec", [c for c in CODECS if c.startswith("nvencc")])
def test_gpu_workflow_nvencc_filter_pipe(qt_app, source, tmp_path, hardware, codec):
    # Le resize en pourcentage force le pipe FFmpeg, contrairement au resize VPP.
    video = VideoEncodeSettings(
        codec=codec, source_path=source, quality_mode=QualityMode.CQ, cq=26,
        preset=hardware_preset(codec),
        resize=VideoResizeSettings(enabled=True, mode="percent", percent=50),
    )
    execute_case(source, tmp_path, hardware, video, "ffmpeg", expected_size=(320, 180))


@pytest.mark.parametrize("codec", [c for c in CODECS if "hevc" in c])
@pytest.mark.parametrize("backend", ["ffmpeg", "native", "auto"])
def test_gpu_workflow_dynamic_hdr(qt_app, tmp_path, hardware, codec, backend):
    from core.workflows.encode.runtime.frame_count_guard import FrameCountGuard

    raw_source = os.environ.get("MUXIVEO_TEST_HDR_SOURCE")
    if not raw_source:
        pytest.skip("MUXIVEO_TEST_HDR_SOURCE doit désigner le corpus DoVi + HDR10+")
    source = Path(raw_source).resolve()
    assert source.is_file()
    ffmpeg, nvencc, available = hardware
    assert codec in available, f"Encodeur indisponible : {codec}"
    output = tmp_path / "hdr.mkv"
    video = VideoEncodeSettings(
        codec=codec, source_path=source, quality_mode=QualityMode.CQ, cq=26,
        force_10bit=True, preset=hardware_preset(codec), copy_dv=True, copy_hdr10plus=True,
    )
    cfg = EncodeConfig(
        source=source, output=output, video=video, copy_subtitles=False,
        keep_chapters=False, mux_backend=backend, work_dir=tmp_path / "work",
    )
    workflow = EncodeWorkflow(
        ffmpeg_bin=ffmpeg, nvencc_bin=nvencc, ffmpeg_threads=2,
        ram_buffer_enabled=False, generate_nfo=False,
    )
    logs = []
    workflow.log_message.connect(lambda level, message: logs.append(f"{level}: {message}"))
    signals = workflow.run(cfg, validate=False)
    state = wait_task(signals, timeout=120)
    if not any((state["finished"], state["failed"], state["cancelled"])):
        signals.cancel()
        wait_task(signals, timeout=10)
    (tmp_path / "workflow.log").write_text("\n".join(logs + state["progress"]), encoding="utf-8")
    if codec.startswith("nvencc") and backend == "native":
        # Contrat actuel : cet intermédiaire HDR n'est pas éligible au mux natif.
        assert state["failed"] is not None, state
        assert "HDR dynamique NVEncC" in str(state["failed"])
        assert state["finished"] is None and not state["cancelled"], state
        assert not output.exists()
        return
    assert state["failed"] is None, state["failed"]
    assert state["finished"] is not None and not state["cancelled"], state
    probe = ffprobe_json(output)
    (tmp_path / "probe.json").write_text(json.dumps(probe, indent=2), encoding="utf-8")
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert v["codec_name"] == "hevc" and v["pix_fmt"] == "yuv420p10le"
    assert v["color_transfer"] == "smpte2084" and v["color_primaries"] == "bt2020"
    assert any(s.get("dv_profile") == 8 for s in v.get("side_data_list", []))
    hevc, rpu, hdr = (tmp_path / name for name in ("video.hevc", "rpu.bin", "hdr10plus.json"))
    commands = [
        [ffmpeg, "-v", "error", "-i", str(output), "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb", str(hevc)],
        ["dovi_tool", "extract-rpu", "-i", str(hevc), "-o", str(rpu)],
        ["hdr10plus_tool", "extract", "-i", str(hevc), "-o", str(hdr)],
        [ffmpeg, "-v", "error", "-xerror", "-i", str(output), "-map", "0:v:0", "-f", "null", "-"],
    ]
    for index, cmd in enumerate(commands):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        (tmp_path / f"verify-{index}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stderr
    guard = FrameCountGuard()
    audit = guard.audit(source=source, encoded=output, rpu_bin=rpu, hdr10p_json=hdr)
    (tmp_path / "counts.txt").write_text(repr(audit), encoding="utf-8")
    assert audit.source is not None and audit.source > 0
    assert audit.source == audit.encoded == audit.rpu == audit.hdr10p, audit


@pytest.mark.parametrize("codec", [c for c in CODECS if "hevc" in c])
def test_gpu_cancel_active_encoder(qt_app, source, tmp_path, hardware, codec):
    ffmpeg, nvencc, available = hardware
    assert codec in available, f"Encodeur indisponible : {codec}"
    output = tmp_path / "cancelled.mkv"
    video = VideoEncodeSettings(
        codec=codec, source_path=source, quality_mode=QualityMode.CQ, cq=26,
        preset=hardware_preset(codec),
    )
    cfg = EncodeConfig(
        source=source, output=output, video=video, copy_subtitles=False,
        keep_chapters=False, work_dir=tmp_path / "work",
    )
    workflow = EncodeWorkflow(
        ffmpeg_bin=ffmpeg, nvencc_bin=nvencc, ffmpeg_threads=2,
        ram_buffer_enabled=False, generate_nfo=False,
    )
    signals = workflow.run(cfg)
    active = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        qt_app.processEvents()
        with signals._procs_lock:
            procs = list(signals._active_procs)
        active = [p for p in procs if p.poll() is None and (
            codec in cast(list[str], p.args)
            or (codec.startswith("nvencc") and "nvencc" in Path(cast(list[str], p.args)[0]).name.lower())
        )]
        if active:
            break
        time.sleep(0.001)
    signals.cancel()
    state = wait_task(signals, timeout=15)
    (tmp_path / "cancel.json").write_text(json.dumps({
        "pids": [p.pid for p in active], "commands": [p.args for p in active],
        "state": state,
    }, default=str, indent=2), encoding="utf-8")
    assert active, "Aucun processus d'encodage actif observé : annulation non exercée"
    assert state["cancelled"] and state["failed"] is None and state["finished"] is None, state
    for proc in active:
        proc.wait(timeout=5)
    assert not output.exists()
    assert not list(tmp_path.glob("*.partial"))
