"""
tests/test_main_window_startup_panel.py — Mapping startup panel -> index stack.
"""

from ui.main_window import MainWindow


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
