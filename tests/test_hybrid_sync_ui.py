"""Affichage complet des tableaux et partage du Sync Studio avec Hybrid."""
from __future__ import annotations

import copy
import json
import threading
import time

import pytest
from PySide6.QtWidgets import QDialog, QGroupBox, QMessageBox, QScrollArea, QTableWidgetItem

from core.config import AppConfig
from core.i18n import translate_text
from core.inspector import FileInfo, SubtitleTrack
from core.workflows.hybrid_matrix import MatrixEpisode, MatrixSource
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


@pytest.fixture
def season(panel, tmp_path, monkeypatch):
    panel.reference.setText(str(tmp_path / "masters"))
    panel.donor.setText(str(tmp_path / "donors"))
    panel.output.setText(str(tmp_path / "out"))
    source = MatrixSource("donor", tmp_path / "donors")
    episodes = [MatrixEpisode(
        season=1, episode=i + 1, master_file=tmp_path / f"master{i}.mkv",
        donor_files=[(source, tmp_path / f"donor{i}.mkv")] if i != 1 else [],
    ) for i in range(4)]
    monkeypatch.setattr(panel, "_build_matrix", lambda: panel.matrix)
    monkeypatch.setattr(panel.matrix, "scan", lambda: episodes)
    calls = []

    def prepare(episode, recipe, *_args, calibrate=True, **_kwargs):
        calls.append((episode.episode, calibrate))
        config, calibration = _job(tmp_path)
        config.output = tmp_path / "out" / f"episode{episode.episode}.mkv"
        if not calibrate:
            config.sync_calibrations.clear()
            calibration = None
        return config, calibration

    monkeypatch.setattr("ui.panels.hybrid_studio.prepare_matrix_episode", prepare)
    return calls


@pytest.mark.parametrize("accept", [False, True])
def test_season_analysis_prompts_only_after_all_pairs_are_prepared(panel, season, monkeypatch, qt_app, accept):
    prompts = []

    def question(*_args):
        prompts.append(sorted(season))
        assert not panel.busy
        assert panel._pending_sync_rows == {0, 2, 3}
        assert panel._sync_editor is None
        return QMessageBox.StandardButton.Yes if accept else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", question)
    panel.scan_button.click()
    _wait(qt_app, lambda: not panel.busy)
    analysis = [(1, False), (3, False), (4, False)]
    assert prompts == [analysis]
    # Paires préparées en parallèle : ordre libre dans chaque phase.
    assert sorted(season[:3]) == analysis
    assert sorted(season[3:]) == ([(1, True), (3, True), (4, True)] if accept else [])
    assert set(panel.jobs) == {0, 2, 3}
    assert panel._pending_sync_rows == (set() if accept else {0, 2, 3})
    assert panel.verify_audio_button.isEnabled()
    assert panel.sync_episode.count() == (3 if accept else 0)


def test_pairs_are_prepared_in_parallel_with_a_shared_cache(panel, season, monkeypatch, qt_app, tmp_path):
    import ui.panels.hybrid_studio as studio
    base = studio.prepare_matrix_episode
    lock, running, peaks, caches = threading.Lock(), {}, {}, set()

    def prepare(*args, calibrate=True, **kwargs):
        caches.add(id(kwargs["cache"]))
        with lock:
            running[calibrate] = running.get(calibrate, 0) + 1
            peaks[calibrate] = max(peaks.get(calibrate, 0), running[calibrate])
        time.sleep(0.1)
        with lock:
            running[calibrate] -= 1
        return base(*args, calibrate=calibrate, **kwargs)

    monkeypatch.setattr(studio, "prepare_matrix_episode", prepare)
    monkeypatch.setattr(QMessageBox, "question", lambda *_a: QMessageBox.StandardButton.Yes)
    panel.scan_button.click()
    _wait(qt_app, lambda: not panel.busy and len(season) == 6)
    # Trois paires complètes : préparées ensemble, puis synchro audio deux par deux.
    assert peaks == {False: 3, True: 2}
    assert caches == {id(panel._prep_cache)}
    assert set(panel.jobs) == {0, 2, 3} and not panel._pending_sync_rows


