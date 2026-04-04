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
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QStandardPaths


# ---------------------------------------------------------------------------
# Chemin du fichier config.ini
# ---------------------------------------------------------------------------

# config.ini est placé dans le même dossier que main.py
# (mediarecode/config.ini), soit le dossier parent de core/
_INI_PATH = Path(__file__).parent.parent / "config.ini"

_WINDOWS_TOOL_FILENAMES: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("ffmpeg.exe",),
    "ffprobe": ("ffprobe.exe",),
    "mkvmerge": ("mkvmerge.exe",),
    "mkvextract": ("mkvextract.exe",),
    "mkvinfo": ("mkvinfo.exe",),
    "mediainfo": ("MediaInfo.exe", "mediainfo.exe"),
    "dovi_tool": ("dovi_tool.exe",),
    "hdr10plus_tool": ("hdr10plus_tool.exe",),
    "eac3to": ("eac3to.exe",),
}

_WINDOWS_WINGET_PATTERNS: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("Gyan.FFmpeg*",),
    "ffprobe": ("Gyan.FFmpeg*",),
    "mkvmerge": ("MKVToolNix.MKVToolNix*",),
    "mkvextract": ("MKVToolNix.MKVToolNix*",),
    "mkvinfo": ("MKVToolNix.MKVToolNix*",),
    "mediainfo": ("MediaArea.MediaInfo.CLI*",),
}


def _load_ini() -> configparser.ConfigParser:
    """Charge config.ini s'il existe, retourne un parser vide sinon."""
    parser = configparser.ConfigParser(
        inline_comment_prefixes=("#",),
        default_section="DEFAULT",
    )
    if _INI_PATH.exists():
        parser.read(_INI_PATH, encoding="utf-8")
    return parser


def _is_windows() -> bool:
    return sys.platform == "win32"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _tools_section_bounds(lines: list[str]) -> tuple[int, int]:
    start = -1
    end = len(lines)

    for index, line in enumerate(lines):
        if line.strip().lower() == "[tools]":
            start = index
            break

    if start == -1:
        return -1, len(lines)

    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break

    return start, end


def _update_ini_tools_section(path: Path, tool_values: dict[str, str]) -> None:
    """Ajoute les chemins détectés dans [tools] sans écraser une valeur explicite."""
    if not tool_values:
        return

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    start, end = _tools_section_bounds(lines)

    if start == -1:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[tools]"])
        start = len(lines) - 1
        end = len(lines)

    insert_at = end
    for key, value in tool_values.items():
        updated = False
        for index in range(start + 1, end):
            stripped = lines[index].strip()
            if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
                continue
            lhs, rhs = stripped.split("=", 1)
            if lhs.strip().lower() != key.lower():
                continue
            if not rhs.strip():
                lines[index] = f"{key} = {value}"
            updated = True
            break

        if not updated:
            lines.insert(insert_at, f"{key} = {value}")
            insert_at += 1
            end += 1

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _windows_repo_tool_dirs() -> list[Path]:
    repo_dir = _INI_PATH.parent
    return [repo_dir / "tools", repo_dir / "tools" / "bin"]


