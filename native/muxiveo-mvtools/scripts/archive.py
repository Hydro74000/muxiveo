#!/usr/bin/env python3
"""Archive native, tailles exactes et sources correspondantes GPL."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=Path("build/muxiveo-mvtools"))
    parser.add_argument("--platform", required=True, choices=["linux-x86_64", "windows-x86_64", "macos-arm64"])
    parser.add_argument("--output", type=Path, default=Path("build/mvtools-assets"))
    parser.add_argument("--sources", action="store_true")
    args = parser.parse_args()
    bundle = args.build_dir / "bundle"
    manifest = json.loads((bundle / "mvtools-runtime/manifest.json").read_text())
    name = f"muxiveo-mvtools-{manifest['version']}-{args.platform}"
    args.output.mkdir(parents=True, exist_ok=True)
    if args.platform.startswith("windows"):
        archive = args.output / (name + ".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    zf.write(path, name + "/" + path.relative_to(bundle).as_posix())
    else:
        archive = args.output / (name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(bundle, arcname=name)
    summary = {"platform": args.platform, "version": manifest["version"],
               "installed_bytes": sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file()),
               "compressed_bytes": archive.stat().st_size,
               "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "models_bytes": 0}
    (args.output / (name + "-sizes.json")).write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    report = os.environ.get("GITHUB_STEP_SUMMARY")
    if report:
        with open(report, "a") as stream:
            stream.write(f"{args.platform} : installé {summary['installed_bytes'] / 2**20:.2f} Mio ; "
                         f"compressé {summary['compressed_bytes'] / 2**20:.2f} Mio ; modèles 0 Mio.\n")
    if args.sources:
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / f"muxiveo-mvtools-{manifest['version']}-sources"
            shutil.copytree(ROOT, stage / "native/muxiveo-mvtools")
            shutil.copy2(ROOT.parent.parent / "LICENSE", stage / "LICENSE")
            shutil.copytree(args.build_dir / "sources", stage / "build/muxiveo-mvtools/sources",
                            ignore=shutil.ignore_patterns(".git", "__pycache__"))
            (stage / "BUILD.txt").write_text(
                "Sources amont vérifiées + sources modifiées + scripts de construction.\n"
                "Prérequis : Python >=3.12, Meson, CMake >=3.20, Ninja, pkg-config, compilateur C/C++17, nasm (x86), patchelf (Linux).\n"
                "python native/muxiveo-mvtools/scripts/build.py --jobs 4\n")
            with tarfile.open(args.output / f"muxiveo-mvtools-{manifest['version']}-sources.tar.gz", "w:gz") as tf:
                tf.add(stage, arcname=stage.name)


if __name__ == "__main__":
    main()