def test_audio_button_rechecks_every_pair_and_preserves_manual_pairing(panel, season, monkeypatch, qt_app, tmp_path):
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    panel.scan_button.click()
    _wait(qt_app, lambda: not panel.busy)
    reassigned = panel.matrix_episodes[2]
    reassigned.donor_files[0] = (reassigned.donor_files[0][0], tmp_path / "manually_assigned.mkv")
    panel._on_donor_reassigned(2, reassigned)
    panel.table.selectRow(3)
    monkeypatch.setattr(panel.matrix, "scan", lambda: pytest.fail("Le plan ne doit pas être rescanné"))
    for _ in range(2):
        season.clear()
        panel.verify_audio_button.click()
        assert panel.busy and not panel.verify_audio_button.isEnabled()
        _wait(qt_app, lambda: not panel.busy)
        assert sorted(season) == [(1, True), (3, True), (4, True)]
        assert panel.matrix_episodes[2] is reassigned
        assert reassigned.donor_files[0][1].name == "manually_assigned.mkv"
        assert not panel._pending_sync_rows


@pytest.mark.parametrize("action", ["run", "export"])
def test_run_and_export_calibrate_all_pending_episodes(panel, season, monkeypatch, qt_app, tmp_path, action):
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    panel.scan_button.click()
    _wait(qt_app, lambda: not panel.busy)
    season.clear()
    completed = []

    def finish(*_args):
        completed.append(set(panel.jobs))
        assert not panel._pending_sync_rows
        panel._finish_operation("finished", "test")

    if action == "run":
        monkeypatch.setattr(panel, "run_next", finish)
        panel.run()
    else:
        monkeypatch.setattr("ui.panels.hybrid_studio.QFileDialog.getExistingDirectory", lambda *_a: str(tmp_path))
        monkeypatch.setattr(panel, "_save_jobs", finish)
        panel.export_jobs()
    _wait(qt_app, lambda: not panel.busy)
    assert completed == [{0, 2, 3}]
    assert sorted(season) == [(1, True), (3, True), (4, True)]


def test_cancelled_season_analysis_does_not_offer_audio_verification(panel, season, monkeypatch, qt_app, tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def prepare(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return _job(tmp_path)

    monkeypatch.setattr("ui.panels.hybrid_studio.prepare_matrix_episode", prepare)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: pytest.fail("Analyse annulée : aucune confirmation"))
    try:
        panel.scan_button.click()
        _wait(qt_app, entered.is_set)
        panel.cancel()
    finally:
        release.set()
    _wait(qt_app, lambda: not panel.busy)
    assert not panel.jobs and not panel._pending_sync_rows
    assert not panel._defer_audio_sync


def test_season_with_no_complete_pair_does_not_offer_audio_verification(panel, season, monkeypatch, qt_app):
    for episode in panel.matrix.scan():
        episode.donor_files.clear()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: pytest.fail("Aucune paire à vérifier"))
    panel.scan_button.click()
    _wait(qt_app, lambda: not panel.busy)
    assert not season and not panel.jobs
    assert not panel.verify_audio_button.isEnabled()


def _fail_episode(monkeypatch, number, *, calibrated_only=False):
    import ui.panels.hybrid_studio as studio
    base = studio.prepare_matrix_episode

    def prepare(episode, *args, calibrate=True, **kwargs):
        if episode.episode == number and (calibrate or not calibrated_only):
            raise RuntimeError(f"Épisode {number} illisible")
        return base(episode, *args, calibrate=calibrate, **kwargs)

    monkeypatch.setattr(studio, "prepare_matrix_episode", prepare)


def test_failing_episode_does_not_stop_season_analysis(panel, season, monkeypatch, qt_app):
    _fail_episode(monkeypatch, 3)
    prompts = []
    monkeypatch.setattr(QMessageBox, "question", lambda *_a: prompts.append(set(panel._pending_sync_rows))
                        or QMessageBox.StandardButton.No)
    panel.scan_button.click()
    _wait(qt_app, lambda: not panel.busy)
    assert sorted(season) == [(1, False), (4, False)]
    assert set(panel.jobs) == {0, 3} and prompts == [{0, 3}]
    assert panel.table.item(2, 3).text() == translate_text("Échec")
    assert "illisible" in panel.table.item(2, 3).toolTip()
    assert panel.status.text() == translate_text("Analyse terminée : {count} épisode(s) en échec ({names}).",
                                                 count=1, names=panel.matrix_episodes[2].display_name)


