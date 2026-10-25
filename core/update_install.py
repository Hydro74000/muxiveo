"""
core/update_install.py — Téléchargement vérifié et installation d'une mise à jour.

Installation intégrée proposée (`SELF_INSTALL_KINDS`) :
- Windows : lancement de l'installeur NSIS (élévation UAC gérée par ShellExecute) ;
- AppImage : remplacement atomique du fichier `$APPIMAGE` puis relance (l'instance en cours garde son image montée) ;
- archive source (marqueur `.muxiveo-installation`, pas de dépôt git) et application macOS (bundle copié depuis le
  DMG) : échange de dossiers par `core/update_apply.py`, processus détaché après la sortie de l'application.

Les autres modes relèvent de leur gestionnaire de mises à jour ou de la page de release : Flatpak, Homebrew,
dépôt git (`git pull`).
Intégrité : SHA-256 contrôlé contre l'asset `SHA256SUMS` publié avec la release ;
sans lui, l'installation est refusée.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path

from core import update_apply
from core.subprocess_utils import host_environment
from core.update_check import RELEASE_DOWNLOAD_URL_PREFIX, ReleaseAsset, UpdateInfo, is_repository_url
from core.version import APP_MACOS_BUNDLE_ID, APP_USER_AGENT

CHECKSUMS_ASSET_NAME = "SHA256SUMS"
_MAX_ASSET_BYTES = 2 * 1024**3
_MAX_CHECKSUMS_BYTES = 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_WINDOWS_INSTALLER_RE = re.compile(r"^Muxiveo-Setup-.+\.exe$", re.IGNORECASE)
_SOURCE_ARCHIVE_RE = re.compile(r"^Muxiveo-.+-source\.tar\.gz$")
_MACOS_DMG_RE = re.compile(r"^Muxiveo-.+\.dmg$")
_HELPER_READY_TIMEOUT_S = 15.0
_CHECKSUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")

ProgressCallback = Callable[[int, int], None]
CancelCallback = Callable[[], bool]


class InstallKind(Enum):
    """Mode d'installation de l'application en cours."""

    APPIMAGE = "appimage"
    FLATPAK = "flatpak"
    MACOS_APP = "macos-app"
    SOURCE_ARCHIVE = "source-archive"
    WINDOWS_INSTALLER = "windows-installer"
    UNSUPPORTED = "unsupported"


# Flatpak, Homebrew et dépôt git : gestionnaire de mises à jour externe ou téléchargement depuis la page de la release.
SELF_INSTALL_KINDS = frozenset({
    InstallKind.WINDOWS_INSTALLER, InstallKind.APPIMAGE, InstallKind.SOURCE_ARCHIVE, InstallKind.MACOS_APP,
})
_SWAP_KINDS = {InstallKind.SOURCE_ARCHIVE: update_apply.KIND_SOURCE, InstallKind.MACOS_APP: update_apply.KIND_MACOS}
_FLATPAK_INFO = Path("/.flatpak-info")
_SOURCE_ROOT = Path(__file__).resolve().parent.parent

# Raisons, propres à cette machine, qui empêchent l'installation intégrée (`self_install_blocker`).
BLOCKER_NOT_WRITABLE = "not-writable"
BLOCKER_TRANSLOCATED = "translocated"
BLOCKER_PYTHON_INSIDE = "python-inside"
BLOCKER_NO_HDIUTIL = "no-hdiutil"
BLOCKER_NO_MARKER = "no-marker"


class UpdateInstallError(Exception):
    """Échec du téléchargement ou de l'installation d'une mise à jour."""


def _is_homebrew(executable: str) -> bool:
    exe = executable.replace("\\", "/")
    return "/Cellar/" in exe or "/homebrew/" in exe.lower()


def detect_install_kind(
    env: Mapping[str, str] | None = None,
    sys_platform: str | None = None,
    frozen: bool | None = None,
    executable: str | None = None,
    source_root: Path | None = None,
) -> InstallKind:
    """Mode d'installation de l'application courante (bac à sable Flatpak sondé seulement sur l'environnement réel)."""
    real_env = env is None
    env = os.environ if env is None else env
    sys_platform = sys.platform if sys_platform is None else sys_platform
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable = sys.executable if executable is None else executable
    root = _SOURCE_ROOT if source_root is None else source_root
    if sys_platform.startswith("linux"):
        if env.get("FLATPAK_ID") or (real_env and _FLATPAK_INFO.exists()):
            return InstallKind.FLATPAK
        if env.get("APPIMAGE"):
            return InstallKind.APPIMAGE
    if frozen:
        if sys_platform == "win32":
            return InstallKind.WINDOWS_INSTALLER
        if sys_platform == "darwin" and not _is_homebrew(executable):
            return InstallKind.MACOS_APP
        return InstallKind.UNSUPPORTED
    # Sources : dépôt git (développement, `git pull`) jamais remplacé ; archive source reconnue à son marqueur.
    if not _is_homebrew(executable) and not (root / ".git").exists() and (root / update_apply.MARKER_NAME).is_file():
        return InstallKind.SOURCE_ARCHIVE
    return InstallKind.UNSUPPORTED


def manual_update_hint(
    executable: str | None = None, frozen: bool | None = None, source_root: Path | None = None,
) -> str:
    """Commande de mise à jour manuelle pour les modes non auto-installables."""
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if _is_homebrew(executable or sys.executable):
        return "brew upgrade muxiveo"
    if not frozen and ((_SOURCE_ROOT if source_root is None else source_root) / ".git").exists():
        return "git pull"
    return ""


def installation_root(kind: InstallKind) -> Path:
    """Fichier ou dossier remplacé par la mise à jour : AppImage, dossier des sources, bundle `.app`."""
    if kind is InstallKind.APPIMAGE:
        return _appimage_path()
    if kind is InstallKind.SOURCE_ARCHIVE:
        return _SOURCE_ROOT
    if kind is InstallKind.MACOS_APP:
        return Path(sys.executable).resolve().parents[2]       # Muxiveo.app/Contents/MacOS/Muxiveo
    raise UpdateInstallError("Mode d'installation sans emplacement remplaçable.")


def self_install_blocker(kind: InstallKind) -> str:
    """Raison, propre à cette machine, qui empêche l'installation intégrée (`BLOCKER_*`), "" sinon."""
    if kind is InstallKind.UNSUPPORTED:
        # Sources hors dépôt git ni Homebrew : archive antérieure au marqueur, à remplacer une fois à la main.
        frozen = bool(getattr(sys, "frozen", False))
        sources = not frozen and not _is_homebrew(sys.executable) and not (_SOURCE_ROOT / ".git").exists()
        return BLOCKER_NO_MARKER if sources else ""
    if kind not in (InstallKind.APPIMAGE, InstallKind.SOURCE_ARCHIVE, InstallKind.MACOS_APP):
        return ""
    try:
        root = installation_root(kind)
    except UpdateInstallError:
        return BLOCKER_NOT_WRITABLE
    if kind is InstallKind.MACOS_APP:
        if "/AppTranslocation/" in str(root):
            return BLOCKER_TRANSLOCATED
        if shutil.which("hdiutil") is None or shutil.which("ditto") is None:
            return BLOCKER_NO_HDIUTIL
    if kind is InstallKind.SOURCE_ARCHIVE and os.name == "nt":
        # Windows : un exécutable en cours bloque le renommage de son dossier.
        if Path(sys.executable).resolve().is_relative_to(root):
            return BLOCKER_PYTHON_INSIDE
    if not os.access(root.parent, os.W_OK) or (kind is not InstallKind.APPIMAGE and not os.access(root, os.W_OK)):
        return BLOCKER_NOT_WRITABLE
    return ""


def select_asset(info: UpdateInfo, kind: InstallKind, machine: str | None = None) -> ReleaseAsset | None:
    """Choisit l'asset de release adapté au mode d'installation."""
    if kind is InstallKind.APPIMAGE:
        arch = (machine or platform.machine()).lower()
        return next(
            (a for a in info.assets if a.name.endswith(".AppImage") and arch in a.name.lower()),
            None,
        )
    if kind is InstallKind.SOURCE_ARCHIVE:
        return next((a for a in info.assets if _SOURCE_ARCHIVE_RE.match(a.name)), None)
    if kind is InstallKind.MACOS_APP:
        return next((a for a in info.assets if _MACOS_DMG_RE.match(a.name)), None)
    if kind is InstallKind.WINDOWS_INSTALLER:
        is_allinc = False
        if getattr(sys, "frozen", False):
            is_allinc = (Path(sys.executable).parent / "_ALLINC").exists()
        if is_allinc:
            allinc_asset = next(
                (a for a in info.assets if re.match(r"^Muxiveo-Setup-AllInc-.+\.exe$", a.name, re.IGNORECASE)),
                None,
            )
            if allinc_asset is not None:
                return allinc_asset
        else:
            std_asset = next(
                (a for a in info.assets if re.match(r"^Muxiveo-Setup-(?!AllInc).+\.exe$", a.name, re.IGNORECASE)),
                None,
            )
            if std_asset is not None:
                return std_asset
        return next((a for a in info.assets if _WINDOWS_INSTALLER_RE.match(a.name)), None)
    return None


def can_self_update(info: UpdateInfo, kind: InstallKind | None = None) -> bool:
    """True si le mode d'installation se met à jour lui-même ici et que la release fournit son paquet et ses sommes."""
    kind = detect_install_kind() if kind is None else kind
    return (
        kind in SELF_INSTALL_KINDS
        and select_asset(info, kind) is not None
        and info.asset(CHECKSUMS_ASSET_NAME) is not None
        and not self_install_blocker(kind)
    )


def parse_checksums(text: str) -> dict[str, str]:
    """Parse un fichier au format `sha256sum` → {nom: sha256}."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        match = _CHECKSUM_LINE_RE.match(line.strip())
        if match:
            sums[match.group(2)] = match.group(1).lower()
    return sums


def _urlopen(url: str, timeout: float):
    if not is_repository_url(url, RELEASE_DOWNLOAD_URL_PREFIX):
        raise UpdateInstallError(f"URL de téléchargement refusée : {url}")
    req = urllib.request.Request(url, headers={"User-Agent": APP_USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310  # préfixe HTTPS GitHub vérifié


def fetch_expected_sha256(info: UpdateInfo, asset: ReleaseAsset, timeout: float = 15.0) -> str:
    """Lit la somme SHA-256 attendue de `asset` dans l'asset SHA256SUMS de la release."""
    checksums = info.asset(CHECKSUMS_ASSET_NAME)
    if checksums is None:
        raise UpdateInstallError("La release ne publie pas de fichier SHA256SUMS : installation refusée.")
    try:
        with _urlopen(checksums.url, timeout) as resp:
            text = resp.read(_MAX_CHECKSUMS_BYTES).decode("utf-8", errors="replace")
    except OSError as exc:
        raise UpdateInstallError(f"Téléchargement de SHA256SUMS impossible : {exc}") from exc
    expected = parse_checksums(text).get(asset.name)
    if not expected:
        raise UpdateInstallError(f"Aucune somme SHA-256 pour {asset.name} dans SHA256SUMS.")
    return expected


def download_asset(
    asset: ReleaseAsset,
    dest: Path,
    expected_sha256: str,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
    timeout: float = 30.0,
) -> Path:
    """Télécharge `asset` vers `dest` en vérifiant sa somme SHA-256 (fichier .part intermédiaire)."""
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    received = 0
    try:
        if cancelled is not None and cancelled():
            raise UpdateInstallError("Téléchargement annulé.")
        with _urlopen(asset.url, timeout) as resp, part.open("wb") as fh:
            total = int(resp.headers.get("Content-Length") or asset.size or 0)
            if total > _MAX_ASSET_BYTES:
                raise UpdateInstallError(f"Taille de {asset.name} anormale ({total} octets).")
            while chunk := resp.read(_CHUNK_BYTES):
                if cancelled is not None and cancelled():
                    raise UpdateInstallError("Téléchargement annulé.")
                fh.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if received > _MAX_ASSET_BYTES:
                    raise UpdateInstallError(f"Taille de {asset.name} anormale.")
                if progress is not None:
                    progress(received, total)
        if digest.hexdigest() != expected_sha256.lower():
            raise UpdateInstallError(f"Somme SHA-256 invalide pour {asset.name} : fichier rejeté.")
        if cancelled is not None and cancelled():
            raise UpdateInstallError("Téléchargement annulé.")
        os.replace(part, dest)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise UpdateInstallError(f"Téléchargement de {asset.name} impossible : {exc}") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return dest


def _appimage_path(env: Mapping[str, str] | None = None) -> Path:
    raw = (os.environ if env is None else env).get("APPIMAGE", "")
    if not raw:
        raise UpdateInstallError("Variable APPIMAGE absente : application non lancée depuis une AppImage.")
    return Path(raw)


def _download_target(kind: InstallKind, asset: ReleaseAsset) -> Path:
    if kind is InstallKind.APPIMAGE:
        # Même dossier que l'AppImage : os.replace() reste atomique (même système de fichiers).
        folder = _appimage_path().parent
        if not os.access(folder, os.W_OK):
            raise UpdateInstallError(f"Dossier non modifiable : {folder}")
        return folder / f".{asset.name}.download"
    folder = Path(tempfile.gettempdir()) / "Muxiveo-update"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / asset.name


def download_update(
    info: UpdateInfo,
    kind: InstallKind,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
) -> Path:
    """Télécharge et vérifie l'asset de mise à jour ; renvoie le fichier prêt à installer.

    Archive source, application macOS : renvoie le dossier de travail préparé (`update_apply.prepare`).
    """
    asset = select_asset(info, kind)
    if asset is None:
        raise UpdateInstallError("Aucun paquet compatible dans la release.")
    expected = fetch_expected_sha256(info, asset)
    if kind not in _SWAP_KINDS:
        return download_asset(asset, _download_target(kind, asset), expected, progress, cancelled)
    root = installation_root(kind)
    # Dossier de travail à côté de l'installation : même système de fichiers, échange par simple renommage.
    work = Path(tempfile.mkdtemp(prefix=update_apply.WORKDIR_PREFIX, dir=root.parent))
    try:
        (work / update_apply.WORKDIR_TAG).touch()
        archive = download_asset(asset, work / asset.name, expected, progress, cancelled)
        if cancelled is not None and cancelled():
            raise UpdateInstallError("Téléchargement annulé.")
        probe, relaunch = _swap_commands(kind)
        update_apply.prepare(
            _SWAP_KINDS[kind], archive, work, root, info.version, probe, relaunch, APP_MACOS_BUNDLE_ID,
        )
    except update_apply.UpdateApplyError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise UpdateInstallError(str(exc)) from exc
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return work


def _swap_commands(kind: InstallKind) -> tuple[list[str], list[str]]:
    """Commandes de contrôle (`--cli version` ajouté) et de relance ; `{root}` = dossier contrôlé ou installé."""
    root = update_apply.ROOT_TOKEN
    if kind is InstallKind.MACOS_APP:
        return [os.path.join(root, "Contents", "MacOS", "Muxiveo")], ["/usr/bin/open", "-n", root]
    entry = Path(sys.argv[0]).name if sys.argv and Path(sys.argv[0]).name in ("launcher.py", "main.py") else "main.py"
    return [sys.executable, os.path.join(root, "main.py")], [sys.executable, os.path.join(root, entry)]


def _start_swap_helper(kind: InstallKind, work: Path) -> None:
    """Lance l'échange détaché, qui attend la sortie de l'application ; rend la main une fois prêt."""
    if kind is InstallKind.MACOS_APP:
        command = [sys.executable, "--apply-update"]
    else:
        # Copie hors du dossier remplacé, interpréteur isolé (ni dossier courant ni PYTHONPATH).
        script = work / "update_apply.py"
        shutil.copyfile(Path(update_apply.__file__), script)
        command = [sys.executable, "-I", str(script)]
    command += ["commit", str(work), "--pid", str(os.getpid())]
    kwargs: dict = {"cwd": work, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        detached = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:   # hors de l'objet job éventuel du terminal, qui le tuerait avec l'application
            helper = subprocess.Popen(command, creationflags=detached | 0x01000000, **kwargs)  # nosec B603
        except OSError:
            helper = subprocess.Popen(command, creationflags=detached, **kwargs)  # nosec B603
    else:
        helper = subprocess.Popen(command, start_new_session=True, **kwargs)  # nosec B603
    deadline = time.monotonic() + _HELPER_READY_TIMEOUT_S
    while not (work / update_apply.READY_FILE).exists():
        if helper.poll() is not None or time.monotonic() > deadline:
            if helper.poll() is None:
                helper.kill()
            reason = ""
            result = work / update_apply.RESULT_FILE
            if result.is_file():
                try:
                    reason = str(json.loads(result.read_text(encoding="utf-8")).get("error") or "")
                except (OSError, ValueError):
                    reason = ""
            shutil.rmtree(work, ignore_errors=True)
            raise UpdateInstallError(f"Lancement de la mise à jour impossible{' : ' + reason if reason else '.'}")
        time.sleep(0.05)


def collect_update_results(kind: InstallKind | None = None) -> list[dict]:
    """Résultats des mises à jour par échange de dossiers (signalés une fois), ménage des dossiers de travail."""
    kind = detect_install_kind() if kind is None else kind
    if kind not in _SWAP_KINDS:
        return []
    try:
        return update_apply.collect_results(installation_root(kind).parent)
    except OSError:
        return []


def apply_update(kind: InstallKind, downloaded: Path) -> None:
    """Installe le fichier téléchargé ; l'appelant doit ensuite quitter l'application."""
    if kind in _SWAP_KINDS:
        _start_swap_helper(kind, downloaded)
        return
    if kind is InstallKind.APPIMAGE:
        target = _appimage_path()
        try:
            downloaded.chmod(0o755)
            os.replace(downloaded, target)
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args, python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            subprocess.Popen(  # nosec B603  # nosemgrep
                [str(target)],  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
                env=host_environment(),  # sans les bibliothèques de l'AppImage courante
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise UpdateInstallError(f"Remplacement de l'AppImage impossible : {exc}") from exc
        return
    if kind is InstallKind.WINDOWS_INSTALLER:
        startfile = getattr(os, "startfile", None)
        if not callable(startfile):
            raise UpdateInstallError("os.startfile indisponible : installeur Windows non lançable.")
        try:
            # ShellExecute : déclenche l'invite UAC requise par l'installeur (admin).
            startfile(str(downloaded))  # pylint: disable=not-callable
        except OSError as exc:
            raise UpdateInstallError(f"Lancement de l'installeur impossible : {exc}") from exc
        return
    raise UpdateInstallError("Mise à jour automatique non supportée pour ce mode d'installation.")
