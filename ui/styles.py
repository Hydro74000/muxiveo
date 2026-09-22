"""
ui/styles.py — Helpers UI partagés (boutons, cartes, séparateurs, styles d'input).

Module neutre, importable depuis n'importe quel panneau ou dialog sans risque
de cycle d'imports. Utilise ``ui.design_system`` pour les couleurs et le scale.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QWidget

from ui.design_system import colors as _C, font_px as _font_px, scale as _scale


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{_C.TEXT_DIM};font-size:{_font_px(9)}px;font-weight:700;"
                      f"letter-spacing:{_scale(2)}px;background:transparent;")
    return lbl


def _card(parent: QWidget | None = None) -> QWidget:
    w = QWidget(parent)
    w.setStyleSheet(f"QWidget{{background:{_C.BG_CARD};border:1px solid {_C.BORDER};"
                    f"border-radius:6px;}}")
    return w


def _primary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(36))
    btn.setStyleSheet(f"""
        QPushButton{{background:{_C.ACCENT};color:#fff;border:none;border-radius:6px;
                     font-size:{_font_px(12)}px;font-weight:700;padding:0 {_scale(20)}px;}}
        QPushButton:hover{{background:#6070f0;}}
        QPushButton:pressed{{background:#3a52c0;}}
        QPushButton:disabled{{background:{_C.BG_ACTIVE};color:{_C.TEXT_DIM};}}
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
        QPushButton{{background:{_C.BG_CARD};color:{_C.TEXT_SEC};
                     border:1px solid {_C.BORDER};border-radius:5px;
                     font-size:{_font_px(11)}px;font-weight:500;padding:0 {pad_px}px;
                     text-align: center;}}
        QPushButton:hover{{background:{_C.BG_HOVER};color:{_C.TEXT_PRI};
                           border-color:{_C.BORDER_LT};}}
        QPushButton:pressed{{background:{_C.BG_ACTIVE};}}
        QPushButton:disabled{{background:{_C.BG_DEEP};color:{_C.TEXT_DIM};
                              border-color:{_C.BORDER};}}
    """)
    return btn


def _separator() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setFixedHeight(_scale(1))
    sep.setStyleSheet(f"background:{_C.BORDER};border:none;")
    return sep


def _input_style() -> str:
    return (f"QLineEdit{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
            f"border:1px solid {_C.BORDER};border-radius:5px;"
            f"padding:{_scale(4)}px {_scale(10)}px;font-size:{_font_px(11)}px;}}"
            f"QLineEdit:focus{{border-color:{_C.ACCENT};}}"
            f"QLineEdit:disabled{{background:{_C.BG_DEEP};color:{_C.TEXT_DIM};"
            f"border-color:{_C.BORDER};}}")


def _combo_style() -> str:
    return (f"QComboBox{{background:{_C.BG_CARD};color:{_C.TEXT_PRI};"
            f"border:1px solid {_C.BORDER};border-radius:5px;"
            f"padding:{_scale(3)}px {_scale(8)}px;font-size:{_font_px(11)}px;}}"
            f"QComboBox:focus{{border-color:{_C.ACCENT};}}"
            f"QComboBox QAbstractItemView{{background:{_C.BG_CARD};"
            f"color:{_C.TEXT_PRI};selection-background-color:{_C.ACCENT_DIM};}}")


def _checkbox_style() -> str:
    from ui.design_system import CHECK_ICON_PATH
    return (
        f"QCheckBox {{ color: {_C.TEXT_PRI}; font-size: {_font_px(11)}px; spacing: {_scale(8)}px; background: transparent; }}"
        f"QCheckBox::indicator {{ width: {_scale(14)}px; height: {_scale(14)}px; border-radius: {_scale(3)}px; "
        f"border: 1px solid {_C.CHECKBOX_BORDER}; background: {_C.CHECKBOX_BG}; }}"
        f"QCheckBox::indicator:hover {{ border-color: {_C.ACCENT}; }}"
        f"QCheckBox::indicator:checked {{ background: {_C.ACCENT}; border-color: {_C.ACCENT}; image: url('{CHECK_ICON_PATH}'); }}"
        f"QCheckBox::indicator:disabled {{ border-color: {_C.BORDER}; background: {_C.BG_DEEP}; }}"
        f"QCheckBox:disabled {{ color: {_C.TEXT_DIM}; }}"
    )


def _groupbox_checkable_style() -> str:
    from ui.design_system import CHECK_ICON_PATH
    return (
        f"QGroupBox {{ color: {_C.TEXT_PRI}; font-size: {_font_px(11)}px; font-weight: 700; "
        f"background: {_C.BG_CARD}; border: 1px solid {_C.BORDER}; border-radius: {_scale(6)}px; "
        f"margin-top: {_scale(8)}px; padding-top: {_scale(14)}px; }}"
        f"QGroupBox::title {{ subcontrol-origin: border; subcontrol-position: top left; "
        f"left: {_scale(12)}px; padding: {_scale(2)}px {_scale(6)}px; background: {_C.BG_CARD}; }}"
        f"QGroupBox::indicator {{ width: {_scale(14)}px; height: {_scale(14)}px; border-radius: {_scale(3)}px; "
        f"border: 1px solid {_C.CHECKBOX_BORDER}; background: {_C.CHECKBOX_BG}; margin-right: {_scale(6)}px; }}"
        f"QGroupBox::indicator:hover {{ border-color: {_C.ACCENT}; }}"
        f"QGroupBox::indicator:checked {{ background: {_C.ACCENT}; border-color: {_C.ACCENT}; image: url('{CHECK_ICON_PATH}'); }}"
    )
