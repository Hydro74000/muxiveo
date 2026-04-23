"""
tests/test_main_window_startup_panel.py — Mapping startup panel -> index stack.
"""

from types import MethodType
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from ui.main_window import LogLevel, LogPanel, MainWindow


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


def test_open_startup_paths_routes_files_to_container_page(tmp_path, qt_app) -> None:
    media_path = tmp_path / "movie.mkv"
    media_path.write_text("", encoding="utf-8")
    fake_window = SimpleNamespace(
        _PAGE_INDEX_BY_PANEL_KEY=MainWindow._PAGE_INDEX_BY_PANEL_KEY,
        _stack=SimpleNamespace(setCurrentIndex=MagicMock()),
        _sidebar=SimpleNamespace(select_page=MagicMock()),
        _remux_panel=SimpleNamespace(add_sources=MagicMock()),
    )

    MainWindow.open_startup_paths(fake_window, [media_path, Path(tmp_path / "missing.mp4")])

    fake_window._stack.setCurrentIndex.assert_called_once_with(3)
    fake_window._sidebar.select_page.assert_called_once_with(3)
    fake_window._remux_panel.add_sources.assert_called_once()
    routed_paths = fake_window._remux_panel.add_sources.call_args.args[0]
    assert routed_paths == [media_path]


def test_emit_log_entry_writes_verbose_file_when_enabled(tmp_path) -> None:
    fake_window = SimpleNamespace(
        _config=SimpleNamespace(
            verbose_file_logging=True,
            app_data_dir=tmp_path,
            verbose_log_dir=tmp_path / "chosen_logs",
        ),
        _log_panel=SimpleNamespace(log=MagicMock()),
        _verbose_log_file_path=None,
        _verbose_log_session_stamp=None,
        _verbose_log_file_index=1,
        _verbose_log_file_error_reported=False,
    )
    fake_window._verbose_log_part_path = MethodType(MainWindow._verbose_log_part_path, fake_window)
    fake_window._verbose_log_session_path = MethodType(MainWindow._verbose_log_session_path, fake_window)
    fake_window._prepare_verbose_log_target = MethodType(MainWindow._prepare_verbose_log_target, fake_window)
    fake_window._append_verbose_log_file = MethodType(MainWindow._append_verbose_log_file, fake_window)
    fake_window._emit_log_entry = MethodType(MainWindow._emit_log_entry, fake_window)

    fake_window._emit_log_entry("Bonjour", LogLevel.INFO)

    fake_window._log_panel.log.assert_called_once_with("Bonjour", LogLevel.INFO)
    log_files = sorted((tmp_path / "chosen_logs").glob("mediarecode-verbose-*.log"))
    assert len(log_files) == 1
    assert "[INFO] Bonjour" in log_files[0].read_text(encoding="utf-8")


def test_emit_log_entry_skips_file_when_verbose_logging_disabled(tmp_path) -> None:
    fake_window = SimpleNamespace(
        _config=SimpleNamespace(
            verbose_file_logging=False,
            app_data_dir=tmp_path,
            verbose_log_dir=tmp_path / "chosen_logs",
        ),
        _log_panel=SimpleNamespace(log=MagicMock()),
        _verbose_log_file_path=None,
        _verbose_log_session_stamp=None,
        _verbose_log_file_index=1,
        _verbose_log_file_error_reported=False,
    )
    fake_window._verbose_log_part_path = MethodType(MainWindow._verbose_log_part_path, fake_window)
    fake_window._verbose_log_session_path = MethodType(MainWindow._verbose_log_session_path, fake_window)
    fake_window._prepare_verbose_log_target = MethodType(MainWindow._prepare_verbose_log_target, fake_window)
    fake_window._append_verbose_log_file = MethodType(MainWindow._append_verbose_log_file, fake_window)
    fake_window._emit_log_entry = MethodType(MainWindow._emit_log_entry, fake_window)

    fake_window._emit_log_entry("Bonjour", LogLevel.INFO)

    fake_window._log_panel.log.assert_called_once_with("Bonjour", LogLevel.INFO)
    assert not (tmp_path / "chosen_logs").exists()


def test_verbose_log_rotation_rolls_and_caps_at_three_files(tmp_path) -> None:
    fake_window = SimpleNamespace(
        _config=SimpleNamespace(
            verbose_file_logging=True,
            app_data_dir=tmp_path,
            verbose_log_dir=tmp_path / "chosen_logs",
        ),
        _log_panel=SimpleNamespace(log=MagicMock()),
        _verbose_log_file_path=None,
        _verbose_log_session_stamp="20260423-181000",
        _verbose_log_file_index=1,
        _verbose_log_file_error_reported=False,
    )
    fake_window._verbose_log_part_path = MethodType(MainWindow._verbose_log_part_path, fake_window)
    fake_window._verbose_log_session_path = MethodType(MainWindow._verbose_log_session_path, fake_window)
    fake_window._prepare_verbose_log_target = MethodType(MainWindow._prepare_verbose_log_target, fake_window)
    fake_window._append_verbose_log_file = MethodType(MainWindow._append_verbose_log_file, fake_window)

    with patch("ui.main_window._VERBOSE_LOG_MAX_BYTES", 40):
        fake_window._append_verbose_log_file("A" * 20, LogLevel.INFO)
        fake_window._append_verbose_log_file("B" * 20, LogLevel.INFO)
        fake_window._append_verbose_log_file("C" * 20, LogLevel.INFO)
        fake_window._append_verbose_log_file("D" * 20, LogLevel.INFO)

    log_files = sorted((tmp_path / "chosen_logs").glob("mediarecode-verbose-20260423-181000-*.log"))
    assert [path.name for path in log_files] == [
        "mediarecode-verbose-20260423-181000-01.log",
        "mediarecode-verbose-20260423-181000-02.log",
        "mediarecode-verbose-20260423-181000-03.log",
    ]
    assert "D" * 20 in (tmp_path / "chosen_logs" / "mediarecode-verbose-20260423-181000-01.log").read_text(encoding="utf-8")
    assert "B" * 20 in (tmp_path / "chosen_logs" / "mediarecode-verbose-20260423-181000-02.log").read_text(encoding="utf-8")
    assert "C" * 20 in (tmp_path / "chosen_logs" / "mediarecode-verbose-20260423-181000-03.log").read_text(encoding="utf-8")
