#!/usr/bin/env python3
"""
package.py — Script de packaging de Mediarecode.

Cibles :
  Linux (défaut) → AppImage  (Mediarecode-x86_64.AppImage dans dist/)
  Windows natif  → .exe      (dist/mediarecode/mediarecode.exe)
  Windows cross  → Mediarecode-Setup.exe via Wine + NSIS (--windows)

Workflow Linux :
  1. PyInstaller --onedir  → dist/mediarecode/
  2. Construction du AppDir (AppRun + .desktop + icône)
  3. appimagetool           → dist/Mediarecode-<arch>.AppImage

Workflow Windows natif (exécuté sur Windows) :
  1. PyInstaller --onedir  → dist/mediarecode/
  2. (optionnel) NSIS       → Mediarecode-Setup.exe  (nécessite --nsis)

Workflow Windows cross (depuis Linux avec --windows) :
  1. Installe Wine + préfixe dédié si absent
  2. Installe Python Windows + PyInstaller dans le préfixe Wine
  3. PyInstaller via wine python.exe → dist/mediarecode-win/
  4. Génère un script NSIS + makensis → Mediarecode-Setup.exe

Usage :
  python3 package.py [options]

Options :
  --onefile     Produit un binaire monolithique (lent au démarrage, ignoré pour AppImage)
  --exe         Force le packaging .exe même sur Linux (PyInstaller natif, pas d'AppImage)
  --windows     Cross-compile un installateur Windows depuis Linux via Wine + NSIS
  --skip-wine   Réutilise dist/mediarecode-win/ existant (skip étape Wine/PyInstaller)
  --clean       Nettoie tous les artefacts de build (build/, dist/, .wine_build/, *.AppImage…). Utilise sudo si nécessaire. Quitte sans builder.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
OS   = platform.system()

# Préfixe Wine isolé (dans le projet, ignoré par .gitignore)
_WINE_PREFIX  = ROOT / ".wine_build"
# Version Python Windows embarquée dans le préfixe Wine
_WIN_PY_VER   = "3.11.9"
_WIN_PY_URL   = f"https://www.python.org/ftp/python/{_WIN_PY_VER}/python-{_WIN_PY_VER}-amd64.exe"
# Chemin de python.exe à l'intérieur du préfixe Wine
_WIN_PY_EXE   = _WINE_PREFIX / "drive_c" / "Python311" / "python.exe"
# Bundle PyInstaller Windows (dans dist/)
_WIN_BUNDLE   = ROOT / "mediarecode-win"   # hors de dist/ (owned by nfsnobody)

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


def _run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    _info("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=check, **kwargs)


def _ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
        _ok("PyInstaller disponible")
    except ImportError:
        _info("Installation de PyInstaller...")
        _run([sys.executable, "-m", "pip", "install", "pyinstaller"])


def _clean_dirs() -> None:
    """Supprime les artefacts de build avec sudo si nécessaire (fichiers nfsnobody)."""
    to_remove: list[Path] = [
        ROOT / "build",
        ROOT / "dist",
        ROOT / "Mediarecode.AppDir",
        ROOT / "mediarecode-win",
        ROOT / "mediarecode.nsi",
        ROOT / ".wine_build",
        *ROOT.glob("*.spec"),
    ]
    for path in to_remove:
        if not path.exists():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            _ok(f"Supprimé : {path.relative_to(ROOT)}")
        except PermissionError:
            _info(f"Permission refusée, tentative avec sudo : {path.relative_to(ROOT)}")
            result = subprocess.run(["sudo", "rm", "-rf", str(path)])
            if result.returncode == 0:
                _ok(f"Supprimé (sudo) : {path.relative_to(ROOT)}")
            else:
                _warn(f"Impossible de supprimer : {path.relative_to(ROOT)}")


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
# Build Windows cross (Wine + PyInstaller + NSIS) — Linux uniquement
# ─────────────────────────────────────────────────────────────────────────────

def _wine(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Lance une commande via Wine avec le préfixe isolé."""
    env = os.environ.copy()
    env["WINEPREFIX"] = str(_WINE_PREFIX)
    env["WINEDEBUG"]  = "-all"          # supprime le bruit de Wine en console
    env.pop("DISPLAY", None)            # headless : évite les popups Wine
    env["WINEDLLOVERRIDES"] = "mscoree,mshtml="
    return _run(["wine", *args], env=env, check=check, **kwargs)


