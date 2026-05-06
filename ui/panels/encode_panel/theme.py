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

from ui.design_system import colors as _C
from ui.styles import (
    _card, _checkbox_style, _combo_style, _input_style,
    _primary_button, _secondary_button, _section_label, _separator,
)


__all__ = [
    "_C", "_card", "_checkbox_style", "_combo_style", "_input_style",
    "_primary_button", "_secondary_button", "_section_label", "_separator",
    "_TIME_RE", "_FPS_RE", "ffmpeg_progress_seconds", "_fmt_eta",
    "EtaTracker",
]


# =============================================================================
# Progress helpers
# =============================================================================

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_FPS_RE  = re.compile(r"\bfps=\s*([\d.]+)")
_OUT_TIME_RE = re.compile(r"\bout_time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_OUT_TIME_TICKS_RE = re.compile(r"\bout_time_(?:ms|us)=(\d+)")
_FRAME_RE = re.compile(r"\bframe=\s*(\d+)")


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


class EtaTracker:
    """
    Lisse la vitesse de progression pour produire un ETA stable.

    Calcule une vitesse instantanée (delta progrès / delta wall-time) entre
    échantillons consécutifs, puis applique une moyenne exponentielle (EWMA).
    Cela évite l'effet "ETA énorme au début, qui chute brutalement à la fin"
    causé par une vitesse moyenne calculée depuis le début (qui inclut
    l'init lente de ffmpeg).

    Le warmup ignore les premières mesures tant que peu de progrès média a
    été produit — pendant ce temps, ``eta()`` retourne ``None``.
    """

    # Why: alpha=0.3 → la vitesse instantanée pèse 30 %, l'historique 70 %.
    # Assez réactif pour suivre une vraie variation, assez lissé pour ne pas
    # osciller à chaque ligne de ffmpeg.
    _ALPHA = 0.3
    # Why: en-dessous, le delta est trop court pour donner une vitesse fiable
    # (jitter de l'I/O, ligne ffmpeg agrégée). On accumule jusqu'à ce seuil.
    _MIN_DELTA_WALL = 0.5
    _MIN_DELTA_PROGRESS = 0.0

    def __init__(self, *, warmup_progress: float = 1.0) -> None:
        self._warmup = float(warmup_progress)
        self._smoothed_speed: float | None = None
        self._last_progress: float | None = None
        self._last_wall: float | None = None
        self._initial_progress: float | None = None

    def reset(self) -> None:
        self._smoothed_speed = None
        self._last_progress = None
        self._last_wall = None
        self._initial_progress = None

    def update(self, progress: float, wall_time: float) -> None:
        """Enregistre un nouvel échantillon (progrès média et wall-time absolu)."""
        if progress < 0 or wall_time < 0:
            return
        if self._initial_progress is None:
            self._initial_progress = progress
            self._last_progress = progress
            self._last_wall = wall_time
            return
        if self._last_progress is None or self._last_wall is None:
            self._last_progress = progress
            self._last_wall = wall_time
            return

        # Pendant la phase de warmup (init ffmpeg), on ne pollue pas l'EWMA
        # avec des vitesses non représentatives. On suit juste la position
        # pour pouvoir mesurer le delta dès la sortie de warmup.
        if (progress - self._initial_progress) < self._warmup:
            self._last_progress = progress
            self._last_wall = wall_time
            return

        delta_progress = progress - self._last_progress
        delta_wall = wall_time - self._last_wall
        if delta_wall < self._MIN_DELTA_WALL or delta_progress <= self._MIN_DELTA_PROGRESS:
            # Échantillon trop court : on attend, sans écraser le précédent.
            return

        instant_speed = delta_progress / delta_wall
        if instant_speed <= 0:
            return

        if self._smoothed_speed is None:
            self._smoothed_speed = instant_speed
        else:
            self._smoothed_speed = (
                self._ALPHA * instant_speed + (1.0 - self._ALPHA) * self._smoothed_speed
            )
        self._last_progress = progress
        self._last_wall = wall_time

    def eta(self, total: float, current: float) -> float | None:
        """Retourne l'ETA en secondes, ou ``None`` si indéterminé (warmup)."""
        if total <= 0 or current < 0 or current >= total:
            return None
        if self._smoothed_speed is None or self._smoothed_speed <= 0:
            return None
        return (total - current) / self._smoothed_speed

    def eta_from_speed(self, total: float, current: float, speed: float) -> float | None:
        """ETA basé sur une vitesse externe (ex: fps ffmpeg) — sans EWMA interne."""
        if total <= 0 or current < 0 or current >= total or speed <= 0:
            return None
        return (total - current) / speed
