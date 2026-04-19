"""
tests/test_design_system.py — Vérifie les helpers de scaling globaux.
"""

import pytest

from ui.design_system import DesignSystem


@pytest.fixture(autouse=True)
def _reset_ui_scale():
    previous = DesignSystem.current_ui_scale()
    DesignSystem.set_ui_scale(100)
    yield
    DesignSystem.set_ui_scale(previous)


def test_design_system_scale_helpers_follow_ui_scale():
    DesignSystem.set_ui_scale(125)

    assert DesignSystem.current_ui_scale() == 125
    assert DesignSystem.scale_factor() == 1.25
    assert DesignSystem.scale(16) == 20
    assert DesignSystem.font_px(12) == 15
    assert DesignSystem.size(24, 32) == (30, 40)
    assert DesignSystem.spacing(8) == 10


def test_design_system_scale_is_clamped():
    assert DesignSystem.set_ui_scale(10) == 75
    assert DesignSystem.set_ui_scale(500) == 150
    assert DesignSystem.current_ui_scale() == 150
