"""Installation et contrôle du runtime MVTools distribué avec Muxiveo."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from core.version import MUXIVEO_MVTOOLS_VERSION
from core.subprocess_utils import subprocess_text_kwargs


def bundle_errors(binary: Path, *, render: bool = True,
                  expected_version: str | None = MUXIVEO_MVTOOLS_VERSION) -> list[str]:
    """Vérifie les empreintes et, sur l'hôte cible, le rendu autonome."""
    runtime = binary.parent / "mvtools-runtime"
    errors: list[str] = []
    try:
        manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
            raise ValueError("structure du manifeste")
        version = manifest["version"]
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("version du manifeste")
        if expected_version is not None and version != expected_version:
            errors.append("version du manifeste MVTools incorrecte")
        files = manifest["files"]
        suffix = ".dll" if binary.suffix == ".exe" else ".dylib" if (runtime / "mvtools.dylib").exists() else ".so"
        required = [binary.name] + [f"mvtools-runtime/{name}{suffix}" for name in
                                    ("libvapoursynth", "libvapoursynthfilters", "mvtools")]
        for name in required:
            if name not in files:
                errors.append(f"composant absent du manifeste : {name}")
        for name, spec in files.items():
            rel = PurePosixPath(name)
            if not isinstance(name, str) or not name or rel.is_absolute() or ".." in rel.parts or "\\" in name or ":" in name:
                errors.append(f"chemin de manifeste invalide : {name}")
                continue
            if (not isinstance(spec, dict) or type(spec.get("size")) is not int or spec["size"] < 0
                    or not isinstance(spec.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", spec["sha256"])):
                errors.append(f"empreinte de manifeste invalide : {name}")
                continue
            path = binary.parent / rel
            if not path.is_file() or any((binary.parent / Path(*rel.parts[:i])).is_symlink()
                                         for i in range(1, len(rel.parts) + 1)):
                errors.append(f"composant MVTools absent : {name}")
            elif path.stat().st_size != spec["size"] or hashlib.sha256(path.read_bytes()).hexdigest() != spec["sha256"]:
                errors.append(f"empreinte MVTools incorrecte : {name}")
    except (OSError, ValueError, KeyError, TypeError):
        errors.append("manifeste MVTools absent ou invalide")
    if not errors and render:
        try:
            # Outil configuré, arguments constants, aucun shell.
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            result = subprocess.run([str(binary.resolve()), "--self-test", "--json"],
                                    capture_output=True, timeout=60, check=False,
                                    **subprocess_text_kwargs())  # nosec B603
            info = json.loads(result.stdout)
            if result.returncode or info.get("ok") is not True or info.get("version") != version:
                errors.append("autotest MVTools en échec")
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            errors.append("autotest MVTools impossible")
    return errors


def extract_bundle(archive: Path, destination: Path) -> Path:
    """Extrait seulement des fichiers ordinaires sous l'unique racine du paquet."""
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or any(destination.iterdir()):
        raise ValueError("destination MVTools non vide ou lien symbolique")

    def relative(name: str) -> Path | None:
        parts = PurePosixPath(name).parts
        if not parts or name.startswith("/") or ".." in parts or "\\" in name or ":" in name:
            raise ValueError(f"chemin d'archive invalide : {name}")
        return Path(*parts[1:]) if len(parts) > 1 else None

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            roots = {PurePosixPath(n).parts[0] for n in zf.namelist() if PurePosixPath(n).parts}
            if len(roots) != 1:
                raise ValueError("archive MVTools sans racine unique")
            for member in zf.infolist():
                kind = stat.S_IFMT(member.external_attr >> 16)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("type d'entrée d'archive invalide")
                rel = relative(member.filename)
                if rel is not None and not member.is_dir():
                    target = destination / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(member))
    else:
        with tarfile.open(archive) as tf:
            roots = {PurePosixPath(m.name).parts[0] for m in tf.getmembers() if PurePosixPath(m.name).parts}
            if len(roots) != 1:
                raise ValueError("archive MVTools sans racine unique")
            for member in tf.getmembers():
                if not (member.isfile() or member.isdir()):
                    raise ValueError("type d'entrée d'archive invalide")
                rel = relative(member.name)
                if rel is not None and member.isfile():
                    stream = tf.extractfile(member)
                    if stream is None:
                        raise ValueError("fichier d'archive illisible")
                    target = destination / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(stream.read())
    binary = destination / ("muxiveo-mvtools.exe" if (destination / "muxiveo-mvtools.exe").exists() else "muxiveo-mvtools")
    if os.name != "nt":
        binary.chmod(binary.stat().st_mode | 0o111)
    errors = bundle_errors(binary, render=False)
    if errors:
        raise ValueError(" ; ".join(errors))
    return binary
