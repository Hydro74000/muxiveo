"""Non-régression des zones de saisie numériques avec les styles Qt."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox, QWidget

from ui.design_system import DesignSystem


@pytest.fixture
def styled_app(qt_app):
    previous_theme = DesignSystem.current_theme()
    previous_scale = DesignSystem.current_ui_scale()
    previous_stylesheet = qt_app.styleSheet()
    previous_palette = qt_app.palette()
    DesignSystem.apply_to_application(qt_app)
    yield qt_app
    DesignSystem.set_theme(previous_theme)
    DesignSystem.set_ui_scale(previous_scale)
    qt_app.setStyleSheet(previous_stylesheet)
    qt_app.setPalette(previous_palette)


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("percent", [100, 150, 200])
@pytest.mark.parametrize("value", [18, 26])
def test_quality_value_remains_visible_and_editable(styled_app, theme, percent, value):
    from ui.panels.encode_panel.panel import EncodePanel

    DesignSystem.set_theme(theme)
    DesignSystem.set_ui_scale(percent)
    DesignSystem.apply_to_application(styled_app)
    spin = EncodePanel._make_quality_spin(value)
    spin.show()
    styled_app.processEvents()
    try:
        editor = spin.lineEdit()
        assert editor.geometry().left() >= 7
        assert editor.width() >= spin.fontMetrics().horizontalAdvance("51") + 4
        assert editor.alignment() == Qt.AlignmentFlag.AlignCenter

        editor.selectAll()
        QTest.keyClicks(editor, "51")
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        assert spin.value() == 51
        assert editor.text() == "51"
        assert editor.width() >= spin.fontMetrics().horizontalAdvance("51") + 4
    finally:
        spin.close()
        spin.deleteLater()


@pytest.mark.parametrize("spin_type", [QSpinBox, QDoubleSpinBox])
@pytest.mark.parametrize("buttons", [True, False])
@pytest.mark.parametrize("direction", [Qt.LayoutDirection.LeftToRight, Qt.LayoutDirection.RightToLeft])
def test_styled_spinbox_preserves_padding_after_geometry_and_style_changes(
    styled_app, spin_type, buttons, direction,
):
    host = QWidget()
    spin = spin_type(host)
    spin.setLayoutDirection(direction)
    spin.setValue(18)
    if not buttons:
        spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
    host.show()
    try:
        for width, horizontal, vertical in ((100, 6, 3), (140, 10, 5)):
            # Style hérité, puis modifié après l'affichage du champ.
            host.setStyleSheet(
                f"QAbstractSpinBox {{ border:1px solid; padding:{vertical}px {horizontal}px; }}"
            )
            spin.resize(width, 40)
            styled_app.processEvents()
            editor = spin.lineEdit()
            contents = QRect(horizontal + 1, vertical + 1, width - 2 * (horizontal + 1), 40 - 2 * (vertical + 1))
            assert contents.contains(editor.geometry())
            if not buttons:
                assert editor.geometry() == contents
            assert editor.width() >= spin.fontMetrics().horizontalAdvance(spin.text())
    finally:
        host.close()
        host.deleteLater()
