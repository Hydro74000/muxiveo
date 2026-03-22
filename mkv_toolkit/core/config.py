"""
core/config.py — Configuration centralisée de l'application.

Priorité de résolution :
    1. Variable d'environnement
    2. Fichier de configuration persistant (QSettings)
    3. Valeur par défaut
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QStandardPaths


# ---------------------------------------------------------------------------
# Chemins applicatifs
# ---------------------------------------------------------------------------

def _app_data_dir() -> Path:
    """Retourne le dossier de données persistantes de l'application."""
    raw = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    p = Path(raw) / "mkv_toolkit"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _default_work_dir() -> Path:
    tmp = Path(
        os.environ.get("TMPDIR", os.environ.get("TEMP", "/tmp"))
    )
    p = tmp / "mkv_toolkit_work"
    return p


def _default_output_dir() -> Path:
    raw = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.MoviesLocation
    )
    return Path(raw) if raw else Path.home() / "Videos"


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------

class AppConfig:
    """
    Configuration centralisée de l'application.

    Les propriétés sont persistées via QSettings (INI) dans le dossier
    app data. Les variables d'environnement ont priorité sur les valeurs
    sauvegardées, elles-mêmes prioritaires sur les défauts.

    Usage :
        config = AppConfig()
        config.work_dir            # Path
        config.output_dir          # Path
        config.dovi_profile        # str  "8"
        config.dovi_compat_id      # str  "1"
        config.save()              # Persiste les modifications
    """

    # Chemin du fichier QSettings
    _SETTINGS_ORG  = "mkv_toolkit"
    _SETTINGS_APP  = "MKVToolkit"

    def __init__(self) -> None:
        self._settings = QSettings(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            self._SETTINGS_ORG,
            self._SETTINGS_APP,
        )
        self._load()

    # ------------------------------------------------------------------
    # Chargement
    # ------------------------------------------------------------------

    def _load(self) -> None:
        env = os.environ.get
        s   = self._settings

        # --- Dossiers ---
        self.work_dir: Path = Path(
            env("WORK_DIR")
            or s.value("paths/work_dir", "")
            or str(_default_work_dir())
        )
        self.output_dir: Path = Path(
            env("OUTPUT_DIR")
            or s.value("paths/output_dir", "")
            or str(_default_output_dir())
        )
        self.app_data_dir: Path = _app_data_dir()

        # --- Outils externes (noms ou chemins absolus) ---
        self.tool_ffmpeg: str       = env("TOOL_FFMPEG")       or s.value("tools/ffmpeg",        "ffmpeg")
        self.tool_ffprobe: str      = env("TOOL_FFPROBE")      or s.value("tools/ffprobe",       "ffprobe")
        self.tool_mkvmerge: str     = env("TOOL_MKVMERGE")     or s.value("tools/mkvmerge",      "mkvmerge")
        self.tool_mkvextract: str   = env("TOOL_MKVEXTRACT")   or s.value("tools/mkvextract",    "mkvextract")
        self.tool_mkvinfo: str      = env("TOOL_MKVINFO")      or s.value("tools/mkvinfo",       "mkvinfo")
        self.tool_mediainfo: str    = env("TOOL_MEDIAINFO")    or s.value("tools/mediainfo",     "mediainfo")
        self.tool_dovi_tool: str    = env("TOOL_DOVI_TOOL")    or s.value("tools/dovi_tool",     "dovi_tool")
        self.tool_hdr10plus: str    = env("TOOL_HDR10PLUS")    or s.value("tools/hdr10plus_tool","hdr10plus_tool")
        self.tool_eac3to: str       = env("TOOL_EAC3TO")       or s.value("tools/eac3to",        "eac3to")

        # --- Paramètres HDR ---
        self.dovi_profile: str    = env("DOVI_PROFILE")    or s.value("hdr/dovi_profile",    "8")
        self.dovi_compat_id: str  = env("DOVI_COMPAT_ID")  or s.value("hdr/dovi_compat_id",  "1")

        # --- Interface ---
        self.log_max_lines: int = int(
            env("LOG_MAX_LINES") or s.value("ui/log_max_lines", 2000)
        )
        self.theme: str = (
            env("APP_THEME") or s.value("ui/theme", "dark")
        )
        self.window_geometry: bytes | None = s.value("ui/geometry", None)

    # ------------------------------------------------------------------
    # Sauvegarde
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persiste la configuration dans le fichier INI."""
        s = self._settings

        s.setValue("paths/work_dir",   str(self.work_dir))
        s.setValue("paths/output_dir", str(self.output_dir))

        s.setValue("tools/ffmpeg",         self.tool_ffmpeg)
        s.setValue("tools/ffprobe",        self.tool_ffprobe)
        s.setValue("tools/mkvmerge",       self.tool_mkvmerge)
        s.setValue("tools/mkvextract",     self.tool_mkvextract)
        s.setValue("tools/mkvinfo",        self.tool_mkvinfo)
        s.setValue("tools/mediainfo",      self.tool_mediainfo)
        s.setValue("tools/dovi_tool",      self.tool_dovi_tool)
        s.setValue("tools/hdr10plus_tool", self.tool_hdr10plus)
        s.setValue("tools/eac3to",         self.tool_eac3to)

        s.setValue("hdr/dovi_profile",    self.dovi_profile)
        s.setValue("hdr/dovi_compat_id",  self.dovi_compat_id)

        s.setValue("ui/log_max_lines", self.log_max_lines)
        s.setValue("ui/theme",         self.theme)

        s.sync()

    def save_geometry(self, geometry: bytes) -> None:
        self._settings.setValue("ui/geometry", geometry)
        self._settings.sync()

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------

    def tool_path(self, name: str) -> Path | None:
        """Retourne le Path résolu d'un outil, ou None s'il est absent."""
        attr = f"tool_{name.replace('-', '_')}"
        value: str = getattr(self, attr, name)
        found = shutil.which(value)
        return Path(found) if found else None

    def all_tools_available(self) -> dict[str, bool]:
        """Vérifie la disponibilité de tous les outils configurés."""
        tools = {
            "ffmpeg":        self.tool_ffmpeg,
            "ffprobe":       self.tool_ffprobe,
            "mkvmerge":      self.tool_mkvmerge,
            "mkvextract":    self.tool_mkvextract,
            "mkvinfo":       self.tool_mkvinfo,
            "mediainfo":     self.tool_mediainfo,
            "dovi_tool":     self.tool_dovi_tool,
            "hdr10plus_tool":self.tool_hdr10plus,
            "eac3to":        self.tool_eac3to,
        }
        return {name: shutil.which(cmd) is not None for name, cmd in tools.items()}

    def ensure_work_dir(self) -> Path:
        """Crée le dossier de travail s'il n'existe pas, le retourne."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return self.work_dir

    def ensure_output_dir(self) -> Path:
        """Crée le dossier de sortie s'il n'existe pas, le retourne."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir

    def to_dict(self) -> dict[str, Any]:
        """Sérialise la configuration en dictionnaire (debug/export)."""
        return {
            "paths": {
                "work_dir":   str(self.work_dir),
                "output_dir": str(self.output_dir),
                "app_data":   str(self.app_data_dir),
            },
            "tools": {
                "ffmpeg":         self.tool_ffmpeg,
                "ffprobe":        self.tool_ffprobe,
                "mkvmerge":       self.tool_mkvmerge,
                "mkvextract":     self.tool_mkvextract,
                "mkvinfo":        self.tool_mkvinfo,
                "mediainfo":      self.tool_mediainfo,
                "dovi_tool":      self.tool_dovi_tool,
                "hdr10plus_tool": self.tool_hdr10plus,
                "eac3to":         self.tool_eac3to,
            },
            "hdr": {
                "dovi_profile":   self.dovi_profile,
                "dovi_compat_id": self.dovi_compat_id,
            },
            "ui": {
                "log_max_lines": self.log_max_lines,
                "theme":         self.theme,
            },
        }

    def __repr__(self) -> str:
        return f"AppConfig({json.dumps(self.to_dict(), indent=2)})"
