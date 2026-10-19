"""Audio must remain seekable after muxing prepared video and sparse subtitles.

Uses the checked-in corpus and repeats its existing packets with new timestamps.
No encoder, GPU or interpolation process is invoked, including fixture creation.
"""

from collections import defaultdict
from dataclasses import replace
from pathlib import Path
import json
import shutil
import subprocess

import pytest

from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaReader
from core.matroska.writer import MatroskaWriter
from core.workflows.encode import AudioTrackSettings, EncodeConfig, EncodeWorkflow, VideoEncodeSettings


pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")), reason="FFmpeg and ffprobe required",
)


@pytest.fixture(scope="module")
def sparse_subtitle_source(tmp_path_factory):
    work = tmp_path_factory.mktemp("sparse-subtitle-seek")
    corpus = Path(__file__).resolve().parents[1] / "corpus/matroska/avc_aac_srt_chapters.mkv"
    reader = MatroskaReader(corpus)
    tracks = tuple(MatroskaMuxTrack(corpus, track, track.number, track.uid) for track in reader.tracks())
    blocks = list(reader.blocks())
    packets = []
    for cycle in range(250):
        for block in blocks:
            if block.track_number == 3:
                continue
            timestamp = cycle * 640 + block.timestamp_ms
            packets.append(MatroskaMuxPacket(block.track_number, replace(
                block, timestamp_ms=timestamp, timestamp_ns=timestamp * 1_000_000,
            )))
    subtitle = next(block for block in blocks if block.track_number == 3)
    for timestamp in (100_000, 140_000):
        packets.append(MatroskaMuxPacket(3, replace(
            subtitle, timestamp_ms=timestamp, timestamp_ns=timestamp * 1_000_000,
        )))
    source = work / "source.mkv"
    prepared = work / "prepared-video.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(source, tracks, tuple(packets), duration_ms=160_000))
    MatroskaWriter().write(MatroskaMuxPlan(
        prepared, tracks[:1], tuple(p for p in packets if p.output_track_number == 1), duration_ms=160_000,
    ))
    return source, prepared


@pytest.mark.parametrize("builder", ["nvencc", "multi_video_preview"])
def test_prepared_video_mux_preserves_audio_after_video_seek(sparse_subtitle_source, tmp_path, qt_app, builder):
    source, prepared = sparse_subtitle_source
    config = EncodeConfig(
        source=source, output=tmp_path / "output.mkv", work_dir=tmp_path,
        video=VideoEncodeSettings(codec="nvencc_hevc"),
        audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")],
        copy_subtitles=True, mux_backend="ffmpeg", duration_s=160,
    )
    workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg", ram_buffer_enabled=False, generate_nfo=False)
    if builder == "nvencc":
        command, live, cleanup = workflow._build_runtime_nvencc_remux_cmd(config, prepared)
        assert live is None and cleanup == []
    else:
        command = workflow._build_multi_video_final_mux_command(
            config, [{"path": prepared, "map_arg": "0:v:0", "input_args": []}],
        )
    assert all(command[i + 1] == "copy" for i, token in enumerate(command[:-1]) if token.startswith("-c:"))
    subprocess.run(command, capture_output=True, check=True, timeout=30)

    for timestamp in (30, 90, 140):
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-skip_frame", "nokey", "-read_intervals", f"{timestamp}%+1",
            "-show_frames", "-show_entries", "frame=stream_index,pts_time,nb_samples", "-of", "json",
            str(config.output),
        ], capture_output=True, text=True, check=True, timeout=15)
        assert not probe.stderr
        frames = json.loads(probe.stdout)["frames"]
        # Keep video selected: an audio-only probe would seek via the audio
        # stream and hide the physical interleaving regression.
        assert any(frame["stream_index"] == 0 for frame in frames)
        assert any(frame["stream_index"] == 1 and frame["nb_samples"] > 0 for frame in frames), timestamp

    def payloads(path):
        result = defaultdict(list)
        for block in MatroskaReader(path).blocks():
            result[block.track_number].append(block.payload)
        return result

    assert payloads(config.output) == payloads(source)
