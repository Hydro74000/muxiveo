"""Non-régression : seek du scanner et application des calibrations multi-segments."""
from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner, _seek_args
from core.workflows.common.sync_rewrite import SyncRewriteService
from core.workflows.remux_models import RemuxError
from core.workflows.sync_calibration import SyncCalibration, SyncSegment, effective_calibration

_CUT = SyncCalibration((SyncSegment(0, 256.0), SyncSegment(90_000, 0.0)))
_EAC3_PROBE = {"codec_name": "eac3", "codec_long_name": "E-AC-3", "profile": "", "channels": 6,
               "bit_rate": "640000", "tags": {}}


def test_seek_args_preroll_before_window():
    # Pas de seek d'entrée près du début : un seek MKV peut perdre les premiers paquets audio.
    assert _seek_args(0) == ([], [])
    assert _seek_args(0.1) == ([], ["atrim=start=0.100000", "asetpts=PTS-STARTPTS"])
    assert _seek_args(197.0) == (["-ss", "192.000000"], ["atrim=start=5.000000", "asetpts=PTS-STARTPTS"])


def test_scanner_samples_trim_after_input(monkeypatch):
    scanner = AudioSyncScanner("ffmpeg", "ffprobe")
    seen: dict[str, list[str]] = {}

    class _Done:
        returncode = 0
        stdout = b""

    def fake_run(command, **_kwargs):
        seen["cmd"] = command
        return _Done()

    monkeypatch.setattr(scanner, "_run", fake_run)
    scanner.samples(AudioSyncTrack(Path("in.mkv"), 2), 0.0, 10)
    assert "-ss" not in seen["cmd"]
    scanner.samples(AudioSyncTrack(Path("in.mkv"), 2), 30.0, 10)
    cmd = seen["cmd"]
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd.count("-ss") == 1
    assert cmd[cmd.index("-af") + 1].startswith("atrim=start=5.000000,asetpts=PTS-STARTPTS,")


@pytest.mark.parametrize("method", ["samples", "pitch_samples"])
@pytest.mark.parametrize("start", [4, 8])
def test_scanner_trims_source_window_before_cadence(tmp_path, method, start):
    import shutil
    import wave
    import numpy as np

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg requis")
    sr = 16000
    samples = np.zeros(sr * (start + 4), dtype="<i2")
    samples[start * sr:(start + 1) * sr] = (
        12000 * np.sin(2 * np.pi * 1000 * np.arange(sr) / sr)
    ).astype("<i2")
    path = tmp_path / "cadence.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, sr, 0, "NONE", "not compressed"))
        output.writeframes(samples.tobytes())
    scanner = AudioSyncScanner(ffmpeg)
    audio = getattr(scanner, method)(AudioSyncTrack(path, 0), start, 1, cadence_filter="atempo=0.5")
    assert len(audio) == sr
    assert np.sqrt(np.mean(audio ** 2)) > 0.1


def test_effective_calibration_ignores_linear():
    assert effective_calibration(SyncCalibration.linear(256)) is None
    assert effective_calibration(_CUT.to_dict()) == _CUT
    assert effective_calibration({"bad": 1}) is None


def _service(monkeypatch, probe=_EAC3_PROBE):
    service = SyncRewriteService(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    monkeypatch.setattr(service, "_probe_stream", lambda _source, _stream_index: dict(probe))
    seen: dict[str, object] = {}

    def fake_run(cmd, destination, _error_prefix, **_kwargs):
        seen["cmd"] = cmd
        destination.write_bytes(b"x")

    monkeypatch.setattr(service, "_run_checked", fake_run)
    return service, seen


def test_audio_rewrite_applies_segments_not_global_delay(tmp_path, monkeypatch):
    service, seen = _service(monkeypatch)
    prepared = service.maybe_materialize(
        source_path=tmp_path / "in.mkv", stream_index=1, track_type="audio", codec="eac3",
        offset_ms=256, tmp_dir=tmp_path, input_idx=2, calibration=_CUT.to_dict(),
    )
    assert prepared is not None
    cmd = cast(list[str], seen["cmd"])
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:1]" in graph and "concat=n=2" in graph
    assert "-af" not in cmd and cmd[cmd.index("-map") + 1] == "[out]"


