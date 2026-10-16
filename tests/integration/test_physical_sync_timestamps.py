"""La sync physique garde l'origine vidéo et la compensation de l'encodeur."""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace

import numpy as np
import pytest

from core.matroska.assembly import (
    MatroskaAssemblyPlan, MatroskaAssemblyTrack, assembly_output_contract, compile_assembly_plan,
)
from core.matroska.reader import MatroskaReader
from core.matroska.writer import MatroskaWriter
from core.workflows.common.sync_rewrite import SyncRewriteService
from core.workflows.physical_sync import prepare_physical
from core.workflows.remux_command import build_remux_command
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.workflows.sync_calibration import SyncCalibration


pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")),
    reason="FFmpeg et ffprobe requis",
)


def _run(command, _label=""):
    return subprocess.run(command, capture_output=True, check=True, timeout=30).stdout


def _streams(path):
    return json.loads(_run([
        "ffprobe", "-v", "error", "-show_entries", "stream=start_time,initial_padding,sample_rate",
        "-of", "json", str(path),
    ]))["streams"]


def _assert_audio_origin(stream):
    # FFprobe 6 annonce le PTS du paquet de pré-roll (-5/-21 ms), FFprobe 8
    # l'origine présentée (0). Le CodecDelay doit rester présent dans les
    # deux cas ; c'est le premier Block qui prouve l'absence de délai ajouté.
    start = float(stream["start_time"])
    padding = stream.get("initial_padding", 0) / int(stream["sample_rate"])
    assert abs(start) < 0.001 or abs(start + padding) < 0.001


@pytest.mark.parametrize("codec", ["ac3", "eac3", "aac", "flac"])
@pytest.mark.parametrize("backend", ["native", "ffmpeg"])
@pytest.mark.parametrize("mode,offset", [
    ("physical", -125), ("physical", 0), ("physical", 125),
    ("rewrite", -125), ("rewrite", 125),
])
def test_rewritten_audio_keeps_video_origin(tmp_path, codec, mode, backend, offset):
    source = tmp_path / "source.mkv"
    # Le début du signal sert de repère acoustique après la coupe/le silence.
    _run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=25:d=1.5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-af", "adelay=500:all=1", "-c:v", "ffv1", "-c:a", codec, str(source),
    ])
    video = TrackEntry(0, "video", "FFV1", "", "und", "")
    audio = TrackEntry(1, "audio", codec, "", "fra", "", time_shift_ms=offset)
    if mode == "physical":
        config = RemuxConfig(
            [SourceInput(source, 0, [video, audio])], tmp_path / "out.mkv",
            [(0, 0, video.entry_id), (0, 1, audio.entry_id)], sync_mode="physical",
            sync_calibrations={"0": SyncCalibration.linear(offset).to_dict()},
        )
        prepared = prepare_physical(config, tmp_path, "ffmpeg", _run)
        artifact = prepared.sources[-1].path
        assert all(track.time_shift_ms == 0 for track in prepared.sources[-1].tracks)
    else:
        prepared_audio = SyncRewriteService(
            ffmpeg_bin="ffmpeg", advanced_audio_enabled=True,
        ).maybe_materialize(
            source_path=source, stream_index=1, track_type="audio", codec=codec,
            offset_ms=offset, tmp_dir=tmp_path, input_idx=1,
        )
        assert prepared_audio is not None
        artifact = prepared_audio.path

    # Le filtre PCM a déjà posé l'origine ; aucun retard encodeur ne doit
    # s'ajouter, et le premier Block Matroska doit rester à zéro.
    audio_index = 1 if artifact == source else 0
    artifact_stream = _streams(artifact)[audio_index]
    _assert_audio_origin(artifact_stream)
    reader = MatroskaReader(artifact)
    track_number = reader.tracks()[audio_index].number
    assert next(reader.blocks(track_numbers={track_number})).timestamp_ms == 0

    output = tmp_path / "output.mkv"
    if backend == "native":
        plan = MatroskaAssemblyPlan(
            output, (
                MatroskaAssemblyTrack(source, 0, "video"),
                MatroskaAssemblyTrack(artifact, audio_index, "audio"),
            ),
        )
        MatroskaWriter().write(compile_assembly_plan(replace(
            plan, expected_output_contract=assembly_output_contract(plan),
        )))
    else:
        config = RemuxConfig(
            [SourceInput(source, 0, [video]),
             SourceInput(artifact, 1, [replace(audio, mkv_tid=audio_index, time_shift_ms=0)])],
            output, [(0, 0), (1, audio_index)], sync_mode="physical",
        )
        _run(build_remux_command(
            config, ffmpeg_bin="ffmpeg", ffmpeg_progress_args=[],
            ffmpeg_thread_args=[], cli_path=str,
        ))
    streams = _streams(output)
    assert float(streams[0]["start_time"]) == 0
    _assert_audio_origin(streams[1])
    reader = MatroskaReader(output)
    assert all(next(reader.blocks(track_numbers={track.number})).timestamp_ms == 0 for track in reader.tracks())
    assert streams[1].get("initial_padding", 0) == artifact_stream.get("initial_padding", 0)

    # Vérifier aussi le son décodé : supprimer CodecDelay pour masquer le
    # retard déplacerait le repère de 256 échantillons (AC-3/E-AC-3).
    decoded = np.frombuffer(_run([
        "ffmpeg", "-v", "error", "-copyts", "-i", str(output), "-map", "0:a",
        "-af", "aresample=async=1:first_pts=0", "-ar", "48000", "-f", "f32le", "pipe:1",
    ]), dtype="<f4")
    expected_start = (500 + offset) * 48
    assert np.max(np.abs(decoded[:expected_start - 96])) < 0.01
    assert np.max(np.abs(decoded[expected_start:expected_start + 96])) > 0.05
