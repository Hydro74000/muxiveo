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


def test_sync_calibration_summary_and_cuts_count():
    from core.workflows.sync_calibration import format_calibration_summary

    calib_single = calibration((0, -100))
    assert calib_single.cuts_count == 0
    lines_single = format_calibration_summary(calib_single)
    assert len(lines_single) == 1
    assert "00:00:00.000" in lines_single[0]
    assert "-100.0 ms" in lines_single[0]

    calib_multi = calibration((0, -67), (239738, -180), (838566, -821))
    assert calib_multi.cuts_count == 2
    assert SyncCalibration.format_timestamp(239738) == "00:03:59.738"
    assert SyncCalibration.format_timestamp(3661000) == "01:01:01.000"
    lines_multi = format_calibration_summary(calib_multi.to_dict())
    assert len(lines_multi) == 3
    assert "départ à 00:00:00.000" in lines_multi[0] and "-67.0 ms" in lines_multi[0]
    assert "coupure à 00:03:59.738" in lines_multi[1] and "-180.0 ms" in lines_multi[1] and "(saut de -113.0 ms)" in lines_multi[1]
    assert "coupure à 00:13:58.566" in lines_multi[2] and "-821.0 ms" in lines_multi[2] and "(saut de -641.0 ms)" in lines_multi[2]


def test_track_entry_cuts_label_and_full_info():
    track = TrackEntry(1, "audio", "E-AC-3", "5.1  640 kbps", "fre", "VFF", time_shift_ms=-100)
    assert track.cuts_count == 0
    assert track.cuts_label == ""
    assert "✂" not in track.full_info_label

    calib = calibration((0, -67), (239738, -180), (838566, -821)).to_dict()
    track.sync_calibration = calib
    assert track.cuts_count == 2
    assert track.cuts_label == "✂ 2 coupures"
    assert "✂ 2 coupures" in track.full_info_label


def test_prepare_physical_logs_cuts(tmp_path):
    config = make_config(tmp_path, offset=-67)
    calib = calibration((0, -67), (239738, -180), (838566, -821)).to_dict()
    config.sources[0].tracks[0].sync_calibration = calib
    config.sync_calibrations = {"0": calib}
    logs = []
    prepared = prepare_physical(
        config,
        tmp_path,
        "ffmpeg",
        lambda cmd, label: None,
        log=lambda level, msg: logs.append(f"{level}: {msg}"),
    )
    assert prepared is not None
    # Check that logs contain the cut breakdown
    log_text = "\n".join(logs)
    assert "Synchronisation physique (réécriture exacte)" in log_text
    assert "3 segments (2 coupures" in log_text
    assert "départ à 00:00:00.000 -> décalage -67.0 ms" in log_text
    assert "coupure à 00:03:59.738 -> décalage -180.0 ms (saut de -113.0 ms)" in log_text
    assert "coupure à 00:13:58.566 -> décalage -821.0 ms (saut de -641.0 ms)" in log_text


def test_track_table_renders_cuts_action_button(qt_app, tmp_path):
    from ui.panels.remux_panel.widgets.track_table import _TrackTable
    table = _TrackTable()
    track = TrackEntry(1, "audio", "E-AC-3", "5.1  640 kbps", "fre", "VFF", time_shift_ms=-67, file_id="src0")
    track.sync_calibration = calibration((0, -67), (239738, -180)).to_dict()
    table.append_tracks("#3b82f6", [track])

    # Row 0, column COL_EDIT should contain a widget with the scissors button
    actions_widget = table.cellWidget(0, table.COL_EDIT)
    assert actions_widget is not None
    from PySide6.QtWidgets import QPushButton
    buttons = actions_widget.findChildren(QPushButton)
    # We should have at least 2 buttons (scissors and edit)
    assert len(buttons) >= 2
    # Check tooltip contains multi-segments info
    tips = [b.toolTip() for b in buttons if b.toolTip()]
    assert any("Synchronisation multi-segments" in t or "Multi-segment synchronization" in t for t in tips)
    table.deleteLater()


def test_waveform_view_construction_and_paint(qt_app):
    from ui.widgets.waveform_view import WaveformView
    wave = WaveformView()
    wave.set_loading("Chargement...")
    wave.resize(400, 150)
    wave.show()
    qt_app.processEvents()

    series = ([0.1, 0.5, 0.9, 0.2], [0.05, 0.45, 0.85, 0.15])
    wave.set_series(series)
    wave.set_shift(120.5)
    wave.repaint()
    assert wave.shift_ms == 120.5
    assert len(wave.series) == 2
    wave.close()


