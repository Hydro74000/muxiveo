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
import locale
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QStandardPaths

from core.lang_tags import Rfc5646LanguageTags


# ---------------------------------------------------------------------------
# Chemin du fichier config.ini
# ---------------------------------------------------------------------------

# config.ini est placé dans le même dossier que main.py
# (mediarecode/config.ini), soit le dossier parent de core/
_INI_PATH = Path(__file__).parent.parent / "config.ini"
_MISSING = object()

_WINDOWS_TOOL_FILENAMES: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("ffmpeg.exe",),
    "ffprobe": ("ffprobe.exe",),
    "mkvmerge": ("mkvmerge.exe",),
    "mkvextract": ("mkvextract.exe",),
    "mkvinfo": ("mkvinfo.exe",),
    "mkvpropedit": ("mkvpropedit.exe",),
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
    "mkvpropedit": ("MKVToolNix.MKVToolNix*",),
    "mediainfo": ("MediaArea.MediaInfo_*",),
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


def _section_bounds(lines: list[str], section: str) -> tuple[int, int]:
    start = -1
    end = len(lines)
    target = f"[{section.lower()}]"

    for index, line in enumerate(lines):
        if line.strip().lower() == target:
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


def _upsert_ini_section(
    lines: list[str],
    section: str,
    values: dict[str, str],
    *,
    replace_blank_only: bool = False,
) -> list[str]:
    start, end = _section_bounds(lines, section)

    if start == -1:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"[{section}]"])
        start = len(lines) - 1
        end = len(lines)

    insert_at = end
    for key, value in values.items():
        updated = False
        for index in range(start + 1, end):
            stripped = lines[index].strip()
            if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
                continue
            lhs, rhs = stripped.split("=", 1)
            if lhs.strip().lower() != key.lower():
                continue
            if (not replace_blank_only) or (not rhs.strip()):
                lines[index] = f"{key} = {value}"
            updated = True
            break

        if not updated:
            lines.insert(insert_at, f"{key} = {value}")
            insert_at += 1
            end += 1

    return lines


