"""Vérifie que la release épinglée de chaque extension (core/version.py) est publiée et vérifiable.

Muxiveo propose l'installation de l'extension mvo-rife-trt : sa release doit exister dans le dépôt des
extensions, avec une archive Linux et Windows dont GitHub publie le SHA-256. Code 1 sinon.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.version import MUXIVEO_PLUGINS_REPOSITORY, MVO_RIFE_TRT_RELEASE_TAG, MVO_RIFE_TRT_VERSION  # noqa: E402

PLATFORMS = {"linux-x86_64": ".tar.gz", "windows-x86_64": ".zip"}


def main() -> int:
    url = f"https://api.github.com/repos/{MUXIVEO_PLUGINS_REPOSITORY}/releases/tags/{MVO_RIFE_TRT_RELEASE_TAG}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Muxiveo-release"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:  # nosec B310
            release = json.loads(resp.read().decode("utf-8"))
    except OSError as exc:
        print(f"::error::Release {MVO_RIFE_TRT_RELEASE_TAG} introuvable dans {MUXIVEO_PLUGINS_REPOSITORY} ({exc}) : "
              "publier l'extension avant Muxiveo")
        return 1
    if release.get("draft") or release.get("prerelease"):
        print(f"::error::Release {MVO_RIFE_TRT_RELEASE_TAG} en brouillon ou pré-version")
        return 1
    assets = {str(a.get("name")): a for a in release.get("assets") or []}
    ok = True
    for platform, suffix in PLATFORMS.items():
        name = f"mvo-rife-trt-{MVO_RIFE_TRT_VERSION}-{platform}{suffix}"
        digest = str((assets.get(name) or {}).get("digest") or "")
        if not digest.startswith("sha256:"):
            print(f"::error::{MVO_RIFE_TRT_RELEASE_TAG} : {name} absent ou sans SHA-256 publié")
            ok = False
        else:
            print(f"{name} : {digest}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
