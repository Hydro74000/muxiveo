"""Assemblage natif : une variante audio matérialisée (copie ou réencodée) démarre avec sa piste d'origine."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from core.inspector import FileInspector
from core.matroska.reader import MatroskaReader
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, clone_track_entry, tracks_from_file_info

from ._synth import wait_task

pytestmark = [
    pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                       reason="ffmpeg/ffprobe requis"),
    pytest.mark.usefixtures("qt_app"),
]


def _late_source(path: Path, *, audio_delay_ms: int = 0) -> Path:
    """Vidéo FFV1 + audio E-AC-3 démarrant à 0,5 s ; l'audio commence `audio_delay_ms` plus tard."""
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=s=64x64:r=25:d=3",
        "-itsoffset", f"{audio_delay_ms / 1000:.3f}",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "ffv1", "-c:a", "eac3", "-b:a", "384k", "-output_ts_offset", "0.5", str(path),
    ], check=True)
    return path


def _first_timestamps(path: Path) -> dict[int, int]:
    reader = MatroskaReader(path)
    first: dict[int, int] = {}
    for block in reader.blocks(first_lace_only=True):
        timestamp = block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000
        first.setdefault(block.track_number, round(timestamp / 1_000_000))
    return first


@pytest.mark.parametrize("audio_delay_ms", [0, 300])
@pytest.mark.parametrize("variant_codec", ["copy", "AAC"])
def test_native_variant_starts_with_its_original_track(tmp_path: Path, variant_codec: str, audio_delay_ms: int) -> None:
    """Vidéo à 500 ms, audio à 500 ou 800 ms : la variante démarre avec sa piste d'origine."""
    source = _late_source(tmp_path / "late.mkv", audio_delay_ms=audio_delay_ms)
    tracks = tracks_from_file_info(FileInspector().inspect(source), file_id="src")
    audio = next(track for track in tracks if track.track_type == "audio")
    variant = clone_track_entry(audio, entry_id="variant")
    if variant_codec != "copy":
        variant = replace(variant, codec=variant_codec, display_info="2.0  192 kbps")
    tracks.append(variant)
    order = [(0, track.mkv_tid, track.entry_id) for track in tracks]
    config = RemuxConfig(sources=[SourceInput(source, 0, tracks)], output=tmp_path / "out.mkv", track_order=order,
                         keep_chapters=False, mux_backend="native", work_dir=tmp_path / "work")
    state = wait_task(RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(config), timeout=120.0)
    assert state["failed"] is None, state["failed"]
    first = _first_timestamps(config.output)
    numbers = sorted(first)
    original, copied = first[numbers[1]], first[numbers[2]]
    assert original >= 400 + audio_delay_ms
    assert abs(copied - original) <= 40, first


def _late_mp4(path: Path) -> Path:
    """MP4 (MPEG-4 + E-AC-3) dont la vidéo et l'audio démarrent à 0,5 s : lu en flux, rebasé sur son départ."""
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=64x64:rate=25:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
        "-c:v", "mpeg4", "-c:a", "eac3", "-b:a", "384k", "-output_ts_offset", "0.5", str(path),
    ], check=True)
    return path


@pytest.mark.parametrize("variant_codec", ["copy", "AAC"])
def test_native_variant_of_a_streamed_mp4_starts_with_its_original_track(tmp_path: Path, variant_codec: str) -> None:
    source = _late_mp4(tmp_path / "late.mp4")
    tracks = tracks_from_file_info(FileInspector().inspect(source), file_id="src")
    audio = next(track for track in tracks if track.track_type == "audio")
    variant = clone_track_entry(audio, entry_id="variant")
    if variant_codec != "copy":
        variant = replace(variant, codec=variant_codec, display_info="2.0  192 kbps")
    tracks.append(variant)
    order = [(0, track.mkv_tid, track.entry_id) for track in tracks]
    config = RemuxConfig(sources=[SourceInput(source, 0, tracks)], output=tmp_path / "out.mkv", track_order=order,
                         keep_chapters=False, mux_backend="native", work_dir=tmp_path / "work")
    state = wait_task(RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(config), timeout=120.0)
    assert state["failed"] is None, state["failed"]
    first = _first_timestamps(config.output)
    numbers = sorted(first)
    original, copied = first[numbers[1]], first[numbers[2]]
    assert abs(copied - original) <= 40, first
