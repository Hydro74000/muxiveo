"""
ui/panels/encode_panel/theme.py — Color palette, UI helper factories and progress helpers.

Public:
    _C              — color constants
    _section_label  — returns a styled section QLabel
    _card           — returns a styled card QWidget
    _primary_button — returns a primary QPushButton
    _secondary_button — returns a secondary QPushButton
    _separator      — returns a horizontal QFrame separator
    _input_style    — stylesheet string for QLineEdit
    _combo_style    — stylesheet string for QComboBox
    _checkbox_style — stylesheet string for QCheckBox
    _TIME_RE        — compiled regex matching ffmpeg time= output
    _FPS_RE         — compiled regex matching ffmpeg fps= output
    ffmpeg_progress_seconds — extracts elapsed media time from ffmpeg progress output
    _fmt_eta        — formats remaining seconds as human-readable string
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QWidget
from ui.design_system import colors as _C, font_px as _font_px, scale as _scale


# =============================================================================
# Helpers UI
# =============================================================================

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


# =============================================================================
# Progress helpers
# =============================================================================

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_FPS_RE  = re.compile(r"\bfps=\s*([\d.]+)")
_OUT_TIME_RE = re.compile(r"\bout_time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_OUT_TIME_TICKS_RE = re.compile(r"\bout_time_(?:ms|us)=(\d+)")


def _hms_to_seconds(match: re.Match[str]) -> float:
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def ffmpeg_progress_seconds(line: str) -> float | None:
    """
    Retourne le temps média écoulé pour une ligne de progression FFmpeg.

    Gère à la fois :
      - les stats texte historiques (`time=...`)
      - la sortie machine de `-progress pipe:1` (`out_time=...`)
      - le fallback numérique `out_time_ms` / `out_time_us`

    Note : malgré son nom, `out_time_ms` est souvent exprimé en microsecondes
    par FFmpeg ; on traite donc ces compteurs comme des microsecondes.
    """
    match = _TIME_RE.search(line)
    if match:
        return _hms_to_seconds(match)

    match = _OUT_TIME_RE.search(line)
    if match:
        return _hms_to_seconds(match)

    match = _OUT_TIME_TICKS_RE.search(line)
    if match:
        return int(match.group(1)) / 1_000_000.0

    return None


def _fmt_eta(seconds: float) -> str:
    """Formate une durée en 'Xm Xs' ou 'Xs'. Retourne '—' si indéterminé."""
    if seconds <= 0 or seconds != seconds:   # négatif ou NaN/inf
        return "—"
    s = int(seconds)
    m, s = divmod(s, 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"
