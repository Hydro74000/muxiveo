from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace

import pytest

from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.workflows.sync_calibration import SyncCalibration, SyncSegment


@pytest.mark.parametrize("kind,codec,title", [
    ("audio", "EAC3", "English Atmos"),
    ("subtitle", "HDMV_PGS_SUBTITLE", "French PGS"),
])
def test_unsupported_calibration_is_reported_before_run(tmp_path, kind, codec, title):
    source = tmp_path / "source.mkv"
    source.touch()
    track = TrackEntry(0, kind, codec, "", "fra", title, sync_calibration=SyncCalibration(
        (SyncSegment(0, 0), SyncSegment(1000, 200)),
    ).to_dict())
    config = RemuxConfig([SourceInput(source, 0, [track])], tmp_path / "out.mkv", [(0, 0)])
    errors = RemuxWorkflow().validate(config)
    assert any("incompatible" in error and title in error and "stream=0" in error for error in errors)
    # Une piste retirée de la sortie ne doit pas bloquer le reste du job.
    supported = TrackEntry(1, "audio", "EAC3", "", "fra", "VF")
    config.sources[0].tracks.append(supported)
    config.track_order = [(0, 1)]
    assert not any("incompatible" in error for error in RemuxWorkflow().validate(config))


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg requis")
def test_bounded_interleave_preserves_packets_with_late_subtitle(tmp_path):
    def run(command):
        return subprocess.run(command, check=True, capture_output=True, timeout=60)

    source = tmp_path / "av.mkv"
    donor = tmp_path / "donor.mkv"
    subtitle = tmp_path / "late.srt"
    subtitle.write_text("1\n00:03:15,600 --> 00:03:17,000\nLate forced subtitle\n", encoding="utf-8")
    run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=64x64:r=24:d=210",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=210",
        "-c:v", "libx264", "-threads", "1", "-c:a", "ac3", "-b:a", "96k", str(source),
    ])
    shutil.copyfile(source, donor)
    video = TrackEntry(0, "video", "H264", "", "und", "")
    audio = TrackEntry(1, "audio", "AC3", "", "eng", "")
    foreign = replace(audio, time_shift_ms=168)
    sub = TrackEntry(0, "subtitle", "SRT", "", "fra", "", flag_forced=True)
    config = RemuxConfig(
        [SourceInput(source, 0, [video, audio]), SourceInput(donor, 1, [foreign]),
         SourceInput(subtitle, 2, [sub])],
        tmp_path / "bounded.mkv", [(0, 0), (0, 1), (1, 1), (2, 0)], keep_chapters=False,
    )
    command = RemuxWorkflow().build_command(config)
    assert command[command.index("-max_interleave_delta") + 1] == "5000000"
    run(command)
    baseline = tmp_path / "unbounded.mkv"
    baseline_command = list(command)
    baseline_command[baseline_command.index("-max_interleave_delta") + 1] = "0"
    baseline_command[-1] = str(baseline)
    run(baseline_command)

    def packets(path):
        result = run([
            "ffprobe", "-v", "error", "-show_packets", "-show_data_hash", "sha256",
            "-show_entries", "packet=stream_index,pts,dts,duration,data_hash", "-of", "json", str(path),
        ])
        tracks = {}
        for packet in json.loads(result.stdout)["packets"]:
            tracks.setdefault(packet["stream_index"], []).append(packet)
        return tracks

    actual, expected = packets(config.output), packets(baseline)
    assert len(actual) == 4
    assert actual == expected  # Ni perte de paquet, ni décalage audio/vidéo/sous-titre.
    assert actual[2][0]["pts"] - actual[1][0]["pts"] == 168
    assert actual[3][0]["pts"] >= 195600
