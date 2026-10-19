"""Écrit le flux de mise à jour d'un canal (`<canal>.json`), lu par Muxiveo à la place de la liste des releases.

Usage : python scripts/write_update_feed.py --channel stable|unstable --tag vX.Y.Z[-unstable…] --repo owner/repo
                                            --artifacts <dossier des fichiers publiés> --out <fichier .json>

Appelé par `release.yml` en dernière étape, une fois la release et ses fichiers publiés : le flux ne pointe
jamais vers un fichier absent. Les sommes SHA-256 restent dans l'asset SHA256SUMS de la release (vérifiées
par la mise à jour intégrée) ; le flux ne liste que les fichiers.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

FEED_SCHEMA = 1
CHANNELS = ("stable", "unstable")
_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(-unstable\.\d+\.\d+\.[0-9a-f]+)?$")


def build_feed(channel: str, tag: str, repo: str, artifacts: Path, published: str | None = None) -> dict:
    """Contenu du flux d'un canal : version, page de la release, fichiers publiés (nom, URL, taille)."""
    if channel not in CHANNELS:
        raise ValueError(f"canal inconnu : {channel}")
    if not _TAG_RE.match(tag):
        raise ValueError(f"tag de release inattendu : {tag}")
    if (channel == "unstable") != ("-unstable." in tag):
        raise ValueError(f"tag {tag} incohérent avec le canal {channel}")
    files = sorted(p for p in artifacts.iterdir() if p.is_file())
    if not files:
        raise ValueError(f"aucun fichier publié dans {artifacts}")
    base = f"https://github.com/{repo}/releases"
    return {
        "schema": FEED_SCHEMA,
        "channel": channel,
        "version": tag[1:],
        "tag": tag,
        "url": f"{base}/tag/{tag}",
        "published": published or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "assets": [{"name": p.name, "url": f"{base}/download/{tag}/{p.name}", "size": p.stat().st_size}
                   for p in files],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Écrit le flux de mise à jour d'un canal.")
    parser.add_argument("--channel", required=True, choices=CHANNELS)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        feed = build_feed(args.channel, args.tag, args.repo, args.artifacts)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(feed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"flux {args.channel} : {feed['tag']}, {len(feed['assets'])} fichier(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
