"""Sync native : pistes vides, préflight et timestamps face au mux FFmpeg réel."""

import json
import shutil
import subprocess
from dataclasses import replace

import pytest

from core.matroska.reader import MatroskaReader
from core.workflows.common.sync_rewrite import SyncRewriteService
from core.workflows.common.track_types import TrackOffset
from core.workflows.encode import AudioTrackSettings, EncodeConfig, EncodeWorkflow, VideoEncodeSettings
from core.workflows.encode.models import EncodeError
from core.workflows.encode.runtime.native_mux import NativeVideoArtifactRef, assemble_encode_output_native
from core.workflows.sync_calibration import SyncCalibration, SyncSegment


pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")), reason="FFmpeg et ffprobe requis",
)


def run(command, _label=""):
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def source(tmp_path):
    text = tmp_path / "captions.srt"
    text.write_text("1\n00:00:00,100 --> 00:00:00,400\nFirst\n\n2\n00:00:01,500 --> 00:00:02,000\nSecond\n")
    path = tmp_path / "source.mkv"
    run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=64x64:r=25:d=3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3", "-i", str(text),
        "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "ffv1", "-c:a", "ac3",
        "-b:a", "192k", "-c:s", "srt", "-metadata:s:s", "language=fra",
        "-metadata:s:s", "title=French forced", "-disposition:s", "default+forced", str(path),
    ])
    return path


def config(source, tmp_path, offsets):
    return EncodeConfig(
        source=source, output=tmp_path / "out.mkv", work_dir=tmp_path,
        video=VideoEncodeSettings(codec="copy"), audio_tracks=[AudioTrackSettings(1)],
        copy_subtitles=False, subtitle_tracks=[(source, 2)], keep_chapters=False,
        track_time_offsets=offsets, mux_backend="native",
    )


def assemble(cfg, tmp_path):
    return assemble_encode_output_native(
        cfg, video_artifacts=[NativeVideoArtifactRef(cfg.source)], work_dir=tmp_path,
        signals=None, run_cmd=run, log=lambda *_: None, resolved_subtitles=cfg.subtitle_tracks,
        sync_rewrite=SyncRewriteService(ffmpeg_bin="ffmpeg"),
    )


@pytest.mark.parametrize("codec", ["srt", "ass", "webvtt"])
@pytest.mark.parametrize("calibrated", [False, True])
def test_fully_trimmed_subtitle_keeps_empty_track(source, tmp_path, codec, calibrated):
    converted = tmp_path / "converted.mkv"
    run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-map", "0", "-c", "copy", "-c:s", codec, str(converted)])
    calibration = SyncCalibration((SyncSegment(0, -4000), SyncSegment(1000, -3500))).to_dict() if calibrated else None
    cfg = config(converted, tmp_path, [TrackOffset("subtitle", converted, 2, -2500, calibration=calibration)])
    output = assemble(cfg, tmp_path)
    original = MatroskaReader(converted).tracks()[2]
    reader = MatroskaReader(output)
    track = reader.tracks()[2]
    assert track.codec_id == original.codec_id
    assert track.codec_private == original.codec_private
    assert track.name == "French forced"
    assert track.language in {"fre", "fra"}
    assert track.flag_default and track.flag_forced
    assert not list(reader.blocks(track_numbers={track.number}))
    run(["ffprobe", "-v", "error", "-show_streams", str(output)])
    assert not list(tmp_path.glob("native_empty_subtitle_*.mks"))


@pytest.mark.parametrize("offset", [-125, 125])
def test_rewritten_ac3_timestamps_match_ffmpeg(source, tmp_path, offset):
    cfg = config(source, tmp_path, [TrackOffset("audio", source, 1, offset)])
    output = assemble(cfg, tmp_path)
    rewritten = SyncRewriteService(ffmpeg_bin="ffmpeg").maybe_materialize(
        source_path=source, stream_index=1, track_type="audio", codec="ac3",
        offset_ms=offset, tmp_dir=tmp_path, input_idx=1,
    )
    reference = tmp_path / "reference.mkv"
    from core.workflows.common.audio_timestamps import input_timestamp_offset_args
    run(["ffmpeg", "-v", "error", "-y", *input_timestamp_offset_args(rewritten.source_timestamp_offset_s),
         "-i", str(source), *rewritten.ffmpeg_input_args(),
         "-map", "0:v", "-map", "1:a", "-c", "copy", str(reference)])

    def packets(path):
        payload = json.loads(run([
            "ffprobe", "-v", "error", "-select_streams", "a", "-show_packets", "-show_data_hash", "sha256",
            "-show_entries", "packet=pts_time,data_hash", "-of", "json", str(path),
        ]))
        return [(p["pts_time"], p["data_hash"]) for p in payload["packets"]]

    assert packets(output) == packets(reference)


@pytest.mark.parametrize("validate", [False, True])
@pytest.mark.parametrize("backend", ["native", "ffmpeg"])
def test_disabled_rewrite_rejected_before_preparation(source, tmp_path, monkeypatch, qt_app, validate, backend):
    calibration = SyncCalibration((SyncSegment(0, 0), SyncSegment(1500, 250))).to_dict()
    cfg = config(source, tmp_path, [TrackOffset("audio", source, 1, calibration=calibration)])
    cfg = replace(cfg, video=VideoEncodeSettings(codec="libx264", preset="ultrafast"), mux_backend=backend)
    workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg", sync_rewrite_enabled=False, generate_nfo=False)
    assert any("sync.rewrite_enabled" in message for message in workflow.validate(cfg))
    monkeypatch.setattr(workflow, "_preparation_runner", lambda: pytest.fail("Preparation started"))
    with pytest.raises(EncodeError, match="sync.rewrite_enabled"):
        workflow._run_with_preparation(cfg, validate=validate)
    workflow.set_sync_rewrite_enabled(True)
    assert not any("sync.rewrite_enabled" in message for message in workflow.validate(cfg))
