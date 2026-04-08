#!/usr/bin/env python3
"""
package_appimage.py — Packaging multiplateforme de Mediarecode.

Modes :
  AppImage Linux (défaut)
    Mediarecode-<arch>.AppImage          outils installés au 1er lancement
    Mediarecode-<arch>_allinc.AppImage   tous les outils embarqués (--allinc)

  Installateur Windows (--windows)
    Mediarecode-Setup.exe                construit via Wine + PyInstaller + NSIS
    Nécessite : wine, winetricks, makensis (installés automatiquement si absents)

Étapes AppImage :
  1. Vérifie / installe PyInstaller + dépendances
  2. Construit un bundle --onedir avec PyInstaller (entrée : launcher.py)
  3. Assemble l'AppDir (structure AppImage standard)
     → si --allinc : télécharge et embarque ffmpeg, mkvtoolnix, mediainfo, dovi_tool, hdr10plus_tool
  4. Télécharge appimagetool si nécessaire
  5. Produit l'AppImage finale

Étapes Windows :separate
  1. Vérifie / installe Wine + un préfixe Python Windows dédié
  2. Installe PyInstaller + dépendances dans le préfixe Wine
  3. Construit un bundle --onedir via wine python.exe -m PyInstaller
  4. Génère un script NSIS et produit l'installateur .exe via makensis

Usage :
    distrobox enter my-distrobox -- python3 package_appimage.py
    distrobox enter my-distrobox -- python3 package_appimage.py --allinc
    distrobox enter my-distrobox -- python3 package_appimage.py --skip-pyinstaller
    distrobox enter my-distrobox -- python3 package_appimage.py --arch aarch64
    distrobox enter my-distrobox -- python3 package_appimage.py --windows
    distrobox enter my-distrobox -- python3 package_appimage.py --windows --skip-pyinstaller

Options :
    --allinc             Embarque tous les outils externes dans l'AppImage
    --skip-pyinstaller   Réutilise le bundle PyInstaller existant dans dist/
    --arch ARCH          Architecture cible AppImage : x86_64 (défaut) ou aarch64
    --dest PATH          Copie le fichier final vers un chemin personnalisé (dossier ou fichier)
    --windows            Build installateur Windows via Wine (cross-compilation)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
APPDIR = ROOT / "Mediarecode.AppDir"
APP_NAME = "mediarecode"
APP_DISPLAY_NAME = "Mediarecode"
APP_VERSION = "1.0.0"

# Préfixe Wine dédié au build Windows (isolé du préfixe utilisateur ~/.wine)
WINE_PREFIX = ROOT / ".wine_build"
# Répertoire d'installation de Python Windows dans ce préfixe
_WINE_PY_VER  = "3.11.9"
_WINE_PY_URL  = f"https://www.python.org/ftp/python/{_WINE_PY_VER}/python-{_WINE_PY_VER}-amd64.exe"
_WINE_PY_DEST = ROOT / f"python-{_WINE_PY_VER}-amd64.exe"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

def ok(msg: str)   -> None: print(_c("32",   f"  ✔  {msg}"))
def info(msg: str) -> None: print(_c("36",   f"  →  {msg}"))
def step(msg: str) -> None: print(_c("1;37", f"\n  ▸ {msg}"))
def err(msg: str)  -> None: print(_c("31",   f"  ✘  {msg}"), file=sys.stderr)


def run(cmd: list[str | Path], **kwargs) -> None:
    info(" ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def _resolve_dest_file(dest: str | None, default_output: Path) -> Path:
    """
    Résout le chemin de destination du fichier final.
    - dest absent: conserve default_output
    - dest dossier (existant, trailing slash, ou sans extension): utilise le nom auto
    - dest fichier: utilise ce nom
    """
    if not dest:
        return default_output

    raw = dest.strip()
    if not raw:
        return default_output

    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()

    if target.exists() and target.is_dir():
        return target / default_output.name

    if raw.endswith(("/", "\\")):
        return target / default_output.name

    if target.suffix == "":
        return target / default_output.name

    return target


def _copy_final_file_if_requested(src: Path, dest: str | None) -> Path:
    """Copie le fichier final vers --dest si fourni, sinon retourne src inchangé."""
    target = _resolve_dest_file(dest, src)

    src_resolved = src.resolve()
    target_resolved = target.resolve(strict=False)
    if src_resolved == target_resolved:
        return src

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    ok(f"Copie finale : {target}")
    return target


# ---------------------------------------------------------------------------
# Étape 0 — Dépendances du script lui-même
# ---------------------------------------------------------------------------

# Paquets nécessaires au build (PyInstaller doit pouvoir les importer)
_BUILD_DEPS: list[str] = [
    "pyinstaller",
    "PySide6>=6.6.0",
    "pymediainfo>=6.1.0",
]


def _pip_install(packages: list[str]) -> None:
    run([sys.executable, "-m", "pip", "install", "--upgrade", *packages])


def _install_system_package(package: str, fatal: bool = True) -> bool:
    """
    Installe un paquet système via dnf ou apt selon la distro disponible.
    Retourne True si l'installation a réussi ou si le gestionnaire est connu.
    Si fatal=True, exit(1) en cas d'échec.
    """
    if shutil.which("dnf"):
        run(["sudo", "dnf", "install", "-y", package])
        return True
    elif shutil.which("apt-get"):
        run(["sudo", "apt-get", "install", "-y", package])
        return True
    else:
        err(f"Impossible d'installer '{package}' automatiquement — gestionnaire de paquets inconnu.")
        err(f"Installez-le manuellement : {package}")
        if fatal:
            sys.exit(1)
        return False


def ensure_build_deps() -> None:
    """Installe toutes les dépendances nécessaires au script de build."""
    step("Vérification des dépendances de build")

    # ── Dépendances Python ────────────────────────────────────────────────
    missing_py: list[str] = []

    if importlib.util.find_spec("PyInstaller") is None:
        missing_py.append("pyinstaller")

    if importlib.util.find_spec("PySide6") is None:
        missing_py.append("PySide6>=6.6.0")

    if importlib.util.find_spec("pymediainfo") is None:
        missing_py.append("pymediainfo>=6.1.0")

    if missing_py:
        info(f"Paquets Python manquants : {', '.join(missing_py)}")
        _pip_install(missing_py)

    # ── mksquashfs — appimagetool l'embarque en interne, non bloquant ────────
    if not shutil.which("mksquashfs"):
        info("mksquashfs introuvable — tentative d'installation de squashfs-tools…")
        _install_system_package("squashfs-tools", fatal=False)
        if not shutil.which("mksquashfs"):
            info("mksquashfs absent du PATH — appimagetool utilisera son mksquashfs interne.")

    ok("Toutes les dépendances sont présentes")


# ---------------------------------------------------------------------------
# Étape 1 — PyInstaller
# ---------------------------------------------------------------------------


def build_onedir() -> Path:
    step("Compilation PyInstaller (--onedir)")

    out_dir = DIST_DIR / APP_NAME
    if out_dir.exists():
        info(f"Suppression du build précédent : {out_dir}")
        shutil.rmtree(out_dir)

    sep = ";" if sys.platform == "win32" else ":"

    # Modules Python à embarquer explicitement (imports dynamiques dans setup.py
    # ou dans launcher.py qui ne sont pas détectés automatiquement)
    hidden = [
        "PySide6.QtCore",
        "PySide6.QtWidgets",
        "PySide6.QtGui",
        "PySide6.QtSvg",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
        "PySide6.QtDBus",
        "pymediainfo",
        "configparser",
        "tarfile",
        "zipfile",
        "urllib.request",
        "urllib.error",
    ]

    cmd: list[str | Path] = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--onedir",
        "--noconfirm",
        # Données non-Python à embarquer dans le bundle
        f"--add-data={ROOT / 'locales.json'}{sep}.",
        f"--add-data={ROOT / 'requirements.txt'}{sep}.",
        # collect-data uniquement : inclut les plugins Qt (plateforme, imageformats…)
        # sans aspirer les binaires QML Wayland/NFC qui causent des warnings
        "--collect-data=PySide6",
        "--collect-all=pymediainfo",
        # Exclusions — modules Python inutiles
        "--exclude-module=tkinter",
        "--exclude-module=matplotlib",
        "--exclude-module=numpy",
        "--exclude-module=scipy",
        "--exclude-module=PIL",
        "--exclude-module=test",
        "--exclude-module=unittest",
        # Exclusions — sous-modules PySide6 non utilisés par l'app
        # (évite les warnings "Library not found" pour Wayland compositor, NFC, etc.)
        "--exclude-module=PySide6.QtWaylandCompositor",
        "--exclude-module=PySide6.QtNfc",
        "--exclude-module=PySide6.QtBluetooth",
        "--exclude-module=PySide6.QtLocation",
        "--exclude-module=PySide6.QtPositioning",
        "--exclude-module=PySide6.QtRemoteObjects",
        "--exclude-module=PySide6.QtScxml",
        "--exclude-module=PySide6.QtSensors",
        "--exclude-module=PySide6.QtSerialPort",
        "--exclude-module=PySide6.QtTextToSpeech",
        "--exclude-module=PySide6.QtWebChannel",
        "--exclude-module=PySide6.QtWebEngineCore",
        "--exclude-module=PySide6.QtWebEngineWidgets",
        "--exclude-module=PySide6.QtWebSockets",
        "--exclude-module=PySide6.Qt3DCore",
        "--exclude-module=PySide6.Qt3DRender",
        "--exclude-module=PySide6.Qt3DInput",
        "--exclude-module=PySide6.Qt3DAnimation",
        "--exclude-module=PySide6.Qt3DLogic",
        "--exclude-module=PySide6.Qt3DExtras",
        "--exclude-module=PySide6.QtCharts",
        "--exclude-module=PySide6.QtDataVisualization",
        "--exclude-module=PySide6.QtVirtualKeyboard",
        # Répertoires de sortie
        f"--distpath={DIST_DIR}",
        f"--workpath={BUILD_DIR}",
        # Point d'entrée
        str(ROOT / "launcher.py"),
    ]

    for mod in hidden:
        cmd += ["--hidden-import", mod]

    run(cmd, cwd=ROOT)

    if not out_dir.exists():
        err(f"Build PyInstaller raté : {out_dir} introuvable")
        sys.exit(1)

    ok(f"Bundle créé : {out_dir}")
    return out_dir


# ---------------------------------------------------------------------------
# Étape 2 — AppDir
# ---------------------------------------------------------------------------

_DESKTOP = textwrap.dedent("""\
    [Desktop Entry]
    Name=Mediarecode
    Comment=MKV/MP4 Workflow — DoVi · HDR10+ · Remux · Encode
    Exec=mediarecode
    Icon=mediarecode
    Type=Application
    Categories=AudioVideo;Video;
    Terminal=false
