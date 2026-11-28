"""
ui/panels/encode_panel/theme.py — Color palette, UI helper factories and progress helpers.

Public:
    _C              — color constants
    _bolt_icon      — QIcon éclair (encodeurs matériels)
    _section_label  — returns a styled section QLabel
    _card           — returns a styled card QWidget
    _primary_button — returns a primary QPushButton
    _secondary_button — returns a secondary QPushButton
    _separator      — returns a horizontal QFrame separator
    _input_style    — stylesheet string for QLineEdit
    _combo_style    — stylesheet string for QComboBox
    _checkbox_style — stylesheet string for QCheckBox
    _FPS_RE, _FRAME_RE, ffmpeg_progress_seconds, EtaTracker — progression, réexportés depuis
                      core/workflows/encode/progress_tracking.py (partagés avec l'API)
    _fmt_eta        — formats remaining seconds as human-readable string
"""

from __future__ import annotations

from core.workflows.encode.progress_tracking import (
    FPS_RE as _FPS_RE,
    FRAME_RE as _FRAME_RE,
    EtaTracker,
    ffmpeg_progress_seconds,
)
from ui.design_system import colors as _C
from ui.styles import (
    _bolt_icon, _card, _checkbox_style, _combo_style, _input_style,
    _primary_button, _secondary_button, _section_label, _separator,
)


__all__ = [
    "_C", "_bolt_icon", "_card", "_checkbox_style", "_combo_style", "_input_style",
    "_primary_button", "_secondary_button", "_section_label", "_separator",
    "_FPS_RE", "_FRAME_RE", "ffmpeg_progress_seconds", "_fmt_eta",
    "EtaTracker",
]


# =============================================================================
# Progress helpers
# =============================================================================

def _fmt_eta(seconds: float) -> str:
    """Formate une durée en 'Xm Xs' ou 'Xs'. Retourne '—' si indéterminé."""
    if seconds <= 0 or seconds != seconds:   # négatif ou NaN/inf
        return "—"
    s = int(seconds)
    m, s = divmod(s, 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


