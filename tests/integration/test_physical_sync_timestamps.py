"""La sync physique garde l'origine vidéo et la compensation de l'encodeur."""
from __future__ import annotations

import json
import os
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

_FFMPEG = os.environ.get("MUXIVEO_TEST_FFMPEG", "ffmpeg")
_FFPROBE = os.environ.get("MUXIVEO_TEST_FFPROBE", "ffprobe")


pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")),
    reason="FFmpeg et ffprobe requis",
)


def _run(command, _label=""):
    command = [{"ffmpeg": _FFMPEG, "ffprobe": _FFPROBE}.get(command[0], command[0]), *command[1:]]
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
        prepared = prepare_physical(config, tmp_path, _FFMPEG, _run, ffprobe=_FFPROBE)
        artifact = prepared.sources[-1].path
        assert all(track.time_shift_ms == 0 for track in prepared.sources[-1].tracks)
    else:
        prepared_audio = SyncRewriteService(
            ffmpeg_bin=_FFMPEG, ffprobe_bin=_FFPROBE, advanced_audio_enabled=True,
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
        if mode == "physical":
            # Exécuter le vrai résultat de préparation (mode container), sans
            # réintroduire artificiellement le mode physical comme auparavant.
            config = replace(prepared, output=output)
        else:
            config = RemuxConfig(
                [SourceInput(source, 0, [video]),
                 SourceInput(artifact, 1, [replace(audio, mkv_tid=audio_index, time_shift_ms=0)])],
                output, [(0, 0), (1, audio_index)],
                input_timestamp_offsets={artifact: prepared_audio.ffmpeg_timestamp_offset_s,
                                         source: prepared_audio.source_timestamp_offset_s},
            )
        _run(build_remux_command(
            config, ffmpeg_bin=_FFMPEG, ffmpeg_progress_args=[],
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


@pytest.mark.parametrize("suffix,origin", [("ts", 0), ("m2ts", 600)])
@pytest.mark.parametrize("offset", [-125, 0, 125])
@pytest.mark.parametrize("backend", ["ffmpeg", "native"])
def test_physical_runtime_normalizes_transport_origin(qt_app, tmp_path, suffix, origin, offset, backend):
    """L'origine du transport ne devient ni du silence, ni un délai de piste en copie."""
    from core.workflows.remux import RemuxWorkflow
    from tests.integration._synth import wait_task

    source = tmp_path / f"source.{suffix}"
    _run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=25:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
        "-af", "adelay=500:all=1", "-c:v", "mpeg2video", "-c:a", "ac3",
        "-output_ts_offset", str(origin), "-f", "mpegts", str(source),
    ])
    video = TrackEntry(0, "video", "MPEG2VIDEO", "", "und", "")
    audio = TrackEntry(1, "audio", "ac3", "", "fra", "", time_shift_ms=offset)
    config = RemuxConfig(
        [SourceInput(source, 0, [video, audio])], tmp_path / "output.mkv", [(0, 0), (0, 1)],
        sync_mode="physical", mux_backend=backend, work_dir=tmp_path,
        sync_calibrations={"0": SyncCalibration.linear(offset).to_dict()},
    )
    workflow = RemuxWorkflow(ffmpeg_bin=_FFMPEG, ffprobe_bin=_FFPROBE, generate_nfo=False)
    state = wait_task(workflow.run(config), timeout=60)
    assert state["finished"] is not None and state["failed"] is None, state
    decoded = np.frombuffer(_run([
        "ffmpeg", "-v", "error", "-copyts", "-i", str(config.output), "-map", "0:a",
        "-af", "aresample=async=1:first_pts=0", "-ar", "48000", "-f", "f32le", "pipe:1",
    ]), dtype="<f4")
    peaks = np.flatnonzero(np.abs(decoded) > 0.04)
    assert peaks.size
    assert peaks[0] / 48000 == pytest.approx((500 + offset) / 1000, abs=0.012)
    assert len(decoded) / 48000 < 2.2 + max(0, offset) / 1000


@pytest.mark.parametrize("offset", [-125, 125])
@pytest.mark.parametrize("advanced", [False, True])
@pytest.mark.parametrize("origin", [0, 600])
def test_rewrite_preserves_intrinsic_track_delay(qt_app, tmp_path, offset, advanced, origin):
    """Une piste audio volontairement tardive reste tardive après réécriture."""
    source = tmp_path / "late-audio.mkv"
    _run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=25:d=2",
        "-itsoffset", "0.5", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-c:v", "ffv1", "-c:a", "flac" if advanced else "ac3", "-output_ts_offset", str(origin), str(source),
    ])
    service = SyncRewriteService(ffmpeg_bin=_FFMPEG, ffprobe_bin=_FFPROBE, advanced_audio_enabled=True)
    prepared = service.maybe_materialize(
        source_path=source, stream_index=1, track_type="audio", codec="flac" if advanced else "ac3",
        offset_ms=offset, tmp_dir=tmp_path, input_idx=1,
    )
    assert prepared is not None
    samples = np.frombuffer(_run([
        "ffmpeg", "-v", "error", "-copyts", "-i", str(prepared.path), "-map", "0:a",
        "-af", "aresample=async=1:first_pts=0", "-ar", "48000", "-f", "f32le", "pipe:1",
    ]), dtype="<f4")
    onset = np.flatnonzero(np.abs(samples) > 0.04)[0] / 48000
    assert onset == pytest.approx((500 + offset) / 1000, abs=0.002)


