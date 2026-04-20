#!/usr/bin/env python3
"""
package.py — Script de packaging de Mediarecode.

Cibles :
  Linux (défaut) → AppImage  (Mediarecode-x86_64.AppImage dans dist/)
  Windows natif  → .exe / .msix
  Windows cross  → Mediarecode-Setup.exe via Wine + NSIS (--windows)

Workflow Linux :
  1. PyInstaller --onedir  → dist/mediarecode/
  2. Construction du AppDir (AppRun + .desktop + icône)
  3. appimagetool           → dist/Mediarecode-<arch>.AppImage

Workflow Windows natif (exécuté sur Windows) :
  1. PyInstaller --onedir  → dist/mediarecode/
  2. (optionnel) MSIX       → Mediarecode.msix       (nécessite --msix)
  3. (optionnel) NSIS       → Mediarecode-Setup.exe  (nécessite --nsis)

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
  --msix        Produit un package MSIX signé sur Windows natif
  --skip-wine   Réutilise dist/mediarecode-win/ existant (skip étape Wine/PyInstaller)
  --version TAG Suffixe de version pour le fichier final (défaut: APP_VERSION)
  --dest PATH   Copie le fichier final vers un chemin personnalisé (dossier ou fichier)
  --clean       Nettoie tous les artefacts de build (build/, dist/, .wine_build/, *.AppImage…). Utilise sudo si nécessaire. Quitte sans builder.
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable

from core.version import APP_NAME, APP_VERSION

ROOT = Path(__file__).parent
DIST_RELEASES = ROOT / "dist" / "releases"
OS   = platform.system()

# Préfixe Wine isolé (dans le projet, ignoré par .gitignore)
_WINE_PREFIX  = ROOT / ".wine_build"
# Version Python Windows embarquée dans le préfixe Wine
_WIN_PY_VER   = "3.11.9"
_WIN_PY_URL   = f"https://www.python.org/ftp/python/{_WIN_PY_VER}/python-{_WIN_PY_VER}-amd64.exe"
# Chemin de python.exe à l'intérieur du préfixe Wine
_WIN_PY_EXE   = _WINE_PREFIX / "drive_c" / "Python311" / "python.exe"
# Version PySide6 figée pour le build cross Wine.
# Un override ponctuel reste possible via l'environnement si besoin de tester
# une autre release sans modifier le dépôt.
_WIN_PYSIDE6_VER = os.environ.get("MEDIARECODE_WINE_PYSIDE6_VERSION", "6.10.2").strip() or "6.10.2"
# Runtime ICU Windows utilisé pour satisfaire les dépendances Qt sous Wine.
_WIN_ICU_NUGET_VERSION = os.environ.get("MEDIARECODE_WINE_ICU_VERSION", "72.1.0.3").strip() or "72.1.0.3"
_WIN_ICU_NUGET_URL = (
    "https://www.nuget.org/api/v2/package/"
    f"Microsoft.ICU.ICU4C.Runtime.win-x64/{_WIN_ICU_NUGET_VERSION}"
)
# Bundle PyInstaller Windows (dans dist/)
_WIN_BUNDLE   = ROOT / "mediarecode-win"   # hors de dist/ (owned by nfsnobody)
# Bundle macOS
_MACOS_BUNDLE_NAME = "Mediarecode.app"
_MACOS_BUNDLE_ID = os.environ.get("MEDIARECODE_MACOS_BUNDLE_ID", "com.hydro74000.mediarecode").strip() or "com.hydro74000.mediarecode"
_MACOS_MIN_VERSION = os.environ.get("MEDIARECODE_MACOS_MIN_VERSION", "11.0").strip() or "11.0"
_APPIMAGE_UPDATE_OWNER = os.environ.get("MEDIARECODE_APPIMAGE_UPDATE_OWNER", "Hydro74000").strip() or "Hydro74000"
_APPIMAGE_UPDATE_REPO = os.environ.get("MEDIARECODE_APPIMAGE_UPDATE_REPO", "mediarecode").strip() or "mediarecode"
_APPIMAGE_UPDATE_RELEASE = os.environ.get("MEDIARECODE_APPIMAGE_UPDATE_RELEASE", "latest").strip() or "latest"
_MSIX_IDENTITY = os.environ.get("MEDIARECODE_MSIX_IDENTITY", "Hydro74000.Mediarecode").strip() or "Hydro74000.Mediarecode"
_MSIX_PUBLISHER = os.environ.get("MEDIARECODE_MSIX_PUBLISHER", "CN=Hydro74000").strip() or "CN=Hydro74000"
_MSIX_PUBLISHER_DISPLAY_NAME = os.environ.get("MEDIARECODE_MSIX_PUBLISHER_DISPLAY_NAME", "Hydro74000").strip() or "Hydro74000"
_MSIX_DESCRIPTION = os.environ.get("MEDIARECODE_MSIX_DESCRIPTION", "Mediarecode video workflow").strip() or "Mediarecode video workflow"
_MSIX_CERT_PFX = os.environ.get("MEDIARECODE_MSIX_CERT_PFX", "").strip()
_MSIX_CERT_PASSWORD = os.environ.get("MEDIARECODE_MSIX_CERT_PASSWORD", "").strip()
_MSIX_TIMESTAMP_URL = os.environ.get("MEDIARECODE_MSIX_TIMESTAMP_URL", "http://timestamp.digicert.com").strip() or "http://timestamp.digicert.com"
_MSIX_STORE_CONFIG = os.environ.get("MEDIARECODE_MSIX_STORE_CONFIG", "").strip()
_WINDOWS_SDK_WINGET_ID = os.environ.get("MEDIARECODE_WINDOWS_SDK_WINGET_ID", "Microsoft.WindowsSDK").strip() or "Microsoft.WindowsSDK"
_WINDOWS_SDK_INSTALLER = os.environ.get("MEDIARECODE_WINDOWS_SDK_INSTALLER", "").strip()

# ── Modules Python exclus du bundle ──────────────────────────────────────────

EXCLUDED_MODULES: list[str] = [
    "tkinter",
    "matplotlib",
    "numpy",
    "scipy",
    "PIL",
    "IPython",
    "notebook",
    # PySide6 deploy helper not used by the app; importing it can warn on
    # some environments due to an internal absolute import ("project_lib").
    "PySide6.scripts.deploy_lib",
]

# ── Fichiers/dossiers copiés comme données non-Python ────────────────────────
# Format : (source_relative_to_ROOT, dest_in_bundle)
DATA_FILES: list[tuple[str, str]] = [
    ("locales.json", "."),
    ("requirements.txt", "."),
    ("README.md", "."),
]


def _windows_version_tuple(version: str) -> tuple[int, int, int, int]:
    """Convertit `1.2` ou `1.2.3` vers un tuple PE à 4 entiers."""
    parts = [int(p) for p in re.findall(r"\d+", version)]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])  # type: ignore[return-value]


def _write_windows_version_file() -> Path:
    """Génère un fichier `--version-file` PyInstaller avec métadonnées PE."""
    build_dir = ROOT / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    version_file = build_dir / "windows_version_info.txt"
    version_tuple = _windows_version_tuple(APP_VERSION)
    version_str = ".".join(str(p) for p in version_tuple)
    content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple},
    prodvers={version_tuple},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040C04B0',
        [
          StringStruct('CompanyName', 'Mediarecode'),
          StringStruct('FileDescription', 'Mediarecode video workflow'),
          StringStruct('FileVersion', '{version_str}'),
          StringStruct('InternalName', 'mediarecode'),
          StringStruct('OriginalFilename', 'mediarecode.exe'),
          StringStruct('ProductName', '{APP_NAME}'),
          StringStruct('ProductVersion', '{version_str}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1036, 1200])])
  ]
)
"""
    version_file.write_text(content, encoding="utf-8")
    return version_file

# ── Icône (optionnelle) ───────────────────────────────────────────────────────
# Placez une icône 256×256 px à cet emplacement pour l'intégrer au bundle.
ICON_PNG = ROOT / "icon.png"
ICON_ICO = ROOT / "icon.ico"
ICON_ICO_GENERATED = ROOT / "build" / "icon.ico"


# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────────────────────

def _can_stream_encode(stream: object, text: str) -> bool:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        text.encode(encoding)
        return True
    except Exception:
        return False


def _configure_stdio_for_windows() -> None:
    """
    Configure stdout/stderr on Windows so console output never crashes on
    Unicode glyphs. If UTF-8 reconfigure is unavailable, fallback to
    `errors=replace`.
    """
    if OS != "Windows":
        return

    probe = "✔→⚠✘─"
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        if _can_stream_encode(stream, probe):
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            try:
                reconfigure(errors="replace")
            except Exception:
                pass


_configure_stdio_for_windows()
_UI_UNICODE = _can_stream_encode(sys.stdout, "✔→⚠✘─")
_UI_OK = "✔" if _UI_UNICODE else "OK"
_UI_INFO = "→" if _UI_UNICODE else "->"
_UI_WARN = "⚠" if _UI_UNICODE else "!"
_UI_ERR = "✘" if _UI_UNICODE else "X"
_UI_BAR = "─" if _UI_UNICODE else "-"


def _ok(msg: str) -> None:   print(f"  \033[32m{_UI_OK}\033[0m  {msg}")
def _info(msg: str) -> None: print(f"  \033[36m{_UI_INFO}\033[0m  {msg}")
def _warn(msg: str) -> None: print(f"  \033[33m{_UI_WARN}\033[0m  {msg}")
def _err(msg: str) -> None:  print(f"  \033[31m{_UI_ERR}\033[0m  {msg}", file=sys.stderr)
def _title(msg: str) -> None:
    bar = _UI_BAR * 60
    print(f"\n\033[1;34m{bar}\n  {msg}\n{bar}\033[0m")


