"""
ui/design_system.py — Centralized design tokens and theme management.

This module centralizes all UI colors used by panels, dialogs and main window.
The selected theme comes from config.ui.theme (dark|light).
"""

from __future__ import annotations

from typing import Final

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


ThemeMap = dict[str, str]


_THEMES: Final[dict[str, ThemeMap]] = {
    "dark": {
        "BG_DEEP": "#0d0f14",
        "BG_PANEL": "#141720",
        "BG_SIDEBAR": "#0f1117",
        "BG_CARD": "#1a1e2a",
        "BG_HOVER": "#1f2435",
        "BG_ACTIVE": "#232840",
        "BORDER": "#252a3a",
        "BORDER_LT": "#2e3450",
        "TEXT_PRI": "#e8ecf4",
        "TEXT_SEC": "#7a85a0",
        "TEXT_DIM": "#3d4560",
        "ACCENT": "#4f6ef7",
        "ACCENT_DIM": "#2a3a8a",
        "OK": "#5dcc8a",
        "WARN": "#f5c842",
        "ERROR": "#f55a5a",
        "INFO": "#7ab3f5",
        "PURPLE": "#ce93d8",
        "LOG_INFO": "#7ab3f5",
        "LOG_OK": "#5dcc8a",
        "LOG_WARN": "#f5c842",
        "LOG_ERROR": "#f55a5a",
        "LOG_TS": "#3d4560",
        "LOG_BG": "#0a0c11",
        "TRACK_VIDEO": "#7ab3f5",
        "TRACK_AUDIO": "#ce93d8",
        "TRACK_SUBTITLE": "#5dcc8a",
        "TRACK_ATTACHMENT": "#f5c842",
        "TRACK_TAGS": "#f5a030",
        "HDR_NONE": "#3d4560",
        "HDR_HDR10": "#1a4060",
        "HDR_HDR10P": "#1a3a60",
        "HDR_DOVI": "#3a1a60",
        "HDR_BOTH": "#3a1060",
        "BADGE_OK_BG": "#0f2318",
        "BADGE_OK_BORDER": "#1a4a2e",
        "BADGE_ERROR_BG": "#1f0e0e",
        "BADGE_ERROR_BORDER": "#3a1515",
        "BADGE_PENDING_BG": "#1a1e2a",
        "BADGE_PENDING_BORDER": "#252a3a",
    },
    "light": {
        "BG_DEEP": "#f4f6fb",
        "BG_PANEL": "#ffffff",
        "BG_SIDEBAR": "#eef2f8",
        "BG_CARD": "#ffffff",
        "BG_HOVER": "#e9eefb",
        "BG_ACTIVE": "#dfe8ff",
        "BORDER": "#d6ddea",
        "BORDER_LT": "#c4cde0",
        "TEXT_PRI": "#1c2533",
        "TEXT_SEC": "#52607a",
        "TEXT_DIM": "#7b879f",
        "ACCENT": "#355ee6",
        "ACCENT_DIM": "#cbd8ff",
        "OK": "#1e9b62",
        "WARN": "#b87900",
        "ERROR": "#c03f3f",
        "INFO": "#2f6bbd",
        "PURPLE": "#8a5fbf",
        "LOG_INFO": "#2f6bbd",
        "LOG_OK": "#1e9b62",
        "LOG_WARN": "#b87900",
        "LOG_ERROR": "#c03f3f",
        "LOG_TS": "#7b879f",
        "LOG_BG": "#eef2f7",
        "TRACK_VIDEO": "#2f6bbd",
        "TRACK_AUDIO": "#8a5fbf",
        "TRACK_SUBTITLE": "#1e9b62",
        "TRACK_ATTACHMENT": "#b87900",
        "TRACK_TAGS": "#c06b00",
        "HDR_NONE": "#8b95aa",
        "HDR_HDR10": "#d9ecff",
        "HDR_HDR10P": "#dbe8ff",
        "HDR_DOVI": "#eadfff",
        "HDR_BOTH": "#e7dcff",
        "BADGE_OK_BG": "#e8f7ef",
        "BADGE_OK_BORDER": "#b8e5ca",
        "BADGE_ERROR_BG": "#fdeeee",
        "BADGE_ERROR_BORDER": "#f2c3c3",
        "BADGE_PENDING_BG": "#eef2f8",
        "BADGE_PENDING_BORDER": "#d6ddea",
    },
}


