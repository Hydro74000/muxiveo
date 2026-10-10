"""Bout en bout FEL sur le plugin compilé, activé par des chemins de test explicites."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from core.fel.engine import FelEngine, FelSource
from core.matroska.hevc.payload_rewriter import MatroskaHevcPayloadRewriter
from core.matroska.hevc.access_units import iter_hevc_nal_units
from core.workflows.encode import EncodeConfig, EncodeWorkflow, FrameInterpolationSettings, QualityMode, VideoEncodeSettings
from tests.integration._synth import wait_task

_LIBRARY = os.environ.get("MUXIVEO_TEST_FEL_LIBRARY", "")
_FIXTURE = os.environ.get("MUXIVEO_TEST_FEL_FIXTURE", "")
pytestmark = pytest.mark.skipif(
    not (_LIBRARY and _FIXTURE and all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "dovi_tool"))),
    reason="Plugin et séquence synthétique FEL requis (MUXIVEO_TEST_FEL_LIBRARY/FIXTURE)",
)


def _mkv_fixture(tmp_path: Path, *, variable: bool = False) -> Path:
    """Squelette horodaté avec le même GOP ; remplacer ses AU par la séquence FEL."""
    template = tmp_path / "timing.mkv"
    timing = ["-vf", "setpts=PTS+if(gte(N\\,6)\\,3\\,0)", "-fps_mode", "passthrough"] if variable else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=gray:s=256x144:r=24000/1001", *timing,
        "-frames:v", "12", "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-x265-params",
        "log-level=0:pools=1:frame-threads=1:keyint=12:min-keyint=12:scenecut=0:bframes=3:chromaloc=2",
        str(template)], check=True, capture_output=True)
    source = tmp_path / "source.mkv"
    MatroskaHevcPayloadRewriter().rewrite(encoded_mkv=template, injected_hevc=Path(_FIXTURE), output=source)
    return source


@pytest.mark.parametrize("mode", ["hdr10", "dv", "sdr", "two_pass", "ffmpeg_nvenc", "nvencc", "rife"])
def test_fel_software_workflow(qt_app, tmp_path, monkeypatch, mode):
    engine = FelEngine(Path(_LIBRARY))
    monkeypatch.setattr(FelEngine, "installed", classmethod(lambda cls: engine))
    source = Path(_FIXTURE)
    if mode == "rife":
        source = _mkv_fixture(tmp_path)
    output = tmp_path / f"fel_{mode}.mkv"
    if mode == "nvencc" and not shutil.which("nvencc"):
        pytest.skip("NVEncC requis")
    if mode == "rife" and not shutil.which("muxiveo-rife"):
        pytest.skip("RIFE requis")
    video = VideoEncodeSettings(
        codec={"ffmpeg_nvenc": "hevc_nvenc", "nvencc": "nvencc_hevc"}.get(mode, "libx265"),
        quality_mode=QualityMode.SIZE if mode == "two_pass" else QualityMode.CRF,
        crf=24, preset="p1" if mode == "ffmpeg_nvenc" else "default" if mode == "nvencc" else "ultrafast",
        target_size_mb=1,
        bake_dovi_fel=True, dovi_source_profile="p7_fel", copy_dv=mode in {"dv", "nvencc", "rife"},
        source_path=source, source_bit_depth=10, force_10bit=mode != "sdr",
        bit_depth="8" if mode == "sdr" else "10",
        input_frame_rate="24000/1001", source_color_transfer="smpte2084",
        tonemap_to_sdr=mode == "sdr",
        interpolation=FrameInterpolationSettings(enabled=mode == "rife", factor=2, quality="fast"),
    )
    config = EncodeConfig(source=source, output=output, video=video, audio_tracks=[],
        copy_subtitles=False, keep_chapters=False, duration_s=12*1001/24000, work_dir=tmp_path)
    workflow = EncodeWorkflow(ffmpeg_threads=2, ram_buffer_enabled=False, generate_nfo=False,
        nvencc_bin=shutil.which("nvencc"), rife_bin=shutil.which("muxiveo-rife"))
    messages: list[str] = []
    workflow.log_message.connect(lambda _level, message: messages.append(message))
    state = wait_task(workflow.run(config), timeout=120)
    assert state["failed"] is None, state["failed"]
    assert any("FEL confirmé" in message for message in messages), messages
    assert any("reconstruction FEL réussie" in message for message in messages), messages
    probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_streams",
        "-select_streams", "v:0", "-of", "json", str(output)], check=True, text=True, capture_output=True)
    stream = json.loads(probe.stdout)["streams"][0]
    assert int(stream["nb_read_frames"]) == (24 if mode == "rife" else 12)
    assert stream["color_transfer"] == ("bt709" if mode == "sdr" else "smpte2084")
    assert stream["pix_fmt"] == ("yuv420p" if mode == "sdr" else "yuv420p10le")
    record = next((s for s in stream.get("side_data_list", [])
        if s.get("side_data_type") == "DOVI configuration record"), None)
    if mode in {"dv", "nvencc", "rife"}:
        assert record is not None
        assert record["dv_profile"] == 8 and record["dv_bl_signal_compatibility_id"] == 1
        assert record["rpu_present_flag"] == 1 and record["el_present_flag"] == 0
    else:
        assert record is None


def test_fel_presentation_timestamps_on_selected_track(qt_app, tmp_path):
    """Deux pistes, B-frames et départ non nul : comparer chaque PTS décodé."""
    source = tmp_path / "two_tracks.mkv"
    fel_mkv = _mkv_fixture(tmp_path)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=64x64:r=24",
        "-itsoffset", "1.25", "-i", str(fel_mkv),
        "-map", "0:v", "-map", "1:v", "-c:v:0", "ffv1", "-c:v:1", "copy", "-t", "2", str(source)],
        check=True, capture_output=True)
    probe_args = ["ffprobe", "-v", "error", "-show_entries", "frame=pts_time", "-of", "json"]
    expected = subprocess.run([*probe_args, "-select_streams", "v:1", str(source)],
        check=True, capture_output=True, text=True)
    with subprocess.Popen([*probe_args, "-i", "pipe:0"], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE) as consumer:
        producer = FelSource(FelEngine(Path(_LIBRARY)), source, 1, 2, "24000/1001").start(
            consumer.stdin, threading.Event())
        assert consumer.stdout is not None and consumer.stderr is not None
        output = consumer.stdout.read()
        error = consumer.stderr.read()
        consumer.wait(timeout=30)
        producer.finish()
        producer.check_error()
        assert consumer.returncode == 0, error
    original = [f["pts_time"] for f in json.loads(expected.stdout)["frames"]]
    reconstructed = [f["pts_time"] for f in json.loads(output)["frames"]]
    assert len(original) == 12 and original == reconstructed
    assert float(original[0]) >= 1.25


@pytest.mark.parametrize("codec", ["libx265", "nvencc_hevc"])
def test_fel_variable_timestamps_survive_encoding(qt_app, tmp_path, monkeypatch, codec):
    """Une pause en milieu de séquence reste présente, sans dupliquer de trame/RPU."""
    if codec == "nvencc_hevc" and not shutil.which("nvencc"):
        pytest.skip("NVEncC requis")
    engine = FelEngine(Path(_LIBRARY))
    monkeypatch.setattr(FelEngine, "installed", classmethod(lambda cls: engine))
    source = _mkv_fixture(tmp_path, variable=True)
    output = tmp_path / "variable.mkv"
    video = VideoEncodeSettings(codec=codec, preset="default" if codec == "nvencc_hevc" else "ultrafast",
        source_path=source, bake_dovi_fel=True, dovi_source_profile="p7_fel", copy_dv=True,
        bit_depth="10", input_frame_rate="24000/1001", source_color_transfer="smpte2084")
    config = EncodeConfig(source=source, output=output, video=video, audio_tracks=[],
        copy_subtitles=False, keep_chapters=False, duration_s=15*1001/24000, work_dir=tmp_path)
    workflow = EncodeWorkflow(ffmpeg_threads=2, ram_buffer_enabled=False, generate_nfo=False,
        nvencc_bin=shutil.which("nvencc"))
    state = wait_task(workflow.run(config), timeout=120)
    assert state["failed"] is None, state["failed"]

    def times(path):
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "frame=pts_time", "-of", "json", str(path)],
            check=True, capture_output=True, text=True)
        pts = [float(frame["pts_time"]) for frame in json.loads(probe.stdout)["frames"]]
        return [t-pts[0] for t in pts]

    before, after = times(source), times(output)
    assert len(before) == len(after) == 12
    assert before[6]-before[5] > .15
    assert after == pytest.approx(before, abs=.001)


@pytest.mark.parametrize("two_pass", [False, True])
def test_missing_enhancement_frame_restarts_whole_track_on_bl(qt_app, tmp_path, monkeypatch, two_pass):
    """EL amputée en milieu de flux : aucune sortie mélangeant FEL et BL."""
    engine = FelEngine(Path(_LIBRARY))
    monkeypatch.setattr(FelEngine, "installed", classmethod(lambda cls: engine))
    source = tmp_path / "missing_el.hevc"
    content = bytearray()
    enhancement_count = 0
    for nal in iter_hevc_nal_units(Path(_FIXTURE)):
        # dovi_tool encapsule l'EL dans des NAL 63 ; son en-tête HEVC suit les deux octets.
        if nal.nal_type == 63 and len(nal.payload) > 4 and ((nal.payload[2] >> 1) & 63) < 32:
            enhancement_count += 1
            if enhancement_count == 7:
                continue
        content.extend(b"\x00\x00\x00\x01" + nal.payload)
    assert enhancement_count == 12
    source.write_bytes(content)
    output = tmp_path / "fallback.mkv"
    video = VideoEncodeSettings(codec="libx265", preset="ultrafast", source_path=source,
        bake_dovi_fel=True, dovi_source_profile="p7_fel", bit_depth="10", input_frame_rate="24000/1001",
        quality_mode=QualityMode.SIZE if two_pass else QualityMode.CRF, target_size_mb=1)
    config = EncodeConfig(source=source, output=output, video=video, audio_tracks=[],
        copy_subtitles=False, keep_chapters=False, duration_s=12*1001/24000, work_dir=tmp_path)
    workflow = EncodeWorkflow(ffmpeg_threads=2, ram_buffer_enabled=False, generate_nfo=False)
    messages: list[str] = []
    workflow.log_message.connect(lambda _level, message: messages.append(message))
    state = wait_task(workflow.run(config), timeout=120)
    assert state["failed"] is None, state["failed"]
    assert any("FEL confirmé" in message for message in messages)
    assert any("reprise complète sur le BL" in message for message in state["progress"])
    probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames", "-of", "json", str(output)],
        check=True, capture_output=True, text=True)
    assert int(json.loads(probe.stdout)["streams"][0]["nb_read_frames"]) == 12