def test_sync_studio_dialog_construction_and_spin_change(qt_app, tmp_path):
    from ui.panels.remux_panel.widgets.sync_studio_dialog import SyncStudioDialog
    target_track = TrackEntry(1, "audio", "E-AC-3", "5.1  640 kbps", "fre", "VFF", time_shift_ms=-67, file_id="src1")
    ref_track = TrackEntry(1, "audio", "DTS-HD MA", "5.1  1509 kbps", "eng", "VO", time_shift_ms=0, file_id="src0")

    calib = calibration((0, -67), (239738, -180)).to_dict()
    dialog = SyncStudioDialog(
        target_entry=target_track,
        target_source_path=tmp_path / "target.mkv",
        target_stream_index=1,
        reference_entry=ref_track,
        reference_source_path=tmp_path / "ref.mkv",
        reference_stream_index=1,
        calibration=calib,
    )
    assert dialog.cuts_table is not None
    assert dialog.cuts_table.rowCount() == 2

    # Test spinbox change
    dialog.spin_shift.setValue(-100.0)
    cal, offset = dialog.result_calibration()
    assert offset == -100
    assert cal.segments[0].shift_ms == -100.0
    # Relative delta was -113 ms, so segment 1 is -100 + (-113) = -213 ms
    assert cal.segments[1].shift_ms == pytest.approx(-213.0)
    dialog.close()


def test_profile_selector_lists_and_picks_profiles(qt_app, tmp_path):
    from ui.panels.hybrid_studio import ProfileSelector
    from core.profiles.decision import DecisionProfileManager

    profiles_dir = tmp_path / "profiles"
    mgr = DecisionProfileManager(profiles_dir / "decision")
    mgr.save({
        "name": "TV HD Multi",
        "description": "Profil de test",
        "tags": [],
        "variables": {"aliases": {}},
        "groups": [],
        "selection_policy": {"disable_unmatched_types": []},
        "rules": [],
    })

    selector = ProfileSelector(profiles_dir)
    assert selector.combo.count() >= 2
    # Select the profile
    selector.setText("TV HD Multi")
    assert selector.text() == "TV HD Multi"


def test_track_table_sync_studio_requested_signal(qt_app):
    from ui.panels.remux_panel.widgets.track_table import _TrackTable
    from core.workflows.remux_models import TrackEntry

    table = _TrackTable()
    entry = TrackEntry(
        1,
        "audio",
        "E-AC-3",
        "5.1  640 kbps",
        "fre",
        "VFF",
        time_shift_ms=-100,
    )
    entry.sync_calibration = calibration((0, -100), (200000, -200)).to_dict()

    received = []
    table.sync_studio_requested.connect(lambda e: received.append(e))

    # Calling _open_sync_studio directly should emit sync_studio_requested
    table._open_sync_studio(entry)
    assert len(received) == 1
    assert received[0] is entry

    # Test via cell action button (as clicked in the UI)
    table.append_tracks("#fff", [entry])
    action_cell_widget = table.cellWidget(0, table.COL_EDIT)
    assert action_cell_widget is not None
    # Find all QPushButton in container
    from PySide6.QtWidgets import QPushButton
    buttons = action_cell_widget.findChildren(QPushButton)
    assert len(buttons) >= 1
    # Click each button until sync_studio_requested is emitted again
    count_before = len(received)
    for btn in buttons:
        btn.click()
    assert len(received) > count_before
    assert received[-1] is entry


