"""
core/github_release.py — Releases GitHub des outils tiers : sélection d'asset et vérification SHA-256.

Aucune version figée : dernière release par défaut, tag explicite possible via
``MUXIVEO_<OUTIL>_TAG`` (ex. ``MUXIVEO_DOVI_TOOL_TAG=2.3.4``). Les métadonnées
sont lues sur l'API GitHub (HTTPS vérifié) ; un asset n'est accepté que si son
SHA-256 correspond à la somme publiée par GitHub (champ ``digest``). Asset sans
somme publiée (release antérieure à mi-2025) ou somme différente : refus.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: Outils tiers téléchargés depuis GitHub → dépôt amont.
THIRD_PARTY_TOOLS: dict[str, str] = {
    "dovi_tool": "quietvoid/dovi_tool",
    "hdr10plus_tool": "quietvoid/hdr10plus_tool",
    "nvencc": "rigaya/NVEnc",
}

#: Fichiers annexes publiés à côté des binaires (sommes, signatures) : jamais téléchargés comme outil.
_SIDECAR_SUFFIXES = (".sha256", ".sha512", ".md5", ".asc", ".sig")


@dataclass(frozen=True)
class ReleaseAsset:
    """Asset de release : URL de téléchargement et SHA-256 publié par GitHub."""

    repo: str
    tag: str
    name: str
    url: str
    sha256: str


def requested_tag(tool: str) -> str | None:
    """Tag demandé via ``MUXIVEO_<OUTIL>_TAG``, ``None`` = dernière release."""
    value = os.environ.get(f"MUXIVEO_{tool.upper()}_TAG", "").strip()
    return value or None


def release_api_url(repo: str, tag: str | None = None) -> str:
    """URL API de la release ``tag`` (ou de la dernière release)."""
    endpoint = f"tags/{urllib.parse.quote(tag, safe='')}" if tag else "latest"
    return f"https://api.github.com/repos/{repo}/releases/{endpoint}"


def fetch_release(tool: str, *, user_agent: str = "Muxiveo-builder", timeout: float = 30) -> dict:
    """Métadonnées de la release de ``tool`` (tag demandé ou dernière)."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": user_agent}
    # Jeton facultatif (limite anonyme de l'API vite atteinte en CI), envoyé à api.github.com seulement.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = release_api_url(THIRD_PARTY_TOOLS[tool], requested_tag(tool))
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310  # HTTPS api.github.com constant
        return json.loads(resp.read().decode("utf-8"))


def asset_sha256(asset: dict) -> str | None:
    """SHA-256 publié par GitHub (``digest``) d'un asset de l'API, ``None`` si absent."""
    digest = str(asset.get("digest") or "")
    return digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None


def select_asset(release: dict, repo: str, *patterns: str) -> ReleaseAsset:
    """Unique asset de ``release`` dont le nom contient tous les ``patterns`` (annexes exclues).

    Refuse un asset sans SHA-256 publié ou hors de ``github.com/<repo>/releases/download/``.
    """
    tag = str(release.get("tag_name") or "?")
    assets = release.get("assets", [])
    matches = [
        a for a in assets
        if all(p in str(a.get("name") or "") for p in patterns)
        and not str(a.get("name") or "").endswith(_SIDECAR_SUFFIXES)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"{repo} {tag} : {len(matches)} asset(s) pour {patterns} "
            f"(disponibles : {sorted(str(a.get('name')) for a in assets)})"
        )
    asset = matches[0]
    name = str(asset["name"])
    url = str(asset.get("browser_download_url") or "")
    if not url.startswith(f"https://github.com/{repo}/releases/download/"):
        raise RuntimeError(f"{repo} {tag} : URL inattendue pour {name} : {url!r}")
    sha256 = asset_sha256(asset)
    if sha256 is None:
        raise RuntimeError(
            f"{repo} {tag} : aucun SHA-256 publié par GitHub pour {name}, "
            "intégrité invérifiable — téléchargement refusé."
        )
    return ReleaseAsset(repo, tag, name, url, sha256)


def release_asset(tool: str, *patterns: str, user_agent: str = "Muxiveo-builder") -> ReleaseAsset:
    """Asset ``patterns`` de la release de ``tool`` (tag demandé ou dernière)."""
    return select_asset(fetch_release(tool, user_agent=user_agent), THIRD_PARTY_TOOLS[tool], *patterns)


def file_sha256(path: Path) -> str:
    """SHA-256 hexadécimal d'un fichier (lecture par blocs)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_download(asset: ReleaseAsset, path: Path) -> None:
    """Lève ``RuntimeError`` si ``path`` ne correspond pas au SHA-256 publié."""
    actual = file_sha256(path)
    if actual != asset.sha256:
        raise RuntimeError(
            f"SHA-256 invalide pour {asset.name} ({asset.repo} {asset.tag}) : attendu {asset.sha256}, "
            f"obtenu {actual}. Téléchargement refusé."
        )


__all__ = [
    "THIRD_PARTY_TOOLS",
    "ReleaseAsset",
    "asset_sha256",
    "fetch_release",
    "file_sha256",
    "release_api_url",
    "release_asset",
    "requested_tag",
    "select_asset",
    "verify_download",
]
