"""
tests/test_main_window_sidebar.py — Tests ciblés du comportement compact/full de la sidebar.
"""

from ui.design_system import DesignSystem
from ui.main_window import _Sidebar


def test_sidebar_starts_expanded_by_default(qt_app) -> None:
    DesignSystem.set_ui_scale(100)
    sidebar = _Sidebar(compact=False)
    assert sidebar.is_compact() is False
    assert sidebar.width() == sidebar._FULL_WIDTH
    assert sidebar._toggle_btn.text() == "◀"


def test_sidebar_toggle_switches_compact_and_back(qt_app) -> None:
    DesignSystem.set_ui_scale(100)
    sidebar = _Sidebar(compact=False)

    sidebar.toggle_compact()
    assert sidebar.is_compact() is True
    assert sidebar.width() == sidebar._COMPACT_WIDTH
    assert sidebar._toggle_btn.text() == "▶"

    sidebar.toggle_compact()
    assert sidebar.is_compact() is False
    assert sidebar.width() == sidebar._FULL_WIDTH
    assert sidebar._toggle_btn.text() == "◀"