def test_panel_on_sync_studio_requested_resolves_reference_and_applies(qt_app, monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    from core.config import AppConfig
    from ui.panels.remux_panel.panel import RemuxPanel
    from ui.panels.remux_panel.models import SourceFile
    from core.workflows.remux_models import TrackEntry
    from core.workflows.sync_calibration import SyncCalibration

    panel = RemuxPanel(AppConfig())
    src_a = tmp_path / "ref.mkv"
    src_b = tmp_path / "target.mkv"
    src_a.touch()
    src_b.touch()

    # Ref audio: English 5.1
    ref_track = TrackEntry(1, "audio", "E-AC-3", "5.1  640 kbps", "eng", "VO", file_id="fid_a")
    # Target audio: French 5.1
    tgt_track = TrackEntry(1, "audio", "E-AC-3", "5.1  640 kbps", "fre", "VF", file_id="fid_b", time_shift_ms=-100)

    source_a = SourceFile(id="fid_a", path=src_a, color="#111", info=MagicMock(), tracks=[ref_track])
    source_b = SourceFile(id="fid_b", path=src_b, color="#222", info=MagicMock(), tracks=[tgt_track])

    panel._source_files = [source_a, source_b]
    panel._source_colors = {"fid_a": "#111", "fid_b": "#222"}
    panel._source_names = {"fid_a": "ref.mkv", "fid_b": "target.mkv"}
    panel._track_table.append_tracks("#111", [ref_track])
    panel._track_table.append_tracks("#222", [tgt_track])

    dialog_instances = []

    class MockSyncStudioDialog:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            dialog_instances.append(self)

        def exec(self):
            from PySide6.QtWidgets import QDialog
            return QDialog.DialogCode.Accepted

        def result_calibration(self):
            cal = SyncCalibration.from_dict(calibration((0, -80), (150000, -160)).to_dict())
            return cal, -80

    monkeypatch.setattr("ui.panels.remux_panel.widgets.sync_studio_dialog.SyncStudioDialog", MockSyncStudioDialog)

    # Trigger Synchro Studio on target track
    panel._on_sync_studio_requested(tgt_track)

    assert len(dialog_instances) == 1
    args = dialog_instances[0].kwargs
    assert args["target_entry"] is tgt_track
    assert args["target_source_path"] == src_b
    assert args["reference_entry"] is ref_track
    assert args["reference_source_path"] == src_a
    assert args["reference_stream_index"] == 1

    # Verify calibration was applied
    assert "sync_calibrations" in panel._workflow_options
    assert "1" in panel._workflow_options["sync_calibrations"]
    assert tgt_track.sync_calibration is not None
    assert tgt_track.cuts_count == 1
    assert tgt_track.time_shift_ms == -80


def test_panel_on_sync_studio_requested_multiple_choices_dialog(qt_app, monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    from core.config import AppConfig
    from ui.panels.remux_panel.panel import RemuxPanel
    from ui.panels.remux_panel.models import SourceFile
    from core.workflows.remux_models import TrackEntry

    panel = RemuxPanel(AppConfig())
    src_a = tmp_path / "ref1.mkv"
    src_b = tmp_path / "target.mkv"
    src_c = tmp_path / "ref2.mkv"
    src_a.touch()
    src_b.touch()
    src_c.touch()

    ref1 = TrackEntry(1, "audio", "E-AC-3", "5.1  640 kbps", "eng", "VO 1", file_id="fid_a")
    ref2 = TrackEntry(1, "audio", "E-AC-3", "5.1  640 kbps", "spa", "VO 2", file_id="fid_c")
    tgt_track = TrackEntry(1, "audio", "E-AC-3", "5.1  640 kbps", "fre", "VF", file_id="fid_b")

    source_a = SourceFile(id="fid_a", path=src_a, color="#111", info=MagicMock(), tracks=[ref1])
    source_b = SourceFile(id="fid_b", path=src_b, color="#222", info=MagicMock(), tracks=[tgt_track])
    source_c = SourceFile(id="fid_c", path=src_c, color="#333", info=MagicMock(), tracks=[ref2])

    panel._source_files = [source_a, source_b, source_c]
    panel._source_colors = {"fid_a": "#111", "fid_b": "#222", "fid_c": "#333"}
    panel._source_names = {"fid_a": "ref1.mkv", "fid_b": "target.mkv", "fid_c": "ref2.mkv"}
    panel._track_table.append_tracks("#111", [ref1])
    panel._track_table.append_tracks("#222", [tgt_track])
    panel._track_table.append_tracks("#333", [ref2])

    studio_dialog_opened = []

    class MockSyncStudioDialog:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            studio_dialog_opened.append(self)

        def exec(self):
            from PySide6.QtWidgets import QDialog
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr("ui.panels.remux_panel.widgets.sync_studio_dialog.SyncStudioDialog", MockSyncStudioDialog)

    # Mock user rejecting the reference dialog
    class MockRefDialogReject:
        def __init__(self, choices, parent=None):
            pass
        def exec(self):
            from PySide6.QtWidgets import QDialog
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr("ui.panels.remux_panel.panel._AudioSyncReferenceDialog", MockRefDialogReject)
    panel._on_sync_studio_requested(tgt_track)
    assert len(studio_dialog_opened) == 0  # Not opened because user cancelled ref choice

    # Mock user selecting ref2
    class MockRefDialogAccept:
        def __init__(self, choices, parent=None):
            pass
        def exec(self):
            from PySide6.QtWidgets import QDialog
            return QDialog.DialogCode.Accepted
        def selected_entry(self):
            return ref2

    monkeypatch.setattr("ui.panels.remux_panel.panel._AudioSyncReferenceDialog", MockRefDialogAccept)
    panel._on_sync_studio_requested(tgt_track)
    assert len(studio_dialog_opened) == 1
    assert studio_dialog_opened[0].kwargs["reference_entry"] is ref2







