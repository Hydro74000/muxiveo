"""
core/config.py — Configuration centralisée de l'application.

Priorité de résolution :
    1. config.ini  (fichier statique à côté de main.py)
    2. Fichier de configuration persistant (QSettings)
    3. Valeur par défaut
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QStandardPaths


# ---------------------------------------------------------------------------
# Chemin du fichier config.ini
# ---------------------------------------------------------------------------

# config.ini est placé dans le même dossier que main.py
# (mediarecode/config.ini), soit le dossier parent de core/
_INI_PATH = Path(__file__).parent.parent / "config.ini"


def _load_ini() -> configparser.ConfigParser:
    """Charge config.ini s'il existe, retourne un parser vide sinon."""
    parser = configparser.ConfigParser(
        inline_comment_prefixes=("#",),
        default_section="DEFAULT",
    )
    if _INI_PATH.exists():
        parser.read(_INI_PATH, encoding="utf-8")
    return parser


# ---------------------------------------------------------------------------
# Chemins applicatifs
# ---------------------------------------------------------------------------

def _app_data_dir() -> Path:
    """Retourne le dossier de données persistantes de l'application."""
    raw = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    p = Path(raw) / "mediarecode"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _default_work_dir() -> Path:
    tmp = Path(
        os.environ.get("TMPDIR", os.environ.get("TEMP", "/tmp"))
    )
    p = tmp / "mediarecode_work"
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
    app data. config.ini a priorité sur les valeurs sauvegardées,
    elles-mêmes prioritaires sur les défauts.

    Usage :
        config = AppConfig()
        config.work_dir            # Path
        config.output_dir          # Path
        config.dovi_profile        # str  "8"
        config.dovi_compat_id      # str  "1"
        config.save()              # Persiste les modifications
    """

    # Chemin du fichier QSettings
    _SETTINGS_ORG  = "mediarecode"
    _SETTINGS_APP  = "Mediarecode"

    def __init__(self) -> None:
        self._settings = QSettings(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            self._SETTINGS_ORG,
            self._SETTINGS_APP,
        )
        self._ini = _load_ini()
        self._load()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ini_get(self, section: str, key: str) -> str:
        """Retourne la valeur depuis config.ini, ou '' si absente/vide."""
        try:
            return self._ini.get(section, key).strip()
        except (configparser.NoSectionError, configparser.NoOptionError):
            return ""

    # ------------------------------------------------------------------
    # Chargement
    # ------------------------------------------------------------------

    def _load(self) -> None:
        s = self._settings

        # --- Dossiers ---
        self.work_dir: Path = Path(
            self._ini_get("paths", "work_dir")
            or s.value("paths/work_dir", "")
            or str(_default_work_dir())
        )
        self.output_dir: Path = Path(
            self._ini_get("paths", "output_dir")
            or s.value("paths/output_dir", "")
            or str(_default_output_dir())
        )
        self.app_data_dir: Path = _app_data_dir()

        # --- Outils externes (noms ou chemins absolus) ---
        # Les valeurs dans config.ini [tools] surchargent les valeurs
        # persistées et les défauts, mais ne désactivent pas l'autodetect.
        self.tool_ffmpeg: str       = self._ini_get("tools", "ffmpeg")       or s.value("tools/ffmpeg",        "ffmpeg")
        self.tool_ffprobe: str      = self._ini_get("tools", "ffprobe")      or s.value("tools/ffprobe",       "ffprobe")
        self.tool_mkvmerge: str     = self._ini_get("tools", "mkvmerge")     or s.value("tools/mkvmerge",      "mkvmerge")
        self.tool_mkvextract: str   = self._ini_get("tools", "mkvextract")   or s.value("tools/mkvextract",    "mkvextract")
        self.tool_mkvinfo: str      = self._ini_get("tools", "mkvinfo")      or s.value("tools/mkvinfo",       "mkvinfo")
        self.tool_mediainfo: str    = self._ini_get("tools", "mediainfo")    or s.value("tools/mediainfo",     "mediainfo")
        self.tool_dovi_tool: str    = self._ini_get("tools", "dovi_tool")    or s.value("tools/dovi_tool",     "dovi_tool")
        self.tool_hdr10plus: str    = self._ini_get("tools", "hdr10plus_tool") or s.value("tools/hdr10plus_tool","hdr10plus_tool")
        self.tool_eac3to: str       = self._ini_get("tools", "eac3to")       or s.value("tools/eac3to",        "eac3to")

        # --- Paramètres HDR ---
        self.dovi_profile: str    = self._ini_get("hdr", "dovi_profile")    or s.value("hdr/dovi_profile",    "8")
        self.dovi_compat_id: str  = self._ini_get("hdr", "dovi_compat_id")  or s.value("hdr/dovi_compat_id",  "1")

        # --- Buffer RAM pour les fichiers HEVC intermédiaires ---
        # ram_buffer_enabled  : active l'utilisation de /dev/shm (Linux/macOS) comme tampon
        # ram_buffer_threshold_pct : % de RAM totale devant rester libre après chargement du fichier
        _rb_ini = self._ini_get("encoding", "ram_buffer_enabled")
        self.ram_buffer_enabled: bool = (
            _rb_ini.lower() not in ("0", "false", "no") if _rb_ini
            else s.value("encoding/ram_buffer_enabled", "true").lower()
               not in ("0", "false", "no")
        )
        _rbt_ini = self._ini_get("encoding", "ram_buffer_threshold_pct")
        self.ram_buffer_threshold_pct: int = int(
            _rbt_ini
            or s.value("encoding/ram_buffer_threshold_pct", 15)
        )

        # --- Interface ---
        self.log_max_lines: int = int(
            self._ini_get("ui", "log_max_lines") or s.value("ui/log_max_lines", 2000)
        )
        self.theme: str = (
            self._ini_get("ui", "theme") or s.value("ui/theme", "dark")
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

        s.setValue("encoding/ram_buffer_enabled",
                   "true" if self.ram_buffer_enabled else "false")
        s.setValue("encoding/ram_buffer_threshold_pct", self.ram_buffer_threshold_pct)

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
            "encoding": {
                "ram_buffer_enabled":       self.ram_buffer_enabled,
                "ram_buffer_threshold_pct": self.ram_buffer_threshold_pct,
            },
            "ui": {
                "log_max_lines": self.log_max_lines,
                "theme":         self.theme,
            },
        }

    def __repr__(self) -> str:
        return f"AppConfig({json.dumps(self.to_dict(), indent=2)})"
