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


def test_native_physical_aac_is_encoded_once(tmp_path: Path, monkeypatch) -> None:
    """AAC recalée dans un MKV à +500 ms, « Sync réelle » active : l'origine rendue à la piste préparée
    est un décalage d'assemblage, la piste n'est pas réécrite une seconde fois."""
    import core.workflows.remux_backend as backend

    path = tmp_path / "late_aac.mkv"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=s=64x64:r=25:d=3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=3",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1", "-c:a", "aac",
        "-output_ts_offset", "0.5", str(path),
    ], check=True)
    rewrites: list = []
    original = backend.prepare_native_sync_rewrites

    def capture(*args, **kwargs):
        config, planned = original(*args, **kwargs)
        rewrites.extend(planned)
        return config, planned

    monkeypatch.setattr(backend, "prepare_native_sync_rewrites", capture)
    tracks = tracks_from_file_info(FileInspector().inspect(path), file_id="src")
    target = [track for track in tracks if track.track_type == "audio"][1]
    target.sync_calibration = SyncCalibration.linear(250).to_dict()
    target.time_shift_ms = 250
    order = [(0, track.mkv_tid, track.entry_id) for track in tracks]
    config = RemuxConfig(sources=[SourceInput(path, 0, tracks)], output=tmp_path / "out.mkv", track_order=order,
                         keep_chapters=False, mux_backend="native", sync_mode="physical", work_dir=tmp_path / "work")
    workflow = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", sync_rewrite_enabled=True)
    state = wait_task(workflow.run(config), timeout=120.0)
    assert state["failed"] is None, state["failed"]
    assert rewrites == []
    video, reference, shifted = _first_timestamps(config.output)
    assert shifted == pytest.approx(reference, abs=30) and reference == pytest.approx(video, abs=30), \
        (video, reference, shifted)


_EARLY_SRT = """1
00:00:00,100 --> 00:00:00,350
Tôt

2
00:00:02,000 --> 00:00:02,600
Plus tard
"""


def test_native_physical_subtitle_advanced_before_the_extracted_start_is_kept(tmp_path: Path) -> None:
    """MKV à +500 ms, réplique à 600–850 ms avancée de 400 ms : 200–450 ms dans la sortie native
    (l'origine entre dans le recalage avant l'écrêtage à zéro, la réplique n'est pas perdue)."""
    srt = tmp_path / "early.srt"
    srt.write_text(_EARLY_SRT, encoding="utf-8")
    path = tmp_path / "early.mkv"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=s=64x64:r=25:d=3", "-i", str(srt), "-i", str(srt),
        "-map", "0:v", "-map", "1:s", "-map", "2:s", "-c:v", "ffv1", "-c:s", "srt",
        "-output_ts_offset", "0.5", str(path),
    ], check=True)
    tracks = tracks_from_file_info(FileInspector().inspect(path), file_id="src")
    target = [track for track in tracks if track.track_type == "subtitle"][1]
    target.sync_calibration = SyncCalibration.linear(-400).to_dict()
    target.time_shift_ms = -400
    order = [(0, track.mkv_tid, track.entry_id) for track in tracks]
    config = RemuxConfig(sources=[SourceInput(path, 0, tracks)], output=tmp_path / "out.mkv", track_order=order,
                         keep_chapters=False, mux_backend="native", sync_mode="physical", sync_subtitles="none",
                         work_dir=tmp_path / "work")
    state = wait_task(RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(config), timeout=120.0)
    assert state["failed"] is None, state["failed"]
    _video, reference, shifted = _first_timestamps(config.output)
    assert reference == pytest.approx(600, abs=5) and shifted == pytest.approx(200, abs=5), (reference, shifted)


def _block_bounds(path: Path) -> dict[int, tuple[int, int]]:
    """(premier, dernier) horodatage (ms) des blocs de chaque piste."""
    bounds: dict[int, tuple[int, int]] = {}
    for block in MatroskaReader(path).blocks(first_lace_only=True):
        timestamp = block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000
        value = round(timestamp / 1_000_000)
        first, last = bounds.get(block.track_number, (value, value))
        bounds[block.track_number] = (min(first, value), max(last, value))
    return bounds


def test_native_physical_audio_advanced_before_the_extracted_start_is_not_cut(tmp_path: Path) -> None:
    """MKV à +500 ms, FLAC avancé de 400 ms : départ à +100 ms dans la sortie native, rien de coupé
    (les 400 premières millisecondes restent, l'origine couvre l'avance)."""
    source = _audio_source(tmp_path)
    tracks = tracks_from_file_info(FileInspector().inspect(source), file_id="src")
    target = [track for track in tracks if track.track_type == "audio"][1]
    target.sync_calibration = SyncCalibration.linear(-400).to_dict()
    target.time_shift_ms = -400
    order = [(0, track.mkv_tid, track.entry_id) for track in tracks]
    config = RemuxConfig(sources=[SourceInput(source, 0, tracks)], output=tmp_path / "out.mkv", track_order=order,
                         keep_chapters=False, mux_backend="native", sync_mode="physical", work_dir=tmp_path / "work")
    state = wait_task(RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(config), timeout=120.0)
    assert state["failed"] is None, state["failed"]
    (_v, _vl), (ref_first, ref_last), (first, last) = [_block_bounds(config.output)[n] for n in sorted(_block_bounds(config.output))]
    assert first == pytest.approx(ref_first - 400, abs=30), (ref_first, first)
    assert last - first == pytest.approx(ref_last - ref_first, abs=60), (ref_first, ref_last, first, last)


