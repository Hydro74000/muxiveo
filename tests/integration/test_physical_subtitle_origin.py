"""Synchro physique : une piste réécrite garde l'origine de sa source (MKV démarrant à 500 ms)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from core.inspector import FileInspector
from core.matroska.reader import MatroskaReader
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, tracks_from_file_info
from core.workflows.sync_calibration import SyncCalibration

from ._synth import wait_task

pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                       reason="ffmpeg/ffprobe requis"),
    pytest.mark.usefixtures("qt_app"),
]

_SRT = """1
00:00:00,750 --> 00:00:01,500
Bonjour

2
00:00:02,000 --> 00:00:02,600
Au revoir
"""


def _source(tmp_path: Path) -> Path:
    """Vidéo FFV1 + deux SRT, le tout décalé de 500 ms (origine du MKV)."""
    srt = tmp_path / "cues.srt"
    srt.write_text(_SRT, encoding="utf-8")
    path = tmp_path / "late.mkv"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=s=64x64:r=25:d=3", "-i", str(srt), "-i", str(srt),
        "-map", "0:v", "-map", "1:s", "-map", "2:s", "-c:v", "ffv1", "-c:s", "srt",
        "-output_ts_offset", "0.5", str(path),
    ], check=True)
    return path


def _first_timestamps(path: Path) -> list[int]:
    reader = MatroskaReader(path)
    first: dict[int, int] = {}
    for block in reader.blocks(first_lace_only=True):
        timestamp = block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000
        first.setdefault(block.track_number, round(timestamp / 1_000_000))
    return [first[number] for number in sorted(first)]


@pytest.mark.parametrize("backend", ["ffmpeg", "native"])
def test_rewritten_subtitle_keeps_the_source_origin(tmp_path: Path, backend: str) -> None:
    source = _source(tmp_path)
    tracks = tracks_from_file_info(FileInspector().inspect(source), file_id="src")
    target = [track for track in tracks if track.track_type == "subtitle"][1]
    target.sync_calibration = SyncCalibration.linear(250).to_dict()
    target.time_shift_ms = 250
    order = [(0, track.mkv_tid, track.entry_id) for track in tracks]
    config = RemuxConfig(sources=[SourceInput(source, 0, tracks)], output=tmp_path / "out.mkv", track_order=order,
                         keep_chapters=False, mux_backend=backend, sync_mode="physical", sync_subtitles="none",
                         work_dir=tmp_path / "work")
    state = wait_task(RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(config), timeout=120.0)
    assert state["failed"] is None, state["failed"]
    video, reference, shifted = _first_timestamps(config.output)
    assert shifted - reference == pytest.approx(250, abs=5), (video, reference, shifted)


def _audio_source(tmp_path: Path) -> Path:
    """Vidéo FFV1 + deux pistes FLAC, le tout décalé de 500 ms (origine du MKV)."""
    path = tmp_path / "late_audio.mkv"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=s=64x64:r=25:d=3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=3",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1", "-c:a", "flac",
        "-output_ts_offset", "0.5", str(path),
    ], check=True)
    return path


@pytest.mark.parametrize("backend", ["ffmpeg", "native"])
def test_rewritten_audio_keeps_the_source_origin(tmp_path: Path, backend: str) -> None:
    source = _audio_source(tmp_path)
    tracks = tracks_from_file_info(FileInspector().inspect(source), file_id="src")
    target = [track for track in tracks if track.track_type == "audio"][1]
    target.sync_calibration = SyncCalibration.linear(250).to_dict()
    target.time_shift_ms = 250
    order = [(0, track.mkv_tid, track.entry_id) for track in tracks]
    config = RemuxConfig(sources=[SourceInput(source, 0, tracks)], output=tmp_path / "out.mkv", track_order=order,
                         keep_chapters=False, mux_backend=backend, sync_mode="physical", work_dir=tmp_path / "work")
    state = wait_task(RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(config), timeout=120.0)
    assert state["failed"] is None, state["failed"]
    # Le recalage est écrit dans la piste (silence en tête) : elle démarre avec les autres.
    video, reference, shifted = _first_timestamps(config.output)
    assert shifted == pytest.approx(reference, abs=5) and reference == pytest.approx(video, abs=30), \
        (video, reference, shifted)