@pytest.mark.parametrize("mode", ["physical", "rewrite"])
def test_piecewise_rewrite_uses_relative_time_on_delayed_source(tmp_path, mode):
    from core.workflows.sync_calibration import SyncSegment

    source = tmp_path / "source.mkv"
    _run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=25:d=2",
        "-itsoffset", "0.5", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-c:v", "ffv1", "-c:a", "ac3", "-output_ts_offset", "600", str(source),
    ])
    calibration = SyncCalibration((SyncSegment(0, 125), SyncSegment(1000, -125)))
    if mode == "physical":
        track = TrackEntry(1, "audio", "ac3", "", "fra", "", sync_calibration=calibration.to_dict())
        config = RemuxConfig([SourceInput(source, 0, [track])], tmp_path / "out.mkv", [(0, 1)], sync_mode="physical")
        prepared = prepare_physical(config, tmp_path, _FFMPEG, _run, ffprobe=_FFPROBE)
        artifact = prepared.sources[-1].path
    else:
        prepared = SyncRewriteService(ffmpeg_bin=_FFMPEG, ffprobe_bin=_FFPROBE).maybe_materialize(
            source_path=source, stream_index=1, track_type="audio", codec="ac3", offset_ms=0,
            tmp_dir=tmp_path, input_idx=1, calibration=calibration,
        )
        assert prepared is not None
        artifact = prepared.path
    samples = np.frombuffer(_run([
        "ffmpeg", "-v", "error", "-copyts", "-i", str(artifact), "-map", "0:a",
        "-af", "aresample=async=1:first_pts=0", "-ar", "48000", "-f", "f32le", "pipe:1",
    ]), dtype="<f4")
    assert np.flatnonzero(np.abs(samples) > 0.04)[0] / 48000 == pytest.approx(0.625, abs=0.002)
    assert len(samples) / 48000 == pytest.approx(1.375, abs=0.035)


@pytest.mark.parametrize("backend", ["ffmpeg", "native"])
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("second_mode", ["", "offset"])
@pytest.mark.parametrize("second_offset", [-250, 250])
@pytest.mark.parametrize("operation", ["encode", "remux"])
def test_mixed_audio_sync_keeps_common_origin(qt_app, tmp_path, monkeypatch, backend, external, second_mode, second_offset, operation):
    """Réécritures et offsets gardent leur origine, avec sources communes ou séparées."""
    from core.workflows.common.track_types import TrackOffset
    from core.workflows.encode import AudioTrackSettings, EncodeConfig, EncodeWorkflow, VideoEncodeSettings
    from core.workflows.remux import RemuxWorkflow
    from tests.integration._synth import wait_task

    source = tmp_path / "source.mkv"
    _run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=25:d=1.5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-map", "0:v", "-map", "1:a", "-map", "1:a", "-af", "adelay=500:all=1",
        "-c:v", "ffv1", "-c:a", "ac3", str(source),
    ])
    donor = source
    if external:
        donor = tmp_path / "donor.mkv"
        shutil.copyfile(source, donor)
    config = EncodeConfig(
        source=source, output=tmp_path / "output.mkv", video=VideoEncodeSettings(codec="copy"),
        audio_tracks=[AudioTrackSettings(1, source_path=donor), AudioTrackSettings(2, source_path=donor)], copy_subtitles=False,
        keep_chapters=False, mux_backend=backend, work_dir=tmp_path,
        track_time_offsets=[TrackOffset("audio", donor, 1, 125), TrackOffset("audio", donor, 2, second_offset, second_mode)],
    )
    if operation == "encode":
        workflow = EncodeWorkflow(ffmpeg_bin=_FFMPEG, ffprobe_bin=_FFPROBE, generate_nfo=False,
                                  ram_buffer_enabled=False, sync_rewrite_enabled=True)
    else:
        video = TrackEntry(0, "video", "FFV1", "", "und", "")
        audio = [TrackEntry(1, "audio", "ac3", "", "fra", "", time_shift_ms=125),
                 TrackEntry(2, "audio", "ac3", "", "fra", "", time_shift_ms=second_offset,
                            sync_rewrite_mode=second_mode)]
        sources = ([SourceInput(source, 0, [video]), SourceInput(donor, 1, audio)] if external
                   else [SourceInput(source, 0, [video, *audio])])
        audio_source_index = 1 if external else 0
        config = RemuxConfig(sources, tmp_path / "output.mkv",
                             [(0, 0), (audio_source_index, 1), (audio_source_index, 2)],
                             mux_backend=backend, work_dir=tmp_path)
        workflow = RemuxWorkflow(ffmpeg_bin=_FFMPEG, ffprobe_bin=_FFPROBE, generate_nfo=False,
                                 sync_rewrite_enabled=True)
    commands = []
    original_run = workflow._runner._run_cmd

    def run(command, **kwargs):
        commands.append(list(command))
        return original_run(command, **kwargs)

    monkeypatch.setattr(workflow._runner, "_run_cmd", run)
    state = wait_task(workflow.run(config), timeout=60)
    assert state["finished"] is not None and state["failed"] is None, state
    streams = _streams(config.output)
    assert float(streams[0]["start_time"]) == 0, "\n".join(" ".join(cmd) for cmd in commands)
    for index, offset in enumerate((125, second_offset)):
        samples = np.frombuffer(_run([
            "ffmpeg", "-v", "error", "-copyts", "-i", str(config.output), "-map", f"0:a:{index}",
            "-af", "aresample=async=1:first_pts=0", "-ar", "48000", "-f", "f32le", "pipe:1",
        ]), dtype="<f4")
        onset = np.flatnonzero(np.abs(samples) > 0.04)[0] / 48000
        assert onset == pytest.approx((500 + offset) / 1000, abs=0.002)
