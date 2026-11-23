"""
core/tool_versions.py — Version et provenance des outils externes, pour le diagnostic.

Un outil embarqué (paquet all-inclusive) est décrit par ``tools-manifest.json``,
lu sans lancer de processus (utilisable au démarrage de l'interface). Un outil
système est sondé à la demande (``-version`` pour FFmpeg, ``--version`` sinon).
Partagé par l'info-bulle du tableau de bord, le journal des jobs et
l'opération ``system.tools``.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.subprocess_utils import subprocess_text_kwargs
from core.tool_manifest import UNKNOWN_BUILD, bundled_tool_entry, ffmpeg_build_details

#: Outils dont ``-version`` donne la build et les bibliothèques (FFmpeg).
_FFMPEG_TOOLS = frozenset({"ffmpeg", "ffprobe"})


@dataclass(frozen=True)
class ToolVersionReport:
    """Version d'un outil résolu, et sa provenance quand il est embarqué."""

    name: str
    path: str | None
    version: str | None = None
    bundled: bool = False
    release: str | None = None
    published: str | None = None
    verification: str | None = None
    libraries: dict[str, str] = field(default_factory=dict)

    def label(self) -> str:
        """Une ligne lisible : version, puis provenance (embarqué) ou chemin (système)."""
        if self.path is None:
            return "introuvable"
        version = self.version or "version inconnue"
        if self.bundled:
            origin = ", ".join(part for part in (self.release, self.published) if part)
            return f"{version} — embarqué" + (f" ({origin})" if origin else "")
        return f"{version} — {self.path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "version": self.version,
            "bundled": self.bundled,
            "release": self.release,
            "published": self.published,
            "verification": self.verification,
            "libraries": dict(self.libraries),
        }


def resolve_tool(command: str | Path | None) -> Path | None:
    """Chemin de l'exécutable désigné par une commande (nom du PATH ou chemin)."""
    if not command:
        return None
    found = shutil.which(str(command))
    if found:
        return Path(found)
    candidate = Path(command)
    return candidate if candidate.is_file() else None


def _meaningful(version: object) -> str | None:
    """Version utile d'un manifeste (les anciens consignaient « dépôt@tag » ou « ? »)."""
    text = str(version or "").strip()
    if not text or text in {"?", UNKNOWN_BUILD} or "@" in text:
        return None
    return text


def _generic_version(path: Path) -> str | None:
    """Première ligne utile de ``--version`` (deux lignes jointes si la première se poursuit)."""
    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, timeout=10, check=False,
            **{**subprocess_text_kwargs(), "errors": "replace"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in ((result.stdout or "") or (result.stderr or "")).splitlines() if line.strip()]
    if not lines:
        return None
    first = lines[0]
    if first.endswith(",") and len(lines) > 1:
        first = f"{first} {lines[1]}"
    return first


def tool_version_report(name: str, command: str | Path | None, *, probe: bool = True) -> ToolVersionReport:
    """Version et provenance de l'outil ``name`` désigné par ``command``.

    ``probe=False`` : aucun processus lancé (manifeste seulement) ; un outil
    système garde alors une version inconnue.
    """
    path = resolve_tool(command)
    if path is None:
        return ToolVersionReport(name=name, path=None)
    entry = bundled_tool_entry(path)
    details = dict(entry.get("details") or {}) if entry else {}
    version = _meaningful(entry.get("version")) if entry else None
    if name == "ffprobe" and entry:
        version = _meaningful(details.get("ffprobe_build")) or version
    libraries = {key: str(details[key]) for key in ("libavformat", "libavcodec") if key in details}
    if version is None and probe:
        if name in _FFMPEG_TOOLS:
            probed = ffmpeg_build_details(path)
            version = _meaningful(probed.get("build"))
            libraries = {key: probed[key] for key in ("libavformat", "libavcodec") if key in probed}
        else:
            version = _generic_version(path)
    if entry is None:
        return ToolVersionReport(name=name, path=str(path), version=version, libraries=libraries)
    release = details.get("release") or (str(entry.get("version")) if "@" in str(entry.get("version") or "") else None)
    return ToolVersionReport(
        name=name, path=str(path), version=version, bundled=True,
        release=release or None, published=details.get("asset_updated_at") or None,
        verification=str(entry.get("verification") or "") or None, libraries=libraries,
    )


__all__ = ["ToolVersionReport", "resolve_tool", "tool_version_report"]
