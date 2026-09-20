"""Contrats hybrides, chronologie et intégration sur des médias synthétiques."""
from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest

from core.workflows.sync_calibration import SyncCalibration, SyncSegment
from core.workflows.subtitle_sync import shift_text, subtitle_hints
from core.workflows.hybrid import pair_directories, episode_key
from core.workflows.workflow_store import load_workflow, save_workflow
from core.workflows.audio_sync_scan import AudioSyncScanner
from core.workflows.audio_sync import AudioSyncError
from core.workflows.physical_sync import audio_filter, prepare_physical, preparation_commands
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry


def calibration(*segments):
    return SyncCalibration(tuple(SyncSegment(*s) for s in segments))


@pytest.mark.parametrize("segments", [[], [(1, 0)], [(0, float("nan"))], [(0, 0), (0, 10)], [(0, 0), (-1, 10)]])
def test_invalid_calibration(segments):
    with pytest.raises(ValueError):
        calibration(*segments)


def test_piecewise_intervals_remove_overlap():
    value = calibration((0, 200), (1000, 500), (2000, -100))
    assert list(value.intervals(800, 3000)) == [(1000, 1200), (1500, 2500), (2500, 2900)]
    assert SyncCalibration.from_dict(value.to_dict()) == value


@pytest.mark.parametrize("offset,expected", [(103, "00:00:01,103"), (-500, "00:00:00,500")])
def test_srt_shift(offset, expected):
    result = shift_text("1\r\n00:00:01,000 --> 00:00:02,000\r\nÉté !\r\n", SyncCalibration.linear(offset))
    assert expected in result and "Été !" in result and "\r\n" in result


def test_srt_crossing_cut_split_and_clamped():
    result = shift_text("1\n00:00:00,500 --> 00:00:02,000\nSalut\n", calibration((0, -1000), (1000, 200)))
    assert result == "1\n00:00:01,200 --> 00:00:02,200\nSalut\n"


def test_ass_preserves_style_and_commas():
    text = "[Events]\nFormat: Layer, Start, End, Style, Text\nDialogue: 0,0:00:01.00,0:00:03.00,Default,{\\i1}Oui, non\n"
    result = shift_text(text, calibration((0, 0), (2000, 500)), suffix=".ass")
    assert result.count("Dialogue:") == 2
    assert result.count("{\\i1}Oui, non") == 2
    assert "0:00:02.50,0:00:03.50" in result


def test_vtt_settings_and_header_preserved():
    result = shift_text("WEBVTT\n\nintro\n00:01.000 --> 00:02.000 align:start\nHi\n", SyncCalibration.linear(150), suffix=".vtt")
    assert "WEBVTT" in result and "intro" in result and "align:start" in result
    assert "00:00:01.150" in result


@pytest.mark.parametrize("count,forced", [(None, False), (0, False), (49, True), (50, False)])
def test_forced_unknown_count_and_language(count, forced):
    assert subtitle_hints("", count=count, language="fr", audio_language="fra")["forced"] is forced
    assert not subtitle_hints("", count=2, language="eng", audio_language="fra")["forced"]


def test_sdh_hints():
    assert subtitle_hints("[Musique douce]", count=None, language="fra", audio_language="fra")["hearing_impaired"]
    assert not subtitle_hints("[Bonjour]", count=None, language="fra", audio_language="fra")["hearing_impaired"]


def test_pairing_case_order_and_missing(tmp_path):
    ref, donor = tmp_path / "Référence", tmp_path / "Donneur"
    ref.mkdir(); donor.mkdir()
    for name in ("B.s01e02.MKV", "A.S01E01.mkv"):
        (ref / name).touch(); (donor / name).touch()
    pairs = pair_directories(ref, donor)
    assert [pair.episode for pair in pairs] == [1, 2]
    (donor / "extra.S01E03.mkv").touch()
    with pytest.raises(ValueError, match="incomplet"):
        pair_directories(ref, donor)


@pytest.mark.parametrize("name", ["S01E01E02.mkv", "S01E01-E02.mkv", "movie.mkv", "S01E01.S01E02.mkv"])
def test_pairing_rejects_ambiguous_names(name):
    with pytest.raises(ValueError):
        episode_key(name)


def test_portable_workflow_windows_source(tmp_path):
    source = tmp_path / "Épisode.mkv"
    source.touch()
    path = tmp_path / "job.json"
    save_workflow(path, {"version": 1, "sources": [{"path": r"Z:\ancien\Épisode.mkv"}], "output": "output.mkv"})
    loaded = load_workflow(path)
    assert loaded["sources"][0]["path"] == str(source)
    assert loaded["output"] == str(tmp_path / "output.mkv")