@pytest.mark.parametrize("action", ["verify", "run"])
def test_failing_audio_check_still_covers_the_rest_of_the_season(panel, season, monkeypatch, qt_app, action):
    monkeypatch.setattr(QMessageBox, "question", lambda *_a: QMessageBox.StandardButton.No)
    panel.scan_button.click()
    _wait(qt_app, lambda: not panel.busy)
    season.clear()
    _fail_episode(monkeypatch, 3, calibrated_only=True)
    executions = []
    monkeypatch.setattr(panel, "run_next", lambda: executions.append(True))
    (panel.verify_audio if action == "verify" else panel.run)()
    _wait(qt_app, lambda: not panel.busy)
    assert sorted(season) == [(1, True), (4, True)]
    assert set(panel.jobs) == {0, 3} and not panel._pending_sync_rows
    assert not executions and not panel._operation_active
    message = ("Analyse terminée : {count} épisode(s) en échec ({names})." if action == "verify"
               else "Préparation impossible pour {count} épisode(s) : {names}")
    assert panel.status.text() == translate_text(message, count=1, names=panel.matrix_episodes[2].display_name)


def test_export_keeps_edited_plans_and_calibrates_only_pending_pairs(panel, season, monkeypatch, qt_app, tmp_path):
    monkeypatch.setattr(QMessageBox, "question", lambda *_a: QMessageBox.StandardButton.No)
    panel.scan_button.click()
    _wait(qt_app, lambda: not panel.busy)
    panel.calibrate_single_row(0)
    _wait(qt_app, lambda: not panel.busy)
    edited = panel.jobs[0]
    season.clear()
    saved = []

    def save(*_args):
        saved.append(dict(panel.jobs))
        panel._finish_operation("finished", "test")

    monkeypatch.setattr("ui.panels.hybrid_studio.QFileDialog.getExistingDirectory", lambda *_a: str(tmp_path))
    monkeypatch.setattr(panel, "_save_jobs", save)
    panel.export_jobs()
    _wait(qt_app, lambda: not panel.busy)
    assert sorted(season) == [(3, True), (4, True)]
    assert saved[0][0] is edited and set(saved[0]) == {0, 2, 3}


def test_standard_options_are_visible_and_advanced_options_are_hidden(panel, qt_app):
    panel.show()
    qt_app.processEvents()
    assert not panel._advanced_options.isChecked()
    for key in ("auto-forced-subs", "auto-sdh", "auto-tmdb", "output-template"):
        assert panel.controls[key].isVisible() and panel.controls[key].isEnabled()
        assert panel._standard_options.isAncestorOf(panel.controls[key])
    assert panel.controls["auto-forced-subs"].isChecked()
    assert panel.controls["auto-sdh"].isChecked()
    assert panel.output_preview.isVisible()
    assert panel._sources_card.isAncestorOf(panel.matching_mode_combo)
    assert panel.matching_mode_combo.isVisible()
    for key in ("no-clean-nfo", "no-cover", "tag", "calibration", "forced-threshold"):
        assert not panel.controls[key].isVisible()
    panel._advanced_options.setChecked(True)
    qt_app.processEvents()
    assert panel.controls["calibration"].isVisible()
    assert panel.controls["tag"].isVisible()
    panel._advanced_options.setChecked(False)
    args = panel._parse_preparation_args()
    assert args.auto_forced_subs and args.auto_sdh and args.remove_closed_captions


def test_caption_option_requires_detection_and_keeps_user_choice(panel, qt_app, tmp_path):
    from core.inspector import VideoTrack
    checkbox = panel.controls["remove-closed-captions"]
    panel._advanced_options.setChecked(True)
    assert checkbox.isChecked() and not checkbox.isEnabled()
    video = VideoTrack(0, "hevc", "HEVC", 1920, 1080, "24", 10, None, None, None, None,
                       has_closed_captions=True)
    info = FileInfo(tmp_path / "witness.mkv", "matroska", 1, 100, None, video_tracks=[video])
    panel._populate_witness_table([("Master", "master", info)])
    assert checkbox.isEnabled() and checkbox.isChecked()
    checkbox.setChecked(False)
    assert panel._parse_preparation_args().remove_closed_captions is False
    panel.set_busy(True)
    assert not checkbox.isEnabled()
    panel.set_busy(False)
    assert checkbox.isEnabled() and not checkbox.isChecked()
    panel.reference.setText(str(tmp_path / "another"))
    panel._update_output_preview()
    assert not checkbox.isEnabled()