def _ensure_wine() -> None:
    """Vérifie que Wine est installé, sinon tente de l'installer."""
    if shutil.which("wine"):
        _ok("wine trouvé")
        return
    _info("wine introuvable — tentative d'installation…")
    if shutil.which("apt-get"):
        _run(["sudo", "apt-get", "install", "-y", "wine64"])
    elif shutil.which("dnf"):
        _run(["sudo", "dnf", "install", "-y", "wine"])
    else:
        print("  Installez Wine manuellement puis relancez.", file=sys.stderr)
        sys.exit(1)
    if not shutil.which("wine"):
        print("  Wine toujours introuvable après installation.", file=sys.stderr)
        sys.exit(1)
    _ok("wine installé")


def _ensure_makensis() -> None:
    """Vérifie que makensis (NSIS) est installé, sinon tente de l'installer."""
    if shutil.which("makensis"):
        _ok("makensis trouvé")
        return
    _info("makensis introuvable — tentative d'installation…")
    if OS == "Windows":
        # Sur Windows, winget est disponible sur W10 1709+ et W11
        if shutil.which("winget"):
            _run(["winget", "install", "--id", "NSIS.NSIS", "-e", "--silent"])
        else:
            print(
                "  Installez NSIS manuellement : https://nsis.sourceforge.io\n"
                "  Ajoutez le dossier NSIS à votre PATH (ex: C:\\Program Files (x86)\\NSIS).",
                file=sys.stderr,
            )
            sys.exit(1)
    elif shutil.which("apt-get"):
        _run(["sudo", "apt-get", "install", "-y", "nsis"])
    elif shutil.which("dnf"):
        _run(["sudo", "dnf", "install", "-y", "mingw32-nsis"])
    else:
        print("  Installez NSIS manuellement : https://nsis.sourceforge.io", file=sys.stderr)
        sys.exit(1)
    if not shutil.which("makensis"):
        print("  makensis toujours introuvable après installation.", file=sys.stderr)
        sys.exit(1)
    _ok("makensis installé")


def _setup_wine_python() -> None:
    """
    Installe Python Windows dans le préfixe Wine dédié si absent.

    Installation silencieuse dans C:\\Python311\\ (pas dans AppData) pour avoir
    un chemin fixe et prévisible quelle que soit la version de Wine.
    """
    if _WIN_PY_EXE.exists():
        _ok(f"Python Windows déjà installé : {_WIN_PY_EXE}")
        return

    _info(f"Création du préfixe Wine : {_WINE_PREFIX}")
    _WINE_PREFIX.mkdir(parents=True, exist_ok=True)

    # Initialise le préfixe (crée drive_c/ etc.) — on ignore les erreurs de
    # première initialisation qui produisent des messages non-fatals.
    env = os.environ.copy()
    env["WINEPREFIX"] = str(_WINE_PREFIX)
    env["WINEDEBUG"]  = "-all"
    env.pop("DISPLAY", None)
    subprocess.run(["wineboot", "--init"], env=env, check=False)

    installer = ROOT / f"python-{_WIN_PY_VER}-amd64.exe"
    if not installer.exists():
        _info(f"Téléchargement Python {_WIN_PY_VER} Windows…")
        urllib.request.urlretrieve(_WIN_PY_URL, installer)
        _ok(f"Téléchargé : {installer.name}")

    _info("Installation silencieuse de Python dans Wine (C:\\Python311)…")
    _wine(
        str(installer),
        "/quiet",
        "InstallAllUsers=0",
        "TargetDir=C:\\Python311",
        "AssociateFiles=0",
        "Shortcuts=0",
        "Include_launcher=0",
        "PrependPath=0",
    )

    if not _WIN_PY_EXE.exists():
        print(f"  Python Windows introuvable après installation : {_WIN_PY_EXE}", file=sys.stderr)
        sys.exit(1)
    _ok("Python Windows installé dans le préfixe Wine")


def _wine_pip(*packages: str) -> None:
    """Installe des paquets Python dans le préfixe Wine."""
    _wine(str(_WIN_PY_EXE), "-m", "pip", "install", "--upgrade", *packages)