def test_audio_rewrite_runs_when_first_segment_is_zero(tmp_path, monkeypatch):
    service, seen = _service(monkeypatch)
    calibration = SyncCalibration((SyncSegment(0, 0.0), SyncSegment(60_000, -500.0)))
    prepared = service.maybe_materialize(
        source_path=tmp_path / "in.mkv", stream_index=1, track_type="audio", codec="eac3",
        offset_ms=0, tmp_dir=tmp_path, input_idx=2, calibration=calibration,
    )
    assert prepared is not None and "-filter_complex" in cast(list[str], seen["cmd"])


def test_object_audio_calibration_refused(tmp_path, monkeypatch):
    service, _seen = _service(monkeypatch)
    with pytest.raises(RemuxError, match="multi-segments"):
        service.maybe_materialize(
            source_path=tmp_path / "in.mkv", stream_index=1, track_type="audio", codec="eac3",
            title="English Atmos", offset_ms=256, tmp_dir=tmp_path, input_idx=2, calibration=_CUT,
        )


def test_subtitle_rewrite_uses_calibration(tmp_path, monkeypatch):
    service, _seen = _service(monkeypatch)
    captured: dict[str, object] = {}

    def fake_shift(_src, dst, calibration):
        captured["calibration"] = calibration
        Path(dst).write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")

    monkeypatch.setattr("core.workflows.subtitle_sync.shift_file", fake_shift)
    prepared = service.maybe_materialize(
        source_path=tmp_path / "in.mkv", stream_index=3, track_type="subtitle", codec="subrip",
        offset_ms=256, tmp_dir=tmp_path, input_idx=2, calibration=_CUT,
    )
    assert prepared is not None
    assert captured["calibration"] == _CUT


def test_bridge_carries_calibration_to_encode(tmp_path):
    from core.workflows.encode.models import AudioTrackSettings, EncodeConfig, VideoEncodeSettings
    from core.workflows.encode.mux_backend import PIPELINE_FFMPEG_DIRECT, encode_native_mux_blockers
    from core.workflows.encode.remux_bridge import merge_remux_into_encode_config
    from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry

    src1, src2, out = tmp_path / "a.mkv", tmp_path / "b.mkv", tmp_path / "out.mkv"
    src1.touch()
    src2.touch()
    video = TrackEntry(mkv_tid=0, track_type="video", codec="HEVC", display_info="", language="und", title="")
    audio = TrackEntry(mkv_tid=1, track_type="audio", codec="EAC3", display_info="", language="fre", title="",
                       time_shift_ms=0, sync_calibration=SyncCalibration(
                           (SyncSegment(0, 0.0), SyncSegment(60_000, -500.0))).to_dict())
    rmx = RemuxConfig(
        sources=[SourceInput(path=src1, file_index=0, tracks=[video]),
                 SourceInput(path=src2, file_index=1, tracks=[audio])],
        output=out, track_order=[(0, 0), (1, 1)],
    )
    enc = EncodeConfig(source=src1, output=out, video=VideoEncodeSettings(),
                       audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src2)])
    merged = merge_remux_into_encode_config(enc, rmx)
    offsets = [o for o in merged.track_time_offsets if o.track_type == "audio"]
    assert len(offsets) == 1 and offsets[0].calibration == audio.sync_calibration
    assert any("multi-segments" in r for r in encode_native_mux_blockers(merged, pipeline=PIPELINE_FFMPEG_DIRECT))


def test_physical_applies_track_calibration_once(tmp_path):
    from core.workflows.physical_sync import prepare_physical, preparation_commands
    from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry

    calibration = SyncCalibration((SyncSegment(0, 0), SyncSegment(1500, 256)))
    track = TrackEntry(0, "audio", "EAC3", "", "fra", "", sync_calibration=calibration.to_dict())
    config = RemuxConfig(
        [SourceInput(tmp_path / "in.mkv", 0, [track])], tmp_path / "out.mkv",
        [(0, 0, track.entry_id)], sync_mode="physical",
    )
    commands = []
    prepared = prepare_physical(config, tmp_path, "ffmpeg", lambda cmd, _label: commands.append(cmd))
    assert len(commands) == 1
    assert "concat=n=2" in commands[0][commands[0].index("-filter_complex") + 1]
    assert prepared.sources[-1].tracks[0].sync_calibration is None
    assert prepared.sync_calibrations == {}
    assert track.sync_calibration == calibration.to_dict()
    prepared.sync_mode = "physical"
    assert preparation_commands(prepared, tmp_path, "ffmpeg") == []
