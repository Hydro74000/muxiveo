"""Installe un paquet FEL local via le gestionnaire Extensions, sans publication.

Depuis la racine : python3 -m scripts.install_fel_prototype PAQUET.tar.gz.
--root DOSSIER permet une installation de test isolée de celle de l'utilisateur.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path

from core import plugins
from core.fel.engine import FelEngine
from core.github_release import ReleaseAsset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    archive = args.archive.resolve(strict=True)
    with tarfile.open(archive, "r:gz") as bundle:
        manifests = [m for m in bundle.getmembers()
                     if len(Path(m.name).parts) == 2 and Path(m.name).name == "manifest.json"]
        if len(manifests) != 1 or not manifests[0].isfile() or manifests[0].size > 65536:
            parser.error("Manifeste FEL absent ou invalide.")
        stream = bundle.extractfile(manifests[0])
        assert stream is not None
        manifest = json.load(stream)
    if manifest.get("name") != "mvo-fel":
        parser.error("Ce paquet ne contient pas l'extension mvo-fel.")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    asset = ReleaseAsset("local", "prototype", archive.name, archive.as_uri(), digest)
    installed = plugins.install(plugins.FEL, root=args.root, version=manifest["version"], asset=asset)
    engine = FelEngine(plugins.main_file(plugins.FEL, installed))
    print(f"FEL installé : {installed.path}")
    print(f"SHA-256 du paquet : {digest}")
    print("Moteurs : " + ", ".join(["Auto", *(d.name for d in engine.devices()), "CPU"]))


if __name__ == "__main__":
    main()
