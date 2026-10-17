"""Affichage complet des tableaux et partage du Sync Studio avec Hybrid."""
from __future__ import annotations

import copy
import json
import time

import pytest
from PySide6.QtWidgets import QScrollArea, QTableWidgetItem

from core.config import AppConfig
from core.inspector import FileInfo, SubtitleTrack
from core.workflows.hybrid_matrix import MatrixEpisode
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.workflows.sync_calibration import SyncCalibration, SyncSegment
from ui.panels.hybrid_studio import HybridStudio
from ui.panels.remux_panel.widgets.sync_studio_dialog import SyncStudioDialog


def _wait(qt_app, condition):
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert condition()


@pytest.fixture
def panel(qt_app, monkeypatch):
    monkeypatch.setattr(SyncStudioDialog, "_load_segment_audio_async", lambda *_a: None)
    studio = HybridStudio(AppConfig())
    yield studio
    studio.close()
    _wait(qt_app, lambda: studio._shutdown.done.is_set())
    qt_app.processEvents()


def _job(tmp_path):
    sources = [
        SourceInput(tmp_path / "master.mkv", 0, [TrackEntry(0, "audio", "EAC3", "5.1", "en-US", "VO", file_id="src0")]),
        SourceInput(tmp_path / "donor1.mkv", 1, [
            TrackEntry(0, "audio", "EAC3", "5.1", "fr-FR", "VF", file_id="src1"),
            TrackEntry(1, "audio", "EAC3", "5.1", "en-US", "VO", file_id="src1", enabled=False),
            TrackEntry(2, "subtitle", "SUBRIP", "", "fr-FR", "Forcés", file_id="src1"),
        ]),
        SourceInput(tmp_path / "donor2.mkv", 2, [
            TrackEntry(0, "audio", "EAC3", "5.1", "fr-FR", "VF", file_id="src2"),
            TrackEntry(1, "subtitle", "SUBRIP", "", "fr-FR", "Complets", file_id="src2"),
        ]),
    ]
    calibration = SyncCalibration((SyncSegment(0, -67), SyncSegment(239738, -180), SyncSegment(838566, -821)))
    second = SyncCalibration((SyncSegment(0, 25), SyncSegment(400000, 250)))
    config = RemuxConfig(sources, tmp_path / "output.mkv", [], sync_mode="physical",
                         sync_calibrations={"1": calibration.to_dict(), "2": second.to_dict()}, crossfade_ms=0)
    return config, calibration


def _select_job(panel, job):
    panel.jobs[0] = job
    panel.matrix_episodes = [MatrixEpisode()]
    panel.table.setRowCount(1)
    panel.table.setItem(0, 3, QTableWidgetItem("Prêt"))
    panel.table.selectRow(0)


def test_correspondence_and_witness_tables_fit_all_rows_and_shrink(panel, qt_app, tmp_path):
    panel.table.setRowCount(8)
    info = FileInfo(tmp_path / "witness.mkv", "matroska", 1, 100, None,
                    subtitle_tracks=[SubtitleTrack(i, "subrip", "eng", f"Piste {i}") for i in range(11)])
    panel._populate_witness_table([("Master", "master", info)])
    panel.resize(1200, 900)
    panel.show()
    qt_app.processEvents()
    for table, count in ((panel.table, 8), (panel.witness_table, 11)):
        _wait(qt_app, lambda: table.viewport().height() >= sum(table.rowHeight(i) for i in range(count)))
        assert table.verticalScrollBar().maximum() == 0
        before = table.height()
        table.verticalHeader().resizeSection(0, table.rowHeight(0) + 20)
        _wait(qt_app, lambda: table.height() == before + 20)
        table.setRowCount(1)
        _wait(qt_app, lambda: table.height() < before)
    assert len(panel.findChildren(QScrollArea)) == 1


def test_correspondence_limits_visible_rows_to_ten(panel, qt_app):
    panel.table.setRowCount(15)
    panel.resize(1200, 900)
    panel.show()
    _wait(qt_app, lambda: panel.table.verticalScrollBar().maximum() > 0 and
          panel.table.viewport().height() == sum(panel.table.rowHeight(i) for i in range(10)))
    height = panel.table.height()
    assert panel.table.viewport().height() == sum(panel.table.rowHeight(i) for i in range(10))
    panel.table.verticalScrollBar().setValue(panel.table.verticalScrollBar().maximum())
    assert panel.table.rowAt(panel.table.viewport().height() - 1) == 14
    panel.table.setRowCount(20)
    qt_app.processEvents()
    assert panel.table.height() == height