def test_failed_save_preserves_previous_file(tmp_path):
    path = tmp_path / "job.json"
    path.write_text("previous")
    from cli.errors import ContractError
    with pytest.raises(ContractError):
        save_workflow(path, {"version": 1, "sync_calibrations": {"0": {}}})
    assert path.read_text() == "previous"


@pytest.mark.parametrize("shift", [-230, 170])
def test_fft_signed_offset(shift):
    rng = np.random.default_rng(19)
    reference = rng.normal(size=16000 * 4) * np.repeat(rng.uniform(0.1, 2, 4000), 16)
    donor = np.roll(reference, -shift * 16)
    offset, confidence = AudioSyncScanner.correlate(reference, donor, 500)
    assert abs(offset - shift) <= 1 and confidence > 0.95


def test_fft_silence_rejected():
    with pytest.raises(AudioSyncError):
        AudioSyncScanner.correlate(np.zeros(16000), np.zeros(16000), 100)


def make_config(tmp_path, offset=100, codec="FLAC"):
    track = TrackEntry(0, "audio", codec, "2.0  640 kbps", "fra", "", time_shift_ms=offset, file_id="src0")
    return RemuxConfig([SourceInput(tmp_path / "in.flac", 0, [track])], tmp_path / "out.mkv",
                       [(0, 0, track.entry_id)], mux_backend="native", sync_mode="physical")


def test_physical_keeps_source_state(tmp_path):
    config = make_config(tmp_path)
    commands = []
    prepared = prepare_physical(config, tmp_path, "ffmpeg", lambda cmd, label: commands.append(cmd))
    assert config.sources[0].tracks[0].time_shift_ms == 100
    assert prepared.sources[-1].tracks[0].time_shift_ms == 0
    assert prepared.track_order[0][0] == 1
    assert commands and "-filter_complex" in commands[0]


def test_immersive_physical_rejected(tmp_path):
    config = make_config(tmp_path, codec="TRUEHD")
    with pytest.raises(Exception, match="immersif"):
        preparation_commands(config, tmp_path, "ffmpeg")


@pytest.mark.parametrize("backend", ["native", "ffmpeg"])
def test_physical_real_mux(tmp_path, backend):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg indisponible")
    config = make_config(tmp_path)
    config.mux_backend = backend
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "flac", str(config.sources[0].path)], check=True)
    from core.inspector import FileInspector
    from core.workflows.remux_models import tracks_from_file_info
    info = FileInspector(ffprobe_bin=ffprobe, mediainfo_bin=shutil.which("mediainfo") or "mediainfo").inspect(config.sources[0].path)
    tracks = tracks_from_file_info(info, file_id="src0")
    tracks[0].time_shift_ms = 100
    config.sources[0].tracks = tracks
    config.track_order = [(0, tracks[0].mkv_tid, tracks[0].entry_id)]
    import sys
    from core.profiles.selectors import remux_config_to_exact_job
    job = tmp_path / "job.json"
    save_workflow(job, remux_config_to_exact_job(config))
    process = subprocess.run([sys.executable, "main.py", "--cli", "run", "--config", str(job),
        "--ffmpeg", ffmpeg, "--ffprobe", ffprobe, "--no-nfo"], capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stderr
    result = subprocess.run([ffmpeg, "-v", "error", "-i", str(config.output), "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"], capture_output=True, check=True)
    values = np.frombuffer(result.stdout, dtype="<f4")
    assert len(values) == pytest.approx(17600, abs=32)
    assert np.max(np.abs(values[:1500])) < 0.0001
    assert np.max(np.abs(values[1800:])) > 0.05


@pytest.mark.parametrize("segments,expected", [(((0, 100),), 1.1), (((0, -100),), 0.9), (((0, 0), (500, 200)), 1.2), (((0, 0), (500, -200)), 0.8)])
def test_audio_graph_duration(tmp_path, segments, expected):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg indisponible")
    result = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=duration=1:sample_rate=16000",
        "-filter_complex", audio_filter(calibration(*segments)), "-map", "[out]", "-f", "f32le", "pipe:1"],
        capture_output=True, check=True)
    assert len(result.stdout) / 4 / 16000 == pytest.approx(expected, abs=0.001)


def test_cli_parser_contract():
    from cli.parser import build_parser
    args = build_parser().parse_args(["hybrid", "--ref", "a.mkv", "--donor", "b.mkv", "-o", "out", "--auto-tmdb", "2734"])
    assert args.sync_mode == "physical" and args.auto_tmdb == 2734
    args = build_parser().parse_args(["remux", "--no-clean-nfo", "--sync-mode", "physical"])
    assert args.clean_nfo is False


def test_studio_constructs(qt_app):
    from core.config import AppConfig
    from ui.panels.hybrid_studio import HybridStudio
    dialog = HybridStudio(AppConfig())
    assert not dialog.run_button.isEnabled()
    dialog.close()
