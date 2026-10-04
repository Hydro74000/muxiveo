#!/usr/bin/env python3
"""Contrôle bloquant du runtime MVTools dans les paquets tout inclus."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.native_mvtools import bundle_errors  # noqa: E402


def check(package: Path) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="mvtools-check-") as tmp:
        dest = Path(tmp)
        if package.name.endswith(".AppImage"):
            for pattern in ("usr/bin/tools/muxiveo-mvtools", "usr/bin/tools/mvtools-runtime/*"):
                subprocess.run([str(package.resolve()), "--appimage-extract", pattern],
                               cwd=dest, check=True, stdout=subprocess.DEVNULL)
        elif package.suffix == ".exe":
            subprocess.run(["7z", "x", str(package.resolve()), f"-o{dest}",
                            "tools/muxiveo-mvtools.exe", "tools/mvtools-runtime", "-r", "-y"],
                           check=True, stdout=subprocess.DEVNULL)
        elif package.suffix == ".zip":
            with zipfile.ZipFile(package) as zf:
                for member in zf.infolist():
                    path = PurePosixPath(member.filename)
                    if path.is_absolute() or ".." in path.parts or "\\" in member.filename or ":" in member.filename:
                        raise ValueError("chemin d'archive invalide")
                    if "tools" in path.parts and (path.name == "muxiveo-mvtools.exe" or "mvtools-runtime" in path.parts) and not member.is_dir():
                        target = dest / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(zf.read(member))
        else:
            raise ValueError("format de paquet non géré")
        binaries = [p for p in dest.rglob("*") if p.name in ("muxiveo-mvtools", "muxiveo-mvtools.exe")]
        if len(binaries) != 1:
            return ["muxiveo-mvtools absent ou dupliqué"]
        binary = binaries[0]
        render = binary.suffix != ".exe" or os.name == "nt"
        return bundle_errors(binary, render=render)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packages", nargs="+", type=Path)
    args = parser.parse_args()
    errors = []
    for package in args.packages:
        found = check(package)
        print(package.name + " : " + (" ; ".join(found) if found else "MVTools complet, contrôles réussis"))
        errors.extend(found)
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
