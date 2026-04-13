"""
tests/test_main_window_startup_panel.py — Mapping startup panel -> index stack.
"""

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