def test_older_witness_inspection_never_replaces_a_newer_one(panel, tmp_path, monkeypatch):
    from concurrent.futures import Future
    from types import SimpleNamespace
    infos = {tmp_path / name: FileInfo(tmp_path / name, "matroska", 1, 100, None,
                                       subtitle_tracks=[SubtitleTrack(i, "subrip", "eng", "") for i in range(count)])
             for name, count in (("a.mkv", 1), ("b.mkv", 2))}
    pending = []
    monkeypatch.setattr(panel, "executor", SimpleNamespace(
        submit=lambda task: pending.append((Future(), task)) or pending[-1][0], shutdown=lambda **_: None))
    monkeypatch.setattr(panel._prep_cache, "inspect", lambda _inspector, path: infos[path])
    for path in infos:
        episode = SimpleNamespace(master_file=path, donor_files=[], display_name=path.stem)
        monkeypatch.setattr(panel, "_build_matrix",
                            lambda episode=episode: SimpleNamespace(sources=[object()], scan=lambda: [episode]))
        panel.inspect_witness()
    signature = panel._source_signature()
    panel.reference.setText(str(tmp_path / "autre"))
    # B (dernière demande) termine avant A : A est ignorée.
    for future, task in reversed(pending):
        future.set_result(task())
    assert panel._witness_sources[0].path == tmp_path / "b.mkv"
    assert panel.witness_table.rowCount() == 2
    # Sources modifiées pendant l'inspection : le résultat n'est pas « actuel ».
    assert panel._witness_signature == signature != panel._source_signature()


def test_multi_donor_row_shows_every_donor_and_its_sync(panel, tmp_path):
    first_donor, second_donor = MatrixSource("src_1", tmp_path, label="Donneur 1"), MatrixSource("src_2", tmp_path, label="Donneur 2")
    episode = MatrixEpisode(season=1, episode=1, master_file=tmp_path / "master.mkv",
                            donor_files=[(first_donor, tmp_path / "donor1.mkv"), (second_donor, tmp_path / "donor2.mkv")])
    panel.matrix_episodes = [episode]
    panel.table.setRowCount(0)
    panel._insert_table_row(0, episode, [])
    config, calibration = _job(tmp_path)
    panel._show_row_sync(0, config, calibration)
    assert panel.table.item(0, 3).text() == "Donneur 1 : -67.0 ms · 3 seg  |  Donneur 2 : +25.0 ms · 2 seg"
    assert "donor2.mkv" in panel.table.item(0, 3).toolTip()
    # Mode conteneur : le décalage du second donneur est lu sur ses pistes.
    config.sync_calibrations = {}
    for track in config.sources[2].tracks:
        track.time_shift_ms = 40
    panel._show_row_sync(0, config, SyncCalibration.linear(-67))
    assert panel.table.item(0, 3).text().endswith("Donneur 2 : +40.0 ms · 1 seg")
    combo = panel.table.cellWidget(0, 2)
    assert combo.currentText() == translate_text("✓ {count} donneurs : {names}", count=2, names="Donneur 1 + Donneur 2")
    assert "donor2.mkv" in combo.toolTip()
    # L'entrée « tous les donneurs » ne retire rien ; « Garder uniquement » réduit à un donneur.
    combo._handle_selection(0)
    assert len(episode.donor_files) == 2
    combo.setCurrentIndex(2)
    assert episode.donor_files == [(second_donor, tmp_path / "donor2.mkv")]


def test_witness_language_and_title_edits_are_kept_by_the_recipe(panel, tmp_path):
    from PySide6.QtCore import Qt
    info = FileInfo(tmp_path / "subs.srt", "srt", 1, 100, None, subtitle_tracks=[SubtitleTrack(0, "subrip", "", "")])
    panel._populate_witness_table([("Master", "master", FileInfo(tmp_path / "m.mkv", "matroska", 1, 100, None)),
                                   ("Donneur 2", "donor", info)])
    item = panel.witness_track_items[0]
    item["checkbox"].setChecked(True)
    assert not panel.witness_table.item(0, 3).flags() & Qt.ItemFlag.ItemIsEditable
    assert item["lang_item"].flags() & Qt.ItemFlag.ItemIsEditable
    item["lang_item"].setText("fr-FR")
    item["title_item"].setText("French SDH")
    panel.apply_witness_recipe()
    assert panel.recipe.track_overrides == {item["choice"]: {"language": "fr-FR", "title": "French SDH"}}
    assert panel.recipe.donor_sub_langs == ["fr-FR"]
    # Une nouvelle inspection du témoin réaffiche les valeurs saisies.
    panel._populate_witness_table([("Master", "master", FileInfo(tmp_path / "m.mkv", "matroska", 1, 100, None)),
                                   ("Donneur 2", "donor", info)])
    assert panel.witness_table.item(0, 4).text() == "fr-FR"
    assert panel.witness_table.item(0, 5).text() == "French SDH"