def test_embedded_studio_navigates_cuts_and_the_complete_track(panel, tmp_path):
    _select_job(panel, _job(tmp_path))
    editor = panel._sync_editor
    assert isinstance(editor, SyncStudioDialog) and not editor.isWindow()
    assert editor.target_stream_index == 1  # VO donneuse, repère acoustique même décoché.
    assert editor.current_calibration.cuts_count == 2
    assert editor.cuts_table.rowCount() == 3
    editor._next_segment()
    assert editor._current_segment_index == 1
    assert editor._current_start_s == pytest.approx(237.738)
    assert editor._current_cut_ms == 239738
    editor._on_duration_ready(3600)
    editor.timeline.setValue(900000)
    assert editor._current_segment_index == 2
    assert editor._current_start_s == 890
    assert "01:00:00" in editor.timeline_label.text()


def test_editing_one_donor_keeps_other_donors_and_exported_cuts(panel, tmp_path):
    config, primary = _job(tmp_path)
    _select_job(panel, (config, primary))
    first = copy.deepcopy(config.sync_calibrations["1"])
    second_index = next(i for i in range(panel.sync_target.count()) if panel.sync_target.itemData(i)[0] == 2)
    panel.sync_target.setCurrentIndex(second_index)
    editor = panel._sync_editor
    editor._next_segment()
    editor.spin_shift.setValue(345)
    assert config.sync_calibrations["1"] == first
    assert config.sync_calibrations["2"]["segments"] == [{"start_ms": 0, "shift_ms": 25}, {"start_ms": 400000, "shift_ms": 345}]
    assert panel.jobs[0][1] is primary
    assert config.sources[1].tracks[0].sync_calibration is None
    assert config.sources[2].tracks[1].sync_calibration == config.sync_calibrations["2"]
    assert editor.crossfade_ms == 0
    panel._save_jobs(tmp_path)
    payload = json.loads((tmp_path / "output.exact-job.json").read_text())
    assert payload["sync_calibrations"] == config.sync_calibrations


def test_switching_episode_cancels_previous_studio_and_ignores_late_edits(panel, tmp_path):
    config, calibration = _job(tmp_path)
    _select_job(panel, (config, calibration))
    previous = panel._sync_editor
    second = copy.deepcopy(config)
    panel.jobs[1] = (second, calibration)
    panel.table.setRowCount(2)
    panel.table.selectRow(1)
    assert previous._closing and previous._cancel_event.is_set()
    assert panel._sync_editor is not previous
    panel._apply_studio_calibration(0, 1, second, SyncCalibration.linear(999))
    assert config.sync_calibrations["1"]["segments"][0]["shift_ms"] == -67


def test_recalibration_updates_cuts_and_navigation_and_discards_old_preview(panel, tmp_path):
    _select_job(panel, _job(tmp_path))
    editor = panel._sync_editor
    token = editor._preview_token
    editor._next_segment()
    editor._on_preview_error(token, "Ancien extrait")
    assert "Ancien extrait" not in editor.listen_status.text()
    editor._on_auto_sub_sync_ready(SyncCalibration.linear(10))
    assert editor.cuts_table.rowCount() == 1
    assert editor._cuts_card.isHidden() and editor.btn_next_seg.isHidden()
    calibration = SyncCalibration((SyncSegment(0, 10), SyncSegment(300000, 25)))
    editor._on_auto_sub_sync_ready(calibration)
    assert editor.cuts_table.rowCount() == 2
    assert not editor._cuts_card.isHidden() and not editor.btn_next_seg.isHidden()


def test_reset_restores_navigation_after_the_calibration_structure_changed(panel, tmp_path):
    _select_job(panel, _job(tmp_path))
    editor = panel._sync_editor
    editor._on_auto_sub_sync_ready(SyncCalibration((SyncSegment(0, 10), SyncSegment(100000, 25))))
    editor._next_segment()
    assert editor._current_start_s == 98
    editor._reset_shifts()
    assert editor._current_segment_index == 1
    assert editor._current_start_s == pytest.approx(237.738)
    assert editor._current_cut_ms == 239738
    assert editor.cuts_table.rowCount() == 3
    assert editor.spin_shift.value() == -180
    assert editor.btn_next_seg.isEnabled()
