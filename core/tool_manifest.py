"""
core/tool_manifest.py — Manifeste des outils embarqués dans un bundle.

Chaque outil téléchargé par le packaging y est consigné avec sa version ou son
tag, l'URL de l'archive, son SHA-256, ceux des binaires extraits et la
méthode de vérification : un même commit reconstruit avec d'autres versions
d'outils devient visible, et l'intégrité de chaque binaire est traçable.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_text
from core.github_release import file_sha256
from core.subprocess_utils import subprocess_text_kwargs

MANIFEST_NAME = "tools-manifest.json"

#: Somme publiée par l'API GitHub (champ ``digest``) comparée à l'archive.
VERIFIED_GITHUB_DIGEST = "github-digest"
#: Somme publiée par l'éditeur (fichier ``.sha256``) comparée à l'archive.
VERIFIED_PUBLISHED_CHECKSUM = "published-checksum"
#: Aucune somme publiée : SHA-256 consigné, sans comparaison possible.
RECORDED_ONLY = "recorded-only"
#: Archive locale fournie au build (``MUXIVEO_RIFE_ARCHIVE``…).
LOCAL_ARCHIVE = "local-archive"
#: Binaire déjà présent dans le dossier d'outils réutilisé.
PREEXISTING = "preexisting"
#: Build d'un binaire impossible à exécuter sur l'hôte de packaging.
UNKNOWN_BUILD = "inconnue"

_VERSION_LINE = re.compile(r"^\S+ version (\S+)")
_LIBRARY_LINE = re.compile(r"^\s*(libavformat|libavcodec)\s+(\d+)\.\s*(\d+)\.\s*(\d+)\s*/", re.MULTILINE)


def ffmpeg_build_details(
    binary: Path,
    *,
    launcher: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    timeout: float = 60,
) -> dict[str, str]:
    """Build réelle d'un ffmpeg / ffprobe (``-version``), pour le manifeste et le diagnostic.

    ``launcher`` préfixe la commande (``("wine",)`` : binaire Windows sondé sous
    Linux). Renvoie ``build`` (ex. ``N-129166-gae9f4540cb-20261121``, ou
    :data:`UNKNOWN_BUILD` si le binaire ne s'exécute pas), ``version_line`` et les
    versions de libavformat / libavcodec quand elles sont affichées.
    """
    try:
        result = subprocess.run(
            [*launcher, str(binary), "-hide_banner", "-version"],
            capture_output=True, timeout=timeout, check=False,
            env=dict(env) if env is not None else None,
            **{**subprocess_text_kwargs(), "errors": "replace"},
        )
    except (OSError, subprocess.SubprocessError):
        return {"build": UNKNOWN_BUILD}
    output = (result.stdout or "").replace("\r\n", "\n")
    first = next((line.strip() for line in output.splitlines() if line.strip()), "")
    match = _VERSION_LINE.match(first)
    if result.returncode != 0 or match is None:
        return {"build": UNKNOWN_BUILD}
    details = {"build": match.group(1), "version_line": first}
    for library, major, minor, micro in _LIBRARY_LINE.findall(output):
        details[library] = f"{major}.{minor}.{micro}"
    return details


def is_master_build(build: str) -> bool:
    """Build issue de la branche de développement de FFmpeg (``N-<révision>-g<commit>-<date>``)."""
    return build.startswith("N-")


@dataclass
class ToolManifest:
    """Entrées de provenance, écrites en JSON trié dans le dossier d'outils."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        tool: str,
        *,
        version: str,
        source: str,
        verification: str,
        archive: Path | None = None,
        files: Iterable[Path] = (),
        details: Mapping[str, str] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "tool": tool,
            "version": version,
            "source": source,
            "verification": verification,
            "archive_sha256": file_sha256(archive) if archive is not None and archive.is_file() else None,
            "files": {
                path.name: file_sha256(path)
                for path in sorted(files, key=lambda item: item.name)
                if path.is_file()
            },
        }
        if details:
            entry["details"] = dict(sorted(details.items()))
        self.entries.append(entry)

    def write(self, directory: Path) -> Path:
        path = Path(directory) / MANIFEST_NAME
        payload = {"version": 1, "tools": sorted(self.entries, key=lambda entry: entry["tool"])}
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return path


def bundled_tool_entry(binary: Path | str | None) -> dict[str, Any] | None:
    """Entrée du manifeste décrivant ``binary`` s'il s'agit d'un outil embarqué.

    Le manifeste est cherché à côté du binaire (dossier d'outils du bundle) ;
    l'entrée doit lister ce fichier. Aucun processus lancé : utilisable au
    démarrage de l'interface. ``None`` pour un outil système ou sans manifeste.
    """
    if not binary:
        return None
    path = Path(binary)
    manifest = path.parent / MANIFEST_NAME
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    for entry in payload.get("tools") or ():
        if isinstance(entry, dict) and path.name in (entry.get("files") or {}):
            return entry
    return None


__all__ = [
    "LOCAL_ARCHIVE", "MANIFEST_NAME", "PREEXISTING", "RECORDED_ONLY", "ToolManifest", "UNKNOWN_BUILD",
    "VERIFIED_GITHUB_DIGEST", "VERIFIED_PUBLISHED_CHECKSUM", "bundled_tool_entry", "ffmpeg_build_details",
    "is_master_build",
]