""")

# Icône SVG de secours (64×64) — remplacez par un PNG 256×256 dans le projet
_ICON_SVG = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
      <rect width="64" height="64" rx="12" fill="#141720"/>
      <text x="32" y="44" font-size="32" text-anchor="middle"
            font-family="monospace" fill="#4f6ef7">M</text>
    </svg>
""")

# AppRun standard
_APPRUN = textwrap.dedent("""\
    #!/bin/bash
    # AppRun — lanceur AppImage pour Mediarecode
    set -e

    HERE="$(dirname "$(readlink -f "$0")")"
    BIN="${HERE}/usr/bin"

    # PyInstaller 6 place les dépendances dans _internal/
    if [ -d "${BIN}/_internal" ]; then
        INTERNAL="${BIN}/_internal"
    else
        INTERNAL="${BIN}"
    fi

    export PATH="${BIN}:${PATH}"
    export LD_LIBRARY_PATH="${INTERNAL}:${LD_LIBRARY_PATH:-}"
    export QT_PLUGIN_PATH="${INTERNAL}/PySide6/Qt/plugins"
    export QML2_IMPORT_PATH="${INTERNAL}/PySide6/Qt/qml"
    export APPDIR="${HERE}"

    exec "${BIN}/mediarecode" "$@"
""")

