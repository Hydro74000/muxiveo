"""
core/update_install.py — Téléchargement vérifié et installation d'une mise à jour.

Modes supportés :
- AppImage : remplacement atomique du fichier `$APPIMAGE` puis relance ;
- Windows : lancement de l'installeur NSIS (élévation UAC gérée par ShellExecute).

Les autres modes (macOS, Homebrew, sources) renvoient vers la page de release.
Intégrité : SHA-256 contrôlé contre l'asset `SHA256SUMS` publié avec la release ;
sans lui, l'installation est refusée.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess  # nosec B404
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path

from core.update_check import RELEASE_DOWNLOAD_URL_PREFIX, ReleaseAsset, UpdateInfo, is_repository_url
from core.version import APP_USER_AGENT

CHECKSUMS_ASSET_NAME = "SHA256SUMS"
_MAX_ASSET_BYTES = 2 * 1024**3
_MAX_CHECKSUMS_BYTES = 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_WINDOWS_INSTALLER_RE = re.compile(r"^Muxiveo-Setup-.+\.exe$", re.IGNORECASE)
_CHECKSUM_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")

ProgressCallback = Callable[[int, int], None]
CancelCallback = Callable[[], bool]


class InstallKind(Enum):
    """Mode d'installation de l'application en cours."""

    APPIMAGE = "appimage"
    WINDOWS_INSTALLER = "windows-installer"
    UNSUPPORTED = "unsupported"


class UpdateInstallError(Exception):
    """Échec du téléchargement ou de l'installation d'une mise à jour."""


def detect_install_kind(
    env: Mapping[str, str] | None = None,
    sys_platform: str | None = None,
    frozen: bool | None = None,
) -> InstallKind:
    """Détermine si l'application courante peut se mettre à jour elle-même."""
    env = os.environ if env is None else env
    sys_platform = sys.platform if sys_platform is None else sys_platform
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if sys_platform.startswith("linux") and env.get("APPIMAGE"):
        return InstallKind.APPIMAGE
    if sys_platform == "win32" and frozen:
        return InstallKind.WINDOWS_INSTALLER
    return InstallKind.UNSUPPORTED


def manual_update_hint(executable: str | None = None, frozen: bool | None = None) -> str:
    """Commande de mise à jour manuelle pour les modes non auto-installables."""
    exe = (executable or sys.executable).replace("\\", "/")
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if "/Cellar/" in exe or "/homebrew/" in exe.lower():
        return "brew upgrade muxiveo"
    if not frozen:
        return "git pull"
    return ""


def select_asset(info: UpdateInfo, kind: InstallKind, machine: str | None = None) -> ReleaseAsset | None:
    """Choisit l'asset de release adapté au mode d'installation."""
    if kind is InstallKind.APPIMAGE:
        arch = (machine or platform.machine()).lower()
        return next(
            (a for a in info.assets if a.name.endswith(".AppImage") and arch in a.name.lower()),
            None,
        )
    if kind is InstallKind.WINDOWS_INSTALLER:
        return next((a for a in info.assets if _WINDOWS_INSTALLER_RE.match(a.name)), None)
    return None


def can_self_update(info: UpdateInfo, kind: InstallKind | None = None) -> bool:
    """True si la release fournit un asset installable et ses sommes de contrôle."""
    kind = detect_install_kind() if kind is None else kind
    return select_asset(info, kind) is not None and info.asset(CHECKSUMS_ASSET_NAME) is not None


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
    """Télécharge et vérifie l'asset de mise à jour ; renvoie le fichier prêt à installer."""
    asset = select_asset(info, kind)
    if asset is None:
        raise UpdateInstallError("Aucun paquet compatible dans la release.")
    expected = fetch_expected_sha256(info, asset)
    return download_asset(asset, _download_target(kind, asset), expected, progress, cancelled)


def apply_update(kind: InstallKind, downloaded: Path) -> None:
    """Installe le fichier téléchargé ; l'appelant doit ensuite quitter l'application."""
    if kind is InstallKind.APPIMAGE:
        target = _appimage_path()
        try:
            downloaded.chmod(0o755)
            os.replace(downloaded, target)
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args, python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            subprocess.Popen(  # nosec B603  # nosemgrep
                [str(target)],  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
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
