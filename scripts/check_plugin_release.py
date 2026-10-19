"""Vérifie que la release épinglée de chaque extension (core/version.py) est publiée et vérifiable.

Muxiveo propose l'installation des extensions mvo-rife (interpolation, préinstallée dans les paquets hors ligne)
et mvo-rife-trt (accélération NVIDIA) : la version épinglée de chacune doit exister dans le dépôt des extensions,
avec une archive par plate-forme dont GitHub publie le SHA-256. Code 1 sinon.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.version import (  # noqa: E402
    MUXIVEO_PLUGINS_REPOSITORY,
    MVO_RIFE_RELEASE_TAG,
    MVO_RIFE_TRT_RELEASE_TAG,
    MVO_RIFE_TRT_VERSION,
    MVO_RIFE_VERSION,
)

# extension → (tag, version, plate-forme → extension de l'archive)
EXTENSIONS: dict[str, tuple[str, str, dict[str, str]]] = {
    "mvo-rife": (MVO_RIFE_RELEASE_TAG, MVO_RIFE_VERSION,
                 {"linux-x86_64": ".tar.gz", "windows-x86_64": ".zip", "macos-arm64": ".tar.gz"}),
    "mvo-rife-trt": (MVO_RIFE_TRT_RELEASE_TAG, MVO_RIFE_TRT_VERSION,
                     {"linux-x86_64": ".tar.gz", "windows-x86_64": ".zip"}),
}


def _release(tag: str) -> dict | None:
    url = f"https://api.github.com/repos/{MUXIVEO_PLUGINS_REPOSITORY}/releases/tags/{tag}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Muxiveo-release"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:  # nosec B310
            return json.loads(resp.read().decode("utf-8"))
    except OSError as exc:
        print(f"::error::Release {tag} introuvable dans {MUXIVEO_PLUGINS_REPOSITORY} ({exc}) : "
              "publier l'extension avant Muxiveo")
        return None


def check(name: str, tag: str, version: str, platforms: dict[str, str]) -> bool:
    release = _release(tag)
    if release is None:
        return False
    if release.get("draft") or release.get("prerelease"):
        print(f"::error::Release {tag} en brouillon ou pré-version")
        return False
    assets = {str(a.get("name")): a for a in release.get("assets") or []}
    ok = True
    for platform, suffix in platforms.items():
        asset = f"{name}-{version}-{platform}{suffix}"
        digest = str((assets.get(asset) or {}).get("digest") or "")
        if not digest.startswith("sha256:"):
            print(f"::error::{tag} : {asset} absent ou sans SHA-256 publié")
            ok = False
        else:
            print(f"{asset} : {digest}")
    return ok


def main() -> int:
    results = [check(name, *spec) for name, spec in EXTENSIONS.items()]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
