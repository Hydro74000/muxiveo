"""Contrats de la barre commune, du propriétaire d'exécution et du journal."""
from __future__ import annotations

import time
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox, QScrollArea

from core.config import AppConfig
from core.logging import LogLevel
from core.runner import TaskSignals
from core.workflows.hybrid_matrix import MatrixEpisode
from core.workflows.merge_dovi import DoviProfile, HdrState, WorkflowStep
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from ui.main_window import DashboardPage, MainWindow
from ui.operation_bar import OperationBar, OperationState
from ui.panels.encode_panel.panel import EncodePanel
from ui.widgets.log_panel import LogPanel


@pytest.fixture
def window(qt_app, monkeypatch):
    monkeypatch.setattr(DashboardPage, "_start_hw_detection", lambda self: None)
    monkeypatch.setattr(DashboardPage, "start_accel_detection", lambda self: None)
    monkeypatch.setattr(MainWindow, "_wire_extensions", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    config = AppConfig()
    config.check_updates = False
    config.language = "fra"
    config.startup_menu_compact = False
    win = MainWindow(config)
    win.resize(1280, 900)
    win.show()
    yield win
    win._running = False
    win._hybrid_panel._operation_active = False
    win._hybrid_panel.busy = False
    win._dovi_panel._running = False
    win.close()
    for _ in range(300):
        qt_app.processEvents()
        if not win.isVisible():
            break
        time.sleep(0.02)
    assert not win.isVisible(), [type(page).__name__ for page in win._shutdown_pages]
    win.deleteLater()


def ready_hybrid(win, tmp_path):
    panel = win._hybrid_panel
    panel.reference.setText(str(tmp_path / "reference"))
    panel.donor.setText(str(tmp_path / "donneur"))
    panel.output.setText(str(tmp_path / "sortie"))
    win._stack.setCurrentIndex(4)
    win._refresh_operation_bar()
    return panel


def _wait_until(qt_app, predicate, timeout=5):
    """Attend un état observable, sans supposer la ponctualité d'un QTimer."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qt_app.processEvents()
        QTest.qWait(10)
    assert predicate()


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
@pytest.mark.parametrize("change", ["none", "foreign", "replaced", "removed", "edited"])
def test_hybrid_retry_preserves_successful_outputs_and_refuses_collisions(
    window, tmp_path, monkeypatch, qt_app, terminal, change,
):
    panel = ready_hybrid(window, tmp_path)
    launched, tasks = [], []

    class Workflow(QObject):
        log_message = Signal(str, str)

        def __init__(self, **_kwargs):
            super().__init__()

        def run(self, config):
            launched.append(config.output)
            task = TaskSignals()
            tasks.append(task)
            return task

    monkeypatch.setattr("core.workflows.remux.RemuxWorkflow", Workflow)
    for row in range(2):
        source = tmp_path / f"source{row}.mkv"
        panel.jobs[row] = (RemuxConfig(
            [SourceInput(source, 0, [TrackEntry(0, "audio", "EAC3", "", "fra", "")])],
            tmp_path / f"out{row}.mkv", [(0, 0)],
        ), None)
        panel.matrix_episodes.append(MatrixEpisode(
            season=1, episode=row + 1, master_file=source, donor_files=[(None, source)],
        ))
    panel._prepared_signature = panel._settings_signature()
    window._run_btn.click()
    assert len(tasks) == 1
    first = panel.jobs[0][0].output
    first.write_bytes(b"completed")
    tasks[0].finished.emit("Terminé")
    _wait_until(qt_app, lambda: len(tasks) == 2)
    if terminal == "failed":
        tasks[1].failed.emit("Erreur temporaire", RuntimeError("Erreur temporaire"))
    else:
        panel.cancel()
        tasks[1].cancelled.emit()
    _wait_until(qt_app, lambda: not window._running)
    if change == "foreign":
        panel.jobs[1][0].output.write_bytes(b"foreign")
    elif change == "replaced":
        first.write_bytes(b"replacement from another operation")
    elif change == "removed":
        first.unlink()
    elif change == "edited":
        panel.jobs[0][0].file_title = "Projet modifié"
    panel.run()
    if change in {"foreign", "replaced", "edited"}:
        _wait_until(qt_app, lambda: not window._running)
        assert len(tasks) == 2
        assert "La sortie existe déjà" in panel.status.text()
    else:
        assert len(tasks) == 3
        expected = panel.jobs[1][0].output if change == "none" else first
        assert launched[-1] == expected
        if change == "none":
            expected.write_bytes(b"completed second")
            tasks[-1].finished.emit("Terminé")
            _wait_until(qt_app, lambda: not window._running)
            panel.run()
            assert not window._running
            assert len(tasks) == 3  # Tous les résultats confirmés sont conservés.
        else:
            tasks[-1].cancelled.emit()
            _wait_until(qt_app, lambda: not window._running)


@pytest.mark.parametrize("collision", ["existing_second", "duplicate"])
def test_hybrid_checks_all_destinations_before_starting_a_job(window, tmp_path, monkeypatch, collision):
    panel = ready_hybrid(window, tmp_path)
    for row in range(2):
        source = tmp_path / f"source-{row}.mkv"
        output = tmp_path / f"output-{row}.mkv"
        panel.matrix_episodes.append(MatrixEpisode(
            item_key=str(row), master_file=source, donor_files=[(None, source)],
        ))
        panel.jobs[row] = (RemuxConfig([SourceInput(source, 0, [])], output, []), None)
    if collision == "duplicate":
        panel.jobs[1][0].output = panel.jobs[0][0].output
    else:
        panel.jobs[1][0].output.write_bytes(b"foreign output")
    panel._prepared_signature = panel._settings_signature()
    monkeypatch.setattr("core.workflows.remux.RemuxWorkflow", lambda **_kwargs: pytest.fail("Job lancé avant préflight"))
    panel.run()
    assert not window._running and not panel.busy
    assert not panel.jobs[0][0].output.exists()
    assert ("Sortie générée en double" if collision == "duplicate" else "La sortie existe déjà") in panel.status.text()


@pytest.mark.parametrize("selected", [False, True])
def test_single_calibration_refreshes_offset_and_waveform(window, tmp_path, monkeypatch, qt_app, selected):
    import numpy as np
    from core.workflows.sync_calibration import SyncCalibration

    panel = ready_hybrid(window, tmp_path)
    reference, donor = tmp_path / "reference.mkv", tmp_path / "donneur.mkv"
    panel.table.setRowCount(1)
    panel.matrix_episodes = [MatrixEpisode(season=1, episode=1, master_file=reference, donor_files=[(None, donor)])]
    if selected:
        panel.table.selectRow(0)
    config = RemuxConfig([
        SourceInput(reference, 0, [TrackEntry(0, "audio", "EAC3", "", "eng", "")]),
        SourceInput(donor, 1, [TrackEntry(0, "audio", "EAC3", "", "fra", "")]),
    ], tmp_path / "out.mkv", [(0, 0), (1, 0)], sync_mode="physical")
    calibration = SyncCalibration.linear(125)
    started, release = threading.Event(), threading.Event()
    panel.args = SimpleNamespace(tag="ancien")
    panel.controls["tag"].setText("nouveau")

    def prepare(*_args, **_kwargs):
        assert _args[4].tag == "nouveau"  # Les options d'un ancien scan ne doivent pas survivre.
        started.set()
        assert release.wait(5)
        return config, calibration

    monkeypatch.setattr("ui.panels.hybrid_studio.prepare_matrix_episode", prepare)
    monkeypatch.setattr("core.workflows.audio_sync_scan.AudioSyncScanner.samples", lambda *_a, **_k: np.ones(1000))
    try:
        panel.calibrate_single_row(0)
        _wait_until(qt_app, started.is_set)
        assert window._running and panel.busy
        assert not panel._wave_section.body.isEnabled()
    finally:
        release.set()
    _wait_until(qt_app, lambda: not panel.busy and panel._sync_editor is not None and len(panel._sync_editor.waveform._tgt_samples) > 0)
    assert not window._running
    assert panel._sync_editor.spin_shift.value() == 125
    assert panel._sync_editor.waveform.shift_ms == 125
    assert panel.jobs[0][1].segments[0].shift_ms == 125
    assert panel._wave_section.toggle.isChecked()
    panel._sync_editor.spin_shift.stepUp()
    assert panel.jobs[0][1].segments[0].shift_ms == 135
    executions = []

    def run_next():
        executions.append(panel.jobs[0][1].segments[0].shift_ms)
        panel._finish_operation("finished", "Terminé")

    monkeypatch.setattr(panel, "run_next", run_next)
    panel.run()
    assert executions == [135]  # Le lancement réutilise la calibration éditée.


def test_merge_result_survives_late_hardware_and_readiness_notifications(window, tmp_path, qt_app):
    panel = window._dovi_panel
    panel._file_section._film1 = tmp_path / "film.mkv"
    panel._file_section._film2 = tmp_path / "donneur.mkv"
    window._stack.setCurrentIndex(1)
    panel._running = True
    panel.op_state_changed.emit("started", "Fusion")
    panel._on_workflow_failed(WorkflowStep.VALIDATION, "Métadonnées absentes")
    result = window._last_operation_result
    window._encode_panel._on_hw_detected(set(), set(), str(window._config.tool_ffmpeg))
    panel._notify_ready()
    _wait_until(qt_app, lambda: not window._operation_refresh_timer.isActive())
    assert window._last_operation_result == result
    assert window._run_btn.text() == "Relancer la fusion"
    panel._config_section._output_input.setText(str(tmp_path / "autre-sortie"))
    _wait_until(qt_app, lambda: window._last_operation_result is None)
    assert window._run_btn.text() != "Relancer la fusion"


def test_single_calibration_does_not_certify_a_matrix_from_old_sources(window, tmp_path, monkeypatch):
    panel = ready_hybrid(window, tmp_path)
    source = tmp_path / "old.mkv"
    panel.matrix_episodes = [MatrixEpisode(season=1, episode=1, master_file=source, donor_files=[(None, source)])]
    panel._matrix_signature = panel._source_signature()
    panel.reference.setText(str(tmp_path / "new-reference"))
    scan = Mock()
    monkeypatch.setattr(panel, "scan", scan)
    monkeypatch.setattr("ui.panels.hybrid_studio.prepare_matrix_episode", lambda *_a, **_k: pytest.fail("Anciennes sources"))
    panel.calibrate_single_row(0)
    scan.assert_called_once()


def test_failed_individual_recalibration_discards_the_previous_plan(window, tmp_path, monkeypatch, qt_app):
    panel = ready_hybrid(window, tmp_path)
    source = tmp_path / "source.mkv"
    panel.table.setRowCount(1)
    panel.matrix_episodes = [MatrixEpisode(season=1, episode=1, master_file=source, donor_files=[(None, source)])]
    track = TrackEntry(0, "audio", "EAC3", "", "fra", "")
    panel.jobs[0] = (RemuxConfig([SourceInput(source, 0, [track])], tmp_path / "out.mkv", [(0, 0)]), None)
    panel._prepared_signature = panel._settings_signature()

    def failure(*_args, **_kwargs):
        raise RuntimeError("Recalibration impossible")

    monkeypatch.setattr("ui.panels.hybrid_studio.prepare_matrix_episode", failure)
    panel.calibrate_single_row(0)
    _wait_until(qt_app, lambda: not panel.busy)
    assert not window._running
    assert 0 not in panel.jobs
    assert "Recalibration impossible" in panel.status.text()


def test_context_and_media_settings_share_one_operation(window, tmp_path, monkeypatch):
    ready_hybrid(window, tmp_path)
    encode = SimpleNamespace(output=tmp_path / "encoded.mkv")
    monkeypatch.setattr(window._encode_panel, "collect_config", lambda: encode)
    monkeypatch.setattr(window._encode_panel, "is_pure_copy", lambda _: False)
    for page in (2, 3):
        window._stack.setCurrentIndex(page)
        assert window._run_btn.text() == "Remux & Encode"
        assert len([op for op in window._operation_states() if op.key == "media"]) == 1
    monkeypatch.setattr(window._encode_panel, "is_pure_copy", lambda _: True)
    monkeypatch.setattr(window._remux_panel, "collect_config", lambda: encode)
    window._encode_panel.operation_changed.emit()
    QTest.qWait(90)
    assert window._run_btn.text() == "Remux & Encode"


def test_incomplete_context_keeps_other_operations_accessible(window, tmp_path, monkeypatch):
    hybrid = ready_hybrid(window, tmp_path)
    monkeypatch.setattr(window._dovi_panel, "operation_state", lambda: OperationState("merge_dovi", 1, "Fusionner DoVi", "film.mkv", True))
    window._refresh_operation_bar()
    assert not window._action_bar.arrow_button.isHidden()
    hybrid.output.clear()
    QTest.qWait(90)
    assert not window._run_btn.isEnabled()
    assert not window._action_bar.arrow_button.isHidden()
    assert "sortie" in window._status_lbl.text()
    launch = Mock()
    monkeypatch.setattr(window._dovi_panel, "_on_run", launch)
    window._action_bar.open_menu()
    actions = [a for a in window._action_bar.menu.actions() if a.isEnabled() and not a.isSeparator()]
    assert len(actions) == 1
    actions[0].trigger()
    assert window._stack.currentIndex() == 1
    launch.assert_called_once()


def test_menu_keyboard_and_single_ready(qt_app):
    bar = OperationBar()
    bar.show()
    calls = []
    bar.launch_requested.connect(calls.append)
    operations = [OperationState("hybrid", 4, "Hybrider", "saison 1", True)]
    bar.set_operations(operations, "hybrid")
    assert bar.arrow_button.isHidden()
    bar.set_operations(operations, None)
    bar.run_button.click()
    action = next(a for a in bar.menu.actions() if a.isEnabled() and not a.isSeparator())
    bar.menu.setActiveAction(action)
    QTest.keyClick(bar.menu, Qt.Key.Key_Return)
    assert calls == ["hybrid"]
    bar.open_menu()
    action = next(a for a in bar.menu.actions() if a.isEnabled() and not a.isSeparator())
    QTest.mouseClick(bar.menu, Qt.MouseButton.LeftButton, pos=bar.menu.actionGeometry(action).center())
    assert calls == ["hybrid", "hybrid"]
    bar.close()


def test_failure_retry_and_stale_auxiliary_messages(window, tmp_path):
    panel = ready_hybrid(window, tmp_path)
    window._on_studio_op_state("hybrid", "started", "Analyse")
    panel.log_message.emit("WARN", "Ancien aperçu indisponible")
    assert window._running
    window._on_studio_op_state("hybrid", "failed", "Espace disque insuffisant")
    assert not window._running
    assert window._run_btn.text() == "Relancer l’hybridation"
    assert "Espace disque" in window._status_lbl.text()
    assert window._run_btn.isEnabled()
    panel.output.setText(str(tmp_path / "autre-sortie"))
    QTest.qWait(90)
    assert window._run_btn.text() == "Lancer l’hybridation"


def test_hdr_studio_correction_uses_the_global_operation(window, tmp_path, monkeypatch, qt_app):
    """HDR Studio : « Écrire le fichier corrigé » est l'opération de la barre d'exécution (page 7)."""
    from ui.operation_bar import OperationState

    studio = window._hdr_panel
    ready = OperationState("hdr", 7, "Écrire le fichier corrigé", "film.hdrfix.mkv ← film.mkv", True)
    monkeypatch.setattr(studio, "operation_state", lambda: ready)
    run, cancel = Mock(), Mock()
    monkeypatch.setattr(studio, "run", run)
    monkeypatch.setattr(studio, "cancel", cancel)
    window._stack.setCurrentIndex(7)
    window._refresh_operation_bar()
    assert window._action_bar._active == "hdr" and window._run_btn.text() == "Écrire le fichier corrigé"
    assert window._action_bar.heading.text() == "HDR STUDIO"
    window._run_btn.click()
    run.assert_called_once()
    studio.op_state_changed.emit("started", "Correction de film.mkv…")
    studio.op_state_changed.emit("step", "Édition du RPU Dolby Vision…")
    studio.op_progress_pct.emit(40)
    assert window._running and window._prog_bar.value() == 40
    assert window._prog_lbl.text().startswith("Édition du RPU Dolby Vision…")
    monkeypatch.setattr("ui.main_window.QMessageBox.question", lambda *_a, **_kw: QMessageBox.StandardButton.Yes)
    window._on_cancel_op()
    cancel.assert_called_once()
    entries: list[tuple[str, str]] = []
    monkeypatch.setattr(window, "_emit_log_entry", lambda message, level: entries.append((level.value, message)))
    studio.op_state_changed.emit("failed", "Correction impossible : disque plein")
    assert not window._running and window._run_btn.text() == "Relancer la correction"
    assert ("ERROR", "[HDR] Correction impossible : disque plein") in entries
    studio.log_message.emit("WARN", "Analyse annulée.")
    _wait_until(qt_app, lambda: ("WARN", "[HDR] Analyse annulée.") in entries)


@pytest.mark.parametrize("owner,page", [("hybrid", 4), ("merge_dovi", 1)])
def test_navigation_and_cancellation_keep_the_owner(window, tmp_path, monkeypatch, owner, page):
    ready_hybrid(window, tmp_path)
    window._on_studio_op_state(owner, "started", "Analyse en cours")
    window._stack.setCurrentIndex(5)
    assert window._running
    assert not window._run_btn.isEnabled()
    assert window._action_bar._active == owner
    assert not window._cancel_btn.isHidden()
    assert not window._log_panel.is_collapsed()
    panel = window._hybrid_panel if owner == "hybrid" else window._dovi_panel
    cancel = Mock()
    monkeypatch.setattr(panel, "cancel" if owner == "hybrid" else "_on_cancel", cancel)
    monkeypatch.setattr("ui.main_window.QMessageBox.question", lambda *_a, **_kw: QMessageBox.StandardButton.Yes)
    window._on_cancel_op()
    cancel.assert_called_once()
    assert window._running  # attendre la confirmation du worker
    window._on_studio_op_state(owner, "cancelled", "Annulé")
    assert not window._running
    assert window._run_btn.text() == "Choisir une opération"


def test_hybrid_cancel_prevents_the_next_episode_and_relays_logs(window, tmp_path, monkeypatch, qt_app):
    panel = ready_hybrid(window, tmp_path)
    signals = TaskSignals()
    started = []

    class Workflow(QObject):
        log_message = Signal(str, str)

        def __init__(self, **_kwargs):
            super().__init__()

        def run(self, config):
            started.append(config.output)
            self.log_message.emit("INFO", "Synchronisation physique de la piste française")
            return signals

    monkeypatch.setattr("core.workflows.remux.RemuxWorkflow", Workflow)
    for row in range(2):
        track = TrackEntry(0, "audio", "EAC3", "", "fra", "")
        source = tmp_path / f"donneur{row}.mkv"
        panel.jobs[row] = (RemuxConfig([SourceInput(source, 0, [track])], tmp_path / f"out{row}.mkv", [(0, 0)]), None)
        panel.matrix_episodes.append(MatrixEpisode(season=1, episode=row + 1, master_file=source, donor_files=[(None, source)]))
    panel._prepared_signature = panel._settings_signature()
    panel.run()
    assert window._running
    qt_app.processEvents()
    qt_app.processEvents()
    window._log_panel._flush_pending()
    assert "Synchronisation physique" in window._log_panel._text.toPlainText()
    assert "S01E01" in window._log_panel._text.toPlainText()
    panel.cancel()
    signals.finished.emit("Terminé")  # course : fin reçue juste après Annuler
    QTest.qWait(30)
    assert len(started) == 1
    assert not window._running
    assert not panel.busy


def test_journal_preserves_scroll_filter_history_and_bounds(qt_app):
    from core.i18n import translate_text
    panel = LogPanel(max_lines=100)
    panel.resize(900, 230)
    panel.show()
    for n in range(60):
        panel.log(f"[HYBRID] [S01E01] ligne {n}")
    panel._flush_pending()
    qt_app.processEvents()
    scroll = panel._text.verticalScrollBar()
    scroll.setValue(0)
    assert not panel._following
    panel.log("[DOVI] Injection RPU", LogLevel.OK)
    panel._flush_pending()
    assert scroll.value() == 0
    assert translate_text("↓ Reprendre le défilement") in panel._follow_btn.text()
    panel._filter.setCurrentIndex(panel._filter.findData("DOVI"))
    assert "Injection RPU" in panel._text.toPlainText()
    assert "S01E01" not in panel._text.toPlainText()
    panel._filter.setCurrentIndex(0)
    assert "S01E01" in panel._text.toPlainText()
    panel._follow_btn.click()
    assert panel._following
    for n in range(150):
        panel.log(f"[MEDIA] message {n}")
    panel._flush_pending()
    assert len(panel._entries) == 100
    assert panel._text.document().blockCount() <= 101
    panel.close()


def test_journal_badges_preserve_copy_and_filter_media_variants(qt_app):
    from PySide6.QtGui import QTextCursor
    from ui.widgets.log_panel import _BADGE_SOURCE

    panel = LogPanel()
    panel.resize(900, 240)
    panel.show()
    for source in ("DOVI", "ENCODE", "HYBRID", "REMUX"):
        panel.log(f"[{source}] Message {source}")
    panel._flush_pending()
    qt_app.processEvents()
    block = panel._text.document().firstBlock()
    sources = []
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            value = iterator.fragment().charFormat().property(_BADGE_SOURCE)
            if value:
                sources.append(value)
            iterator += 1
        block = block.next()
    assert sources == ["DOVI", "ENCODE", "HYBRID", "REMUX"]
    cursor = panel._text.textCursor()
    cursor.select(QTextCursor.SelectionType.Document)
    panel._text.setTextCursor(cursor)
    panel._text.copy()
    assert qt_app.clipboard().text() == panel._text.toPlainText()
    panel._filter.setCurrentIndex(panel._filter.findData("MEDIA"))
    text = panel._text.toPlainText()
    assert "Message ENCODE" in text and "Message REMUX" in text
    assert "HYBRID" not in text and "DOVI" not in text
    panel.close()


def test_journal_context_menu_persists_existing_startup_setting(window, tmp_path, monkeypatch):
    import core.config as config_module

    monkeypatch.setattr(config_module, "_INI_PATH", tmp_path / "config.ini")
    previous = window._config.startup_logs_expanded
    try:
        window._log_panel.set_startup_expanded(False)
        menu = window._log_panel._context_menu()
        action = menu.actions()[0]
        assert action.isCheckable() and not action.isChecked()
        action.trigger()
        assert window._config.startup_logs_expanded
        assert config_module.AppConfig().startup_logs_expanded
        assert window._settings_panel.widget_for("ui", "startup_logs_expanded").isChecked()
        window._apply_startup_log_panel_state()
        assert not window._log_panel.is_collapsed()
        menu.deleteLater()
    finally:
        window._config.startup_logs_expanded = previous


def test_merge_button_describes_detected_metadata(window):
    panel = window._dovi_panel
    section = panel._config_section
    section.set_hdr_states(HdrState(), HdrState(has_dovi=True))
    assert panel.operation_state().label == "Fusionner Dolby Vision"
    section.set_hdr_states(HdrState(), HdrState(has_hdr10plus=True))
    assert panel.operation_state().label == "Fusionner HDR10+"
    section.set_hdr_states(HdrState(), HdrState(has_dovi=True, has_hdr10plus=True))
    assert panel.operation_state().label == "Fusionner DoVi / HDR10+"
    combo = section._profile_combo
    combo.setCurrentIndex(combo.findData(DoviProfile.DISABLED))
    assert panel.operation_state().label == "Fusionner HDR10+"


def test_modal_sync_keeps_its_own_cancellation(window):
    window._on_remux_audio_sync_started({"label": "Analyse synchro"})
    window._refresh_operation_bar()
    assert window._running
    assert window._cancel_btn.isHidden()
    assert not window._run_btn.isEnabled()
    window._on_remux_audio_sync_finished(True, {})


def test_terminal_result_preserves_project_and_progress_context(window, tmp_path):
    ready_hybrid(window, tmp_path)
    title = window._workspace_title.text()
    window._on_studio_op_state("hybrid", "started", "Analyse")
    window._on_studio_op_state("hybrid", "finished", "Hybridation terminée")
    assert window._workspace_title.text() == title
    assert not window._prog_widget.isHidden()
    window._stack.setCurrentIndex(1)
    assert window._prog_widget.isHidden()
    window._stack.setCurrentIndex(4)
    assert not window._prog_widget.isHidden()


def test_merge_failure_keeps_retry_after_readiness_refresh(window, tmp_path):
    panel = window._dovi_panel
    panel._file_section._film1 = tmp_path / "film.mkv"
    panel._file_section._film2 = tmp_path / "donneur.mkv"
    window._stack.setCurrentIndex(1)
    panel._running = True
    panel.op_state_changed.emit("started", "Fusion")
    panel._on_workflow_failed(WorkflowStep.VALIDATION, "Métadonnées absentes")
    QTest.qWait(90)
    assert not window._running
    assert window._run_btn.text() == "Relancer la fusion"
    assert window._run_btn.isEnabled()
    assert "film.mkv" in window._workspace_title.text()


def test_hybrid_invalid_arguments_release_global_operation(window, tmp_path):
    panel = ready_hybrid(window, tmp_path)
    panel.controls["tag"].setText("--option-inconnue")
    window._run_btn.click()
    assert not window._running
    assert not panel._operation_active
    assert not panel.busy
    assert window._run_btn.text() == "Relancer l’hybridation"
    assert "--tag" in panel.status.text()


def test_hybrid_busy_disables_sources_and_recipe_actions(window, tmp_path):
    panel = ready_hybrid(window, tmp_path)
    panel._add_extra_source_row(str(tmp_path / "autre-donneur"))
    panel.set_busy(True)
    assert not panel.extra_sources[0]["widget"].isEnabled()
    assert not panel._sources_card.isEnabled()
    assert not panel._witness_section.body.isEnabled()
    assert not panel.listen_button.isEnabled()
    panel.set_busy(False)
    assert panel._sources_card.isEnabled()
    assert panel.extra_sources[0]["widget"].isEnabled()


def test_filtered_journal_expires_entries_from_other_sources(qt_app):
    panel = LogPanel(max_lines=2)
    panel._filter.setCurrentIndex(panel._filter.findData("HYBRID"))
    panel.log("[HYBRID] Ancienne analyse")
    panel._flush_pending()
    panel.log("[DOVI] Première étape")
    panel.log("[DOVI] Deuxième étape")
    panel._flush_pending()
    assert "Ancienne analyse" not in panel._text.toPlainText()
    panel._filter.setCurrentIndex(0)
    assert "Ancienne analyse" not in panel._text.toPlainText()
    assert "Deuxième étape" in panel._text.toPlainText()
    panel.close()


def test_application_logs_keep_their_source_during_hybrid_operation(window, tmp_path):
    ready_hybrid(window, tmp_path)
    window._on_studio_op_state("hybrid", "started", "Analyse")
    window.log_requested.emit("INFO", "Mise à jour des extensions disponible")
    window._hybrid_panel.log_message.emit("INFO", "Analyse du donneur")
    QTest.qWait(80)
    window._log_panel._flush_pending()
    entries = window._log_panel._entries
    assert any(source == "APP" and "extensions" in message for _, _, source, message in entries)
    assert any(source == "HYBRID" and "donneur" in message for _, _, source, message in entries)
    window._on_studio_op_state("hybrid", "finished", "Terminé")


def test_hybrid_preview_without_donor_audio_releases_operation(window, tmp_path, qt_app):
    panel = ready_hybrid(window, tmp_path)
    panel.player = None
    panel.table.setRowCount(1)
    panel.table.selectRow(0)
    panel.jobs[0] = (RemuxConfig(
        [SourceInput(tmp_path / "reference.mkv", 0, [TrackEntry(0, "video", "FFV1", "", "und", "")])],
        tmp_path / "out.mkv", [(0, 0)],
    ), None)
    panel.listen()
    deadline = time.monotonic() + 3
    while panel.busy and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert not panel.busy
    assert not window._running
    assert not panel._operation_active
    assert "Aucune piste audio donneuse" in panel.status.text()


def test_hybrid_run_without_a_complete_pair_reports_failure(window, tmp_path):
    panel = ready_hybrid(window, tmp_path)
    panel.matrix_episodes = [MatrixEpisode(season=1, episode=1, master_file=tmp_path / "reference.mkv")]
    panel._matrix_signature = panel._source_signature()
    states = []
    panel.op_state_changed.connect(lambda state, label: states.append(state))
    window._run_btn.click()
    assert states == ["started", "failed"]
    assert not window._running
    assert not panel.busy
    assert window._run_btn.text() == "Relancer l’hybridation"


def test_pages_have_one_scroll_owner_and_keep_global_controls_visible(window, qt_app):
    window.resize(1280, 720)
    panels = (window._dovi_panel, window._encode_panel, window._remux_panel,
              window._hybrid_panel, window._settings_panel, window._extensions_panel)
    for index, panel in enumerate(panels, start=1):
        window._stack.setCurrentIndex(index)
        qt_app.processEvents()
        parent = panel.parentWidget()
        while parent is not None:
            assert not isinstance(parent, QScrollArea), type(panel).__name__
            parent = parent.parentWidget()
        scroll = panel.findChildren(QScrollArea)[0]
        bar = scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        qt_app.processEvents()
        bottom = scroll.widget().mapTo(scroll.viewport(), QPoint(0, scroll.widget().height())).y()
        assert bottom <= scroll.viewport().height() + 1, type(panel).__name__
        assert window.rect().contains(window._run_btn.mapTo(window, window._run_btn.rect().center()))
