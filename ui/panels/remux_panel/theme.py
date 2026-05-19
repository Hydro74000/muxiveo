"""ui/panels/remux_panel/theme.py — helpers de style pour RemuxPanel."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QWidget

from ui.design_system import colors as _C, font_px as _font_px, scale as _scale


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"""
        color: {_C.TEXT_DIM};
        font-size: {_font_px(9)}px;
        font-weight: 700;
        letter-spacing: {_scale(2)}px;
        background: transparent;
    """)
    return lbl


def _card(parent: QWidget | None = None) -> QWidget:
    w = QWidget(parent)
    w.setStyleSheet(f"""
        QWidget {{
            background: {_C.BG_CARD};
            border: 1px solid {_C.BORDER};
            border-radius: 6px;
        }}
    """)
    return w


def _primary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(36))
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {_C.ACCENT};
            color: #ffffff;
            border: none;
            border-radius: 6px;
            font-size: {_font_px(12)}px;
            font-weight: 700;
            padding: 0 {_scale(20)}px;
        }}
        QPushButton:hover  {{ background: #6070f0; }}
        QPushButton:pressed {{ background: #3a52c0; }}
        QPushButton:disabled {{
            background: {_C.BG_ACTIVE};
            color: {_C.TEXT_DIM};
        }}
    """)
    return btn


def _secondary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(28))
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_SEC};
            border: 1px solid {_C.BORDER};
            border-radius: 5px;
            font-size: {_font_px(11)}px;
            font-weight: 500;
            padding: 0 {_scale(12)}px;
        }}
        QPushButton:hover {{
            background: {_C.BG_HOVER};
            color: {_C.TEXT_PRI};
            border-color: {_C.BORDER_LT};
        }}
        QPushButton:pressed {{ background: {_C.BG_ACTIVE}; }}
    """)
    return btn


def _separator() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFixedHeight(_scale(1))
    sep.setStyleSheet(f"background: {_C.BORDER}; border: none;")
    return sep


def _input_style() -> str:
    return f"""
        QLineEdit {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_PRI};
            border: 1px solid {_C.BORDER};
            border-radius: 5px;
            font-size: {_font_px(12)}px;
            font-family: 'JetBrains Mono', monospace;
            padding: {_scale(6)}px {_scale(10)}px;
        }}
        QLineEdit:focus {{
            border-color: {_C.ACCENT};
        }}
        QLineEdit::placeholder {{
            color: {_C.TEXT_DIM};
        }}
    """


def _checkbox_style() -> str:
    return f"""
        QCheckBox {{
            color: {_C.TEXT_SEC};
            font-size: {_font_px(12)}px;
            spacing: {_scale(8)}px;
            background: transparent;
        }}
        QCheckBox::indicator {{
            width: {_scale(14)}px;
            height: {_scale(14)}px;
            border-radius: 3px;
            border: 1px solid {_C.BORDER_LT};
            background: {_C.BG_DEEP};
        }}
        QCheckBox::indicator:checked {{
            background: {_C.ACCENT};
            border-color: {_C.ACCENT};
        }}
        QCheckBox:hover {{ color: {_C.TEXT_PRI}; }}
    """


def _pencil_icon(color: str | None = None, size: int = 14) -> QIcon:
    color = color or _C.TEXT_SEC
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
        f' fill="none" stroke="{color}" stroke-width="2.2"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>'
        '</svg>'
    )
    renderer = QSvgRenderer(svg.encode())
    icon_size = _scale(size)
    pix = QPixmap(icon_size, icon_size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    renderer.render(painter)
    painter.end()
    return QIcon(pix)


def _refresh_icon(color: str | None = None, size: int = 14) -> QIcon:
    color = color or _C.TEXT_SEC
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
        f' fill="none" stroke="{color}" stroke-width="2.2"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M21 12a9 9 0 0 0-15.5-6.3L3 8"/>'
        '<path d="M3 3v5h5"/>'
        '<path d="M3 12a9 9 0 0 0 15.5 6.3L21 16"/>'
        '<path d="M21 21v-5h-5"/>'
        '</svg>'
    )
    renderer = QSvgRenderer(svg.encode())
    icon_size = _scale(size)
    pix = QPixmap(icon_size, icon_size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    renderer.render(painter)
    painter.end()
    return QIcon(pix)


def _x_icon(color: str | None = None, size: int = 14) -> QIcon:
    color = color or _C.ERROR
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
        f' fill="none" stroke="{color}" stroke-width="2.5"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M18 6 6 18"/>'
        '<path d="m6 6 12 12"/>'
        '</svg>'
    )
    renderer = QSvgRenderer(svg.encode())
    icon_size = _scale(size)
    pix = QPixmap(icon_size, icon_size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    renderer.render(painter)
    painter.end()
    return QIcon(pix)


def _warning_icon(color: str | None = None, size: int = 14) -> QIcon:
    color = color or "#f0b429"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
        f' fill="none" stroke="{color}" stroke-width="2.2"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'
        '<path d="M12 9v4"/>'
        '<path d="M12 17h.01"/>'
        '</svg>'
    )
    renderer = QSvgRenderer(svg.encode())
    icon_size = _scale(size)
    pix = QPixmap(icon_size, icon_size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    renderer.render(painter)
    painter.end()
    return QIcon(pix)


__all__ = [
    "_C",
    "_card",
    "_checkbox_style",
    "_input_style",
    "_pencil_icon",
    "_primary_button",
    "_refresh_icon",
    "_secondary_button",
    "_section_label",
    "_separator",
    "_warning_icon",
    "_x_icon",
]