# AppRun all-inclusive : les outils embarqués ont priorité sur les outils système
_APPRUN_ALLINC = textwrap.dedent("""\
    #!/bin/bash
    # AppRun — lanceur AppImage all-inclusive pour Mediarecode
    set -e

    HERE="$(dirname "$(readlink -f "$0")")"
    BIN="${HERE}/usr/bin"
    TOOLS="${BIN}/tools"

    # PyInstaller 6 place les dépendances dans _internal/
    if [ -d "${BIN}/_internal" ]; then
        INTERNAL="${BIN}/_internal"
    else
        INTERNAL="${BIN}"
    fi

    # Les outils embarqués ont la priorité sur les outils système
    export PATH="${TOOLS}:${BIN}:${PATH}"
    # TOOLS/lib contient les libs bundlées de MKVToolNix (libboost, libfmt…)
    export LD_LIBRARY_PATH="${TOOLS}/lib:${INTERNAL}:${LD_LIBRARY_PATH:-}"
    export QT_PLUGIN_PATH="${INTERNAL}/PySide6/Qt/plugins"
    export QML2_IMPORT_PATH="${INTERNAL}/PySide6/Qt/qml"
    export APPDIR="${HERE}"

    exec "${BIN}/mediarecode" "$@"
""")


# ---------------------------------------------------------------------------
# Téléchargement des outils externes (mode --allinc)
# ---------------------------------------------------------------------------