_LONELY_SRT = """1
00:00:00,100 --> 00:00:00,350
Seul carton
"""


@pytest.mark.parametrize("backend", ["ffmpeg", "native"])
def test_physical_subtitle_emptied_by_its_shift(tmp_path: Path, backend: str) -> None:
    """Carton unique (600–850 ms dans un MKV à +500 ms) avancé d'une seconde : piste vide gardée par
    l'assemblage natif (en-tête de l'origine), refus explicite avec FFmpeg (comme la réécriture)."""
    srt = tmp_path / "lonely.srt"
    srt.write_text(_LONELY_SRT, encoding="utf-8")
    path = tmp_path / "lonely.mkv"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=s=64x64:r=25:d=3", "-i", str(srt), "-i", str(srt),
        "-map", "0:v", "-map", "1:s", "-map", "2:s", "-c:v", "ffv1", "-c:s", "srt",
        "-output_ts_offset", "0.5", str(path),
    ], check=True)
    tracks = tracks_from_file_info(FileInspector().inspect(path), file_id="src")
    target = [track for track in tracks if track.track_type == "subtitle"][1]
    target.sync_calibration = SyncCalibration.linear(-1000).to_dict()
    target.time_shift_ms = -1000
    order = [(0, track.mkv_tid, track.entry_id) for track in tracks]
    config = RemuxConfig(sources=[SourceInput(path, 0, tracks)], output=tmp_path / "out.mkv", track_order=order,
                         keep_chapters=False, mux_backend=backend, sync_mode="physical", sync_subtitles="none",
                         work_dir=tmp_path / "work")
    state = wait_task(RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(config), timeout=120.0)
    if backend == "ffmpeg":
        assert state["failed"] is not None and "événements" in str(state["failed"]), state["failed"]
        return
    assert state["failed"] is None, state["failed"]
    reader = MatroskaReader(config.output)
    assert len([track for track in reader.tracks() if track.track_type == 17]) == 2  # 17 : sous-titres
    bounds = _block_bounds(config.output)
    assert len(bounds) == 2  # vidéo et sous-titre de référence : la piste vidée n'a aucun bloc


def _late_flac_source(tmp_path: Path) -> Path:
    """Vidéo FFV1 à +500 ms, deux FLAC à +700 ms (retard propre de 200 ms)."""
    path = tmp_path / "late_flac.mkv"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=s=64x64:r=25:d=3",
        "-itsoffset", "0.2", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
        "-itsoffset", "0.2", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000:duration=3",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1", "-c:a", "flac",
        "-output_ts_offset", "0.5", str(path),
    ], check=True)
    return path


@pytest.mark.parametrize("codec", ["FLAC", "AC3"])
def test_native_physical_advanced_audio_keeps_its_own_delay_and_codec(tmp_path: Path, codec: str) -> None:
    """FLAC à +700 ms (vidéo à +500 ms) avancé de 400 ms : son à +300 ms (retard propre gardé, comblé
    de silence par la réécriture comme toute piste physique, jamais une copie remise à zéro qui le
    placerait à +100 ms) ; variante AC-3 demandée : sortie en AC-3."""
    source = _late_flac_source(tmp_path)
    tracks = tracks_from_file_info(FileInspector().inspect(source), file_id="src")
    target = [track for track in tracks if track.track_type == "audio"][1]
    target.orig_codec = target.orig_codec or target.codec
    target.codec = codec if codec != "FLAC" else target.codec
    target.sync_calibration = SyncCalibration.linear(-400).to_dict()
    target.time_shift_ms = -400
    order = [(0, track.mkv_tid, track.entry_id) for track in tracks]
    config = RemuxConfig(sources=[SourceInput(source, 0, tracks)], output=tmp_path / "out.mkv", track_order=order,
                         keep_chapters=False, mux_backend="native", sync_mode="physical", work_dir=tmp_path / "work")
    state = wait_task(RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").run(config), timeout=120.0)
    assert state["failed"] is None, state["failed"]
    bounds = _block_bounds(config.output)
    (_v, _vl), (ref_first, ref_last), (_first, last) = [bounds[n] for n in sorted(bounds)]
    assert ref_first == pytest.approx(700, abs=30), ref_first
    # Fin du son avancée de 400 ms comme son début : rien de coupé, retard propre conservé.
    assert last == pytest.approx(ref_last - 400, abs=60), (ref_last, last)
    codec_ids = [track.codec_id for track in MatroskaReader(config.output).tracks()]
    assert codec_ids[2] == ("A_AC3" if codec == "AC3" else "A_FLAC"), codec_ids
