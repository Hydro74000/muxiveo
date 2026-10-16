"""Contrats de la barre commune, du propriétaire d'exécution et du journal."""
from __future__ import annotations

import time
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
from ui.widgets.log_panel import LogPanel


@pytest.fixture
def window(qt_app, monkeypatch):
    monkeypatch.setattr(DashboardPage, "_start_hw_detection", lambda self: None)
    monkeypatch.setattr(DashboardPage, "start_accel_detection", lambda self: None)
    monkeypatch.setattr(MainWindow, "_wire_extensions", lambda self: None)
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
    panel._on_waveform_failed("Ancien aperçu indisponible")
    assert window._running
    window._on_studio_op_state("hybrid", "failed", "Espace disque insuffisant")
    assert not window._running
    assert window._run_btn.text() == "Relancer l’hybridation"
    assert "Espace disque" in window._status_lbl.text()
    assert window._run_btn.isEnabled()
    panel.output.setText(str(tmp_path / "autre-sortie"))
    QTest.qWait(90)
    assert window._run_btn.text() == "Lancer l’hybridation"


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
    assert "Reprendre" in panel._follow_btn.text()
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
