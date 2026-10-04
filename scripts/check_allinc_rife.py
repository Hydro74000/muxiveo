#!/usr/bin/env python3
"""Vérifie qu'un paquet all-inclusive embarque muxiveo-rife (version épinglée + modèles).

Le packaging se contente d'un avertissement quand muxiveo-rife est indisponible :
ce contrôle fait échouer la release dans ce cas.

Usage : python scripts/check_allinc_rife.py <paquet>...
  .AppImage → extraction ciblée (``--appimage-extract``)
  .zip      → archive portable Windows
  .exe      → installateur NSIS (7-Zip)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.version import MUXIVEO_RIFE_VERSION  # noqa: E402

MODELS: dict = json.loads((ROOT / "native" / "muxiveo-rife" / "models.json").read_text(encoding="utf-8"))["models"]
_TOOLS_MEMBER = re.compile(r"(^|/)tools/(muxiveo-rife(\.exe)?$|rife-models/)")


def _extract(package: Path, dest: Path) -> None:
    """Extrait muxiveo-rife et rife-models/ de ``package`` dans ``dest``."""
    name = package.name
    if name.endswith(".AppImage"):
        package.chmod(package.stat().st_mode | 0o111)
        for pattern in ("usr/bin/tools/muxiveo-rife", "usr/bin/tools/rife-models/*"):
            subprocess.run(
                [str(package.resolve()), "--appimage-extract", pattern],
                cwd=dest, check=True, stdout=subprocess.DEVNULL,
            )
    elif name.endswith(".zip"):
        with zipfile.ZipFile(package) as zf:
            zf.extractall(dest, [n for n in zf.namelist() if _TOOLS_MEMBER.search(n)])
    elif name.endswith(".exe"):
        subprocess.run(
            ["7z", "x", str(package), f"-o{dest}", "tools/muxiveo-rife.exe", "tools/rife-models", "-r", "-y"],
            check=True, stdout=subprocess.DEVNULL,
        )
    else:
        raise ValueError(f"format de paquet non géré : {name}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check(package: Path) -> list[str]:
    """Renvoie la liste des anomalies (vide si muxiveo-rife est correctement embarqué)."""
    with tempfile.TemporaryDirectory(prefix="rife-check-") as tmp:
        dest = Path(tmp)
        _extract(package, dest)
        binaries = [p for p in dest.rglob("*") if p.is_file() and p.name in ("muxiveo-rife", "muxiveo-rife.exe")]
        if not binaries:
            return ["muxiveo-rife absent"]
        exe = binaries[0]
        errors: list[str] = []
        if exe.suffix == ".exe" and os.name != "nt":
            print(f"  {exe.name} : version non vérifiée (hôte non Windows)")
        else:
            out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60, check=False)
            match = re.search(r"muxiveo-rife (\d+\.\d+\.\d+)", out.stdout or "")
            version = match.group(1) if match else None
            if version != MUXIVEO_RIFE_VERSION:
                errors.append(f"version {version or 'illisible'} != {MUXIVEO_RIFE_VERSION} ({(out.stdout or out.stderr).strip()})")
            else:
                print(f"  {exe.name} {version}")
        models_dir = exe.parent / "rife-models"
        for model, spec in MODELS.items():
            for filename, meta in spec["files"].items():
                path = models_dir / model / filename
                if not path.is_file():
                    errors.append(f"modèle manquant : rife-models/{model}/{filename}")
                elif _sha256(path) != meta["sha256"]:
                    errors.append(f"sha256 inattendu : rife-models/{model}/{filename}")
        if not errors:
            print(f"  modèles : {', '.join(MODELS)}")
        return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Vérifie muxiveo-rife dans des paquets all-inclusive.")
    parser.add_argument("packages", nargs="+", type=Path)
    args = parser.parse_args()
    failed = False
    for package in args.packages:
        print(f"{package.name} :")
        errors = check(package)
        for error in errors:
            print(f"::error::{package.name} : {error}")
        failed |= bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
