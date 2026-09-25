"""ui/panels/remux_panel/theme.py — helpers de style pour RemuxPanel."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QWidget

from ui.design_system import (
    CHECK_ICON_PATH,
    colors as _C,
    font_px as _font_px,
    scale as _scale,
)


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
            padding: 0 {_scale(16)}px;
            text-align: center;
        }}
        QPushButton:hover  {{ background: #6070f0; }}
        QPushButton:pressed {{ background: #3a52c0; }}
        QPushButton:disabled {{
            background: {_C.BG_ACTIVE};
            color: {_C.TEXT_DIM};
        }}
    """)
    return btn


def _secondary_button(
    text: str,
    fixed_width: int | None = None,
    *,
    min_width: int | None = None,
    padding_h: int | None = None,
) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(28))

    is_symbol = len(text.strip()) <= 2
    if padding_h is not None:
        pad_px = _scale(padding_h)
    elif is_symbol:
        pad_px = _scale(2)
    elif fixed_width is not None and fixed_width <= 50:
        pad_px = _scale(4)
    else:
        pad_px = _scale(8)

    if fixed_width is not None:
        btn.setFixedWidth(_scale(fixed_width))
    if min_width is not None:
        btn.setMinimumWidth(_scale(min_width))

    btn.setStyleSheet(f"""
        QPushButton {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_SEC};
            border: 1px solid {_C.BORDER};
            border-radius: 5px;
            font-size: {_font_px(11)}px;
            font-weight: 500;
            padding: 0 {pad_px}px;
            text-align: center;
        }}
        QPushButton:hover {{
            background: {_C.BG_HOVER};
            color: {_C.TEXT_PRI};
            border-color: {_C.BORDER_LT};
        }}
        QPushButton:pressed {{ background: {_C.BG_ACTIVE}; }}
        QPushButton:disabled {{
            background: {_C.BG_DEEP};
            color: {_C.TEXT_DIM};
            border-color: {_C.BORDER};
        }}
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
        QLineEdit, QSpinBox, QDoubleSpinBox {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_PRI};
            border: 1px solid {_C.BORDER};
            border-radius: 5px;
            font-size: {_font_px(12)}px;
            font-family: 'JetBrains Mono', monospace;
            padding: {_scale(4)}px {_scale(8)}px;
        }}
        QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
            border-color: {_C.ACCENT};
        }}
        QLineEdit::placeholder {{
            color: {_C.TEXT_DIM};
        }}
    """


def _checkbox_style() -> str:
    return f"""
        QCheckBox {{
            color: {_C.TEXT_PRI};
            font-size: {_font_px(11)}px;
            spacing: {_scale(8)}px;
            background: transparent;
        }}
        QCheckBox::indicator {{
            width: {_scale(14)}px;
            height: {_scale(14)}px;
            border-radius: {_scale(3)}px;
            border: 1px solid {_C.CHECKBOX_BORDER};
            background: {_C.CHECKBOX_BG};
        }}
        QCheckBox::indicator:hover {{
            border-color: {_C.ACCENT};
        }}
        QCheckBox::indicator:checked {{
            background: {_C.ACCENT};
            border-color: {_C.ACCENT};
            image: url('{CHECK_ICON_PATH}');
        }}
        QCheckBox::indicator:disabled {{
            border-color: {_C.BORDER};
            background: {_C.BG_DEEP};
        }}
        QCheckBox:disabled {{
            color: {_C.TEXT_DIM};
        }}
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


def _scissors_icon(color: str | None = None, size: int = 14) -> QIcon:
    color = color or "#e5a50a"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
        f' fill="none" stroke="{color}" stroke-width="2.2"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="6" cy="6" r="3"/>'
        '<circle cx="6" cy="18" r="3"/>'
        '<line x1="20" y1="4" x2="8.12" y2="15.88"/>'
        '<line x1="14.47" y1="14.48" x2="20" y2="20"/>'
        '<line x1="8.12" y1="8.12" x2="12" y2="12"/>'
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


def _waveform_icon(color: str | None = None, size: int = 14) -> QIcon:
    color = color or "#6070f8"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
        f' fill="none" stroke="{color}" stroke-width="2.2"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M2 10v4M6 6v12M10 3v18M14 8v8M18 5v14M22 10v4"/>'
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


def _play_icon(color: str | None = None, size: int = 14) -> QIcon:
    color = color or _C.OK
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
        f' fill="{color}" stroke="{color}" stroke-width="1.5"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<polygon points="5 3 19 12 5 21 5 3"/>'
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


def _stop_icon(color: str | None = None, size: int = 14) -> QIcon:
    color = color or _C.ERROR
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"'
        f' fill="{color}" stroke="{color}" stroke-width="1.5"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<rect x="4" y="4" width="16" height="16" rx="2"/>'
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


def _table_style() -> str:
    return f"""
        QTableWidget {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_PRI};
            border: 1px solid {_C.BORDER};
            border-radius: {_scale(5)}px;
            gridline-color: {_C.BORDER};
            font-size: {_font_px(11)}px;
            selection-background-color: {_C.BG_ACTIVE};
            selection-color: {_C.TEXT_PRI};
        }}
        QHeaderView::section {{
            background: {_C.BG_CARD};
            color: {_C.TEXT_SEC};
            border: none;
            border-bottom: 1px solid {_C.BORDER};
            padding: {_scale(4)}px {_scale(8)}px;
            font-size: {_font_px(10)}px;
            font-weight: 700;
        }}
    """


__all__ = [
    "_C",
    "_card",
    "_checkbox_style",
    "_input_style",
    "_pencil_icon",
    "_play_icon",
    "_primary_button",
    "_refresh_icon",
    "_scissors_icon",
    "_secondary_button",
    "_section_label",
    "_separator",
    "_stop_icon",
    "_table_style",
    "_warning_icon",
    "_waveform_icon",
    "_x_icon",
]