class DesignSystem:
    """Global theme registry + Qt palette application."""

    _theme: str = "dark"
    _colors: ThemeMap = _THEMES["dark"]
    _ui_scale_percent: int = 100

    @classmethod
    def normalize_theme(cls, value: str | None) -> str:
        if (value or "").strip().lower() == "light":
            return "light"
        return "dark"

    @classmethod
    def set_theme(cls, value: str | None) -> str:
        theme = cls.normalize_theme(value)
        cls._theme = theme
        cls._colors = _THEMES[theme]
        return theme

    @classmethod
    def current_theme(cls) -> str:
        return cls._theme

    @classmethod
    def set_ui_scale(cls, percent: int | None) -> int:
        value = 100 if percent is None else int(percent)
        cls._ui_scale_percent = max(75, min(150, value))
        return cls._ui_scale_percent

    @classmethod
    def current_ui_scale(cls) -> int:
        return cls._ui_scale_percent

    @classmethod
    def scale_factor(cls) -> float:
        return cls._ui_scale_percent / 100.0

    @classmethod
    def scale(cls, px: int | float) -> int:
        return max(1, int(round(float(px) * cls.scale_factor())))

    @classmethod
    def font_px(cls, px: int | float) -> int:
        return max(1, int(round(float(px) * cls.scale_factor())))

    @classmethod
    def size(cls, width: int | float, height: int | float) -> tuple[int, int]:
        return cls.scale(width), cls.scale(height)

    @classmethod
    def spacing(cls, px: int | float) -> int:
        return cls.scale(px)

    @classmethod
    def color(cls, name: str) -> str:
        try:
            return cls._colors[name]
        except KeyError as exc:
            raise AttributeError(f"Unknown design token: {name}") from exc

    @classmethod
    def apply_to_application(cls, app: QApplication | None) -> None:
        if app is None:
            return

        p = QPalette()
        p.setColor(QPalette.ColorRole.Window, QColor(cls.color("BG_DEEP")))
        p.setColor(QPalette.ColorRole.WindowText, QColor(cls.color("TEXT_PRI")))
        p.setColor(QPalette.ColorRole.Base, QColor(cls.color("BG_CARD")))
        p.setColor(QPalette.ColorRole.AlternateBase, QColor(cls.color("BG_PANEL")))
        p.setColor(QPalette.ColorRole.ToolTipBase, QColor(cls.color("BG_CARD")))
        p.setColor(QPalette.ColorRole.ToolTipText, QColor(cls.color("TEXT_PRI")))
        p.setColor(QPalette.ColorRole.Text, QColor(cls.color("TEXT_PRI")))
        p.setColor(QPalette.ColorRole.Button, QColor(cls.color("BG_PANEL")))
        p.setColor(QPalette.ColorRole.ButtonText, QColor(cls.color("TEXT_PRI")))
        p.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
        p.setColor(QPalette.ColorRole.Highlight, QColor(cls.color("ACCENT")))
        p.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        app.setPalette(p)


class _ColorProxy:
    """Attribute access proxy so existing code can keep using `_C.X`."""

    def __getattr__(self, name: str) -> str:
        return DesignSystem.color(name)


colors = _ColorProxy()


def set_ui_scale(percent: int | None) -> int:
    return DesignSystem.set_ui_scale(percent)


def current_ui_scale() -> int:
    return DesignSystem.current_ui_scale()


def scale(px: int | float) -> int:
    return DesignSystem.scale(px)


def font_px(px: int | float) -> int:
    return DesignSystem.font_px(px)


def size(width: int | float, height: int | float) -> tuple[int, int]:
    return DesignSystem.size(width, height)


def spacing(px: int | float) -> int:
    return DesignSystem.spacing(px)
