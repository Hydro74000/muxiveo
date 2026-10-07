"""
core/tool_manifest.py — Manifeste des outils embarqués dans un bundle.

Chaque outil téléchargé par le packaging y est consigné avec sa version ou son
tag, l'URL de l'archive, son SHA-256, ceux des binaires extraits et la
méthode de vérification : un même commit reconstruit avec d'autres versions
d'outils devient visible, et l'intégrité de chaque binaire est traçable.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_text
from core.github_release import file_sha256

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
    ) -> None:
        self.entries.append({
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
        })

    def write(self, directory: Path) -> Path:
        path = Path(directory) / MANIFEST_NAME
        payload = {"version": 1, "tools": sorted(self.entries, key=lambda entry: entry["tool"])}
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return path


__all__ = [
    "LOCAL_ARCHIVE", "MANIFEST_NAME", "PREEXISTING", "RECORDED_ONLY", "ToolManifest",
    "VERIFIED_GITHUB_DIGEST", "VERIFIED_PUBLISHED_CHECKSUM",
]