def test_witness_filter_shows_only_selected_tracks(panel, tmp_path):
    info = FileInfo(tmp_path / "witness.mkv", "matroska", 1, 100, None,
                    subtitle_tracks=[SubtitleTrack(i, "subrip", "eng", "") for i in range(3)])
    panel._populate_witness_table([("Master", "master", info)])
    boxes = [item["checkbox"] for item in panel.witness_track_items]
    for box, checked in zip(boxes, (True, False, True)):
        box.setChecked(checked)
    panel.witness_selected_only.setChecked(True)
    assert [panel.witness_table.isRowHidden(row) for row in range(3)] == [False, True, False]
    boxes[0].setChecked(False)
    assert panel.witness_table.isRowHidden(0)
    panel._populate_witness_table([("Master", "master", info)])
    assert panel.witness_selected_only.isChecked()
    assert all(panel.witness_table.isRowHidden(row) != item["checkbox"].isChecked()
               for row, item in enumerate(panel.witness_track_items))
    panel.witness_selected_only.setChecked(False)
    assert not any(panel.witness_table.isRowHidden(row) for row in range(3))


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
    # Fenêtre sur l'axe de sortie : jonction 239 738 − 67 ms, 2 s avant.
    assert editor._current_start_s == pytest.approx(237.671)
    assert editor._current_cut_ms == 239671
    editor._on_duration_ready(3600)
    editor.timeline.setValue(900000)
    assert editor._current_segment_index == 2
    assert editor._current_start_s == 890
    assert "01:00:00" in editor.timeline_label.text()


def test_season_preparation_opens_the_first_ready_studio_without_a_table_selection(panel, tmp_path):
    panel.table.setRowCount(3)
    panel.matrix_episodes = [MatrixEpisode(season=1, episode=i + 1) for i in range(3)]
    panel.jobs = {1: _job(tmp_path), 2: copy.deepcopy(_job(tmp_path))}
    panel.set_busy(True)
    assert panel.table.currentRow() == -1
    panel._finish_preparation()
    assert panel.table.currentRow() == 1
    assert panel.sync_episode.currentData() == 1
    assert panel.sync_episode.count() == 2
    assert panel.sync_target.count() == 5 and panel.sync_reference.count() == 1
    assert isinstance(panel._sync_editor, SyncStudioDialog)
    assert panel.sync_hint.isHidden()


def test_episode_selector_and_correspondence_selection_open_the_same_studio(panel, tmp_path, qt_app):
    panel.table.setRowCount(2)
    panel.matrix_episodes = [MatrixEpisode(season=1, episode=i + 1) for i in range(2)]
    panel.jobs = {0: _job(tmp_path), 1: copy.deepcopy(_job(tmp_path))}
    panel.set_busy(False)
    first = panel._sync_editor
    panel.sync_episode.setCurrentIndex(1)
    assert panel.table.currentRow() == 1
    assert first._closing
    assert panel._sync_editor.target_entry is panel.jobs[1][0].sources[1].tracks[1]
    panel._sync_editor.spin_shift.setValue(125)
    assert panel.jobs[1][0].sync_calibrations["1"]["segments"][0]["shift_ms"] == 125
    assert panel.jobs[0][0].sync_calibrations["1"]["segments"][0]["shift_ms"] == -67
    # Un vrai clic doit aussi mettre à jour le sélecteur et les pistes.
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    panel._wave_section.set_expanded(False)
    panel.resize(1200, 900)
    panel.show()
    panel.table.setItem(0, 0, QTableWidgetItem("S01E01"))
    qt_app.processEvents()
    QTest.mouseClick(panel.table.viewport(), Qt.MouseButton.LeftButton,
                     pos=panel.table.visualItemRect(panel.table.item(0, 0)).center())
    assert panel.table.currentRow() == 0
    assert panel.sync_episode.currentData() == 0
    assert panel._sync_editor.target_entry is panel.jobs[0][0].sources[1].tracks[1]
    # La sélection d'une ligne ne déplie pas la carte Recalage et pré-écoute.
    assert not panel._wave_section.toggle.isChecked()
    panel._wave_section.set_expanded(True)
    panel.findChild(QScrollArea).ensureWidgetVisible(panel.sync_episode)
    qt_app.processEvents()
    QTest.mouseClick(panel.sync_episode, Qt.MouseButton.LeftButton)
    qt_app.processEvents()
    view = panel.sync_episode.view()
    QTest.mouseMove(view.viewport(), view.visualRect(view.model().index(1, 0)).center())
    QTest.qWait(250)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                     pos=view.visualRect(view.model().index(1, 0)).center())
    qt_app.processEvents()
    assert panel.table.currentRow() == 1
    assert panel.sync_episode.currentData() == 1
    assert panel._sync_editor.target_entry is panel.jobs[1][0].sources[1].tracks[1]
    assert panel._sync_editor.spin_shift.value() == 125


