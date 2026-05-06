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


def _secondary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFixedHeight(_scale(28))
    btn.setStyleSheet(f"""
        QPushButton{{background:{_C.BG_CARD};color:{_C.TEXT_SEC};
                     border:1px solid {_C.BORDER};border-radius:5px;
                     font-size:{_font_px(11)}px;font-weight:500;padding:0 {_scale(12)}px;}}
        QPushButton:hover{{background:{_C.BG_HOVER};color:{_C.TEXT_PRI};
                           border-color:{_C.BORDER_LT};}}
        QPushButton:pressed{{background:{_C.BG_ACTIVE};}}
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
    return (f"QCheckBox{{color:{_C.TEXT_SEC};font-size:{_font_px(12)}px;background:transparent;}}"
            f"QCheckBox::indicator{{width:{_scale(14)}px;height:{_scale(14)}px;"
            f"border:1px solid {_C.BORDER_LT};border-radius:3px;"
            f"background:{_C.BG_CARD};}}"
            f"QCheckBox::indicator:checked{{background:{_C.ACCENT};"
            f"border-color:{_C.ACCENT};}}")