def _setup_wine_vcruntime() -> None:
    """
    Installe le runtime Visual C++ 2019 dans le préfixe Wine via winetricks.

    PySide6/Qt6Core.dll dépend de vcruntime140.dll / msvcp140.dll qui sont
    absents de Wine par défaut. Sans eux, PyInstaller ne peut pas importer
    QtCore pour analyser les dépendances PySide6.
    """
    sentinel = _WINE_PREFIX / "vcrun2019.installed"
    if sentinel.exists():
        _ok("vcrun2019 déjà installé")
        return

    if not shutil.which("winetricks"):
        _info("winetricks introuvable — tentative d'installation…")
        if shutil.which("apt-get"):
            _run(["sudo", "apt-get", "install", "-y", "winetricks"])
        elif shutil.which("dnf"):
            _run(["sudo", "dnf", "install", "-y", "winetricks"])
        else:
            print(
                "  Installez winetricks manuellement : https://github.com/Winetricks/winetricks",
                file=sys.stderr,
            )
            sys.exit(1)

    _info("Installation vcrun2019 via winetricks (télécharge ~30 Mo)…")
    env = os.environ.copy()
    env["WINEPREFIX"] = str(_WINE_PREFIX)
    env["WINEDEBUG"]  = "-all"
    env["WINEDLLOVERRIDES"] = "mscoree,mshtml="
    env.pop("DISPLAY", None)
    # winetricks attend WINE= si wine n'est pas dans PATH sous ce nom
    _run(["winetricks", "-q", "vcrun2019"], env=env)
    sentinel.touch()
    _ok("vcrun2019 installé")


def _ensure_wine_deps() -> None:
    """Installe PyInstaller + dépendances Python dans le préfixe Wine."""
    _info("Installation des dépendances Python dans Wine…")
    _wine_pip("pyinstaller", "PySide6>=6.6.0", "pymediainfo>=6.1.0")
    _ok("Dépendances Python Windows installées")


def _build_pyinstaller_wine() -> Path:
    """Lance PyInstaller via Wine et retourne le dossier bundle produit."""
    _title("Étape Wine — PyInstaller")

    if _WIN_BUNDLE.exists():
        shutil.rmtree(_WIN_BUNDLE)

    sep = ";"   # séparateur Windows pour --add-data
    add_data: list[str] = []
    for src, dest in DATA_FILES:
        src_path = ROOT / src
        if src_path.exists():
            # Wine attend des chemins Windows : on passe le chemin Linux,
            # Wine le convertit automatiquement via son VFS.
            win_src = subprocess.check_output(
                ["winepath", "-w", str(src_path)],
                env={**os.environ, "WINEPREFIX": str(_WINE_PREFIX), "WINEDEBUG": "-all"},
                text=True,
            ).strip()
            add_data += ["--add-data", f"{win_src}{sep}{dest}"]

    cmd: list[str] = [
        str(_WIN_PY_EXE), "-m", "PyInstaller",
        "--name", "mediarecode",
        "--onedir",
        "--noconfirm",
        "--windowed",           # pas de console sur Windows (launcher gère son propre terminal)
        *add_data,
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtWidgets",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtSvg",
        "--hidden-import", "pymediainfo",
        "--collect-all", "PySide6",
        "--collect-all", "pymediainfo",
        "--collect-submodules", "core",
        "--collect-submodules", "ui",
        "--collect-submodules", "workers",
        *[arg for mod in EXCLUDED_MODULES for arg in ("--exclude-module", mod)],
    ]

    if ICON_ICO.exists():
        win_ico = subprocess.check_output(
            ["winepath", "-w", str(ICON_ICO)],
            env={**os.environ, "WINEPREFIX": str(_WINE_PREFIX), "WINEDEBUG": "-all"},
            text=True,
        ).strip()
        cmd += ["--icon", win_ico]

    # distpath / workpath en chemins Windows.
    # On utilise dist/win/ (pas dist/) pour éviter les conflits avec le build
    # Linux AppImage qui produit dist/mediarecode/ avec des fichiers appartenant
    # à un autre uid.  workpath dans /tmp pour les mêmes raisons de permissions.
    # Tout le travail Wine (workpath + distpath) dans /tmp pour éviter les
    # conflits de permissions sur dist/ (potentiellement owned by nfsnobody).
    wine_tmpdir = Path(tempfile.mkdtemp(prefix="mediarecode_wine_"))
    wine_env = {**os.environ, "WINEPREFIX": str(_WINE_PREFIX), "WINEDEBUG": "-all"}
    wine_distpath = wine_tmpdir / "dist"
    wine_distpath.mkdir()
    win_dist = subprocess.check_output(
        ["winepath", "-w", str(wine_distpath)], env=wine_env, text=True,
    ).strip()
    win_build = subprocess.check_output(
        ["winepath", "-w", str(wine_tmpdir)], env=wine_env, text=True,
    ).strip()
    cmd += ["--distpath", win_dist, "--workpath", win_build, "--specpath", win_build]

    win_launcher = subprocess.check_output(
        ["winepath", "-w", str(ROOT / "launcher.py")], env=wine_env, text=True,
    ).strip()
    cmd.append(win_launcher)

    try:
        _wine(*cmd)
        # Déplace le bundle produit vers _WIN_BUNDLE dans le projet
        raw_bundle = wine_distpath / "mediarecode"
        if not raw_bundle.exists():
            print(f"  Bundle introuvable après PyInstaller Wine : {raw_bundle}", file=sys.stderr)
            sys.exit(1)
        _WIN_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
        if _WIN_BUNDLE.exists():
            shutil.rmtree(_WIN_BUNDLE)
        shutil.copytree(raw_bundle, _WIN_BUNDLE)
    finally:
        shutil.rmtree(wine_tmpdir, ignore_errors=True)

    _ok(f"Bundle Windows : {_WIN_BUNDLE}")
    return _WIN_BUNDLE


