#!/usr/bin/env python3
"""Vérifie qu'un paquet all-inclusive embarque l'extension mvo-rife préinstallée (version épinglée, contrat,
SHA-256 du moteur, des modèles, des poids du sélecteur et des préréglages d'après son manifest.json).

Le packaging se contente d'un avertissement quand l'extension est indisponible :
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

from core.version import MVO_RIFE_CONTRACT, MVO_RIFE_VERSION  # noqa: E402

_TOOLS_MEMBER = re.compile(r"(^|/)tools/(muxiveo-rife(\.exe)?$|rife-models/|manifest\.json$|presets\.json$)")


def _extract(package: Path, dest: Path) -> None:
    """Extrait muxiveo-rife, rife-models/, manifest.json et presets.json de ``package`` dans ``dest``."""
    name = package.name
    if name.endswith(".AppImage"):
        package.chmod(package.stat().st_mode | 0o111)
        for pattern in ("usr/bin/tools/muxiveo-rife", "usr/bin/tools/rife-models/*", "usr/bin/tools/manifest.json",
                        "usr/bin/tools/presets.json"):
            subprocess.run(
                [str(package.resolve()), "--appimage-extract", pattern],
                cwd=dest, check=True, stdout=subprocess.DEVNULL,
            )
    elif name.endswith(".zip"):
        with zipfile.ZipFile(package) as zf:
            zf.extractall(dest, [n for n in zf.namelist() if _TOOLS_MEMBER.search(n)])
    elif name.endswith(".exe"):
        subprocess.run(
            ["7z", "x", str(package), f"-o{dest}", "tools/muxiveo-rife.exe", "tools/rife-models", "tools/manifest.json",
             "tools/presets.json", "-r", "-y"],
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
            if version != MVO_RIFE_VERSION:
                errors.append(f"version {version or 'illisible'} != {MVO_RIFE_VERSION} ({(out.stdout or out.stderr).strip()})")
            else:
                print(f"  {exe.name} {version}")
        try:
            manifest = json.loads((exe.parent / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return errors + [f"manifest.json de l'extension illisible ({exc})"]
        if manifest.get("name") != "mvo-rife" or manifest.get("version") != MVO_RIFE_VERSION:
            errors.append(f"extension {manifest.get('name')} {manifest.get('version')} au lieu de mvo-rife {MVO_RIFE_VERSION}")
        if manifest.get("contract") != MVO_RIFE_CONTRACT:
            errors.append(f"contrat {manifest.get('contract')} au lieu de {MVO_RIFE_CONTRACT}")
        checked = 0
        for rel, digest in (manifest.get("files") or {}).items():
            if not (rel.startswith("rife-models/") or rel in (exe.name, "presets.json")):
                continue
            path = exe.parent / rel
            if not path.is_file():
                errors.append(f"fichier manquant : {rel}")
            elif _sha256(path) != digest:
                errors.append(f"sha256 inattendu : {rel}")
            checked += 1
        if not errors:
            print(f"  extension mvo-rife {MVO_RIFE_VERSION} : {checked} fichiers vérifiés ({', '.join(manifest.get('models') or [])})")
        return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Vérifie l'extension mvo-rife dans des paquets all-inclusive.")
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
