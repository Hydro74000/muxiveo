"""Extensions et traitements dans la fenêtre principale réelle : exclusion réciproque (audit B01)."""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QMessageBox

from core import plugins
from core.config import AppConfig
from ui.main_window import DashboardPage, MainWindow
from ui.panels.encode_panel.panel import EncodePanel
from ui.plugin_controller import NvenccController


@pytest.fixture
def window(qt_app, monkeypatch):
    monkeypatch.setattr(DashboardPage, "_start_hw_detection", lambda self: None)
    monkeypatch.setattr(DashboardPage, "start_accel_detection", lambda self: None)
    monkeypatch.setattr(MainWindow, "_wire_extensions", lambda self: None)
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    config = AppConfig()
    config.check_updates = False
    config.language = "fra"
    win = MainWindow(config)
    yield win
    win._running = False
    win.close()
    win.deleteLater()


@pytest.fixture
def no_dialogs(monkeypatch):
    info = Mock()
    monkeypatch.setattr(QMessageBox, "information", info)
    monkeypatch.setattr(QMessageBox, "question", Mock(return_value=QMessageBox.StandardButton.Yes))
    return info


def _until(qt_app, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert predicate()


@pytest.mark.parametrize("usage", ["operation", "preview", "hdr_studio"])
def test_extension_changes_refused_while_tools_in_use(window, no_dialogs, monkeypatch, usage):
    if usage == "operation":
        window._running = True
    elif usage == "preview":
        monkeypatch.setattr(window._encode_panel, "is_preview_running", lambda: True)
    else:
        monkeypatch.setattr(window._hdr_panel, "tools_in_use", lambda: True)
    calls = Mock(return_value=True)
    monkeypatch.setattr(window._nvencc_controller, "remove", calls)
    monkeypatch.setattr(window._rife_controller, "remove", calls)
    monkeypatch.setattr(window._rife_controller, "update", calls)
    monkeypatch.setattr(window._fel_controller, "remove", calls)
    window._on_extension_remove_requested(NvenccController.plugin_id)
    window._on_extension_remove_requested(window._rife_controller.spec.id)
    window._on_extension_remove_requested(window._fel_controller.spec.id)
    window._mutate_extension(window._rife_controller.spec.id, window._rife_controller.update)
    calls.assert_not_called()
    assert no_dialogs.call_count == 4
    assert not window._tool_admission.mutation_active()


def test_operation_started_during_confirmation_is_detected(window, no_dialogs, monkeypatch):
    calls = Mock(return_value=True)
    monkeypatch.setattr(window._nvencc_controller, "remove", calls)

    def answer(*_args, **_kwargs):
        window._running = True  # un traitement démarre pendant la question
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", answer)
    window._on_extension_remove_requested(NvenccController.plugin_id)
    calls.assert_not_called()


def test_operations_refused_while_extension_changes(window, no_dialogs, monkeypatch):
    blocking = Mock()
    monkeypatch.setattr(window, "_report_blocking_errors", blocking)
    assert window._tool_admission.acquire_mutation("mvo-rife")
    assert window._can_start_operation() is False
    blocking.assert_called_once()
    assert window._encode_panel._preview_guard() is False
    assert window._hdr_panel._extensions_admitted() is False
    window._tool_admission.release_mutation("mvo-rife")
    assert window._can_start_operation() is True


@pytest.mark.parametrize("failure", [None, PermissionError("fichier utilisé")])
def test_real_removal_always_releases_admission(window, qt_app, no_dialogs, monkeypatch, failure):
    ctrl = window._rife_controller

    def remove(_spec):
        if failure is not None:
            raise failure
        return True

    monkeypatch.setattr(plugins, "remove", remove)
    monkeypatch.setattr(ctrl, "_changed", lambda _installed: None)
    window._on_extension_remove_requested(ctrl.spec.id)
    assert window._tool_admission.mutation_active()
    _until(qt_app, lambda: not window._tool_admission.mutation_active())
    assert ctrl.state().busy == ""
    assert window._can_start_operation() is True


def test_auto_update_deferred_until_operation_ends(window, qt_app, monkeypatch):
    ctrl = window._fel_controller
    started = Mock(return_value=True)
    monkeypatch.setattr(ctrl, "update", started)
    window._running = True
    window._defer_auto_update(ctrl)
    started.assert_not_called()
    window._running = False  # fin d'usage : relance immédiate
    started.assert_called_once()


def test_extension_buttons_greyed_while_tools_in_use(window):
    card = window._extensions_panel._rife_card
    card.sync_buttons(window._rife_controller.state(), offer_install=True, install_enabled=True)
    window._running = True
    assert not card.install_btn.isEnabled() and card.install_btn.toolTip()
    window._running = False
    assert card.install_btn.isEnabled() and not card.install_btn.toolTip()


def test_tool_changes_applied_after_operation(window, qt_app):
    panel = window._encode_panel
    workflow = panel._workflow
    panel.set_operation_running(True)
    before_nvencc = workflow._nvencc_bin
    window._config.tool_nvencc = "/audit/new/NVEncC"
    panel.refresh_nvencc_tool()
    panel.set_trt_plugin("/audit/trt")
    assert workflow._nvencc_bin == before_nvencc  # le traitement garde ses outils
    panel.set_operation_running(False)
    _until(qt_app, lambda: workflow._nvencc_bin == "/audit/new/NVEncC")
    assert panel._trt_plugin_dir == "/audit/trt"


def test_hdr_studio_write_refused_while_extension_changes(window, no_dialogs, tmp_path, monkeypatch):
    from core.hdr_inspection.patch import PatchPlan, StaticEdit

    asked = Mock()
    monkeypatch.setattr("ui.panels.hdr_studio.panel.confirm_output", asked)
    assert window._tool_admission.acquire_mutation("mvo-fel")
    window._hdr_panel.write_patch(PatchPlan(tmp_path / "film.mkv", 0, tmp_path / "out.mkv",
                                            static=StaticEdit(1000, 400)))
    asked.assert_not_called()  # refusé avant toute question ni écriture
    assert not window._hdr_panel.is_busy()
    window._tool_admission.release_mutation("mvo-fel")