# ── Script NSIS ────────────────────────────────────────────────────────────────

_NSIS_TEMPLATE = """\
Unicode true

!define APP_NAME      "Mediarecode"
!define APP_VERSION   "1.0.0"
!define EXE_NAME      "mediarecode.exe"
!define INSTALL_DIR   "$PROGRAMFILES64\\Mediarecode"
!define UNINSTALL_KEY "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Mediarecode"

Name "${{APP_NAME}} ${{APP_VERSION}}"
OutFile "{outfile}"
InstallDir "${{INSTALL_DIR}}"
InstallDirRegKey HKLM "${{UNINSTALL_KEY}}" "InstallLocation"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

Page directory
Page instfiles

Section "Application" SEC_MAIN
  SetOutPath "$INSTDIR"
  File /r "{bundle_dir}/*"

  ; Raccourci Menu Démarrer
  CreateDirectory "$SMPROGRAMS\\Mediarecode"
  CreateShortcut  "$SMPROGRAMS\\Mediarecode\\Mediarecode.lnk" \\
                  "$INSTDIR\\${{EXE_NAME}}" "" "$INSTDIR\\${{EXE_NAME}}" 0

  ; Raccourci Bureau
  CreateShortcut "$DESKTOP\\Mediarecode.lnk" \\
                 "$INSTDIR\\${{EXE_NAME}}" "" "$INSTDIR\\${{EXE_NAME}}" 0

  ; Clés désinstalleur
  WriteRegStr   HKLM "${{UNINSTALL_KEY}}" "DisplayName"      "${{APP_NAME}}"
  WriteRegStr   HKLM "${{UNINSTALL_KEY}}" "DisplayVersion"   "${{APP_VERSION}}"
  WriteRegStr   HKLM "${{UNINSTALL_KEY}}" "Publisher"        "Mediarecode"
  WriteRegStr   HKLM "${{UNINSTALL_KEY}}" "InstallLocation"  "$INSTDIR"
  WriteRegStr   HKLM "${{UNINSTALL_KEY}}" "UninstallString"  "$INSTDIR\\Uninstall.exe"
  WriteRegDWORD HKLM "${{UNINSTALL_KEY}}" "NoModify"         1
  WriteRegDWORD HKLM "${{UNINSTALL_KEY}}" "NoRepair"         1

  WriteUninstaller "$INSTDIR\\Uninstall.exe"
SectionEnd

Section "Uninstall"
  Delete "$INSTDIR\\Uninstall.exe"
  RMDir /r "$INSTDIR"
  Delete "$SMPROGRAMS\\Mediarecode\\Mediarecode.lnk"
  RMDir  "$SMPROGRAMS\\Mediarecode"
  Delete "$DESKTOP\\Mediarecode.lnk"
  DeleteRegKey HKLM "${{UNINSTALL_KEY}}"
SectionEnd
"""


