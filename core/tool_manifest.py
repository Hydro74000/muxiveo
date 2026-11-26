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
    """Entrées de provenance, écrites en JSON trié dans le dossier d'outils.

    ``python`` : inventaire des distributions Python du build (nom, version) ; ``None`` : interpréteur courant,
    relevé à l'écriture (build croisé Wine : inventaire du préfixe fourni par le packaging)."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    python: list[dict[str, str]] | None = None

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
        python = self.python if self.python is not None else python_inventory()
        payload = {"version": 1, "tools": sorted(self.entries, key=lambda entry: entry["tool"]),
                   "python": sorted(python, key=lambda item: item["name"].lower())}
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return path


def python_inventory(command: Sequence[str] | None = None,
                     env: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """Distributions Python installées (nom, version) : interpréteur courant (``importlib.metadata``) ou
    ``command`` (autre interpréteur, ex. Python du préfixe Wine : ``pip list --format=json``). Traçabilité des
    dépendances d'un build, sans verrouillage (décision DE)."""
    if command is None:
        from importlib import metadata

        found: dict[str, str] = {}
        for distribution in metadata.distributions():
            name = distribution.metadata["Name"] or ""
            if name and name.lower() not in found:
                found[name.lower()] = f"{name}\0{distribution.version}"
        return [{"name": value.split("\0")[0], "version": value.split("\0")[1]} for value in found.values()]
    # Interpréteur du build et options fixes, sans shell ; ``env`` : environnement de cet interpréteur (préfixe Wine).
    result = subprocess.run([*command, "-m", "pip", "list", "--format=json", "--disable-pip-version-check"],  # nosec B603
                            capture_output=True, text=True, check=True, timeout=300,
                            env=dict(env) if env is not None else None)
    items = json.loads(result.stdout or "[]")
    return [{"name": str(item["name"]), "version": str(item["version"])} for item in items
            if isinstance(item, dict) and "name" in item and "version" in item]


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
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, list):
        return None
    for entry in tools:
        if not isinstance(entry, dict):
            continue
        files = entry.get("files")
        if not isinstance(files, dict) or path.name not in files:
            continue
        details = entry.get("details")
        # Métadonnées mal typées : ignorées (diagnostic seulement, jamais bloquant).
        clean = {key: value for key, value in entry.items() if key != "details"}
        if isinstance(details, dict):
            clean["details"] = {str(key): str(value) for key, value in details.items()
                                if isinstance(value, (str, int, float))}
        if not isinstance(clean.get("version"), str):
            clean["version"] = ""
        return clean
    return None


__all__ = [
    "LOCAL_ARCHIVE", "MANIFEST_NAME", "PREEXISTING", "RECORDED_ONLY", "ToolManifest", "UNKNOWN_BUILD",
    "VERIFIED_GITHUB_DIGEST", "VERIFIED_PUBLISHED_CHECKSUM", "bundled_tool_entry", "ffmpeg_build_details",
    "is_master_build",
]