def _gh_latest_asset(repo: str, *patterns: str) -> str:
    """Retourne l'URL du premier asset GitHub dont le nom contient un des patterns."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "mediarecode-builder"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    for asset in data["assets"]:
        name: str = asset["name"]
        if any(p in name for p in patterns):
            return asset["browser_download_url"]
    raise RuntimeError(f"Aucun asset trouvé pour {patterns} dans {repo} (assets: {[a['name'] for a in data['assets']]})")


def _download(url: str, dest: Path, timeout: int = 30) -> None:
    """Télécharge url vers dest avec timeout, progress et reprise sur erreur."""
    info(f"Téléchargement : {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "mediarecode-builder"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk = 65536
            with open(dest, "wb") as f:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    downloaded += len(buf)
                    if total:
                        pct = downloaded * 100 // total
                        mb = downloaded / 1_048_576
                        print(f"\r    {pct:3d}%  {mb:.1f} Mo", end="", flush=True)
            print()  # newline après la barre
    except TimeoutError as e:
        raise RuntimeError(
            f"Timeout ({timeout}s) lors du téléchargement de {url}\n"
            f"Vérifiez votre connexion ou la disponibilité du serveur."
        ) from e


def _chmod_x(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _extract_from_tar(archive: Path, binary_names: list[str], dest_dir: Path) -> None:
    """Extrait des binaires nommés depuis une archive tar (gz/bz2/xz)."""
    with tarfile.open(archive) as tf:
        for member in tf.getmembers():
            bname = Path(member.name).name
            if bname in binary_names and member.isfile():
                member.name = bname  # aplatit le chemin
                tf.extract(member, dest_dir)
                _chmod_x(dest_dir / bname)
                binary_names = [b for b in binary_names if b != bname]
    if binary_names:
        raise RuntimeError(f"Binaires introuvables dans l'archive : {binary_names}")


def _dl_ffmpeg(tools_dir: Path, arch: str) -> None:
    """
    Télécharge ffmpeg/ffprobe depuis BtbN/FFmpeg-Builds (master, GPL static).

    Ces builds incluent NVENC, VAAPI, QSV, AMF, libsvtav1.
    Ce sont des binaires statiques — aucune lib externe à embarquer.
    URL directe : github.com/BtbN/FFmpeg-Builds/releases/download/latest/
                  ffmpeg-master-latest-linux64-gpl.tar.xz
    """
    step("Téléchargement ffmpeg + ffprobe (BtbN/FFmpeg-Builds, master GPL static)")

    _arch_tag = {"x86_64": "linux64", "aarch64": "linuxarm64"}.get(arch, f"linux{arch}")
    filename = f"ffmpeg-master-latest-{_arch_tag}-gpl.tar.xz"
    url = f"https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/{filename}"
    info(f"Asset : {filename}")

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "ffmpeg.tar.xz"
        _download(url, archive, timeout=120)
        _extract_from_tar(archive, ["ffmpeg", "ffprobe"], tools_dir)

    ok("ffmpeg + ffprobe (BtbN master GPL) installés")


def _mkvtoolnix_latest_url(arch: str) -> str:
    """
    Retourne l'URL de la dernière AppImage MKVToolNix en parsant le répertoire
    de téléchargement officiel (pas d'API requise).
    """
    import re
    index_url = "https://mkvtoolnix.download/appimage/"
    req = urllib.request.Request(index_url, headers={"User-Agent": "mediarecode-builder"})
    with urllib.request.urlopen(req) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    # Exemple de nom : MKVToolNix_GUI-85.0-x86_64.AppImage
    pattern = re.compile(rf'MKVToolNix_GUI-([\d.]+)-{re.escape(arch)}\.AppImage')
    versions = pattern.findall(html)
    if not versions:
        raise RuntimeError(
            f"Aucune AppImage MKVToolNix trouvée pour {arch} dans {index_url}"
        )
    versions.sort(key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    latest = versions[0]
    info(f"MKVToolNix version : {latest}")
    return f"{index_url}MKVToolNix_GUI-{latest}-{arch}.AppImage"


def _dl_mkvtoolnix(tools_dir: Path, arch: str) -> None:
    step("Téléchargement MKVToolNix (AppImage → extraction)")
    url = _mkvtoolnix_latest_url(arch)
    with tempfile.TemporaryDirectory() as tmp:
        appimage = Path(tmp) / "mkvtoolnix.AppImage"
        _download(url, appimage)
        _chmod_x(appimage)
        # --appimage-extract fonctionne sans FUSE (utilise unsquashfs de squashfs-tools)
        env = os.environ.copy()
        env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
        subprocess.run(
            [str(appimage), "--appimage-extract"],
            check=True, cwd=tmp, env=env,
        )
        root = Path(tmp) / "squashfs-root"
        src_bin = root / "usr" / "bin"
        for name in ("mkvmerge", "mkvextract", "mkvinfo", "mkvpropedit"):
            src = src_bin / name
            if src.exists():
                shutil.copy2(src, tools_dir / name)
                _chmod_x(tools_dir / name)
        # Copie les libs bundlées (libboost, libfmt…) — sans elles les binaires
        # refusent de démarrer avec "cannot open shared object file".
        libs_dir = tools_dir / "lib"
        libs_dir.mkdir(exist_ok=True)
        src_lib = root / "usr" / "lib"
        if src_lib.is_dir():
            for lib in src_lib.iterdir():
                if lib.is_file() and (lib.suffix in (".so",) or ".so." in lib.name):
                    shutil.copy2(lib, libs_dir / lib.name)
    ok("mkvmerge / mkvextract / mkvinfo / mkvpropedit + libs installés")


def _mediainfo_latest_version() -> str:
    """Retourne la dernière version de mediainfo en scrapant le répertoire mediaarea.net."""
    import re
    base = "https://mediaarea.net/download/binary/mediainfo/"
    req = urllib.request.Request(base, headers={"User-Agent": "mediarecode-builder"})
    with urllib.request.urlopen(req) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    versions = re.findall(r'href="(\d{2}\.\d{2})/"', html)
    if not versions:
        raise RuntimeError(f"Aucune version mediainfo trouvée sur {base}")
    versions.sort(key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    return versions[0]


def _dl_mediainfo(tools_dir: Path, arch: str) -> None:
    """
    Télécharge le CLI mediainfo depuis mediaarea.net.
    MediaArea ne fournit pas de binaire Linux natif ; on utilise le build
    Lambda (statiquement lié, compatible Linux x86_64 / arm64).
    """
    step("Téléchargement mediainfo CLI (mediaarea.net — Lambda build)")
    ver = _mediainfo_latest_version()
    info(f"mediainfo version : {ver}")
    # Lambda_x86_64 / Lambda_arm64  — binaires statiques, fonctionnent hors AWS
    _lambda_arch = {"x86_64": "x86_64", "aarch64": "arm64"}.get(arch, arch)
    base = "https://mediaarea.net/download/binary/mediainfo/"
    url = f"{base}{ver}/MediaInfo_CLI_{ver}_Lambda_{_lambda_arch}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "mediainfo.zip"
        _download(url, archive)
        with zipfile.ZipFile(archive) as zf:
            # Le zip contient directement "mediainfo" (ou dans un sous-dossier)
            candidates = [n for n in zf.namelist() if Path(n).name == "mediainfo"]
            if not candidates:
                raise RuntimeError(
                    f"Binaire 'mediainfo' introuvable dans l'archive. "
                    f"Contenu : {zf.namelist()}"
                )
            data = zf.read(candidates[0])
            dest = tools_dir / "mediainfo"
            dest.write_bytes(data)
            _chmod_x(dest)
    ok("mediainfo installé")


def _dl_dovi_tool(tools_dir: Path, arch: str) -> None:
    step("Téléchargement dovi_tool (GitHub)")
    _sfx = {"x86_64": "x86_64-unknown-linux-musl", "aarch64": "aarch64-unknown-linux-musl"}.get(arch, arch)
    url = _gh_latest_asset("quietvoid/dovi_tool", _sfx)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "dovi_tool.tar.gz"
        _download(url, archive)
        _extract_from_tar(archive, ["dovi_tool"], tools_dir)
    ok("dovi_tool installé")


def _dl_hdr10plus_tool(tools_dir: Path, arch: str) -> None:
    step("Téléchargement hdr10plus_tool (GitHub)")
    _sfx = {"x86_64": "x86_64-unknown-linux-musl", "aarch64": "aarch64-unknown-linux-musl"}.get(arch, arch)
    url = _gh_latest_asset("quietvoid/hdr10plus_tool", _sfx)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "hdr10plus_tool.tar.gz"
        _download(url, archive)
        _extract_from_tar(archive, ["hdr10plus_tool"], tools_dir)
    ok("hdr10plus_tool installé")


def bundle_tools(appdir: Path, arch: str) -> None:
    """Télécharge tous les outils externes et les place dans usr/bin/tools/."""
    step("Téléchargement des outils externes (mode all-inclusive)")
    tools_dir = appdir / "usr" / "bin" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)

    _dl_ffmpeg(tools_dir, arch)
    _dl_mkvtoolnix(tools_dir, arch)
    _dl_mediainfo(tools_dir, arch)
    _dl_dovi_tool(tools_dir, arch)
    _dl_hdr10plus_tool(tools_dir, arch)

    ok(f"Tous les outils embarqués dans {tools_dir}")


# ---------------------------------------------------------------------------
# Étape 2 — AppDir
# ---------------------------------------------------------------------------

def _clean_appdir(path: Path) -> None:
    """Supprime le répertoire AppDir, même si les fichiers appartiennent à un autre uid."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        pass
    # Tentative via rm système (peut échouer en sandbox sans sudo)
    result = subprocess.run(["rm", "-rf", str(path)], check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Impossible de supprimer {path} (fichiers appartenant à un autre uid).\n"
            "Supprimez-le manuellement depuis le distrobox :\n"
            f"  rm -rf {path}"
        )


def build_appdir(bundle_dir: Path, allinc: bool = False, arch: str = "x86_64") -> Path:
    step("Construction de l'AppDir")

    appdir = APPDIR
    if appdir.exists():
        try:
            _clean_appdir(appdir)
        except RuntimeError as exc:
            # Dossier non suppressible (permissions uid différent) → utilise un répertoire temporaire
            import tempfile
            appdir = Path(tempfile.mkdtemp(prefix="Mediarecode.AppDir.", dir=ROOT))
            info(f"AppDir alternatif utilisé : {appdir}  ({exc})")
    if not appdir.exists():
        appdir.mkdir()

    # usr/bin/ ← contenu du bundle PyInstaller
    usr_bin = appdir / "usr" / "bin"
    usr_bin.mkdir(parents=True)
    info(f"Copie du bundle → {usr_bin} …")
    shutil.copytree(bundle_dir, usr_bin, dirs_exist_ok=True)
    ok("Bundle copié")

    # Marqueur all-inclusive lu par launcher.py au démarrage
    if allinc:
        (usr_bin / "_ALLINC").touch()
        bundle_tools(appdir, arch)

    # AppRun
    apprun = appdir / "AppRun"
    apprun.write_text(_APPRUN_ALLINC if allinc else _APPRUN)
    apprun.chmod(apprun.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    ok("AppRun créé")

    # .desktop
    desktop = appdir / f"{APP_NAME}.desktop"
    desktop.write_text(_DESKTOP)
    ok(".desktop créé")

    # Icône — cherche d'abord un PNG/SVG dans le projet
    icon_src: Path | None = None
    for candidate in [
        ROOT / "icon.png",
        ROOT / f"{APP_NAME}.png",
        ROOT / "assets" / "icon.png",
        ROOT / "assets" / f"{APP_NAME}.png",
    ]:
        if candidate.exists():
            icon_src = candidate
            break

    if icon_src:
        dest_icon = appdir / f"{APP_NAME}{icon_src.suffix}"
        shutil.copy(icon_src, dest_icon)
        (appdir / ".DirIcon").symlink_to(dest_icon.name)
        ok(f"Icône copiée : {icon_src.name}")
    else:
        svg_path = appdir / f"{APP_NAME}.svg"
        svg_path.write_text(_ICON_SVG)
        (appdir / ".DirIcon").symlink_to(svg_path.name)
        info("Icône SVG de secours utilisée (ajoutez icon.png 256×256 à la racine du projet)")

    ok(f"AppDir prêt : {appdir}")
    return appdir


# ---------------------------------------------------------------------------
# Étape 3 — appimagetool
# ---------------------------------------------------------------------------

_APPIMAGETOOL_URLS: dict[str, str] = {
    "x86_64":  "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage",
    "aarch64": "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-aarch64.AppImage",
}


def get_appimagetool(arch: str) -> Path:
    step("Vérification de appimagetool")

    found = shutil.which("appimagetool")
    if found:
        ok(f"appimagetool trouvé sur le PATH : {found}")
        return Path(found)

    dest = ROOT / f"appimagetool-{arch}.AppImage"
    if dest.exists():
        ok(f"appimagetool déjà téléchargé : {dest.name}")
    else:
        url = _APPIMAGETOOL_URLS.get(arch)
        if not url:
            err(f"Architecture non supportée par appimagetool : {arch}")
            sys.exit(1)
        info(f"Téléchargement de appimagetool ({arch})…")
        urllib.request.urlretrieve(url, dest)
        ok(f"Téléchargé : {dest.name}")

    dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    return dest


# ---------------------------------------------------------------------------
# Étape 4 — AppImage finale
# ---------------------------------------------------------------------------

def build_appimage(appimagetool: Path, appdir: Path, arch: str, allinc: bool = False) -> Path:
    step("Création de l'AppImage")

    suffix = "_allinc" if allinc else ""
    output = ROOT / f"{APP_DISPLAY_NAME}-{arch}{suffix}.AppImage"
    if output.exists():
        output.unlink()

    env = os.environ.copy()
    env["ARCH"] = arch
    # appimagetool est lui-même une AppImage : sans FUSE (distrobox, CI…)
    # il faut lui demander de s'extraire dans un dossier tmp plutôt que
    # de se monter via FUSE.
    env["APPIMAGE_EXTRACT_AND_RUN"] = "1"

    run(
        [appimagetool, "--no-appstream", str(appdir), str(output)],
        env=env,
    )

    output.chmod(output.stat().st_mode | stat.S_IEXEC)
    ok(f"AppImage créée : {output.name}")
    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--allinc",
        action="store_true",
        help=(
            "Embarque ffmpeg, mkvtoolnix, mediainfo, dovi_tool et hdr10plus_tool "
            "dans l'AppImage. Produit Mediarecode-<arch>_allinc.AppImage. "
            "Au premier lancement, seule la configuration est initialisée."
        ),
    )
    p.add_argument(
        "--skip-pyinstaller",
        action="store_true",
        help="Réutilise le bundle PyInstaller existant dans dist/mediarecode/",
    )
    p.add_argument(
        "--arch",
        default=platform.machine(),
        choices=list(_APPIMAGETOOL_URLS),
        help="Architecture cible (défaut : machine courante)",
    )
    p.add_argument(
        "--dest",
        metavar="PATH",
        help=(
            "Chemin de copie du fichier final (dossier ou nom de fichier). "
            "Si le nom de fichier est omis, le nom auto-généré est conservé."
        ),
    )
    return p.parse_args()


def main() -> None:
    if sys.platform != "linux":
        err("Ce script ne fonctionne que sur Linux.")
        sys.exit(1)

    args = parse_args()
    arch = args.arch
    allinc = args.allinc

    print(_c("1;34", """
╔══════════════════════════════════════════╗
║      Mediarecode — AppImage Builder      ║
╚══════════════════════════════════════════╝
"""))
    info(f"Architecture  : {arch}")
    info(f"Mode          : {'all-inclusive (outils embarqués)' if allinc else 'standard (outils installés au 1er lancement)'}")
    info(f"Racine projet : {ROOT}")

    ensure_build_deps()

    if args.skip_pyinstaller:
        bundle_dir = DIST_DIR / APP_NAME
        if not bundle_dir.exists():
            err(f"--skip-pyinstaller : bundle introuvable dans {bundle_dir}")
            sys.exit(1)
        info(f"Bundle existant réutilisé : {bundle_dir}")
    else:
        bundle_dir = build_onedir()

    appdir = build_appdir(bundle_dir, allinc=allinc, arch=arch)
    appimagetool = get_appimagetool(arch)
    appimage_path = build_appimage(appimagetool, appdir, arch, allinc=allinc)
    final_appimage = _copy_final_file_if_requested(appimage_path, args.dest)

    print(_c("1;32", """
╔══════════════════════════════════════════╗
║  Build terminé avec succès !             ║
╚══════════════════════════════════════════╝"""))
    print(f"\n  AppImage : {final_appimage}")
    info("Lancez l'application avec :")
    print(f"\n    chmod +x \"{final_appimage}\"")
    print(f"    \"{final_appimage}\"\n")
    if allinc:
        info("Mode all-inclusive : au 1er lancement, seule la configuration")
        info("est initialisée (~/.config/mediarecode/config.ini).")
    else:
        info("Au 1er lancement, le setup s'exécute si")
        info("~/.config/mediarecode/config.ini est absent.")


if __name__ == "__main__":
    main()