def _build_nsis_installer(bundle_dir: Path) -> Path:
    """Génère le script NSIS et invoque makensis pour produire l'installateur."""
    _title("Étape NSIS — Installateur Windows")

    output = ROOT / "Mediarecode-Setup.exe"   # hors de dist/ (owned by nfsnobody)
    nsi    = ROOT / "mediarecode.nsi"

    nsi.write_text(
        _NSIS_TEMPLATE.format(
            outfile=str(output),
            bundle_dir=str(bundle_dir),
        ),
        encoding="utf-8",
    )
    _info(f"Script NSIS : {nsi}")

    _run(["makensis", str(nsi)])

    if not output.exists():
        print(f"  Installateur introuvable après makensis : {output}", file=sys.stderr)
        sys.exit(1)

    _ok(f"Installateur : {output}")
    return output


def build_windows(skip_wine: bool) -> None:
    """Orchestre le build Windows cross depuis Linux."""
    _title("Build Windows (Wine + PyInstaller + NSIS)")

    _ensure_wine()
    _ensure_makensis()

    if skip_wine and _WIN_BUNDLE.exists():
        _info(f"--skip-wine : bundle existant réutilisé → {_WIN_BUNDLE}")
        bundle_dir = _WIN_BUNDLE
    else:
        _setup_wine_python()
        _setup_wine_vcruntime()
        _ensure_wine_deps()
        bundle_dir = _build_pyinstaller_wine()

    installer = _build_nsis_installer(bundle_dir)

    _title("Résultat")
    _ok(f"Installateur Windows : {installer}")
    print(f"""
  Distribuer :
    {installer.name}
  Au premier lancement (sans config.ini dans %APPDATA%\\Mediarecode),
  le setup s'exécute pour installer les outils externes.
""")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def build(onefile: bool, exe_only: bool, clean: bool = False) -> None:
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
        "--windows",
        action="store_true",
        help=(
            "Cross-compile un installateur Windows depuis Linux via Wine + NSIS. "
            "Ignoré si le script tourne déjà sur Windows (comportement natif)."
        ),
    )
    p.add_argument(
        "--skip-wine",
        action="store_true",
        help="Réutilise mediarecode-win/ existant (skip Wine + PyInstaller)",
    )
    p.add_argument(
        "--nsis",
        action="store_true",
        help=(
            "Génère un installateur NSIS (.exe) après PyInstaller. "
            "Sur Linux : inclus automatiquement dans --windows. "
            "Sur Windows natif : génère l'installateur après le bundle."
        ),
    )
    p.add_argument(
        "--allinc",
        action="store_true",
        help="Délègue à package_appimage.py --allinc (AppImage avec tous les outils embarqués).",
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="Nettoie tous les artefacts de build (build/, dist/, .wine_build/, *.AppImage…). Utilise sudo si nécessaire. Quitte sans builder.",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.clean:
        _clean_dirs()
        sys.exit(0)

    if OS == "Windows":
        # Sur Windows natif : PyInstaller direct, puis NSIS optionnel
        _ensure_pyinstaller()
        exe_path = _build_pyinstaller(onefile=args.onefile)
        _title("Résultat")
        _ok(f"Bundle : {exe_path}")
        if args.nsis:
            _ensure_makensis()
            # onedir → exe_path = dist/mediarecode/mediarecode.exe
            #          NSIS doit recevoir dist/mediarecode/ (le dossier bundle)
            # onefile → un seul exe dans dist/ : on crée un sous-dossier propre
            if args.onefile:
                onefile_dir = ROOT / "dist" / "mediarecode-onefile"
                onefile_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(exe_path, onefile_dir / exe_path.name)
                bundle_dir = onefile_dir
            else:
                bundle_dir = exe_path.parent  # dist/mediarecode/
            installer = _build_nsis_installer(bundle_dir)
            _ok(f"Installateur : {installer}")
    elif args.allinc:
        # Délègue à package_appimage.py --allinc
        script = ROOT / "package_appimage.py"
        if not script.exists():
            print(f"  package_appimage.py introuvable : {script}", file=sys.stderr)
            sys.exit(1)
        os.execv(sys.executable, [sys.executable, str(script), "--allinc"])
    elif args.windows:
        # Cross-compilation Windows depuis Linux via Wine + NSIS
        if OS != "Linux":
            print("--windows est uniquement supporté depuis Linux.", file=sys.stderr)
            sys.exit(1)
        build_windows(skip_wine=args.skip_wine)
    else:
        # Comportement par défaut : AppImage Linux (ou --exe pour PyInstaller natif)
        build(onefile=args.onefile, exe_only=args.exe, clean=False)