def test_reassigning_the_selected_donor_clears_stale_studio_and_lists(panel, tmp_path):
    _select_job(panel, _job(tmp_path))
    previous = panel._sync_editor
    panel._on_donor_reassigned(0, panel.matrix_episodes[0])
    assert previous._closing
    assert panel._sync_editor is None
    assert panel.sync_episode.count() == 0
    assert not panel.sync_episode.isEnabled()
    assert not panel.sync_target.isEnabled() and not panel.sync_reference.isEnabled()
    assert panel.sync_hint.text()


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
    # Jonction en sortie : 100 000 + 25 ms (saut positif de 15 ms), fenêtre 2 s avant.
    assert editor._current_start_s == pytest.approx(98.025)
    editor._reset_shifts()
    assert editor._current_segment_index == 1
    # Fenêtre sur l'axe de sortie : jonction 239 738 − 67 ms, 2 s avant.
    assert editor._current_start_s == pytest.approx(237.671)
    assert editor._current_cut_ms == 239671
    assert editor.cuts_table.rowCount() == 3
    assert editor.spin_shift.value() == -180
    assert editor.btn_next_seg.isEnabled()


@pytest.mark.parametrize("accepted", [False, True])
def test_tmdb_search_reuses_remux_dialog_and_reports_the_selected_id(panel, tmp_path, monkeypatch, accepted):
    from core.media_info_fetcher import MediaDetails, MediaSearchResult
    from ui.panels.tmdb_search_modal import TmdbSearchModal

    reference = tmp_path / "Crystal.Lake.S01E03.2160p.WEB-DL.mkv"
    panel.reference.setText(str(reference))
    panel.donor.setText(str(tmp_path / "donor"))
    panel.output.setText(str(tmp_path / "output"))
    panel.matrix_episodes = [MatrixEpisode(season=1, episode=3, master_file=reference)]
    panel.controls["auto-tmdb"].setText("777")
    panel.controls["output-template"].setText("{title:release}.{season_episode}")
    for group in panel.findChildren(QGroupBox):
        if group.isCheckable():
            group.setChecked(True)
    signature = panel._settings_signature()
    opened = []

    def choose(dialog):
        opened.append(dialog)
        assert dialog._search_edit.text() == "Crystal Lake"
        assert dialog._season_spin.value() == 1 and dialog._episode_spin.value() == 3
        assert dialog._kind_combo.currentIndex() == 2
        if not accepted:
            return QDialog.DialogCode.Rejected
        dialog._on_results([MediaSearchResult(213562, "Crystal Lake", "2026", "tv")])
        # L'URL renvoyée peut être IMDb : l'identifiant doit venir du résultat TMDB.
        dialog._on_details(MediaDetails(title="Crystal Lake", url="https://www.imdb.com/title/tt1234567/"))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(TmdbSearchModal, "exec", choose)
    panel.tmdb_search_button.click()
    assert len(opened) == 1
    assert panel.controls["auto-tmdb"].text() == ("213562" if accepted else "777")
    assert (panel._settings_signature() != signature) is accepted
    assert panel._parse_preparation_args().auto_tmdb == (213562 if accepted else 777)
    if accepted:
        assert panel.output_preview.name.text() == "Crystal.Lake.S01E03.mkv"


def test_hybrid_nfo_paths_require_an_explicit_opt_out(panel, tmp_path):
    panel.reference.setText(str(tmp_path / "reference"))
    panel.donor.setText(str(tmp_path / "donor"))
    panel.output.setText(str(tmp_path / "output"))
    assert panel._parse_preparation_args().clean_nfo is True
    panel.controls["no-clean-nfo"].setChecked(True)
    assert panel._parse_preparation_args().clean_nfo is False
    panel.controls["no-clean-nfo"].setChecked(False)
    assert panel._parse_preparation_args().clean_nfo is True