def _windows_program_files_dirs() -> list[Path]:
    dirs: list[Path] = []
    for env_name, default in (
        ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        raw = os.environ.get(env_name, default)
        if raw:
            dirs.append(Path(raw))
    return _dedupe_paths(dirs)


def _windows_winget_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    return Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"


def _windows_default_tool_candidates(tool_name: str) -> list[Path]:
    exe_names = _WINDOWS_TOOL_FILENAMES.get(tool_name, (f"{tool_name}.exe",))
    candidates: list[Path] = []

    for directory in _windows_repo_tool_dirs():
        for exe_name in exe_names:
            candidates.append(directory / exe_name)

    for base_dir in _windows_program_files_dirs():
        if tool_name in ("ffmpeg", "ffprobe"):
            for folder in ("ffmpeg", "FFmpeg"):
                for exe_name in exe_names:
                    candidates.append(base_dir / folder / "bin" / exe_name)
        elif tool_name in ("mkvmerge", "mkvextract", "mkvinfo"):
            for exe_name in exe_names:
                candidates.append(base_dir / "MKVToolNix" / exe_name)
        elif tool_name == "mediainfo":
            for folder in ("MediaInfo", "MediaInfo CLI", "MediaInfoCLI"):
                for exe_name in exe_names:
                    candidates.append(base_dir / folder / exe_name)
        elif tool_name == "eac3to":
            for exe_name in exe_names:
                candidates.append(base_dir / "eac3to" / exe_name)

    winget_root = _windows_winget_root()
    if winget_root.exists():
        for pattern in _WINDOWS_WINGET_PATTERNS.get(tool_name, ()):
            for package_dir in winget_root.glob(pattern):
                for exe_name in exe_names:
                    candidates.append(package_dir / exe_name)
                    candidates.extend(path for path in package_dir.rglob(exe_name))

    return _dedupe_paths(candidates)


def _detect_windows_tool_path(tool_name: str, current_value: str) -> str:
    current_value = (current_value or "").strip()
    if not _is_windows():
        return current_value

    current_path = Path(current_value)
    if current_value and current_path.is_file():
        return str(current_path)

    resolved = shutil.which(current_value) if current_value else None
    if resolved:
        return resolved

    for candidate in _windows_default_tool_candidates(tool_name):
        if candidate.is_file():
            return str(candidate)

    return current_value


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
        self._detected_ini_tools: dict[str, str] = {}
        self._load()
        if _is_windows() and self._detected_ini_tools:
            _update_ini_tools_section(_INI_PATH, self._detected_ini_tools)
            self._ini = _load_ini()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ini_get(self, section: str, key: str) -> str:
        """Retourne la valeur depuis config.ini, ou '' si absente/vide."""
        try:
            return self._ini.get(section, key).strip()
        except (configparser.NoSectionError, configparser.NoOptionError):
            return ""

    def _resolve_tool_value(self, ini_key: str, settings_key: str, default: str) -> str:
        """Résout une valeur d'outil et persiste l'autodetect Windows dans config.ini."""
        ini_value = self._ini_get("tools", ini_key)
        if ini_value:
            return ini_value

        current_value = str(self._settings.value(settings_key, default) or default)
        resolved = _detect_windows_tool_path(ini_key, current_value)

        if _is_windows():
            resolved_path = Path(resolved)
            if resolved_path.is_file():
                self._detected_ini_tools.setdefault(ini_key, str(resolved_path))

        return resolved

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
        self.tool_ffmpeg: str       = self._resolve_tool_value("ffmpeg", "tools/ffmpeg", "ffmpeg")
        self.tool_ffprobe: str      = self._resolve_tool_value("ffprobe", "tools/ffprobe", "ffprobe")
        self.tool_mkvmerge: str     = self._resolve_tool_value("mkvmerge", "tools/mkvmerge", "mkvmerge")
        self.tool_mkvextract: str   = self._resolve_tool_value("mkvextract", "tools/mkvextract", "mkvextract")
        self.tool_mkvinfo: str      = self._resolve_tool_value("mkvinfo", "tools/mkvinfo", "mkvinfo")
        self.tool_mediainfo: str    = self._resolve_tool_value("mediainfo", "tools/mediainfo", "mediainfo")
        self.tool_dovi_tool: str    = self._resolve_tool_value("dovi_tool", "tools/dovi_tool", "dovi_tool")
        self.tool_hdr10plus: str    = self._resolve_tool_value("hdr10plus_tool", "tools/hdr10plus_tool", "hdr10plus_tool")
        self.tool_eac3to: str       = self._resolve_tool_value("eac3to", "tools/eac3to", "eac3to")

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
