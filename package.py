#!/usr/bin/env python3
"""
package.py — Script de packaging de Mediarecode.

Cibles :
  Linux (défaut) → AppImage  (Mediarecode-x86_64.AppImage dans dist/)
  Windows        → .exe      (dist/mediarecode/mediarecode.exe  ou  dist/mediarecode.exe)

Workflow Linux :
  1. PyInstaller --onedir  → dist/mediarecode/
  2. Construction du AppDir (AppRun + .desktop + icône)
  3. appimagetool           → dist/Mediarecode-<arch>.AppImage

Workflow Windows :
  1. PyInstaller --onedir  (ou --onefile avec --onefile)

Usage :
  python3 package.py [options]

Options :
  --onefile     Produit un binaire monolithique (plus lent au démarrage)
  --exe         Force le packaging .exe (PyInstaller seul, pas d'AppImage)
  --clean       Supprime build/ et dist/ avant de compiler
  --no-github   Passe --no-github au setup embarqué (ignore dovi_tool/hdr10plus_tool)
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
OS = platform.system()

# ── Modules Python exclus du bundle ──────────────────────────────────────────

EXCLUDED_MODULES: list[str] = [
    "tkinter",
    "matplotlib",
    "numpy",
    "scipy",
    "PIL",
    "IPython",
    "notebook",
]

# ── Fichiers/dossiers copiés comme données non-Python ────────────────────────
# Format : (source_relative_to_ROOT, dest_in_bundle)
DATA_FILES: list[tuple[str, str]] = [
    ("locales.json", "."),
    ("requirements.txt", "."),
    ("README.md", "."),
]

# ── Icône (optionnelle) ───────────────────────────────────────────────────────
# Placez une icône 256×256 px à cet emplacement pour l'intégrer au bundle.
ICON_PNG = ROOT / "icon.png"
ICON_ICO = ROOT / "icon.ico"   # Windows uniquement


# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────────────────────

def _ok(msg: str) -> None:   print(f"  \033[32m✔\033[0m  {msg}")
def _info(msg: str) -> None: print(f"  \033[36m→\033[0m  {msg}")
def _warn(msg: str) -> None: print(f"  \033[33m⚠\033[0m  {msg}")
def _err(msg: str) -> None:  print(f"  \033[31m✘\033[0m  {msg}", file=sys.stderr)
def _title(msg: str) -> None:
    bar = "─" * 60
    print(f"\n\033[1;34m{bar}\n  {msg}\n{bar}\033[0m")


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    _info("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, **kwargs)


def _ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
        _ok("PyInstaller disponible")
    except ImportError:
        _info("Installation de PyInstaller...")
        _run([sys.executable, "-m", "pip", "install", "pyinstaller"])


def _clean_dirs() -> None:
    for d in ("build", "dist"):
        target = ROOT / d
        if target.exists():
            shutil.rmtree(target)
            _ok(f"Supprimé : {target}")
    for spec in ROOT.glob("*.spec"):
        spec.unlink()
        _ok(f"Supprimé : {spec}")


# ─────────────────────────────────────────────────────────────────────────────
# Étape 1 — PyInstaller
# ─────────────────────────────────────────────────────────────────────────────

def _build_pyinstaller(onefile: bool) -> Path:
    """Lance PyInstaller et retourne le chemin du binaire produit."""
    _title("Étape 1 — PyInstaller")

    sep = ";" if OS == "Windows" else ":"
    add_data: list[str] = []
    for src, dest in DATA_FILES:
        src_path = ROOT / src
        if src_path.exists():
            add_data += ["--add-data", f"{src_path}{sep}{dest}"]
        else:
            _warn(f"Donnée absente, ignorée : {src}")

    cmd: list[str] = [
        sys.executable, "-m", "PyInstaller",
        "--name", "mediarecode",
        "--console",          # terminal visible — utile pour le setup première installation
        "--noconfirm",
        "--clean",
        "--paths", str(ROOT),
        *add_data,
        # Imports cachés PySide6
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtWidgets",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtSvg",
        "--hidden-import", "PySide6.QtMultimedia",
        "--hidden-import", "pymediainfo",
        # Collecter PySide6 + pymediainfo en entier (plugins Qt, .so, etc.)
        "--collect-all", "PySide6",
        "--collect-all", "pymediainfo",
        # Modules du projet
        "--collect-submodules", "core",
        "--collect-submodules", "ui",
        "--collect-submodules", "workers",
        # Exclusions
        *[arg for mod in EXCLUDED_MODULES for arg in ("--exclude-module", mod)],
    ]

    # Icône Windows
    if OS == "Windows" and ICON_ICO.exists():
        cmd += ["--icon", str(ICON_ICO)]

    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")

    cmd.append(str(ROOT / "launcher.py"))

    _run(cmd, cwd=ROOT)

    if onefile:
        exe_name = "mediarecode.exe" if OS == "Windows" else "mediarecode"
        exe_path = ROOT / "dist" / exe_name
    else:
        exe_name = "mediarecode.exe" if OS == "Windows" else "mediarecode"
        exe_path = ROOT / "dist" / "mediarecode" / exe_name

    if not exe_path.exists():
        raise FileNotFoundError(f"PyInstaller n'a pas produit : {exe_path}")

    _ok(f"Bundle PyInstaller : {exe_path.parent if not onefile else exe_path}")
    return exe_path


# ─────────────────────────────────────────────────────────────────────────────
# Étape 2 — Construction du AppDir (Linux uniquement)
# ─────────────────────────────────────────────────────────────────────────────

_DESKTOP_ENTRY = """\
[Desktop Entry]
Name=Mediarecode
Comment=MKV/MP4 workflow — DoVi, HDR10+, encoding
Exec=mediarecode
Icon=Mediarecode
Type=Application
Categories=AudioVideo;Video;
"""

_APPRUN = """\
#!/bin/bash
# AppRun — point d'entrée du AppImage Mediarecode
set -e
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/mediarecode/mediarecode" "$@"
"""


def _build_appdir() -> Path:
    """Construit le AppDir à partir du bundle PyInstaller onedir."""
    _title("Étape 2 — Construction du AppDir")

    appdir = ROOT / "dist" / "Mediarecode.AppDir"
    if appdir.exists():
        shutil.rmtree(appdir)
    appdir.mkdir(parents=True)

    # Copier le bundle PyInstaller dans le AppDir
    bundle_src = ROOT / "dist" / "mediarecode"
    bundle_dst = appdir / "mediarecode"
    shutil.copytree(bundle_src, bundle_dst)
    _ok(f"Bundle copié → {bundle_dst}")

    # AppRun
    apprun = appdir / "AppRun"
    apprun.write_text(_APPRUN)
    apprun.chmod(apprun.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    _ok("AppRun créé")

    # Fichier .desktop
    desktop = appdir / "Mediarecode.desktop"
    desktop.write_text(_DESKTOP_ENTRY)
    _ok(".desktop créé")

    # Icône
    if ICON_PNG.exists():
        shutil.copy(ICON_PNG, appdir / "Mediarecode.png")
        _ok("Icône copiée")
    else:
        # Icône vide 1×1 px (PNG valide minimum) pour que appimagetool ne bloque pas
        _warn(f"Icône absente ({ICON_PNG.name}) — icône de substitution utilisée")
        _write_minimal_png(appdir / "Mediarecode.png")

    return appdir


def _write_minimal_png(dest: Path) -> None:
    """Écrit un PNG 1×1 px transparent valide (sans dépendance tierce)."""
    import zlib
    import struct

    def _chunk(name: bytes, data: bytes) -> bytes:
        c = name + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    png_header = b"\x89PNG\r\n\x1a\n"
    ihdr_data  = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)   # 1×1 RGB
    idat_raw   = b"\x00\xff\xff\xff"                              # filtre + RGB blanc
    idat_data  = zlib.compress(idat_raw)
    png_bytes  = (
        png_header
        + _chunk(b"IHDR", ihdr_data)
        + _chunk(b"IDAT", idat_data)
        + _chunk(b"IEND", b"")
    )
    dest.write_bytes(png_bytes)


# ─────────────────────────────────────────────────────────────────────────────
# Étape 3 — appimagetool (Linux uniquement)
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_appimagetool() -> Path:
    """
    Retourne le chemin vers appimagetool, en le téléchargeant si nécessaire
    dans build/tools/.
    """
    # Chercher dans PATH d'abord
    found = shutil.which("appimagetool")
    if found:
        _ok(f"appimagetool trouvé : {found}")
        return Path(found)

    # Télécharger depuis GitHub
    tools_dir = ROOT / "build" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    tool_path = tools_dir / "appimagetool"

    if tool_path.exists():
        _ok(f"appimagetool en cache : {tool_path}")
        return tool_path

    arch = platform.machine().lower()
    arch_map = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
    appimage_arch = arch_map.get(arch, arch)
    url = (
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/"
        f"appimagetool-{appimage_arch}.AppImage"
    )
    _info(f"Téléchargement de appimagetool ({appimage_arch}) depuis GitHub...")
    urllib.request.urlretrieve(url, tool_path)
    tool_path.chmod(tool_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    _ok(f"appimagetool téléchargé → {tool_path}")
    return tool_path


def _build_appimage(appdir: Path) -> Path:
    """Invoque appimagetool pour produire le .AppImage final."""
    _title("Étape 3 — appimagetool")

    appimagetool = _ensure_appimagetool()
    arch = platform.machine()   # x86_64 | aarch64
    output = ROOT / "dist" / f"Mediarecode-{arch}.AppImage"

    env = os.environ.copy()
    env["ARCH"] = arch

    _run([str(appimagetool), str(appdir), str(output)], env=env)

    output.chmod(output.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    _ok(f"AppImage produit : {output}")
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def build(onefile: bool, exe_only: bool, clean: bool) -> None:
    _ensure_pyinstaller()

    if clean:
        _clean_dirs()

    if OS == "Linux" and not exe_only:
        # ── Cible AppImage ────────────────────────────────────────────────────
        # PyInstaller doit produire un dossier (onedir) pour structurer le AppDir.
        if onefile:
            _warn("--onefile ignoré pour AppImage (onedir requis pour construire le AppDir)")
        _build_pyinstaller(onefile=False)
        appdir = _build_appdir()
        appimage = _build_appimage(appdir)

        _title("Résultat")
        _ok(f"AppImage : {appimage}")
        print(f"""
  Distribution :
    Copier {appimage.name} n'importe où et l'exécuter directement.
    Au premier lancement (sans config.ini à côté), le setup s'exécute.
""")

    else:
        # ── Cible .exe (Windows ou --exe explicite) ───────────────────────────
        exe = _build_pyinstaller(onefile=onefile)

        _title("Résultat")
        if onefile:
            _ok(f"Exécutable : {exe}")
        else:
            _ok(f"Dossier    : {exe.parent}")
            _ok(f"Exécutable : {exe}")
        print(f"""
  Distribution :
    Distribuer {'le fichier' if onefile else 'le dossier'} {exe if onefile else exe.parent}.
    Au premier lancement (sans config.ini à côté), le setup s'exécute.
""")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--onefile",
        action="store_true",
        help="Bundle monolithique (lent au démarrage, ignoré pour AppImage)",
    )
    p.add_argument(
        "--exe",
        action="store_true",
        help="Force la cible .exe même sur Linux (pas d'AppImage)",
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="Supprime build/ et dist/ avant de compiler",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    build(onefile=args.onefile, exe_only=args.exe, clean=args.clean)