def _update_ini_tools_section(path: Path, tool_values: dict[str, str]) -> None:
    """Ajoute les chemins détectés dans [tools] sans écraser une valeur explicite."""
    if not tool_values:
        return

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    lines = _upsert_ini_section(lines, "tools", tool_values, replace_blank_only=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_ini_settings(section_values: dict[str, dict[str, str]]) -> None:
    """Écrit des valeurs explicites dans config.ini en conservant les commentaires."""
    text = _INI_PATH.read_text(encoding="utf-8") if _INI_PATH.exists() else ""
    lines = text.splitlines()

    for section, values in section_values.items():
        lines = _upsert_ini_section(lines, section, values)

    _INI_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


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
        elif tool_name in ("mkvmerge", "mkvextract", "mkvinfo", "mkvpropedit"):
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


def _default_language_code() -> str:
    candidates: list[str | None] = [
        os.environ.get("LC_ALL"),
        (os.environ.get("LANGUAGE") or "").split(":", 1)[0] or None,
        os.environ.get("LANG"),
    ]
    try:
        candidates.append(locale.getlocale()[0])
    except (TypeError, ValueError):
        pass

    for candidate in candidates:
        code = Rfc5646LanguageTags.from_locale_name(candidate)
        if code:
            return code
    return "eng"


def _normalize_language_code(code: str | None) -> str:
    if not code:
        return _default_language_code()
    raw = code.strip()
    if not raw:
        return _default_language_code()
    if len(raw) == 3:
        ietf = Rfc5646LanguageTags.from_iso639_2(raw)
        if ietf:
            canonical = Rfc5646LanguageTags.to_iso639_2(ietf) or raw.lower()
            return canonical.lower()
    converted = Rfc5646LanguageTags.from_locale_name(raw)
    return converted or _default_language_code()


INI_FIELD_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "section": "paths",
        "title": "Chemins",
        "fields": (
            {
                "key": "work_dir",
                "attr": "work_dir",
                "kind": "directory",
                "label": "Dossier de travail",
                "description": "Répertoire des fichiers intermédiaires utilisés par les workflows.",
            },
            {
                "key": "output_dir",
                "attr": "output_dir",
                "kind": "directory",
                "label": "Dossier de sortie",
                "description": "Répertoire par défaut des fichiers produits.",
            },
        ),
    },
    {
        "section": "tools",
        "title": "Outils externes",
        "fields": (
            {"key": "ffmpeg", "attr": "tool_ffmpeg", "kind": "tool", "label": "FFmpeg", "description": "Binaire FFmpeg utilisé pour l'encodage et certaines inspections."},
            {"key": "ffprobe", "attr": "tool_ffprobe", "kind": "tool", "label": "FFprobe", "description": "Binaire FFprobe utilisé pour l'analyse des médias."},
            {"key": "mkvmerge", "attr": "tool_mkvmerge", "kind": "tool", "label": "mkvmerge", "description": "Binaire MKVToolNix utilisé pour le remuxage."},
            {"key": "mkvextract", "attr": "tool_mkvextract", "kind": "tool", "label": "mkvextract", "description": "Binaire MKVToolNix utilisé pour extraire des pistes."},
            {"key": "mkvinfo", "attr": "tool_mkvinfo", "kind": "tool", "label": "mkvinfo", "description": "Binaire MKVToolNix utilisé pour l'inspection des conteneurs."},
            {"key": "mkvpropedit", "attr": "tool_mkvpropedit", "kind": "tool", "label": "mkvpropedit", "description": "Binaire MKVToolNix utilisé pour réécrire des métadonnées."},
            {"key": "mediainfo", "attr": "tool_mediainfo", "kind": "tool", "label": "MediaInfo", "description": "Binaire MediaInfo utilisé pour enrichir l'inspection."},
            {"key": "dovi_tool", "attr": "tool_dovi_tool", "kind": "tool", "label": "dovi_tool", "description": "Outil Dolby Vision utilisé pour les workflows DoVi."},
            {"key": "hdr10plus_tool", "attr": "tool_hdr10plus", "kind": "tool", "label": "hdr10plus_tool", "description": "Outil HDR10+ utilisé pour les workflows HDR."},
            {"key": "eac3to", "attr": "tool_eac3to", "kind": "tool", "label": "eac3to", "description": "Option facultative sous Windows pour la conversion audio avancée."},
        ),
    },
    {
        "section": "hdr",
        "title": "HDR",
        "fields": (
            {"key": "dovi_profile", "attr": "dovi_profile", "kind": "text", "label": "Profil DoVi", "description": "Profil Dolby Vision utilisé lors de l'injection RPU."},
            {"key": "dovi_compat_id", "attr": "dovi_compat_id", "kind": "text", "label": "Compatibility ID DoVi", "description": "Compatibility ID Dolby Vision appliqué lors de l'injection."},
        ),
    },
    {
        "section": "encoding",
        "title": "Encodage",
        "fields": (
            {"key": "ram_buffer_enabled", "attr": "ram_buffer_enabled", "kind": "bool", "label": "Buffer RAM activé", "description": "Active le buffer RAM pour les fichiers HEVC intermédiaires quand le seuil le permet."},
            {"key": "ram_buffer_threshold_pct", "attr": "ram_buffer_threshold_pct", "kind": "int", "label": "Seuil buffer RAM (%)", "description": "Pourcentage minimal de RAM libre à conserver."},
        ),
    },
    {
        "section": "ui",
        "title": "Interface",
        "fields": (
            {"key": "language", "attr": "language", "kind": "language", "label": "Langue de l'interface", "description": "Langue utilisée pour l'UI et les messages internes."},
            {"key": "log_max_lines", "attr": "log_max_lines", "kind": "int", "label": "Nombre max de lignes de log", "description": "Nombre maximum de lignes conservées dans le panneau de log."},
            {"key": "theme", "attr": "theme", "kind": "choice", "label": "Thème", "description": "Thème principal de l'interface.", "options": (("dark", "Sombre"), ("light", "Clair"))},
        ),
    },
)


def iter_ini_fields() -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for group in INI_FIELD_GROUPS:
        fields.extend(group["fields"])
    return fields


# ---------------------------------------------------------------------------
# AppConfig
# ---------------------------------------------------------------------------

