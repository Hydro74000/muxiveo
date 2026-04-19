"""
tests/test_main_window_startup_panel.py — Mapping startup panel -> index stack.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QMessageBox

from ui.main_window import LogPanel, MainWindow


def test_startup_page_index_mapping() -> None:
    assert MainWindow.startup_page_index("dashboard") == 0
    assert MainWindow.startup_page_index("dovi") == 1
    assert MainWindow.startup_page_index("encoding") == 2
    assert MainWindow.startup_page_index("container") == 3
    assert MainWindow.startup_page_index("settings") == 4


def test_startup_page_index_fallback_dashboard() -> None:
    assert MainWindow.startup_page_index("unknown") == 0
    assert MainWindow.startup_page_index("") == 0
    assert MainWindow.startup_page_index(None) == 0


def test_log_panel_starts_expanded_by_default(qt_app) -> None:
    panel = LogPanel()
    assert panel.is_collapsed() is False


def test_log_panel_can_be_collapsed_and_reexpanded(qt_app) -> None:
    panel = LogPanel()
    panel.set_collapsed(True)
    assert panel.is_collapsed() is True
    panel.set_collapsed(False)
    assert panel.is_collapsed() is False


def test_scale_change_prompt_can_restart_application(qt_app) -> None:
    fake_window = SimpleNamespace(
        _config=SimpleNamespace(restart_application=MagicMock(return_value=True)),
    )
    fake_app = SimpleNamespace(quit=MagicMock())

    with patch("ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
         patch("ui.main_window.QApplication.instance", return_value=fake_app):
        MainWindow._prompt_restart_for_scale_change(fake_window, 125)

    fake_window._config.restart_application.assert_called_once_with()
    fake_app.quit.assert_called_once_with()


def test_scale_change_prompt_shows_warning_when_restart_fails(qt_app) -> None:
    fake_window = SimpleNamespace(
        _config=SimpleNamespace(restart_application=MagicMock(return_value=False)),
    )

    with patch("ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
         patch("ui.main_window.QMessageBox.warning") as mock_warning:
        MainWindow._prompt_restart_for_scale_change(fake_window, 150)

    fake_window._config.restart_application.assert_called_once_with()
    mock_warning.assert_called_once()
