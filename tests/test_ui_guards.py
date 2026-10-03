"""
tests/test_ui_guards.py — Confirmations, erreurs visibles et exclusivité preview / opération.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMainWindow, QMessageBox

from ui.confirm import confirm_close_while_running, confirm_overwrite
from ui.main_window import MainWindow
from ui.panels.encode_panel.panel import EncodePanel
from ui.panels.remux_panel.panel import RemuxPanel
from ui.panels.merge_dovi_panel import MergeDoviPanel
from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings, VideoResizeSettings
import pytest

_YES = QMessageBox.StandardButton.Yes
_NO = QMessageBox.StandardButton.No


# ---------------------------------------------------------------------------
# ui/confirm.py
# ---------------------------------------------------------------------------

def test_confirm_overwrite_skips_dialog_when_output_is_new(qt_app, tmp_path: Path) -> None:
    with patch("ui.confirm.QMessageBox.question") as question:
        assert confirm_overwrite(None, tmp_path / "new.mkv") is True
    question.assert_not_called()


def test_confirm_overwrite_defaults_to_keeping_existing_file(qt_app, tmp_path: Path) -> None:
    existing = tmp_path / "film.mkv"
    existing.write_bytes(b"x")
    with patch("ui.confirm.QMessageBox.question", return_value=_NO) as question:
        assert confirm_overwrite(None, existing) is False
    assert question.call_args.args[-1] == _NO  # bouton par défaut : ne pas remplacer
    with patch("ui.confirm.QMessageBox.question", return_value=_YES):
        assert confirm_overwrite(None, existing) is True


def test_confirm_close_while_running_mentions_elapsed_time(qt_app) -> None:
    with patch("ui.confirm.QMessageBox.warning", return_value=_NO) as warning:
        assert confirm_close_while_running(None, 7260.0) is False
    assert "121" in warning.call_args.args[2]


# ---------------------------------------------------------------------------
# MainWindow : fermeture pendant une opération
# ---------------------------------------------------------------------------

class _Window(MainWindow):
    def __init__(self) -> None:
        QMainWindow.__init__(self)
        self._update_request_id = 0
        self._update_download_cancel = threading.Event()
        self._config = MagicMock()
        self._signals = None
        self._prep_progress_timer = MagicMock()
        self._op_encode_multi_reselect_timer = MagicMock()
        self._verbose_file_logger = MagicMock()
        self._op_start = time.monotonic() - 3600


def test_close_during_operation_can_be_refused(qt_app) -> None:
    window = _Window()
    window._running = True
    try:
        with patch("ui.main_window.confirm_close_while_running", return_value=False) as confirm:
            event = QCloseEvent()
            window.closeEvent(event)
        confirm.assert_called_once()
        assert not event.isAccepted()
        assert not hasattr(window, "_shutdown")  # rien n'a été annulé
        assert window.isEnabled()
    finally:
        window._running = False
        window.deleteLater()


def test_close_when_idle_does_not_ask(qt_app) -> None:
    window = _Window()
    try:
        with patch("ui.main_window.confirm_close_while_running") as confirm:
            window.closeEvent(QCloseEvent())
        confirm.assert_not_called()
        assert hasattr(window, "_shutdown")
    finally:
        window.deleteLater()


# ---------------------------------------------------------------------------
# MainWindow : erreurs bloquantes visibles, état « en cours » propagé
# ---------------------------------------------------------------------------

def _fake_window() -> Any:
    window = SimpleNamespace(
        log_requested=MagicMock(),
        _log_panel=MagicMock(),
        _status_lbl=MagicMock(),
        _on_log_collapsed=MagicMock(),
    )
    window._log_panel.is_collapsed.return_value = True
    window._expand_log_panel = MethodType(MainWindow._expand_log_panel, window)
    window._report_blocking_errors = MethodType(MainWindow._report_blocking_errors, window)
    return window


def test_blocking_errors_are_shown_not_only_logged(qt_app) -> None:
    window = _fake_window()
    errors = [f"erreur {i}" for i in range(10)]
    with patch("ui.main_window.QMessageBox.warning") as warning:
        window._report_blocking_errors(errors)
    assert window.log_requested.emit.call_count == 10
    window._log_panel.set_collapsed.assert_called_once_with(False)
    window._on_log_collapsed.assert_called_once_with(False)
    shown = warning.call_args.args[2]
    assert "erreur 0" in shown and "erreur 9" not in shown and "2" in shown


def test_running_state_is_propagated_to_encode_panel(qt_app) -> None:
    window = _Window()
    try:
        window._encode_panel = cast(Any, MagicMock())
        window._running = True
        window._encode_panel.set_operation_running.assert_called_with(True)
        window._running = False
        window._encode_panel.set_operation_running.assert_called_with(False)
    finally:
        window.deleteLater()


def test_encode_panel_blocks_preview_during_operation(qt_app) -> None:
    panel = SimpleNamespace(
        _preview_generate_btn=MagicMock(),
        _preview_signals=None,
        _preview_status=MagicMock(),
        _current_preview_config=MagicMock(),
    )
    set_running = MethodType(EncodePanel.set_operation_running, panel)
    generate = MethodType(EncodePanel._on_generate_preview, panel)

    set_running(True)
    panel._preview_generate_btn.setEnabled.assert_called_with(False)
    generate()
    panel._current_preview_config.assert_not_called()

    set_running(False)
    panel._preview_generate_btn.setEnabled.assert_called_with(True)


# ---------------------------------------------------------------------------
# Preview : pas de NFO, sans toucher à l'état partagé du workflow
# ---------------------------------------------------------------------------

def test_preview_config_disables_nfo_without_mutating_workflow(tmp_path: Path) -> None:
    from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings
    from core.workflows.encode.workflow import EncodeWorkflow

    wf = EncodeWorkflow(generate_nfo=True)
    config = EncodeConfig(
        source=tmp_path / "src.mkv",
        output=tmp_path / "out.mkv",
        video=VideoEncodeSettings(codec="libx265"),
    )
    preview = wf._preview_encode_config(
        config,
        source_segment=tmp_path / "seg.mkv",
        output=tmp_path / "previews" / "clip.mkv",
        clip_duration_s=5.0,
    )
    assert preview.write_nfo is False
    assert preview.allow_validation_override is False
    assert config.write_nfo is True
    assert wf._generate_nfo is True


def test_preview_blocks_merge_dovi_start(qt_app):
    window = SimpleNamespace(
        _running=False, _encode_panel=SimpleNamespace(is_preview_running=lambda: True),
        _report_blocking_errors=MagicMock(),
    )
    panel = SimpleNamespace(_running=False, _operation_start_guard=lambda: MainWindow._can_start_operation(window))
    MergeDoviPanel._on_run(panel)
    window._report_blocking_errors.assert_called_once()


@pytest.mark.parametrize("handler", ["_on_extract_track", "_on_audio_sync_requested", "_on_subtitle_sync_requested", "_on_sync_studio_requested"])
def test_operation_guard_blocks_remux_auxiliary_work(qt_app, handler):
    panel = SimpleNamespace(_operation_start_guard=lambda: False)
    panel._can_start_auxiliary_operation = MethodType(RemuxPanel._can_start_auxiliary_operation, panel)
    getattr(RemuxPanel, handler)(panel, None)


def test_copy_with_resize_is_routed_through_encode_validation(qt_app, tmp_path):
    video = VideoEncodeSettings(codec="copy", resize=VideoResizeSettings(enabled=True))
    cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video)
    panel = SimpleNamespace(
        _routing_video_tracks=lambda _c: [video],
        _video_requires_dovi_profile_normalization=lambda _v: False,
    )
    assert EncodePanel.is_pure_copy(panel, cfg) is False
