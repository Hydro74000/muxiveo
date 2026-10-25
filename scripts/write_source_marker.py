"""Écrit le marqueur `.muxiveo-installation` d'une archive source : version et fichiers livrés.

Usage : python scripts/write_source_marker.py --version X.Y.Z[-unstable…] --out <fichier> [--extra <chemin> …]

Ajouté par `release.yml` à `Muxiveo-<version>-source.tar.gz` / `.zip` (`git archive --add-file`). Une installation
extraite de cette archive se met à jour d'un clic (`core/update_apply.py`) : les fichiers listés sont remplacés,
tout le reste (config.ini, environnement virtuel…) est reporté dans la nouvelle version.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent


def tracked_files(repo: Path) -> list[str]:
    """Fichiers suivis de HEAD (contenu de `git archive HEAD`, sans attribut export-ignore dans ce dépôt)."""
    output = subprocess.run(["git", "ls-files", "-z"], cwd=repo, check=True, capture_output=True).stdout  # nosec B603 B607
    return [name for name in output.decode("utf-8").split("\0") if name]


def build_marker(version: str, files: list[str]) -> dict:
    """Contenu du marqueur ; chemins relatifs POSIX sans échappement."""
    for name in files:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError(f"chemin refusé : {name}")
    return {"schema": 1, "version": version, "files": sorted(set(files))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Écrit le marqueur d'installation d'une archive source.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--extra", action="append", default=[], help="Fichier ajouté à l'archive hors suivi git.")
    args = parser.parse_args(argv)
    try:
        marker = build_marker(args.version, tracked_files(ROOT) + list(args.extra))
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(marker, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"marqueur {args.out} : {len(marker['files'])} fichier(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