def _run(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    _info("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=check, **kwargs)


def _normalize_version_tag(version_tag: str | None) -> str:
    """
    Normalise un tag de version pour un nom de fichier.
    - fallback: APP_VERSION
    - espaces -> '-'
    - caractères autorisés: [A-Za-z0-9._-]
    """
    raw = (version_tag or "").strip() or APP_VERSION
    raw = re.sub(r"\s+", "-", raw)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "", raw)
    return cleaned or APP_VERSION


def _versioned_output_path(path: Path, version_tag: str | None) -> Path:
    """
    Retourne un path suffixé par '-<version>' avant l'extension.
    Exemple: file.AppImage -> file-1.2.3.AppImage
    """
    tag = _normalize_version_tag(version_tag)
    stem = path.stem if path.suffix else path.name
    if stem.endswith(f"-{tag}"):
        return path
    if path.suffix:
        return path.with_name(f"{path.stem}-{tag}{path.suffix}")
    return path.with_name(f"{path.name}-{tag}")


def _default_msix_store_config_path() -> Path:
    return ROOT / "packaging" / "msix_store.json"


def _load_msix_store_metadata(config_path: Path | None = None) -> dict[str, str]:
    """
    Charge les métadonnées Store/MSIX depuis un JSON optionnel.

    Priorité:
    1. valeurs intégrées/environnement
    2. chemin explicite
    3. `packaging/msix_store.json`
    """
    metadata = {
        "identity": _MSIX_IDENTITY,
        "publisher": _MSIX_PUBLISHER,
        "publisher_display_name": _MSIX_PUBLISHER_DISPLAY_NAME,
        "description": _MSIX_DESCRIPTION,
        "display_name": APP_NAME,
    }

    candidate = config_path
    if candidate is None and _MSIX_STORE_CONFIG:
        candidate = Path(_MSIX_STORE_CONFIG)
    if candidate is None:
        candidate = _default_msix_store_config_path()
    if not candidate.exists():
        return metadata

    payload = json.loads(candidate.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Configuration MSIX invalide : {candidate}")

    for key in metadata:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            metadata[key] = value.strip()
    return metadata


def _resolve_dest_file(dest: str | None, default_output: Path, version_tag: str | None = None) -> Path:
    """
    Résout le chemin de destination du fichier final.
    - dest absent: place dans dist/releases/ (créé si besoin)
    - dest dossier (existant, trailing slash, ou sans extension): utilise le nom auto
    - dest fichier: utilise ce nom
    """
    if not dest or not dest.strip():
        return _versioned_output_path(DIST_RELEASES / default_output.name, version_tag)

    raw = dest.strip()
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()

    if target.exists() and target.is_dir():
        return _versioned_output_path(target / default_output.name, version_tag)

    if raw.endswith(("/", "\\")):
        return _versioned_output_path(target / default_output.name, version_tag)

    if target.suffix == "":
        return _versioned_output_path(target / default_output.name, version_tag)

    return _versioned_output_path(target, version_tag)


def _copy_final_file_if_requested(src: Path, dest: str | None, version_tag: str | None = None) -> Path:
    """Déplace le fichier final vers dist/releases/ (ou --dest si fourni)."""
    target = _resolve_dest_file(dest, src, version_tag)

    src_resolved = src.resolve()
    target_resolved = target.resolve(strict=False)
    if src_resolved == target_resolved:
        return src

    target.parent.mkdir(parents=True, exist_ok=True)
    src.replace(target)
    _ok(f"Fichier final : {target}")
    return target


def _ensure_pyinstaller() -> None:
    required: list[tuple[str, str]] = [
        ("PyInstaller", "pyinstaller"),
        ("PySide6", "PySide6>=6.6.0"),
        ("pymediainfo", "pymediainfo>=6.1.0"),
    ]
    missing: list[str] = []
    for module_name, pip_name in required:
        if importlib.util.find_spec(module_name) is None:
            missing.append(pip_name)
    if not missing:
        _ok("Dépendances de packaging Python disponibles")
        return
    _info(f"Installation des dépendances manquantes : {', '.join(missing)}")
    _run([sys.executable, "-m", "pip", "install", *missing])


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

def _png_dimensions(png_data: bytes) -> tuple[int, int]:
    """Retourne (largeur, hauteur) depuis l'en-tête IHDR du PNG."""
    if len(png_data) < 24 or png_data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("icon.png est invalide (signature PNG absente)")
    return struct.unpack(">II", png_data[16:24])


def _ico_dim(value: int) -> int:
    """Dans un répertoire ICO, 0 représente 256 px."""
    return 0 if value >= 256 else value


def _write_ico_from_png_payload(png_data: bytes, width: int, height: int, dest: Path) -> None:
    """Écrit un .ico minimal contenant une image PNG."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    header = struct.pack("<HHH", 0, 1, 1)  # reserved, type=icon, count=1
    entry = struct.pack(
        "<BBBBHHII",
        _ico_dim(width),
        _ico_dim(height),
        0,
        0,
        1,
        32,
        len(png_data),
        6 + 16,
    )
    dest.write_bytes(header + entry + png_data)


def _build_ico_from_png(src_png: Path, dest_ico: Path) -> Path:
    """
    Construit un .ico Windows depuis icon.png.
    Si l'image n'est pas déjà "ICO-safe", redimensionne en 256×256 via Qt.
    """
    png_data = src_png.read_bytes()
    width, height = _png_dimensions(png_data)
    needs_qt_resize = width != height or width > 256 or height > 256

    if needs_qt_resize:
        try:
            qtcore = importlib.import_module("PySide6.QtCore")
            qtgui = importlib.import_module("PySide6.QtGui")
            Qt = qtcore.Qt
            QImage = qtgui.QImage
        except Exception:
            _warn(
                f"PySide6 indisponible pour redimensionner {src_png.name} ({width}x{height}) ; "
                "fallback vers encapsulation PNG directe."
            )
        else:
            image = QImage(str(src_png))
            if not image.isNull():
                image = image.scaled(
                    256, 256,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                dest_ico.parent.mkdir(parents=True, exist_ok=True)
                if image.save(str(dest_ico), "ICO"):
                    _ok(f"Icône Windows générée depuis {src_png.name} → {dest_ico.relative_to(ROOT)}")
                    return dest_ico
            _warn("Conversion ICO via Qt impossible ; fallback vers encapsulation PNG directe.")

    _write_ico_from_png_payload(png_data, width, height, dest_ico)
    _ok(f"Icône Windows générée depuis {src_png.name} → {dest_ico.relative_to(ROOT)}")
    return dest_ico


def _resolve_windows_icon_ico() -> Path | None:
    """Retourne un .ico Windows prêt à l'emploi (généré depuis icon.png si besoin)."""
    if ICON_ICO.exists():
        return ICON_ICO
    if not ICON_PNG.exists():
        _warn(f"Icône Windows absente : {ICON_ICO.name} / {ICON_PNG.name}")
        return None

    regenerate = (
        not ICON_ICO_GENERATED.exists()
        or ICON_ICO_GENERATED.stat().st_mtime < ICON_PNG.stat().st_mtime
    )
    if regenerate:
        _build_ico_from_png(ICON_PNG, ICON_ICO_GENERATED)
    return ICON_ICO_GENERATED


def _windows_ssl_hidden_import_args() -> list[str]:
    """
    Hidden imports needed so urllib HTTPS support is preserved in frozen builds.
    `_ssl` is critical; `ssl` keeps the high-level API reachable.
    """
    return [
        "--hidden-import", "ssl",
        "--hidden-import", "_ssl",
    ]


def _windows_ctypes_hidden_import_args() -> list[str]:
    """
    Hidden imports needed by stdlib `ctypes` on Windows frozen builds.
    `_ctypes` is the binary extension that depends on libffi runtime DLLs.
    """
    return [
        "--hidden-import", "ctypes",
        "--hidden-import", "_ctypes",
    ]


def _windows_sqlite_hidden_import_args() -> list[str]:
    """
    Hidden imports needed by stdlib `sqlite3` on Windows frozen builds.
    `_sqlite3` may require sqlite3.dll at runtime.
    """
    return [
        "--hidden-import", "sqlite3",
        "--hidden-import", "_sqlite3",
    ]


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _discover_windows_python_homes() -> list[Path]:
    """
    Discover additional CPython homes installed on Windows.
    Used after winget installs/repairs Python to refresh DLL discovery.
    """
    if OS != "Windows":
        return []

    candidates: list[Path] = []
    current_major = sys.version_info.major
    current_minor = sys.version_info.minor
    current_series = f"{current_major}.{current_minor}"

    # 1) py launcher registry view
    py_launcher = shutil.which("py")
    if py_launcher:
        result = subprocess.run(
            [py_launcher, "-0p"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in ((result.stdout or "") + "\n" + (result.stderr or "")).splitlines():
            path_match = re.search(r"([A-Za-z]:\\[^\r\n]*python(?:\.exe)?)", line, flags=re.IGNORECASE)
            if not path_match:
                continue
            ver_match = re.search(r"-V:(\d+\.\d+)", line)
            if ver_match and ver_match.group(1) != current_series:
                continue
            exe = Path(path_match.group(1).strip())
            home = exe.parent.parent if exe.parent.name.lower() == "scripts" else exe.parent
            candidates.append(home)

    # 2) common installation roots
    local_appdata = Path(os.environ.get("LOCALAPPDATA", ""))
    program_files = Path(os.environ.get("ProgramFiles", ""))
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)", ""))
    for base in (
        local_appdata / "Programs" / "Python",
        program_files,
        program_files_x86,
    ):
        if not base or not base.is_dir():
            continue
        for pattern in ("Python*",):
            for home in base.glob(pattern):
                if home.is_dir():
                    # Keep only the same interpreter series (ex: Python310 for 3.10).
                    m = re.fullmatch(r"python(\d+)", home.name.lower())
                    if not m:
                        continue
                    digits = m.group(1)
                    if len(digits) < 2:
                        continue
                    major = int(digits[0])
                    minor = int(digits[1:])
                    if major == current_major and minor == current_minor:
                        candidates.append(home)

    existing = [p for p in candidates if p.is_dir()]
    return _dedupe_paths(existing)


def _native_windows_runtime_roots(python_home: Path) -> list[Path]:
    """
    Return likely CPython home roots on native Windows.
    This handles virtualenv layouts where sys.executable lives in `.../Scripts/`.
    """
    candidates: list[Path] = [python_home]

    if python_home.name.lower() == "scripts":
        candidates.append(python_home.parent)

    for raw in (
        sys.base_prefix,
        sys.base_exec_prefix,
        sys.prefix,
        sys.exec_prefix,
        os.environ.get("PYTHONHOME", ""),
    ):
        if not raw:
            continue
        candidates.append(Path(raw))

    candidates.extend(_discover_windows_python_homes())

    existing = [p for p in candidates if p.is_dir()]
    return _dedupe_paths(existing)


def _windows_runtime_search_dirs(roots: Iterable[Path]) -> list[Path]:
    """
    Expand Python home roots into concrete DLL search directories.
    """
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(
            [
                root,
                root / "DLLs",
                root / "Library" / "bin",  # conda-like layouts
                root / "bin",
                root / "libs",
            ]
        )
    existing = [p for p in candidates if p.is_dir()]
    return _dedupe_paths(existing)


def _find_windows_runtime_dlls(search_dirs: Iterable[Path], patterns: tuple[str, ...]) -> list[Path]:
    selected_by_name: dict[str, Path] = {}
    for base in search_dirs:
        for pattern in patterns:
            for candidate in sorted(base.glob(pattern)):
                key = candidate.name.lower()
                if key in selected_by_name:
                    continue
                selected_by_name[key] = candidate.resolve()
    return list(selected_by_name.values())


def _find_windows_ssl_runtime_dlls(python_home: Path, *, include_native_roots: bool = False) -> list[Path]:
    """
    Locate OpenSSL runtime DLLs required by `_ssl.pyd` on Windows.
    We search both the interpreter root and its `DLLs/` subfolder.
    """
    roots = _native_windows_runtime_roots(python_home) if include_native_roots else [python_home]
    dirs = _windows_runtime_search_dirs(roots)
    return _find_windows_runtime_dlls(dirs, ("libssl-*.dll", "libcrypto-*.dll"))


def _find_windows_ctypes_runtime_dlls(python_home: Path, *, include_native_roots: bool = False) -> list[Path]:
    """
    Locate libffi runtime DLLs required by `_ctypes.pyd` on Windows.
    We search both the interpreter root and its `DLLs/` subfolder.
    """
    roots = _native_windows_runtime_roots(python_home) if include_native_roots else [python_home]
    dirs = _windows_runtime_search_dirs(roots)
    return _find_windows_runtime_dlls(dirs, ("libffi-*.dll", "libffi*.dll"))


def _find_windows_sqlite_runtime_dlls(python_home: Path, *, include_native_roots: bool = False) -> list[Path]:
    """
    Locate sqlite runtime DLLs required by `_sqlite3.pyd` on Windows.
    We search both the interpreter root and its `DLLs/` subfolder.
    """
    roots = _native_windows_runtime_roots(python_home) if include_native_roots else [python_home]
    dirs = _windows_runtime_search_dirs(roots)
    return _find_windows_runtime_dlls(dirs, ("sqlite3.dll",))


def _missing_windows_runtime_labels(python_home: Path, *, include_native_roots: bool = False) -> list[str]:
    """
    Return missing runtime labels among:
      - ctypes/libffi
      - sqlite3
      - ssl/libssl+libcrypto
    """
    missing: list[str] = []

    ffi = _find_windows_ctypes_runtime_dlls(python_home, include_native_roots=include_native_roots)
    if not ffi:
        missing.append("ctypes/libffi (libffi-*.dll)")

    sqlite = _find_windows_sqlite_runtime_dlls(python_home, include_native_roots=include_native_roots)
    if not sqlite:
        missing.append("sqlite3 (sqlite3.dll)")

    ssl = _find_windows_ssl_runtime_dlls(python_home, include_native_roots=include_native_roots)
    ssl_names = {p.name.lower() for p in ssl}
    has_libssl = any(name.startswith("libssl-") and name.endswith(".dll") for name in ssl_names)
    has_libcrypto = any(name.startswith("libcrypto-") and name.endswith(".dll") for name in ssl_names)
    if not (has_libssl and has_libcrypto):
        missing.append("ssl (libssl-*.dll + libcrypto-*.dll)")

    return missing


def _ensure_windows_runtime_dlls_available() -> None:
    """
    Ensure runtime DLLs required for frozen stdlib extensions are discoverable.
    If missing, try to auto-install/repair current CPython via winget.
    """
    if OS != "Windows":
        return

    python_home = Path(sys.executable).resolve().parent
    missing = _missing_windows_runtime_labels(python_home, include_native_roots=True)
    if not missing:
        _ok("Runtime DLLs Windows détectées (libffi/sqlite3/OpenSSL)")
        return

    _warn("Runtime DLLs manquantes détectées : " + ", ".join(missing))
    _info("Tentative de correction automatique via winget...")

    winget = shutil.which("winget")
    if winget:
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        pkg = f"Python.Python.{py_ver}"
        common_args = ["--id", pkg, "--silent", "--accept-package-agreements", "--accept-source-agreements"]
        _run([winget, "install", *common_args], check=False)
        _run([winget, "upgrade", *common_args], check=False)
    else:
        _warn("winget introuvable — correction automatique impossible sur cette machine.")

    # Re-run verification after potential install/repair.
    # Some installers finalize files a bit after process return.
    missing_after: list[str] = []
    for attempt in range(3):
        if attempt > 0:
            time.sleep(1.0)
        missing_after = _missing_windows_runtime_labels(python_home, include_native_roots=True)
        if not missing_after:
            break
        _info(f"Re-vérification runtime DLLs ({attempt + 1}/3) : encore manquant -> {', '.join(missing_after)}")

    if missing_after:
        joined = ", ".join(missing_after)
        raise RuntimeError(
            "DLLs runtime Windows toujours absentes après tentative automatique : "
            f"{joined}. Réinstallez/réparez Python {sys.version_info.major}.{sys.version_info.minor}."
        )

    _ok("Runtime DLLs Windows installées et détectées")


def _add_windows_ssl_to_pyinstaller_native(cmd: list[str]) -> None:
    """Add SSL-related imports/binaries to a native Windows PyInstaller command."""
    cmd.extend(_windows_ssl_hidden_import_args())

    dlls = _find_windows_ssl_runtime_dlls(
        Path(sys.executable).resolve().parent,
        include_native_roots=True,
    )
    if not dlls:
        _warn(
            "DLLs OpenSSL introuvables près de l'interpréteur Python "
            "(libssl/libcrypto) ; HTTPS pourrait rester indisponible dans le bundle."
        )
        return

    for dll in dlls:
        # --add-binary syntax on Windows: SRC;DEST_DIR_IN_BUNDLE
        cmd += ["--add-binary", f"{dll};."]
    _ok("Support SSL Windows ajouté au bundle PyInstaller")


def _add_windows_ctypes_to_pyinstaller_native(cmd: list[str]) -> None:
    """Add ctypes/libffi runtime support to a native Windows PyInstaller command."""
    cmd.extend(_windows_ctypes_hidden_import_args())

    dlls = _find_windows_ctypes_runtime_dlls(
        Path(sys.executable).resolve().parent,
        include_native_roots=True,
    )
    if not dlls:
        _warn(
            "DLL libffi introuvable près de l'interpréteur Python "
            "(libffi-*.dll) ; ctypes peut échouer dans le bundle."
        )
        return

    for dll in dlls:
        cmd += ["--add-binary", f"{dll};."]
    _ok("Support ctypes/libffi Windows ajouté au bundle PyInstaller")


def _add_windows_sqlite_to_pyinstaller_native(cmd: list[str]) -> None:
    """Add sqlite runtime support to a native Windows PyInstaller command."""
    cmd.extend(_windows_sqlite_hidden_import_args())

    dlls = _find_windows_sqlite_runtime_dlls(
        Path(sys.executable).resolve().parent,
        include_native_roots=True,
    )
    if not dlls:
        _warn(
            "DLL sqlite3 introuvable près de l'interpréteur Python "
            "(sqlite3.dll) ; sqlite3 peut échouer dans le bundle."
        )
        return

    for dll in dlls:
        cmd += ["--add-binary", f"{dll};."]
    _ok("Support sqlite3 Windows ajouté au bundle PyInstaller")


def _add_windows_icu_to_pyinstaller_native(cmd: list[str]) -> None:
    """Add ICU runtime DLLs to a native Windows PyInstaller command when found."""
    pyside_dirs: list[Path] = []
    try:
        import PySide6 as _pyside6
    except Exception:
        pyside_dirs = []
    else:
        pyside_dirs.append(Path(_pyside6.__file__).resolve().parent)
    pyside_dirs.append(Path(sys.executable).resolve().parent)

    dlls = _select_windows_icu_runtime_dlls(pyside_dirs)
    source = "PySide6/Python"
    if not dlls:
        dlls = _select_windows_icu_runtime_dlls(_native_windows_system_icu_search_dirs())
        source = "Windows system runtime"
    if not dlls:
        _warn(
            "DLLs ICU introuvables pres de PySide6 et dans le runtime Windows ; "
            "le bundle Qt peut dependre du systeme."
        )
        return

    for dll in dlls:
        cmd += ["--add-binary", f"{dll};."]
    _ok(f"Support ICU Windows ajoute au bundle PyInstaller ({source})")

def _add_windows_ssl_to_pyinstaller_wine(cmd: list[str], wine_env: dict[str, str]) -> None:
    """Add SSL-related imports/binaries to a Wine (Windows target) PyInstaller command."""
    cmd.extend(_windows_ssl_hidden_import_args())

    dlls = _find_windows_ssl_runtime_dlls(_WIN_PY_EXE.parent)
    if not dlls:
        _warn(
            "DLLs OpenSSL introuvables dans le Python Wine "
            "(libssl/libcrypto) ; HTTPS pourrait rester indisponible dans le bundle."
        )
        return

    for dll in dlls:
        win_dll = subprocess.check_output(
            ["winepath", "-w", str(dll)],
            env=wine_env,
            text=True,
        ).strip()
        cmd += ["--add-binary", f"{win_dll};."]
    _ok("Support SSL Windows ajouté au bundle PyInstaller (Wine)")


def _add_windows_ctypes_to_pyinstaller_wine(cmd: list[str], wine_env: dict[str, str]) -> None:
    """Add ctypes/libffi runtime support to a Wine (Windows target) PyInstaller command."""
    cmd.extend(_windows_ctypes_hidden_import_args())

    dlls = _find_windows_ctypes_runtime_dlls(_WIN_PY_EXE.parent)
    if not dlls:
        _warn(
            "DLL libffi introuvable dans le Python Wine "
            "(libffi-*.dll) ; ctypes peut échouer dans le bundle."
        )
        return

    for dll in dlls:
        win_dll = subprocess.check_output(
            ["winepath", "-w", str(dll)],
            env=wine_env,
            text=True,
        ).strip()
        cmd += ["--add-binary", f"{win_dll};."]
    _ok("Support ctypes/libffi Windows ajouté au bundle PyInstaller (Wine)")


def _add_windows_sqlite_to_pyinstaller_wine(cmd: list[str], wine_env: dict[str, str]) -> None:
    """Add sqlite runtime support to a Wine (Windows target) PyInstaller command."""
    cmd.extend(_windows_sqlite_hidden_import_args())

    dlls = _find_windows_sqlite_runtime_dlls(_WIN_PY_EXE.parent)
    if not dlls:
        _warn(
            "DLL sqlite3 introuvable dans le Python Wine "
            "(sqlite3.dll) ; sqlite3 peut échouer dans le bundle."
        )
        return

    for dll in dlls:
        win_dll = subprocess.check_output(
            ["winepath", "-w", str(dll)],
            env=wine_env,
            text=True,
        ).strip()
        cmd += ["--add-binary", f"{win_dll};."]
    _ok("Support sqlite3 Windows ajouté au bundle PyInstaller (Wine)")

def _ensure_windows_icu_runtime(wine_env: dict[str, str]) -> list[Path]:
    """
    Vérifie la présence des DLL ICU dans le préfixe Wine.
    Si absentes : télécharge le package NuGet Microsoft.ICU.ICU4C.Runtime.win-x64
    et extrait les DLL nécessaires.
    Retourne la liste des DLL ICU trouvées.
    """
    icu_dir = _WINE_PREFIX / "drive_c" / "icu"
    icu_dir.mkdir(parents=True, exist_ok=True)

    expected = ["icudt*.dll", "icuin*.dll", "icuuc*.dll"]
    found: list[Path] = []

    # Recherche existante
    for pattern in expected:
        found.extend(icu_dir.glob(pattern))

    if len(found) >= 3:
        _ok(f"ICU Windows déjà présentes ({len(found)} DLL)")
        return found

    _warn("DLL ICU absentes — téléchargement du runtime ICU Windows…")

    # Téléchargement du package NuGet
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".nupkg")
    tmp.close()
    urllib.request.urlretrieve(_WIN_ICU_NUGET_URL, tmp.name)
    _ok(f"Package ICU téléchargé {tmp.name}")

    #Extraction
    with zipfile.ZipFile(tmp.name, "r") as z:
        entries = z.namelist()
        extracted_any = False
        for member in entries:
            normalized = member.replace("\\", "/")
            lname = normalized.lower()
            if not lname.endswith(".dll"):
                continue

            # compare sur le basename (ex: icudt72.dll) pour que "icudt*.dll" matche
            base = Path(normalized).name.lower()

            # expected doit contenir des motifs comme "icudt*.dll", "icuin*.dll", "icuuc*.dll"
            if not any(fnmatch.fnmatch(base, pat) for pat in expected):
                _info(f"SKIP DLL (no expected match): {member}")
                continue

            target = icu_dir / base
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            found.append(target)
            extracted_any = True
            _ok(f"Extracted ICU DLL: {target.name}")

        if not extracted_any:
            _warn("Aucune DLL extraite du package NuGet. Aperçu des 40 premières entrées :")
            for i, name in enumerate(entries[:40], 1):
                _info(f"  {i:02d}: {name}")
            raise RuntimeError("Impossible d'extraire toutes les DLL ICU du package NuGet")
    _ok(f"ICU Windows extraites dans {icu_dir.relative_to(ROOT)}")
    os.unlink(tmp.name)

    if len(found) < 3:
        raise RuntimeError("Impossible d'extraire toutes les DLL ICU du package NuGet")

    _ok(f"ICU Windows extraites ({len(found)} DLL)")
    return found

def _add_windows_icu_to_pyinstaller_wine(cmd: list[str], wine_env: dict[str, str]) -> None:
    """Add ICU runtime DLLs to a Wine (Windows target) PyInstaller command."""
    dlls = _find_windows_icu_dlls([_wine_pyside6_dir(), _WIN_PY_EXE.parent])
    if not dlls:
        _warn("DLLs ICU introuvables dans le Python Wine ; le bundle Qt risque d'être incomplet.")
        return

    for dll in dlls:
        win_dll = subprocess.check_output(
            ["winepath", "-w", str(dll)],
            env=wine_env,
            text=True,
        ).strip()
        cmd += ["--add-binary", f"{win_dll};."]
    _ok("Support ICU Windows ajouté au bundle PyInstaller (Wine)")


def _verify_windows_runtime_bundle(bundle_dir: Path) -> None:
    """
    Validate presence of critical runtime binaries in a Windows onedir bundle.
    Raises RuntimeError when required files are missing.
    """
    internal = bundle_dir / "_internal"
    if not internal.is_dir():
        raise RuntimeError(f"Bundle Windows invalide: dossier _internal absent dans {bundle_dir}")

    required_patterns: dict[str, str] = {
        "_ctypes.pyd": "_ctypes.pyd",
        "_ssl.pyd": "_ssl.pyd",
        "libffi": "libffi-*.dll",
        "libssl": "libssl-*.dll",
        "libcrypto": "libcrypto-*.dll",
        "icuuc": "icuuc*.dll",
        "icuin": "icuin*.dll",
    }
    missing: list[str] = []
    for label, pattern in required_patterns.items():
        if not any(internal.glob(pattern)):
            missing.append(f"{label} ({pattern})")

    if any(internal.glob("_sqlite3.pyd")) and not any(internal.glob("sqlite3.dll")):
        missing.append("sqlite runtime (sqlite3.dll)")
    if not any(internal.glob("icudt*.dll")) and not any(internal.glob("icu.dll")):
        missing.append("icu data runtime (icudt*.dll or icu.dll)")

    if missing:
        raise RuntimeError(f"Bundle Windows incomplet, runtime manquant: {', '.join(missing)}")

    _ok("Vérification runtime Windows: DLLs critiques présentes")


def _pyinstaller_frontend_flag(target_os: str) -> str:
    """Return the right PyInstaller UI mode for the target platform."""
    return "--windowed" if target_os in ("Windows", "Darwin") else "--console"


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

    app_name = "Mediarecode" if OS == "Darwin" else "mediarecode"
    cmd: list[str] = [
        sys.executable, "-m", "PyInstaller",
        "--name", app_name,
        _pyinstaller_frontend_flag(OS),
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
        # Collecte ciblée PySide6 : évite le scan des scripts de déploiement
        # (PySide6.scripts.deploy_lib) qui déclenche des warnings inutiles.
        "--collect-binaries", "PySide6",
        "--collect-data", "PySide6",
        "--collect-all", "pymediainfo",
        # Modules du projet
        "--collect-submodules", "core",
        "--collect-submodules", "ui",
        "--collect-submodules", "workers",
        # Exclusions
        *[arg for mod in EXCLUDED_MODULES for arg in ("--exclude-module", mod)],
    ]

    if OS == "Windows":
        _ensure_windows_runtime_dlls_available()
        _add_windows_ctypes_to_pyinstaller_native(cmd)
        _add_windows_sqlite_to_pyinstaller_native(cmd)
        _add_windows_ssl_to_pyinstaller_native(cmd)
        _add_windows_icu_to_pyinstaller_native(cmd)
        cmd += ["--version-file", str(_write_windows_version_file())]

    # Icône Windows
    if OS == "Windows":
        win_icon = _resolve_windows_icon_ico()
        if win_icon is not None:
            cmd += ["--icon", str(win_icon)]

    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")

    cmd.append(str(ROOT / "launcher.py"))

    _run(cmd, cwd=ROOT)

    if onefile:
        exe_name = "mediarecode.exe" if OS == "Windows" else "mediarecode"
        exe_path = ROOT / "dist" / exe_name
    elif OS == "Darwin":
        # --windowed + --name Mediarecode produit dist/Mediarecode.app/Contents/MacOS/Mediarecode
        exe_path = ROOT / "dist" / "Mediarecode.app" / "Contents" / "MacOS" / "Mediarecode"
    else:
        exe_name = "mediarecode.exe" if OS == "Windows" else "mediarecode"
        exe_path = ROOT / "dist" / "mediarecode" / exe_name

    if not exe_path.exists():
        raise FileNotFoundError(f"PyInstaller n'a pas produit : {exe_path}")

    if OS == "Windows" and not onefile:
        _verify_windows_runtime_bundle(exe_path.parent)

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
    icon_png = appdir / "Mediarecode.png"
    _write_linux_app_icon_png(icon_png)
    diricon = appdir / ".DirIcon"
    if diricon.exists() or diricon.is_symlink():
        diricon.unlink()
    diricon.symlink_to(icon_png.name)

    linux_icon_ico = _resolve_windows_icon_ico()
    if linux_icon_ico is not None:
        shutil.copy2(linux_icon_ico, appdir / "Mediarecode.ico")
        _ok(f"Icône ICO copiée : {linux_icon_ico.name}")

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


def _extract_best_png_from_ico(src_ico: Path) -> bytes | None:
    """
    Return the largest embedded PNG payload from an ICO file when available.

    Many modern ICO files are multi-resolution containers whose entries are
    already PNG-compressed. Extracting the biggest PNG avoids Qt loading the
    first tiny frame (often 16x16), which causes blurry AppImage icons.
    """
    data = src_ico.read_bytes()
    if len(data) < 6:
        return None

    reserved, icon_type, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or icon_type != 1 or count <= 0:
        return None

    best_payload: bytes | None = None
    best_area = -1
    for index in range(count):
        offset = 6 + index * 16
        if offset + 16 > len(data):
            break
        width, height, _colors, _reserved, _planes, _bpp, size, image_offset = struct.unpack_from(
            "<BBBBHHII",
            data,
            offset,
        )
        width = 256 if width == 0 else width
        height = 256 if height == 0 else height
        if image_offset + size > len(data):
            continue
        payload = data[image_offset:image_offset + size]
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            continue
        area = width * height
        if area > best_area:
            best_area = area
            best_payload = payload

    return best_payload


def _convert_ico_to_png(src_ico: Path, dest_png: Path) -> bool:
    """Convert an ICO file to PNG using Qt image plugins."""
    best_png = _extract_best_png_from_ico(src_ico)
    if best_png is not None:
        dest_png.parent.mkdir(parents=True, exist_ok=True)
        dest_png.write_bytes(best_png)
        return True

    try:
        qtgui = importlib.import_module("PySide6.QtGui")
        QImage = qtgui.QImage
    except Exception:
        _warn("PySide6 unavailable to convert icon.ico to PNG.")
        return False

    image = QImage(str(src_ico))
    if image.isNull():
        _warn(f"Unable to read ICO file: {src_ico.name}")
        return False

    dest_png.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(dest_png), b"PNG"):
        _warn(f"Unable to write PNG icon: {dest_png.name}")
        return False
    return True


def _write_linux_app_icon_png(dest_png: Path) -> None:
    """
    Write the AppImage icon file (PNG).

    Priority:
    1) icon.ico (converted to PNG)
    2) icon.png
    3) generated 1x1 placeholder
    """
    if ICON_ICO.exists():
        if _convert_ico_to_png(ICON_ICO, dest_png):
            _ok(f"Icon converted from {ICON_ICO.name}")
            return
        if ICON_PNG.exists():
            _warn(f"{ICON_ICO.name} conversion failed; fallback to {ICON_PNG.name}")
            shutil.copy(ICON_PNG, dest_png)
            _ok("Icon copied")
            return
        _warn(f"Icon missing after {ICON_ICO.name} conversion failure; using placeholder")
        _write_minimal_png(dest_png)
        return

    if ICON_PNG.exists():
        shutil.copy(ICON_PNG, dest_png)
        _ok("Icon copied")
        return

    _warn(f"Icon missing ({ICON_ICO.name} / {ICON_PNG.name}) - using placeholder")
    _write_minimal_png(dest_png)


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


def _build_appimage(appdir: Path, version_tag: str | None = None) -> Path:
    """Invoque appimagetool pour produire le .AppImage final."""
    _title("Étape 3 — appimagetool")

    appimagetool = _ensure_appimagetool()
    arch = platform.machine()   # x86_64 | aarch64
    output = _versioned_output_path(ROOT / "dist" / f"Mediarecode-{arch}.AppImage", version_tag)

    env = os.environ.copy()
    env["ARCH"] = arch
    update_information = _appimage_update_information(arch)
    env["UPDATE_INFORMATION"] = update_information
    _info(f"UPDATE_INFORMATION : {update_information}")

    _run([str(appimagetool), str(appdir), str(output)], env=env)

    output.chmod(output.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    _ok(f"AppImage produit : {output}")
    return output


def _appimage_update_information(arch: str) -> str:
    """Chaîne UPDATE_INFORMATION AppImage au format GitHub Releases."""
    filename_pattern = f"{APP_NAME}-{arch}-*.AppImage.zsync"
    return (
        "gh-releases-zsync|"
        f"{_APPIMAGE_UPDATE_OWNER}|"
        f"{_APPIMAGE_UPDATE_REPO}|"
        f"{_APPIMAGE_UPDATE_RELEASE}|"
        f"{filename_pattern}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Build Windows cross (Wine + PyInstaller + NSIS) — Linux uniquement
# ─────────────────────────────────────────────────────────────────────────────

def _wine_env(debug: str = "-all") -> dict[str, str]:
    """Return a Wine environment for the isolated prefix."""
    env = os.environ.copy()
    env["WINEPREFIX"] = str(_WINE_PREFIX)
    env["WINEDEBUG"]  = debug
    env.pop("DISPLAY", None)            # headless : évite les popups Wine
    env["WINEDLLOVERRIDES"] = "mscoree,mshtml="
    return env


def _wine(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Lance une commande via Wine avec le préfixe isolé."""
    env = _wine_env("-all")             # supprime le bruit de Wine en console
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


_NSIS_COMMON_DIRS: list[Path] = [
    Path(r"C:\Program Files (x86)\NSIS"),
    Path(r"C:\Program Files\NSIS"),
]

_WINDOWS_KITS_BIN_ROOT = Path(r"C:\Program Files (x86)\Windows Kits\10\bin")


def _find_makensis() -> str | None:
    """
    Cherche makensis.exe dans le PATH puis dans les dossiers d'installation
    courants de NSIS sur Windows (utile quand NSIS vient d'être installé et
    que le PATH de la session courante n'a pas encore été mis à jour).
    """
    found = shutil.which("makensis")
    if found:
        return found
    if OS == "Windows":
        for nsis_dir in _NSIS_COMMON_DIRS:
            candidate = nsis_dir / "makensis.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _ensure_makensis() -> None:
    """Vérifie que makensis (NSIS) est installé, sinon tente de l'installer."""
    if _find_makensis():
        _ok("makensis trouvé")
        return
    _info("makensis introuvable — tentative d'installation…")
    if OS == "Windows":
        if shutil.which("winget"):
            # winget renvoie un code non-nul quand le paquet est déjà installé
            # (ex. 2316632107 = APPINSTALLER_ERROR_UPDATE_NOT_APPLICABLE) ;
            # on ignore le code de retour et on vérifie la présence après.
            _run(
                ["winget", "install", "--id", "NSIS.NSIS", "-e", "--silent",
                 "--accept-package-agreements", "--accept-source-agreements"],
                check=False,
            )
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
    if not _find_makensis():
        print("  makensis toujours introuvable après installation.", file=sys.stderr)
        sys.exit(1)
    _ok("makensis installé")


def _install_windows_sdk_tools_if_missing() -> None:
    """
    Best-effort install of Windows SDK tools required for MSIX packaging.

    Preferred path is a repo/CI-provided installer via MEDIARECODE_WINDOWS_SDK_INSTALLER.
    As a fallback, tries winget with MEDIARECODE_WINDOWS_SDK_WINGET_ID.
    """
    if OS != "Windows":
        return
    if _find_windows_sdk_tool("makeappx.exe") and _find_windows_sdk_tool("signtool.exe"):
        return

    if _WINDOWS_SDK_INSTALLER:
        installer_value = _WINDOWS_SDK_INSTALLER
        installer_path = Path(installer_value)
        if re.match(r"^https?://", installer_value, flags=re.IGNORECASE):
            installer_path = Path(tempfile.gettempdir()) / "mediarecode-winsdksetup.exe"
            _info(f"Téléchargement du Windows SDK depuis {installer_value}…")
            urllib.request.urlretrieve(installer_value, installer_path)
        _info("Installation du Windows SDK (makeappx/signtool)…")
        _run([str(installer_path), "/quiet", "/norestart"], check=False)

    elif shutil.which("winget"):
        _info(f"Windows SDK introuvable — tentative via winget ({_WINDOWS_SDK_WINGET_ID})…")
        _run(
            [
                "winget",
                "install",
                "--id",
                _WINDOWS_SDK_WINGET_ID,
                "-e",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            check=False,
        )

    if _find_windows_sdk_tool("makeappx.exe") and _find_windows_sdk_tool("signtool.exe"):
        _ok("Windows SDK installé")


def _find_windows_sdk_tool(tool_name: str) -> str | None:
    """
    Cherche un outil du Windows SDK (ex: makeappx.exe, signtool.exe).
    """
    found = shutil.which(tool_name)
    if found:
        return found

    if OS != "Windows" or not _WINDOWS_KITS_BIN_ROOT.is_dir():
        return None

    version_dirs = sorted(
        (path for path in _WINDOWS_KITS_BIN_ROOT.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    for version_dir in version_dirs:
        candidate = version_dir / "x64" / tool_name
        if candidate.is_file():
            return str(candidate)
    return None


def _ensure_windows_sdk_tool(tool_name: str) -> str:
    _install_windows_sdk_tools_if_missing()
    tool = _find_windows_sdk_tool(tool_name)
    if tool:
        _ok(f"{tool_name} trouvé")
        return tool
    print(
        f"  {tool_name} introuvable. Installez le Windows SDK / App Installer tools sur le runner Windows.",
        file=sys.stderr,
    )
    sys.exit(1)


def _msix_processor_architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        return "x64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86", "i386", "i686"}:
        return "x86"
    return "x64"


def _msix_vfs_root_dir() -> str:
    arch = _msix_processor_architecture()
    if arch == "x86":
        return "ProgramFilesX86"
    if arch == "arm64":
        return "ProgramFilesArm64"
    return "ProgramFilesX64"


def _msix_version(version_tag: str | None) -> str:
    version_source = version_tag if version_tag and re.search(r"\d", version_tag) else APP_VERSION
    major, minor, build, _revision = _windows_version_tuple(version_source)
    # Le Partner Center refuse toute révision MSIX différente de 0.
    return f"{major}.{minor}.{build}.0"


def _msix_assets_dir() -> Path:
    return ROOT / "build" / "msix" / "Assets"


def _prepare_msix_base_png(dest_png: Path) -> Path:
    if ICON_ICO.exists() and _convert_ico_to_png(ICON_ICO, dest_png):
        return dest_png
    if ICON_PNG.exists():
        dest_png.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ICON_PNG, dest_png)
        return dest_png
    _write_minimal_png(dest_png)
    return dest_png


def _write_msix_resized_png(src_png: Path, dest_png: Path, size: tuple[int, int]) -> None:
    try:
        qtcore = importlib.import_module("PySide6.QtCore")
        qtgui = importlib.import_module("PySide6.QtGui")
        Qt = qtcore.Qt
        QImage = qtgui.QImage
    except Exception:
        shutil.copy2(src_png, dest_png)
        return

    image = QImage(str(src_png))
    if image.isNull():
        shutil.copy2(src_png, dest_png)
        return

    scaled = image.scaled(
        size[0],
        size[1],
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QImage(size[0], size[1], QImage.Format.Format_ARGB32)
    canvas.fill(0)
    painter = qtgui.QPainter(canvas)
    x = max((size[0] - scaled.width()) // 2, 0)
    y = max((size[1] - scaled.height()) // 2, 0)
    painter.drawImage(x, y, scaled)
    painter.end()
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    if not canvas.save(str(dest_png), "PNG"):
        shutil.copy2(src_png, dest_png)


def _build_msix_assets(assets_dir: Path) -> None:
    assets_dir.mkdir(parents=True, exist_ok=True)
    base_png = _prepare_msix_base_png(assets_dir / "_base.png")
    required_assets = {
        "StoreLogo.png": (50, 50),
        "Square44x44Logo.png": (44, 44),
        "Square150x150Logo.png": (150, 150),
        "Square310x310Logo.png": (310, 310),
        "Wide310x150Logo.png": (310, 150),
        "SplashScreen.png": (620, 300),
    }
    for filename, size in required_assets.items():
        _write_msix_resized_png(base_png, assets_dir / filename, size)
    base_png.unlink(missing_ok=True)


def _msix_manifest_content(
    version_tag: str | None,
    executable: str,
    metadata: dict[str, str] | None = None,
) -> str:
    package_version = _msix_version(version_tag)
    processor_arch = _msix_processor_architecture()
    raw_meta = metadata or _load_msix_store_metadata()
    from xml.sax.saxutils import escape as _xml_escape
    meta = {k: _xml_escape(str(v), {'"': "&quot;", "'": "&apos;"}) for k, v in raw_meta.items()}
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Package
  xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
  xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
  xmlns:desktop="http://schemas.microsoft.com/appx/manifest/desktop/windows10"
  xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
  IgnorableNamespaces="uap desktop rescap">
  <Identity
    Name="{meta['identity']}"
    Publisher="{meta['publisher']}"
    Version="{package_version}"
    ProcessorArchitecture="{processor_arch}" />
  <Properties>
    <DisplayName>{meta['display_name']}</DisplayName>
    <PublisherDisplayName>{meta['publisher_display_name']}</PublisherDisplayName>
    <Description>{meta['description']}</Description>
    <Logo>Assets\\StoreLogo.png</Logo>
  </Properties>
  <Resources>
    <Resource Language="en-us" />
  </Resources>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.22621.0" />
  </Dependencies>
  <Capabilities>
    <rescap:Capability Name="runFullTrust" />
  </Capabilities>
  <Applications>
    <Application Id="Mediarecode" Executable="{executable}" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements
        DisplayName="{meta['display_name']}"
        Description="{meta['description']}"
        BackgroundColor="transparent"
        Square44x44Logo="Assets\\Square44x44Logo.png"
        Square150x150Logo="Assets\\Square150x150Logo.png">
        <uap:DefaultTile
          Square310x310Logo="Assets\\Square310x310Logo.png"
          Wide310x150Logo="Assets\\Wide310x150Logo.png" />
        <uap:SplashScreen Image="Assets\\SplashScreen.png" />
      </uap:VisualElements>
      <Extensions>
        <desktop:Extension Category="windows.fullTrustProcess" Executable="{executable}">
          <desktop:FullTrustProcess />
        </desktop:Extension>
      </Extensions>
    </Application>
  </Applications>
</Package>
"""


def _stage_msix_layout(
    bundle_dir: Path,
    version_tag: str | None,
    metadata: dict[str, str] | None = None,
) -> Path:
    layout_dir = ROOT / "build" / "msix" / "layout"
    if layout_dir.exists():
        shutil.rmtree(layout_dir)
    layout_dir.mkdir(parents=True)

    assets_dir = layout_dir / "Assets"
    _build_msix_assets(assets_dir)

    vfs_root = layout_dir / "VFS" / _msix_vfs_root_dir() / APP_NAME
    shutil.copytree(bundle_dir, vfs_root)

    executable = f"VFS\\{_msix_vfs_root_dir()}\\{APP_NAME}\\mediarecode.exe"
    manifest_path = layout_dir / "AppxManifest.xml"
    manifest_path.write_text(
        _msix_manifest_content(version_tag, executable, metadata=metadata),
        encoding="utf-8",
    )
    _ok(f"Layout MSIX prêt : {layout_dir}")
    return layout_dir


def _sign_msix_package(msix_path: Path) -> None:
    if not _MSIX_CERT_PFX:
        _warn("MEDIARECODE_MSIX_CERT_PFX absent — package MSIX non signé.")
        return
    if not _MSIX_CERT_PASSWORD:
        raise RuntimeError("MEDIARECODE_MSIX_CERT_PASSWORD absent — signature MSIX impossible.")

    signtool = _ensure_windows_sdk_tool("signtool.exe")
    _run(
        [
            signtool,
            "sign",
            "/fd",
            "SHA256",
            "/f",
            _MSIX_CERT_PFX,
            "/p",
            _MSIX_CERT_PASSWORD,
            "/tr",
            _MSIX_TIMESTAMP_URL,
            "/td",
            "SHA256",
            str(msix_path),
        ]
    )
    _ok(f"MSIX signé : {msix_path.name}")


def _build_msix_package(
    bundle_dir: Path,
    version_tag: str | None = None,
    metadata: dict[str, str] | None = None,
) -> Path:
    _title("Étape MSIX — Package Windows")
    if OS != "Windows":
        raise RuntimeError("Le build MSIX nécessite Windows natif.")

    layout_dir = _stage_msix_layout(bundle_dir, version_tag, metadata=metadata)
    makeappx = _ensure_windows_sdk_tool("makeappx.exe")
    output = _versioned_output_path(ROOT / f"{APP_NAME}.msix", version_tag)
    if output.exists():
        output.unlink()

    pack_result = subprocess.run(
        [makeappx, "pack", "/v", "/d", str(layout_dir), "/p", str(output), "/o"],
        capture_output=True,
        text=True,
    )
    if pack_result.stdout:
        print(pack_result.stdout)
    if pack_result.stderr:
        print(pack_result.stderr, file=sys.stderr)
    if pack_result.returncode != 0:
        manifest_path = layout_dir / "AppxManifest.xml"
        if manifest_path.exists():
            _warn("Contenu AppxManifest.xml :")
            print(manifest_path.read_text(encoding="utf-8"), file=sys.stderr)
        raise subprocess.CalledProcessError(pack_result.returncode, pack_result.args)
    _sign_msix_package(output)
    _ok(f"Package MSIX : {output}")
    return output


def _build_msixupload(msix_path: Path, version_tag: str | None = None) -> Path:
    """
    Génère un conteneur `.msixupload` pour Partner Center à partir du `.msix`.
    """
    upload_path = _versioned_output_path(
        msix_path.with_suffix(".msixupload"),
        version_tag,
    )
    if upload_path.exists():
        upload_path.unlink()

    with zipfile.ZipFile(upload_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(msix_path, arcname=msix_path.name)

    _ok(f"Package Store upload : {upload_path}")
    return upload_path


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


def _windows_icu_cache_dir() -> Path:
    return ROOT / "build" / "tools" / "icu" / f"win-x64-{_WIN_ICU_NUGET_VERSION}"


def _native_windows_system_icu_search_dirs() -> list[Path]:
    """Likely Windows OS directories containing system ICU runtime DLLs."""
    win_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = [
        win_dir / "System32",
        win_dir / "SysWOW64",
    ]
    return [path for path in candidates if path.is_dir()]


def _wine_system_icu_search_dirs() -> list[Path]:
    """Likely Wine directories containing system ICU runtime DLLs."""
    return [
        _WINE_PREFIX / "drive_c" / "windows" / "system32",
        _WINE_PREFIX / "drive_c" / "windows" / "SysWOW64",
    ]


def _select_windows_icu_runtime_dlls(search_dirs: Iterable[Path]) -> list[Path]:
    """
    Select a Qt-compatible ICU runtime set from given directories.

    Required:
      - icuuc.dll
      - icuin.dll
      - either icudt.dll or icu.dll (data/runtime provider)
    """
    wanted_order = ("icuuc.dll", "icuin.dll", "icudt.dll", "icu.dll")
    selected: dict[str, Path] = {}
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for filename in wanted_order:
            candidate = directory / filename
            if not candidate.is_file() or filename in selected:
                continue
            selected[filename] = candidate.resolve()

    has_core = "icuuc.dll" in selected and "icuin.dll" in selected
    has_data = "icudt.dll" in selected or "icu.dll" in selected
    if not (has_core and has_data):
        return []

    ordered = ["icuuc.dll", "icuin.dll"]
    if "icudt.dll" in selected:
        ordered.append("icudt.dll")
    if "icu.dll" in selected:
        ordered.append("icu.dll")
    return [selected[name] for name in ordered]


def _extract_windows_icu_alias_name(filename: str) -> str | None:
    match = re.fullmatch(r"(icu(?:uc|in|dt))\d+\.dll", filename, flags=re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(1).lower()}.dll"


def _ensure_windows_icu_runtime_cache() -> list[Path]:
    """
    Download and extract ICU runtime DLLs for Windows from NuGet.

    The Microsoft ICU runtime package ships versioned DLL names
    (`icuuc72.dll`, `icuin72.dll`, `icudt72.dll`). We keep both the original
    files and later create suffix-less aliases next to Qt when needed because
    some Qt builds import `icuuc.dll` directly.
    """
    cache_dir = _windows_icu_cache_dir()
    extracted = sorted(cache_dir.glob("icu*.dll"))
    if extracted:
        return extracted

    cache_dir.mkdir(parents=True, exist_ok=True)
    nupkg = cache_dir / "icu-runtime.nupkg"
    if not nupkg.exists():
        _info(f"Téléchargement ICU Windows {_WIN_ICU_NUGET_VERSION} depuis NuGet…")
        urllib.request.urlretrieve(_WIN_ICU_NUGET_URL, nupkg)

    extracted_any = False
    with zipfile.ZipFile(nupkg) as archive:
        for member in archive.namelist():
            normalized = member.replace("\\", "/")
            if not normalized.startswith("runtimes/win-x64/native/"):
                continue
            if not normalized.lower().endswith(".dll"):
                continue
            target = cache_dir / Path(normalized).name
            with archive.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted_any = True

    # --- AJOUT : création des alias ICU non suffixés ---
    alias_map: dict[str, Path | None] = {
        "icuuc": None,
        "icuin": None,
        "icudt": None,
        "icu": None,
    }

    for p in cache_dir.iterdir():
        name = p.name.lower()
        if name.startswith("icuuc"):
            alias_map["icuuc"] = p
        elif name.startswith("icuin"):
            alias_map["icuin"] = p
        elif name.startswith("icudt"):
            alias_map["icudt"] = p
        elif name == "icu.dll":
            alias_map["icu"] = p


    # créer les alias
    for plain, src in (
        ("icuuc.dll", alias_map["icuuc"]),
        ("icuin.dll", alias_map["icuin"]),
        ("icudt.dll", alias_map["icudt"] or alias_map["icu"]),
        ("icu.dll", alias_map["icu"] or alias_map["icudt"]),
    ):
        if src is None:
            continue
        dst = cache_dir / plain
        if not dst.exists():
            shutil.copy2(src, dst)
            _ok(f"ICU alias créé : {dst.name} -> {src.name}")


    if not extracted_any:
        raise RuntimeError(
            "Le package ICU Windows téléchargé ne contient aucune DLL exploitable."
        )

    dlls = sorted(cache_dir.glob("icu*.dll"))
    if not dlls:
        raise RuntimeError("Extraction ICU Windows vide après téléchargement.")
    _ok(f"Runtime ICU Windows prêt : {cache_dir}")
    return dlls


def _sync_windows_icu_dlls(target_dir: Path, source_dlls: Iterable[Path]) -> list[Path]:
    """
    Copy ICU DLLs into `target_dir` and create alias names without suffixes.
    """
    return _sync_windows_icu_dlls_with_alias_option(target_dir, source_dlls, create_aliases=True)


def _sync_windows_icu_dlls_with_alias_option(
    target_dir: Path,
    source_dlls: Iterable[Path],
    *,
    create_aliases: bool,
) -> list[Path]:
    """Copy ICU DLLs into `target_dir` with optional suffix-less aliases."""
    target_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for src in source_dlls:
        dest = target_dir / src.name
        shutil.copy2(src, dest)
        copied.append(dest)

        if create_aliases:
            alias_name = _extract_windows_icu_alias_name(src.name)
            if alias_name:
                alias_dest = target_dir / alias_name
                shutil.copy2(src, alias_dest)
                copied.append(alias_dest)

    return _dedupe_paths(copied)

def _find_windows_icu_dlls(search_dirs: Iterable[Path]) -> list[Path]:
    matches: list[Path] = []
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        matches.extend(sorted(directory.glob("icu*.dll")))
    return _dedupe_paths(matches)


def _wine_pyside6_dir() -> Path:
    return _WIN_PY_EXE.parent / "Lib" / "site-packages" / "PySide6"


def _ensure_wine_qt_icu_runtime() -> list[Path]:
    """
    Ensure Qt ICU DLLs are present in the Wine Python environment.

    Prefer unsuffixed ICU DLLs from Wine's Windows runtime (`system32`), which
    match Qt's expected exports (`icuuc.dll`, `icuin.dll`, ...). NuGet ICU is
    only used as a fallback.
    """
    pyside_dir = _wine_pyside6_dir()
    if not pyside_dir.is_dir():
        raise RuntimeError(f"PySide6 introuvable dans Wine: {pyside_dir}")

    existing = _select_windows_icu_runtime_dlls([pyside_dir, _WIN_PY_EXE.parent])
    if existing:
        _ok("DLLs ICU deja presentes dans le runtime Wine")
        return existing

    system_runtime = _select_windows_icu_runtime_dlls(_wine_system_icu_search_dirs())
    if system_runtime:
        installed = _sync_windows_icu_dlls_with_alias_option(
            pyside_dir,
            system_runtime,
            create_aliases=False,
        )
        # Duplicate into the Python root as a fallback search location for loader quirks.
        installed.extend(
            _sync_windows_icu_dlls_with_alias_option(
                _WIN_PY_EXE.parent,
                system_runtime,
                create_aliases=False,
            )
        )
        installed = _dedupe_paths(installed)
        verified = _select_windows_icu_runtime_dlls([pyside_dir, _WIN_PY_EXE.parent])
        if verified:
            _ok("DLLs ICU Windows copiees depuis le runtime systeme Wine")
            return verified

    # Fallback: NuGet (versioned ICU DLL names). Keep original filenames only;
    # we intentionally avoid creating suffix-less aliases here to prevent
    # export mismatches ("specified procedure could not be found").
    source_dlls = _ensure_windows_icu_runtime_cache()
    installed = _sync_windows_icu_dlls_with_alias_option(
        pyside_dir,
        source_dlls,
        create_aliases=False,
    )
    # Duplicate into the Python root as a fallback search location for loader quirks.
    installed.extend(
        _sync_windows_icu_dlls_with_alias_option(
            _WIN_PY_EXE.parent,
            source_dlls,
            create_aliases=False,
        )
    )
    installed = _dedupe_paths(installed)

    verified = _select_windows_icu_runtime_dlls([pyside_dir, _WIN_PY_EXE.parent])
    if verified:
        _ok("DLLs ICU Windows copiees depuis NuGet")
        return verified

    installed_names = sorted({path.name.lower() for path in installed})
    raise RuntimeError(
        "Runtime ICU incompatible dans Wine. "
        "Qt attend des DLLs non suffixees (icuuc.dll/icuin.dll + icudt.dll ou icu.dll), "
        f"mais les DLLs disponibles sont: {', '.join(installed_names)}. "
        "Use a newer Wine runtime (system ICU) or provide compatible ICU DLLs."
    )

def _extract_missing_wine_dlls(log_text: str) -> list[str]:
    """Extract missing DLL names from Wine loader diagnostics."""
    matches = re.findall(r"Library\s+([^\s]+\.dll)\s+\(.*?not found", log_text, flags=re.IGNORECASE)
    seen: set[str] = set()
    dlls: list[str] = []
    for raw in matches:
        name = Path(raw).name
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        dlls.append(name)
    return dlls


def _verify_wine_pyside6_runtime() -> None:
    """
    Ensure `from PySide6 import QtCore, QtWidgets` works in the Wine runtime.

    PyInstaller Qt hooks import Qt modules in isolated child processes. If these
    imports are broken, the build may continue with incomplete Qt collection and
    produce a broken Windows bundle.
    """
    result = subprocess.run(
        [
            "wine",
            str(_WIN_PY_EXE),
            "-c",
            "from PySide6 import QtCore, QtWidgets; print(QtCore.__file__); print(QtWidgets.__file__)",
        ],
        env=_wine_env("err+all"),
        check=False,
        capture_output=True,
        text=True,
    )
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    combined = "\n".join(part for part in (stdout, stderr) if part)
    if result.returncode == 0:
        location = stdout.splitlines()[-1] if stdout else "QtCore/QtWidgets"
        _ok(f"PySide6/QtCore+QtWidgets import OK dans Wine : {location}")
        return

    missing_dlls = _extract_missing_wine_dlls(combined)
    missing_bits = f" DLLs manquantes signalees: {', '.join(missing_dlls)}." if missing_dlls else ""

    pyside_dir = _wine_pyside6_dir()
    package_missing: list[str] = []
    for dll_name in missing_dlls:
        if not (pyside_dir / dll_name).exists():
            package_missing.append(dll_name)
    package_bits = (
        f" Absentes du package PySide6 installe: {', '.join(package_missing)}."
        if package_missing else ""
    )
    icu_files = _find_windows_icu_dlls([pyside_dir, _WIN_PY_EXE.parent])
    icu_names = {path.name.lower() for path in icu_files}
    has_versioned_icu = any(
        re.fullmatch(r"icu(?:uc|in|dt)\d+\.dll", name, flags=re.IGNORECASE)
        for name in icu_names
    )
    procedure_not_found = (
        "specified procedure could not be found" in combined.lower()
        or "procedure specifiee est introuvable" in combined.lower()
    )

    hint = ""
    if any(name.lower().startswith("icu") for name in missing_dlls):
        hint = (
            " Le wheel PySide6 installe dans Wine semble incomplet pour cette "
            "version/environnement. Essayez une autre version via "
            "MEDIARECODE_WINE_PYSIDE6_VERSION ou verifiez le contenu du wheel."
        )
    elif procedure_not_found and has_versioned_icu:
        hint = (
            " ICU semble incompatible (DLLs versionnees detectees). "
            "Qt attend des exports non suffixes dans icuuc.dll/icuin.dll."
        )
    elif missing_dlls:
        hint = " Verifiez le runtime Visual C++ et les DLLs Qt dans le prefixe Wine."

    excerpt = ""
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    if lines:
        excerpt = " Sortie Wine: " + " | ".join(lines[:6])

    raise RuntimeError(
        "Import PySide6.QtCore/QtWidgets impossible dans le Python Wine apres installation."
        + missing_bits
        + package_bits
        + hint
        + excerpt
    )

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
    _info(f"Installation des dépendances Python dans Wine (PySide6=={_WIN_PYSIDE6_VER})…")
    _wine_pip("pyinstaller", f"PySide6=={_WIN_PYSIDE6_VER}", "pymediainfo>=6.1.0")
    _ensure_wine_qt_icu_runtime()
    _verify_wine_pyside6_runtime()
    _ok("Dépendances Python Windows installées")


def _build_pyinstaller_wine() -> Path:
    """Lance PyInstaller via Wine et retourne le dossier bundle produit."""
    _title("Étape Wine — PyInstaller")

    if _WIN_BUNDLE.exists():
        shutil.rmtree(_WIN_BUNDLE)

    wine_env = {**os.environ, "WINEPREFIX": str(_WINE_PREFIX), "WINEDEBUG": "-all"}

    sep = ";"   # séparateur Windows pour --add-data
    add_data: list[str] = []
    for src, dest in DATA_FILES:
        src_path = ROOT / src
        if src_path.exists():
            # Wine attend des chemins Windows : on passe le chemin Linux,
            # Wine le convertit automatiquement via son VFS.
            win_src = subprocess.check_output(
                ["winepath", "-w", str(src_path)],
                env=wine_env,
                text=True,
            ).strip()
            add_data += ["--add-data", f"{win_src}{sep}{dest}"]

    cmd: list[str] = [
        str(_WIN_PY_EXE), "-m", "PyInstaller",
        "--name", "mediarecode",
        "--onedir",
        "--noconfirm",
        _pyinstaller_frontend_flag("Windows"),
        *add_data,
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtWidgets",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PySide6.QtSvg",
        "--hidden-import", "PySide6.QtMultimedia",
        "--hidden-import", "pymediainfo",
        "--collect-binaries", "PySide6",
        "--collect-data", "PySide6",
        "--collect-all", "pymediainfo",
        "--collect-submodules", "core",
        "--collect-submodules", "ui",
        "--collect-submodules", "workers",
        *[arg for mod in EXCLUDED_MODULES for arg in ("--exclude-module", mod)],
    ]
    _add_windows_ctypes_to_pyinstaller_wine(cmd, wine_env)
    _add_windows_sqlite_to_pyinstaller_wine(cmd, wine_env)
    _ensure_windows_icu_runtime(wine_env)
    _add_windows_ssl_to_pyinstaller_wine(cmd, wine_env)
    _add_windows_icu_to_pyinstaller_wine(cmd, wine_env)
    win_ver = subprocess.check_output(
        ["winepath", "-w", str(_write_windows_version_file())],
        env=wine_env,
        text=True,
    ).strip()
    cmd += ["--version-file", win_ver]

    win_icon = _resolve_windows_icon_ico()
    if win_icon is not None:
        win_ico = subprocess.check_output(
            ["winepath", "-w", str(win_icon)],
            env=wine_env,
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
        _verify_windows_runtime_bundle(_WIN_BUNDLE)
    finally:
        shutil.rmtree(wine_tmpdir, ignore_errors=True)

    _ok(f"Bundle Windows : {_WIN_BUNDLE}")
    return _WIN_BUNDLE


# ── Script NSIS ────────────────────────────────────────────────────────────────

_NSIS_TEMPLATE = r"""\
Unicode true
; Cible 64 bits : registre non redirigé, $PROGRAMFILES64, RegView 64
!include "x64.nsh"

!define APP_NAME      "{app_name}"
!define APP_VERSION   "{app_version}"
!define EXE_NAME      "mediarecode.exe"
!define INSTALL_DIR   "$PROGRAMFILES64\\Mediarecode"
!define UNINSTALL_KEY "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Mediarecode"

Name "${{APP_NAME}} ${{APP_VERSION}}"
OutFile "{outfile}"
InstallDir "${{INSTALL_DIR}}"
InstallDirRegKey HKLM "${{UNINSTALL_KEY}}" "InstallLocation"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
{icon_block}

Page directory
Page instfiles

Section "Application" SEC_MAIN
  SetOutPath "$INSTDIR"
  File /r "{bundle_dir_pattern}"

  ; Raccourci Menu Démarrer
  CreateDirectory "$SMPROGRAMS\\Mediarecode"
  CreateShortcut  "$SMPROGRAMS\\Mediarecode\\Mediarecode.lnk" "$INSTDIR\\${{EXE_NAME}}" "" "$INSTDIR\\${{EXE_NAME}}" 0

  ; Raccourci Bureau
  CreateShortcut "$DESKTOP\\Mediarecode.lnk" "$INSTDIR\\${{EXE_NAME}}" "" "$INSTDIR\\${{EXE_NAME}}" 0

  ; Clés désinstalleur — registre 64 bits (évite la redirection WoW64)
  SetRegView 64
  WriteRegStr   HKLM "${{UNINSTALL_KEY}}" "DisplayName"      "${{APP_NAME}}"
  WriteRegStr   HKLM "${{UNINSTALL_KEY}}" "DisplayVersion"   "${{APP_VERSION}}"
  WriteRegStr   HKLM "${{UNINSTALL_KEY}}" "Publisher"        "Mediarecode"
  WriteRegStr   HKLM "${{UNINSTALL_KEY}}" "InstallLocation"  "$INSTDIR"
  WriteRegStr   HKLM "${{UNINSTALL_KEY}}" "UninstallString"  "$INSTDIR\\Uninstall.exe"
  WriteRegDWORD HKLM "${{UNINSTALL_KEY}}" "NoModify"         1
  WriteRegDWORD HKLM "${{UNINSTALL_KEY}}" "NoRepair"         1
  SetRegView lastused

  WriteUninstaller "$INSTDIR\\Uninstall.exe"

  ; ── Windows Security — Controlled Folder Access ─────────────────────────
  ; L'installateur tourne déjà en admin : pas de prompt UAC supplémentaire.
  ; Add-MpPreference est idempotent (pas de doublon si déjà présent).
  ; Note : les variables PowerShell sont préfixées $$ dans le script NSIS
  ;        pour que NSIS les transmette littéralement comme $var à PowerShell.
  DetailPrint "Configuration Windows Security (Controlled Folder Access)..."
  nsExec::ExecToLog 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$ErrorActionPreference=''SilentlyContinue''; $$p=Get-MpPreference; if([int]$$p.EnableControlledFolderAccess -gt 0){{Add-MpPreference -ControlledFolderAccessAllowedApplications ''$INSTDIR\${{EXE_NAME}}''}}"'
SectionEnd

Section "Uninstall"
  ; ── Retrait de l'allowlist CFA ───────────────────────────────────────────
  nsExec::ExecToLog 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$ErrorActionPreference=''SilentlyContinue''; Remove-MpPreference -ControlledFolderAccessAllowedApplications ''$INSTDIR\${{EXE_NAME}}''"'

  Delete "$INSTDIR\\Uninstall.exe"
  RMDir /r "$INSTDIR"
  Delete "$SMPROGRAMS\\Mediarecode\\Mediarecode.lnk"
  RMDir  "$SMPROGRAMS\\Mediarecode"
  Delete "$DESKTOP\\Mediarecode.lnk"

  ; Suppression des clés registre 64 bits
  SetRegView 64
  DeleteRegKey HKLM "${{UNINSTALL_KEY}}"
  SetRegView lastused
SectionEnd
"""


def _nsis_bundle_glob(bundle_dir: Path) -> str:
    """
    Retourne le glob source à passer à `File /r` selon l'OS du build host.

    Le chemin est résolu par makensis au moment de la compilation, donc il doit
    utiliser les séparateurs du système local: POSIX pour le cross-build Linux,
    backslashes pour un build natif Windows.
    """
    if OS == "Windows":
        return str(bundle_dir).replace("/", "\\").rstrip("/\\") + "\\*"
    return bundle_dir.as_posix().rstrip("/\\") + "/*"


def _build_nsis_installer(bundle_dir: Path, version_tag: str | None = None) -> Path:
    """Génère le script NSIS et invoque makensis pour produire l'installateur."""
    _title("Étape NSIS — Installateur Windows")

    output = _versioned_output_path(ROOT / "Mediarecode-Setup.exe", version_tag)
    nsi    = ROOT / "mediarecode.nsi"
    win_icon = _resolve_windows_icon_ico()
    icon_block = ""
    if win_icon is not None:
        icon_path = str(win_icon).replace("\\", "/")
        icon_block = f'Icon "{icon_path}"\nUninstallIcon "{icon_path}"'

    # `File /r` lit le système de fichiers local au moment du build.
    bundle_dir_pattern = _nsis_bundle_glob(bundle_dir)

    nsi.write_text(
        _NSIS_TEMPLATE.format(
            app_name=APP_NAME,
            app_version=APP_VERSION,
            outfile=str(output),
            bundle_dir_pattern=bundle_dir_pattern,
            icon_block=icon_block,
        ),
        encoding="utf-8",
    )
    _info(f"Script NSIS : {nsi}")

    makensis = _find_makensis()
    if not makensis:
        print("  makensis introuvable.", file=sys.stderr)
        sys.exit(1)
    _run([makensis, str(nsi)])

    if not output.exists():
        print(f"  Installateur introuvable après makensis : {output}", file=sys.stderr)
        sys.exit(1)

    _ok(f"Installateur : {output}")
    return output


# ─────────────────────────────────────────────────────────────────────────────
# macOS (.app + .dmg)
# ─────────────────────────────────────────────────────────────────────────────

_MACOS_ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)


def _build_icns_from_png(src_png: Path, dest_icns: Path) -> Path | None:
    """Construit un .icns depuis icon.png via iconutil (natif macOS).

    Retourne None si iconutil absent (Linux/Windows) ou PIL/PySide6 indisponible.
    """
    if shutil.which("iconutil") is None:
        _warn("iconutil absent — .icns non généré (build hors macOS ?)")
        return None
    if not src_png.exists():
        _warn(f"icon.png absent : {src_png} — .icns non généré")
        return None

    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        try:
            from PySide6.QtGui import QImage  # type: ignore[import-not-found]
            qt_backend = True
        except ImportError:
            _warn("Ni PIL ni PySide6.QtGui disponibles — .icns non généré")
            return None
        else:
            qt_backend = True
    else:
        qt_backend = False

    iconset = dest_icns.with_suffix(".iconset")
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    # iconutil attend une structure `{size}x{size}.png` + variantes `@2x`.
    for size in _MACOS_ICNS_SIZES:
        for scale in (1, 2):
            px = size * scale
            if px > 1024:
                continue
            suffix = "" if scale == 1 else "@2x"
            out = iconset / f"icon_{size}x{size}{suffix}.png"
            if qt_backend:
                img = QImage(str(src_png))
                scaled = img.scaled(
                    px, px,
                    aspectRatioMode=1,  # Qt.AspectRatioMode.KeepAspectRatio
                    transformMode=1,    # Qt.TransformationMode.SmoothTransformation
                )
                scaled.save(str(out), "PNG")
            else:
                img = Image.open(src_png).convert("RGBA")
                img.resize((px, px), Image.LANCZOS).save(out, "PNG")

    dest_icns.parent.mkdir(parents=True, exist_ok=True)
    _run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest_icns)])
    shutil.rmtree(iconset, ignore_errors=True)
    _ok(f"Icône macOS : {dest_icns.name}")
    return dest_icns


def _patch_macos_info_plist(app_path: Path, version_tag: str | None) -> None:
    """Met à jour CFBundle* et LSMinimumSystemVersion dans Info.plist."""
    plist_path = app_path / "Contents" / "Info.plist"
    if not plist_path.exists():
        _warn(f"Info.plist absent : {plist_path}")
        return

    import plistlib
    with plist_path.open("rb") as f:
        plist = plistlib.load(f)

    version = _normalize_version_tag(version_tag)
    plist["CFBundleIdentifier"] = _MACOS_BUNDLE_ID
    plist["CFBundleName"] = APP_NAME
    plist["CFBundleDisplayName"] = APP_NAME
    plist["CFBundleShortVersionString"] = version
    plist["CFBundleVersion"] = version
    plist["LSMinimumSystemVersion"] = _MACOS_MIN_VERSION
    plist["NSHighResolutionCapable"] = True
    # Évite le mode "Document-Based App" qui ouvre une fenêtre "Ouvrir un fichier…" au lancement
    plist["LSUIElement"] = False

    with plist_path.open("wb") as f:
        plistlib.dump(plist, f)
    _ok(f"Info.plist mis à jour (version {version}, min macOS {_MACOS_MIN_VERSION})")


def _build_macos_dmg(app_path: Path, version_tag: str | None) -> Path:
    """Crée un .dmg compressé depuis Mediarecode.app."""
    _title("Étape 3 — DMG")
    if shutil.which("hdiutil") is None:
        raise RuntimeError("hdiutil introuvable (requiert macOS)")

    version = _normalize_version_tag(version_tag)
    dist = ROOT / "dist"
    dmg_path = dist / f"Mediarecode-{version}.dmg"
    if dmg_path.exists():
        dmg_path.unlink()

    # Staging dir : l'app + un alias vers /Applications pour drag-n-drop
    staging = dist / "dmg_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(app_path, staging / app_path.name, symlinks=True)
    try:
        (staging / "Applications").symlink_to("/Applications")
    except OSError:
        pass

    _run([
        "hdiutil", "create",
        "-volname", APP_NAME,
        "-srcfolder", str(staging),
        "-ov",
        "-format", "UDZO",
        str(dmg_path),
    ])
    shutil.rmtree(staging, ignore_errors=True)

    if not dmg_path.exists():
        raise FileNotFoundError(f"hdiutil n'a pas produit : {dmg_path}")
    _ok(f"DMG : {dmg_path.name}")
    return dmg_path


def build_macos(dmg: bool, dest: str | None = None, version_tag: str | None = None) -> None:
    """Orchestre le build macOS natif : PyInstaller → .app → (optionnel) .dmg."""
    _title("Build macOS (PyInstaller → .app)")

    # Génère .icns avant PyInstaller pour qu'il l'embarque
    icns_path = ROOT / "build" / "icon.icns"
    _build_icns_from_png(ICON_PNG, icns_path)

    exe_path = _build_pyinstaller(onefile=False)
    # PyInstaller --name Mediarecode --windowed produit dist/Mediarecode.app
    app_final = exe_path.parent.parent.parent  # dist/Mediarecode.app

    # Copy icns into bundle if PyInstaller didn't embed it
    if icns_path.exists():
        resources = app_final / "Contents" / "Resources"
        resources.mkdir(parents=True, exist_ok=True)
        shutil.copy2(icns_path, resources / "icon.icns")

    _patch_macos_info_plist(app_final, version_tag=version_tag)

    if dmg:
        dmg_path = _build_macos_dmg(app_final, version_tag=version_tag)
        final = _copy_final_file_if_requested(dmg_path, dest, version_tag=version_tag)
        _title("Résultat")
        _ok(f"DMG macOS : {final}")
    else:
        _title("Résultat")
        _ok(f"App bundle : {app_final}")
        print(f"""
  Distribution :
    Copier {app_final.name} dans /Applications.
    Au premier lancement : clic-droit → Ouvrir (app non-signée).
""")


def build_windows(skip_wine: bool, dest: str | None = None, version_tag: str | None = None) -> None:
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

    installer = _build_nsis_installer(bundle_dir, version_tag=version_tag)
    final_installer = _copy_final_file_if_requested(installer, dest, version_tag=version_tag)

    _title("Résultat")
    _ok(f"Installateur Windows : {final_installer}")
    print(f"""
  Distribuer :
    {final_installer.name}
  Au premier lancement (sans config.ini dans %APPDATA%\\Mediarecode),
  le setup s'exécute pour installer les outils externes.
""")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def build(
    onefile: bool,
    exe_only: bool,
    clean: bool = False,
    dest: str | None = None,
    version_tag: str | None = None,
) -> None:
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
        appimage = _build_appimage(appdir, version_tag=version_tag)
        final_appimage = _copy_final_file_if_requested(appimage, dest, version_tag=version_tag)

        _title("Résultat")
        _ok(f"AppImage : {final_appimage}")
        print(f"""
  Distribution :
    Copier {final_appimage.name} n'importe où et l'exécuter directement.
    Au premier lancement (sans config.ini à côté), le setup s'exécute.
""")

    else:
        # ── Cible .exe (Windows ou --exe explicite) ───────────────────────────
        exe = _build_pyinstaller(onefile=onefile)
        final_exe = _copy_final_file_if_requested(exe, dest, version_tag=version_tag)

        _title("Résultat")
        if onefile or dest:
            _ok(f"Exécutable : {final_exe}")
        else:
            _ok(f"Dossier    : {exe.parent}")
            _ok(f"Exécutable : {exe}")
        print(f"""
  Distribution :
    Distribuer {'le fichier' if (onefile or dest) else 'le dossier'} {final_exe if (onefile or dest) else exe.parent}.
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
        "--msix",
        action="store_true",
        help=(
            "Génère un package MSIX sur Windows natif. "
            "Utilise makeappx.exe et signe le package si MEDIARECODE_MSIX_CERT_PFX "
            "et MEDIARECODE_MSIX_CERT_PASSWORD sont définis."
        ),
    )
    p.add_argument(
        "--msixupload",
        action="store_true",
        help=(
            "Génère un fichier .msixupload pour soumission Partner Center "
            "à partir du package MSIX Windows natif."
        ),
    )
    p.add_argument(
        "--store-config",
        metavar="PATH",
        help=(
            "Chemin vers un JSON de métadonnées Store/MSIX "
            "(identity, publisher, publisher_display_name, description, display_name)."
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
            "Sur Windows natif : inclus automatiquement (ce flag est ignoré). "
            "Sur Linux : inclus automatiquement dans --windows."
        ),
    )
    p.add_argument(
        "--allinc",
        action="store_true",
        help="Délègue à package_appimage.py --allinc (AppImage avec tous les outils embarqués).",
    )
    p.add_argument(
        "--dmg",
        action="store_true",
        help=(
            "Sur macOS natif : génère un .dmg distribuable depuis le .app. "
            "Sans ce flag, seul le bundle .app est produit dans dist/."
        ),
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="Nettoie tous les artefacts de build (build/, dist/, .wine_build/, *.AppImage…). Utilise sudo si nécessaire. Quitte sans builder.",
    )
    p.add_argument(
        "--dest",
        metavar="PATH",
        help=(
            "Chemin de copie du fichier final (dossier ou nom de fichier). "
            "Si le nom de fichier est omis, le nom auto-généré est conservé."
        ),
    )
    p.add_argument(
        "--version",
        metavar="TAG",
        default=APP_VERSION,
        help=(
            "Tag version à suffixer dans le nom du fichier final "
            f"(défaut: {APP_VERSION})."
        ),
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.clean:
        _clean_dirs()
        sys.exit(0)

    if OS == "Windows":
        # Sur Windows natif : PyInstaller puis format demandé
        _ensure_pyinstaller()
        exe_path = _build_pyinstaller(onefile=args.onefile)
        if args.onefile:
            onefile_dir = ROOT / "dist" / "mediarecode-onefile"
            onefile_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(exe_path, onefile_dir / exe_path.name)
            bundle_dir = onefile_dir
        else:
            bundle_dir = exe_path.parent

        store_metadata = _load_msix_store_metadata(Path(args.store_config) if args.store_config else None)

        if args.msix or args.msixupload:
            package_path = _build_msix_package(
                bundle_dir,
                version_tag=args.version,
                metadata=store_metadata,
            )
            final_artifact = package_path
            if args.msixupload:
                final_artifact = _build_msixupload(package_path, version_tag=args.version)
            final_file = _copy_final_file_if_requested(final_artifact, args.dest, version_tag=args.version)
            _title("Résultat")
            _ok(f"Artefact Windows Store : {final_file}")
        else:
            _ensure_makensis()
            installer = _build_nsis_installer(bundle_dir, version_tag=args.version)
            final_file = _copy_final_file_if_requested(installer, args.dest, version_tag=args.version)
            _title("Résultat")
            _ok(f"Installateur : {final_file}")
    elif args.allinc:
        # Délègue à package_appimage.py --allinc
        script = ROOT / "package_appimage.py"
        if not script.exists():
            print(f"  package_appimage.py introuvable : {script}", file=sys.stderr)
            sys.exit(1)
        argv = [sys.executable, str(script), "--allinc"]
        if args.dest:
            argv += ["--dest", args.dest]
        if args.version:
            argv += ["--version", args.version]
        os.execv(sys.executable, argv)
    elif args.windows:
        # Cross-compilation Windows depuis Linux via Wine + NSIS
        if OS != "Linux":
            print("--windows est uniquement supporté depuis Linux.", file=sys.stderr)
            sys.exit(1)
        build_windows(skip_wine=args.skip_wine, dest=args.dest, version_tag=args.version)
    elif OS == "Darwin":
        _ensure_pyinstaller()
        build_macos(dmg=args.dmg, dest=args.dest, version_tag=args.version)
    else:
        # Comportement par défaut : AppImage Linux (ou --exe pour PyInstaller natif)
        build(
            onefile=args.onefile,
            exe_only=args.exe,
            clean=False,
            dest=args.dest,
            version_tag=args.version,
        )
