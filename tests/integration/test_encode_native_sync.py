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


@pytest.mark.parametrize("route", ["hybrid_to_native", "ffmpeg_rewrite"])
def test_pitch_only_subtitle_calibration_preserves_two_second_offset(source, tmp_path, route):
    from core.workflows.encode.remux_bridge import merge_remux_into_encode_config
    from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry

    calibration = SyncCalibration((SyncSegment(0, 2000),), cadence_pitch_factor=0.96)
    if route == "hybrid_to_native":
        video = TrackEntry(0, "video", "ffv1", "", "und", "")
        subtitle = TrackEntry(2, "subtitle", "subrip", "", "fra", "")
        remux = RemuxConfig([SourceInput(source, 0, [video]), SourceInput(source, 1, [subtitle])],
                            tmp_path / "out.mkv", [(0, 0, video.entry_id), (1, 2, subtitle.entry_id)],
                            sync_mode="physical", sync_subtitles="mirror", sync_calibrations={"1": calibration.to_dict()})
        cfg = merge_remux_into_encode_config(config(source, tmp_path, []), remux)
        output = assemble(cfg, tmp_path)
        output_index = 2
    else:
        prepared = SyncRewriteService(ffmpeg_bin="ffmpeg").maybe_materialize(
            source_path=source, stream_index=2, track_type="subtitle", codec="subrip", offset_ms=0,
            tmp_dir=tmp_path, input_idx=0, calibration=calibration,
        )
        assert prepared is not None
        output, output_index = prepared.path, 0
    original, rewritten = MatroskaReader(source), MatroskaReader(output)
    original_times = [block.timestamp_ms for block in original.blocks(track_numbers={original.tracks()[2].number})]
    shifted_times = [block.timestamp_ms for block in rewritten.blocks(track_numbers={rewritten.tracks()[output_index].number})]
    assert len(shifted_times) == len(original_times) == 2
    assert [new - old for new, old in zip(shifted_times, original_times)] == pytest.approx([2000, 2000], abs=10)


@pytest.mark.parametrize("edit_audio", [False, True])
def test_gui_import_keeps_subtitle_times_and_uses_edited_audio_offset(source, tmp_path, qt_app, edit_audio):
    import numpy as np
    from core.config import AppConfig
    from core.inspector import FileInspector
    from core.workflows.encode.remux_bridge import merge_remux_into_encode_config
    from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
    from ui.panels.remux_panel import RemuxPanel
    from ui.panels.remux_panel.functions.workflow import restore

    video = TrackEntry(0, "video", "ffv1", "", "und", "")
    audio = TrackEntry(1, "audio", "ac3", "192 kbps", "fra", "")
    subtitle = TrackEntry(2, "subtitle", "subrip", "", "fra", "")
    imported = RemuxConfig(
        [SourceInput(source, 0, [video]), SourceInput(source, 1, [audio, subtitle])],
        tmp_path / "out.mkv", [(0, 0, video.entry_id), (1, 1, audio.entry_id), (1, 2, subtitle.entry_id)],
        sync_mode="physical", sync_subtitles="none", sync_calibrations={"1": SyncCalibration.linear(2000).to_dict()},
        keep_chapters=False,
    )
    info = FileInspector().inspect(source)
    panel = RemuxPanel(AppConfig())
    try:
        restore(panel, imported, [info, info])
        if edit_audio:
            audio.time_shift_ms = 3000
            panel._track_table.time_shift_edited.emit(audio)
        cfg = merge_remux_into_encode_config(config(source, tmp_path, []), panel.collect_config())
        output = assemble(cfg, tmp_path)
    finally:
        panel.close()
    original, result = MatroskaReader(source), MatroskaReader(output)
    original_times = [block.timestamp_ms for block in original.blocks(track_numbers={original.tracks()[2].number})]
    output_times = [block.timestamp_ms for block in result.blocks(track_numbers={result.tracks()[2].number})]
    assert output_times == original_times
    pcm = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(output), "-map", "0:a:0", "-ac", "1", "-ar", "48000",
        "-f", "f32le", "pipe:1",
    ], capture_output=True, check=True, timeout=30).stdout
    samples = np.frombuffer(pcm, dtype="<f4")
    active = np.flatnonzero(np.abs(samples) > 0.02)
    assert active.size
    assert active[0] / 48 == pytest.approx(3000 if edit_audio else 2000, abs=20)