class AppConfig:
    """
    Configuration centralisée de l'application.

    Les propriétés sont persistées via QSettings (INI) dans le dossier
    app data. config.ini a priorité sur les valeurs sauvegardées,
    elles-mêmes prioritaires sur les défauts.

    Une clé présente mais vide dans config.ini revient explicitement au défaut
    documenté au lieu de retomber sur une ancienne valeur QSettings.
    """

    _SETTINGS_ORG = "mediarecode"
    _SETTINGS_APP = "Mediarecode"

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
        self._persist_detected_windows_tools()

    def _persist_detected_windows_tools(self) -> None:
        if _is_windows() and self._detected_ini_tools:
            _update_ini_tools_section(_INI_PATH, self._detected_ini_tools)
            self._ini = _load_ini()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ini_lookup(self, section: str, key: str) -> str | object:
        if self._ini.has_option(section, key):
            return self._ini.get(section, key).strip()
        return _MISSING

    def _resolve_text(self, section: str, key: str, settings_key: str, default: str) -> str:
        ini_value = self._ini_lookup(section, key)
        if ini_value is not _MISSING:
            return default if ini_value == "" else str(ini_value)
        value = self._settings.value(settings_key, default)
        return str(value if value not in (None, "") else default)

    def _resolve_path(self, section: str, key: str, settings_key: str, default: Path) -> Path:
        return Path(self._resolve_text(section, key, settings_key, str(default)))

    def _resolve_int(self, section: str, key: str, settings_key: str, default: int) -> int:
        ini_value = self._ini_lookup(section, key)
        if ini_value is not _MISSING:
            return default if ini_value == "" else int(str(ini_value))
        value = self._settings.value(settings_key, default)
        return int(value if value not in (None, "") else default)

    def _resolve_bool(self, section: str, key: str, settings_key: str, default: bool) -> bool:
        def _as_bool(raw: str) -> bool:
            return raw.strip().lower() not in ("0", "false", "no", "off")

        ini_value = self._ini_lookup(section, key)
        if ini_value is not _MISSING:
            return default if ini_value == "" else _as_bool(str(ini_value))

        default_text = "true" if default else "false"
        value = self._settings.value(settings_key, default_text)
        return _as_bool(str(value if value not in (None, "") else default_text))

    def _resolve_tool_value(self, ini_key: str, settings_key: str, default: str) -> str:
        """Résout une valeur d'outil et persiste l'autodetect Windows dans config.ini."""
        ini_value = self._ini_lookup("tools", ini_key)
        if ini_value is not _MISSING:
            if ini_value != "":
                return str(ini_value)
            current_value = default
        else:
            raw = self._settings.value(settings_key, default)
            current_value = str(raw if raw not in (None, "") else default)

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
        self.work_dir = self._resolve_path("paths", "work_dir", "paths/work_dir", _default_work_dir())
        self.output_dir = self._resolve_path("paths", "output_dir", "paths/output_dir", _default_output_dir())
        self.app_data_dir = _app_data_dir()

        self.tool_ffmpeg = self._resolve_tool_value("ffmpeg", "tools/ffmpeg", "ffmpeg")
        self.tool_ffprobe = self._resolve_tool_value("ffprobe", "tools/ffprobe", "ffprobe")
        self.tool_mkvmerge = self._resolve_tool_value("mkvmerge", "tools/mkvmerge", "mkvmerge")
        self.tool_mkvextract = self._resolve_tool_value("mkvextract", "tools/mkvextract", "mkvextract")
        self.tool_mkvinfo = self._resolve_tool_value("mkvinfo", "tools/mkvinfo", "mkvinfo")
        self.tool_mkvpropedit = self._resolve_tool_value("mkvpropedit", "tools/mkvpropedit", "mkvpropedit")
        self.tool_mediainfo = self._resolve_tool_value("mediainfo", "tools/mediainfo", "mediainfo")
        self.tool_dovi_tool = self._resolve_tool_value("dovi_tool", "tools/dovi_tool", "dovi_tool")
        self.tool_hdr10plus = self._resolve_tool_value("hdr10plus_tool", "tools/hdr10plus_tool", "hdr10plus_tool")
        self.tool_eac3to = self._resolve_tool_value("eac3to", "tools/eac3to", "eac3to")

        self.dovi_profile = self._resolve_text("hdr", "dovi_profile", "hdr/dovi_profile", "8")
        self.dovi_compat_id = self._resolve_text("hdr", "dovi_compat_id", "hdr/dovi_compat_id", "1")

        self.ram_buffer_enabled = self._resolve_bool("encoding", "ram_buffer_enabled", "encoding/ram_buffer_enabled", True)
        self.ram_buffer_threshold_pct = self._resolve_int("encoding", "ram_buffer_threshold_pct", "encoding/ram_buffer_threshold_pct", 15)

        self.language = _normalize_language_code(
            self._resolve_text("ui", "language", "ui/language", _default_language_code())
        )
        self.log_max_lines = self._resolve_int("ui", "log_max_lines", "ui/log_max_lines", 2000)
        self.theme = self._resolve_text("ui", "theme", "ui/theme", "dark")
        self.window_geometry: bytes | None = self._settings.value("ui/geometry", None)

    def reload(self) -> None:
        self._ini = _load_ini()
        self._detected_ini_tools = {}
        self._load()
        self._persist_detected_windows_tools()

    # ------------------------------------------------------------------
    # Sauvegarde
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persiste la configuration dans QSettings pour les fallbacks runtime."""
        s = self._settings

        s.setValue("paths/work_dir", str(self.work_dir))
        s.setValue("paths/output_dir", str(self.output_dir))

        s.setValue("tools/ffmpeg", self.tool_ffmpeg)
        s.setValue("tools/ffprobe", self.tool_ffprobe)
        s.setValue("tools/mkvmerge", self.tool_mkvmerge)
        s.setValue("tools/mkvextract", self.tool_mkvextract)
        s.setValue("tools/mkvinfo", self.tool_mkvinfo)
        s.setValue("tools/mkvpropedit", self.tool_mkvpropedit)
        s.setValue("tools/mediainfo", self.tool_mediainfo)
        s.setValue("tools/dovi_tool", self.tool_dovi_tool)
        s.setValue("tools/hdr10plus_tool", self.tool_hdr10plus)
        s.setValue("tools/eac3to", self.tool_eac3to)

        s.setValue("hdr/dovi_profile", self.dovi_profile)
        s.setValue("hdr/dovi_compat_id", self.dovi_compat_id)

        s.setValue("encoding/ram_buffer_enabled", "true" if self.ram_buffer_enabled else "false")
        s.setValue("encoding/ram_buffer_threshold_pct", self.ram_buffer_threshold_pct)

        s.setValue("ui/language", self.language)
        s.setValue("ui/log_max_lines", self.log_max_lines)
        s.setValue("ui/theme", self.theme)
        s.sync()

    def save_to_ini(self) -> None:
        write_ini_settings(self.to_ini_sections())

    def save_geometry(self, geometry: bytes) -> None:
        self._settings.setValue("ui/geometry", geometry)
        self._settings.sync()

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------

    def tool_path(self, name: str) -> Path | None:
        attr = f"tool_{name.replace('-', '_')}"
        value: str = getattr(self, attr, name)
        found = shutil.which(value)
        return Path(found) if found else None

    def all_tools_available(self) -> dict[str, bool]:
        tools = {
            "ffmpeg": self.tool_ffmpeg,
            "ffprobe": self.tool_ffprobe,
            "mkvmerge": self.tool_mkvmerge,
            "mkvextract": self.tool_mkvextract,
            "mkvinfo": self.tool_mkvinfo,
            "mkvpropedit": self.tool_mkvpropedit,
            "mediainfo": self.tool_mediainfo,
            "dovi_tool": self.tool_dovi_tool,
            "hdr10plus_tool": self.tool_hdr10plus,
            "eac3to": self.tool_eac3to,
        }
        return {name: shutil.which(cmd) is not None for name, cmd in tools.items()}

    def ensure_work_dir(self) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return self.work_dir

    def ensure_output_dir(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir

    def to_ini_sections(self) -> dict[str, dict[str, str]]:
        section_values: dict[str, dict[str, str]] = {}
        for group in INI_FIELD_GROUPS:
            section = group["section"]
            section_values[section] = {}
            for field in group["fields"]:
                value = getattr(self, field["attr"])
                if isinstance(value, Path):
                    rendered = str(value)
                elif isinstance(value, bool):
                    rendered = "true" if value else "false"
                else:
                    rendered = str(value)
                section_values[section][field["key"]] = rendered
        return section_values

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths": {
                "work_dir": str(self.work_dir),
                "output_dir": str(self.output_dir),
                "app_data": str(self.app_data_dir),
            },
            "tools": {
                "ffmpeg": self.tool_ffmpeg,
                "ffprobe": self.tool_ffprobe,
                "mkvmerge": self.tool_mkvmerge,
                "mkvextract": self.tool_mkvextract,
                "mkvinfo": self.tool_mkvinfo,
                "mkvpropedit": self.tool_mkvpropedit,
                "mediainfo": self.tool_mediainfo,
                "dovi_tool": self.tool_dovi_tool,
                "hdr10plus_tool": self.tool_hdr10plus,
                "eac3to": self.tool_eac3to,
            },
            "hdr": {
                "dovi_profile": self.dovi_profile,
                "dovi_compat_id": self.dovi_compat_id,
            },
            "encoding": {
                "ram_buffer_enabled": self.ram_buffer_enabled,
                "ram_buffer_threshold_pct": self.ram_buffer_threshold_pct,
            },
            "ui": {
                "language": self.language,
                "log_max_lines": self.log_max_lines,
                "theme": self.theme,
            },
        }

    def __repr__(self) -> str:
        return f"AppConfig({json.dumps(self.to_dict(), indent=2)})"
