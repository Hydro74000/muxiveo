"""
core/nvencc_install.py — Installation de NVEncC (rigaya/NVEnc), « extension externe » de Muxiveo.

Étape partagée par le premier lancement de l'AllInc NoVidia (launcher.py), le setup Linux (setup.py) et la page
Extensions. Méthode selon le système (:func:`plan_method`) :

- Windows : archive officielle (``Aviutl_NVEnc_*.zip``), distribution x64 complète copiée dans le dossier
  utilisateur ``%LOCALAPPDATA%\\Muxiveo\\tools\\nvencc`` (sans élévation) ;
- Debian / Ubuntu : paquet ``.deb`` via apt ; Fedora / RHEL : paquet ``.rpm`` via dnf, avec ``sudo`` dans un
  terminal ou ``pkexec`` (fenêtre polkit) depuis l'interface ; refus ou échec → installation utilisateur ;
- distribution immuable (rpm-ostree) ou autre : installation utilisateur sans droits root, binaire extrait du
  ``.deb`` (archive ar + ``data.tar.xz`` lue en Python ; le paquet ne contient qu'un exécutable autonome :
  glibc et ``libcuda`` du pilote NVIDIA) dans ``$XDG_DATA_HOME/muxiveo/tools/nvencc``.

Archives : dernière release ou tag ``MUXIVEO_NVENCC_TAG`` (``core/github_release.py``), SHA-256 publié par GitHub
obligatoire. Aucune dépendance Qt : les messages sont transmis par ``log(niveau, message)``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from core import plugins
from core.github_release import ReleaseAsset, fetch_release, release_asset
from core.subprocess_utils import host_environment, run_probe, subprocess_windows_no_window_kwargs
from core.version import APP_USER_AGENT

METHOD_WINDOWS = "windows"
METHOD_APT = "apt"
METHOD_DNF = "dnf"
METHOD_USER = "user"

#: Provenance d'un NVEncC trouvé : embarqué (AllInc), installé ici dans le dossier utilisateur, ou autre (système,
#: config.ini, PATH).
PROVENANCE_BUNDLED = "bundled"
PROVENANCE_USER = "user"
PROVENANCE_SYSTEM = "system"

WINDOWS_BINARY = "NVEncC64.exe"
LINUX_BINARY = "nvencc"

LogFn = Callable[[str, str], None]
ProgressFn = Callable[[int, int], None]

_VERSION_RE = re.compile(r"NVEnc\w*\s*\(x64\)\s*(\d+(?:\.\d+)+)", re.IGNORECASE)
_DEB_ASSET = ("_amd64.deb",)
_RPM_ASSET = (".x86_64.rpm",)
_WINDOWS_ASSET = ("Aviutl_NVEnc_", ".zip")
_COPY_CHUNK = 1 << 20


class NvenccInstallError(RuntimeError):
    """Installation ou suppression impossible (message destiné à l'utilisateur)."""


class NvenccCancelled(NvenccInstallError):
    """Opération annulée par l'utilisateur."""


@dataclass(frozen=True)
class NvenccTool:
    """NVEncC utilisable : chemin, version lue (``--version``) et provenance."""

    path: Path
    version: str
    provenance: str


# ---------------------------------------------------------------------------
# Machine
# ---------------------------------------------------------------------------

def supported_platform(sys_platform: str | None = None, machine: str | None = None) -> bool:
    """NVEncC n'existe que pour Linux et Windows x86-64."""
    return plugins.platform_tag(sys_platform, machine) in {"linux-x86_64", "windows-x86_64"}


def nvidia_present() -> bool:
    """GPU NVIDIA détecté : ``nvidia-smi -L`` répond, sinon ``/dev/nvidia0`` (Linux)."""
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            # Outil du pilote résolu dans le PATH, argv fixe, sans shell.
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            result = subprocess.run(  # nosec B603
                [smi, "-L"], capture_output=True, check=False, timeout=5,
                **subprocess_windows_no_window_kwargs(),
            )
            if result.returncode == 0 and (result.stdout or b"").strip():
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    return sys.platform.startswith("linux") and Path("/dev/nvidia0").exists()


def is_atomic_distro() -> bool:
    """Distribution immuable (Silverblue, Kinoite, Bazzite…) : pas d'installation de paquet sur le système."""
    if Path("/run/ostree-booted").exists():
        return True
    ostree = shutil.which("rpm-ostree")
    if ostree:
        try:
            # Outil système résolu dans le PATH, argv fixe, sans shell.
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            result = subprocess.run([ostree, "status"], capture_output=True, check=False, timeout=5)  # nosec B603
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    return False


def linux_distro(os_release: Path = Path("/etc/os-release")) -> str:
    """Famille de la distribution : ``debian``, ``fedora`` ou ``unknown``."""
    try:
        text = os_release.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        text = ""
    if any(k in text for k in ("ubuntu", "debian", "linuxmint", "pop!_os", "kali", "raspbian")):
        return "debian"
    if any(k in text for k in ("fedora", "rhel", "centos", "rocky", "almalinux", "nobara")):
        return "fedora"
    if shutil.which("apt-get"):
        return "debian"
    if shutil.which("dnf") or shutil.which("yum"):
        return "fedora"
    return "unknown"


def plan_method() -> str:
    """Méthode d'installation pour cette machine (voir l'en-tête du module)."""
    if sys.platform == "win32":
        return METHOD_WINDOWS
    if is_atomic_distro():
        return METHOD_USER
    distro = linux_distro()
    if distro == "debian" and (shutil.which("apt") or shutil.which("apt-get")):
        return METHOD_APT
    if distro == "fedora" and (shutil.which("dnf") or shutil.which("yum")):
        return METHOD_DNF
    return METHOD_USER


# ---------------------------------------------------------------------------
# Emplacements et état
# ---------------------------------------------------------------------------

def user_root(env: Mapping[str, str] | None = None, sys_platform: str | None = None) -> Path:
    """Dossier de l'installation utilisateur (``…/muxiveo/tools/nvencc``, ``…\\Muxiveo\\tools\\nvencc``)."""
    environ = os.environ if env is None else env
    platform_name = sys.platform if sys_platform is None else sys_platform
    return plugins._local_base(environ, platform_name, False) / "tools" / "nvencc"


def binary_name(sys_platform: str | None = None) -> str:
    return WINDOWS_BINARY if (sys.platform if sys_platform is None else sys_platform) == "win32" else LINUX_BINARY


def user_binary(env: Mapping[str, str] | None = None, sys_platform: str | None = None) -> Path | None:
    """NVEncC installé par Muxiveo dans le dossier utilisateur, ``None`` sinon."""
    candidate = user_root(env, sys_platform) / binary_name(sys_platform)
    return candidate if candidate.is_file() else None


def read_version(binary: str | Path, timeout: float = 15.0) -> str:
    """Version annoncée par ``NVEncC --version`` (``9.40``), ``""`` si illisible."""
    try:
        result = run_probe([str(binary), "--version"], timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = _VERSION_RE.search(f"{result.stdout or ''}\n{result.stderr or ''}")
    return match.group(1) if match else ""


def describe(resolved: str | None, bundled_dir: Path | None = None) -> NvenccTool | None:
    """NVEncC résolu par la configuration (``AppConfig.tool_nvencc``) : chemin réel, version et provenance."""
    if not resolved:
        return None
    found = shutil.which(resolved)
    path = Path(found) if found else Path(resolved)
    if not path.is_file():
        return None
    real = path.resolve()
    provenance = PROVENANCE_SYSTEM
    if bundled_dir is not None and real.is_relative_to(bundled_dir.resolve()):
        provenance = PROVENANCE_BUNDLED
    elif real.is_relative_to(user_root().resolve()):
        provenance = PROVENANCE_USER
    return NvenccTool(path, read_version(path), provenance)


def latest_version(timeout: float = 15.0) -> str:
    """Tag de la dernière release rigaya/NVEnc (ou du tag demandé), ``""`` si l'API est injoignable."""
    try:
        return str(fetch_release("nvencc", user_agent=APP_USER_AGENT, timeout=timeout).get("tag_name") or "")
    except (OSError, ValueError, RuntimeError):
        return ""


def version_key(version: str) -> tuple[int, ...]:
    return plugins.version_key(version)


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

def _asset(patterns: tuple[str, ...]) -> ReleaseAsset:
    try:
        return release_asset("nvencc", *patterns, user_agent=APP_USER_AGENT)
    except (OSError, ValueError, RuntimeError) as exc:
        raise NvenccInstallError(f"release NVEncC introuvable ({exc})") from exc


def _download(asset: ReleaseAsset, dest: Path, progress: ProgressFn | None, cancel: threading.Event | None) -> None:
    """Téléchargement vérifié (SHA-256 GitHub), progression et annulation de ``core/plugins.py``."""
    try:
        plugins._download(asset, dest, progress, cancel)
    except plugins.PluginCancelled as exc:
        raise NvenccCancelled("installation annulée") from exc
    except plugins.PluginError as exc:
        raise NvenccInstallError(str(exc)) from exc


def privilege_prefix(interactive: bool | None = None, env: Mapping[str, str] | None = None) -> list[str] | None:
    """Préfixe d'élévation pour apt/dnf : rien en root, ``sudo`` dans un terminal, ``pkexec`` dans une session
    graphique ; ``None`` si aucun n'est utilisable."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    environ = os.environ if env is None else env
    if interactive is None:
        interactive = bool(sys.stdin is not None and sys.stdin.isatty())
    if interactive and shutil.which("sudo"):
        return ["sudo"]
    if shutil.which("pkexec") and (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")):
        return ["pkexec"]
    return None


def _native_install(
    method: str, archive: Path, privilege: list[str], log: LogFn, cancel: threading.Event | None,
) -> Path | None:
    """apt/dnf sur le paquet téléchargé ; chemin du binaire installé, ``None`` en cas d'échec."""
    if method == METHOD_APT:
        manager = shutil.which("apt") or shutil.which("apt-get")
    else:
        manager = shutil.which("dnf") or shutil.which("yum")
    if not manager:
        return None
    command = [*privilege, manager, "install", "-y", str(archive)]
    log("INFO", "NVEncC : " + " ".join(command))
    try:
        # Paquet système : jamais les bibliothèques d'une AppImage ou d'un bundle figé. Argv construit ici
        # (élévation, apt/dnf résolu par shutil.which, paquet téléchargé et vérifié), sans shell ; ``env`` :
        # environnement de l'hôte, sans valeur fournie par l'utilisateur.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit, python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
        result = subprocess.run(command, check=False, env=host_environment(), capture_output=True,  # nosec B603
                                text=True)
    except OSError as exc:
        log("WARN", f"NVEncC : {Path(manager).name} indisponible ({exc}).")
        return None
    if cancel is not None and cancel.is_set():
        raise NvenccCancelled("installation annulée")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        log("WARN", f"NVEncC : {Path(manager).name} a échoué (code {result.returncode}"
                    + (f" : {detail[-1]}" if detail else "") + ").")
        return None
    for name in ("nvencc", "NVEncC"):
        found = shutil.which(name, path=host_environment().get("PATH"))
        if found:
            return Path(found)
    log("WARN", "NVEncC : paquet installé mais binaire introuvable dans le PATH.")
    return None


class _Slice:
    """Lecture bornée d'un membre d'archive ar (flux pour ``tarfile`` en mode ``r|*``)."""

    def __init__(self, fh: BinaryIO, size: int) -> None:
        self._fh = fh
        self._left = size

    def read(self, size: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        size = self._left if size is None or size < 0 else min(size, self._left)
        data = self._fh.read(size)
        self._left -= len(data)
        return data


def extract_deb_binary(archive: Path, dest: Path, cancel: threading.Event | None = None) -> Path:
    """Extrait ``usr/bin/nvencc`` (ou ``NVEncC``) du ``.deb`` vers ``dest`` (fichier), sans dpkg-deb."""
    import tarfile

    with archive.open("rb") as fh:
        if fh.read(8) != b"!<arch>\n":
            raise NvenccInstallError("paquet .deb illisible (en-tête ar)")
        while True:
            header = fh.read(60)
            if len(header) < 60:
                break
            name = header[:16].decode("ascii", errors="replace").strip().rstrip("/")
            try:
                size = int(header[48:58])
            except ValueError as exc:
                raise NvenccInstallError("paquet .deb illisible (taille de membre)") from exc
            if not name.startswith("data.tar"):
                fh.seek(size + (size & 1), os.SEEK_CUR)
                continue
            try:
                with tarfile.open(fileobj=_Slice(fh, size), mode="r|*") as tar:  # type: ignore[call-overload]
                    for member in tar:
                        normalized = member.name.lstrip("./")
                        if not member.isfile() or normalized.lower() != "usr/bin/nvencc":
                            continue
                        source = tar.extractfile(member)
                        if source is None:
                            break
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with dest.open("wb") as out:
                            while chunk := source.read(_COPY_CHUNK):
                                if cancel is not None and cancel.is_set():
                                    raise NvenccCancelled("installation annulée")
                                out.write(chunk)
                        dest.chmod(0o755)
                        return dest
            except (tarfile.TarError, EOFError, OSError) as exc:
                raise NvenccInstallError(f"paquet .deb illisible ({exc})") from exc
            break
    raise NvenccInstallError("binaire nvencc absent du paquet .deb")


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise NvenccCancelled("installation annulée")


def extract_windows_zip(archive: Path, dest_dir: Path, cancel: threading.Event | None = None) -> Path:
    """Copie la distribution x64 (``NVEncC64.exe`` et ses DLL) de l'archive officielle vers ``dest_dir``.

    ``cancel`` contrôlé avant l'ouverture, à chaque fichier et à chaque bloc copié (:class:`NvenccCancelled`).
    """
    _check_cancel(cancel)
    try:
        with zipfile.ZipFile(archive) as zf:
            names = [n for n in zf.namelist() if Path(n).name.lower() == WINDOWS_BINARY.lower()]
            if not names:
                raise NvenccInstallError(f"{WINDOWS_BINARY} absent de l'archive NVEnc")
            folder = names[0].rsplit("/", 1)[0] + "/" if "/" in names[0] else ""
            dest_dir.mkdir(parents=True, exist_ok=True)
            for info in zf.infolist():
                if info.is_dir() or not info.filename.startswith(folder):
                    continue
                relative = info.filename[len(folder):]
                if not relative or "/" in relative or "\\" in relative or relative in (".", ".."):
                    continue  # fichiers du dossier x64 seulement, jamais de chemin
                _check_cancel(cancel)
                with zf.open(info) as source, (dest_dir / relative).open("wb") as out:
                    while chunk := source.read(_COPY_CHUNK):
                        _check_cancel(cancel)
                        out.write(chunk)
    except (zipfile.BadZipFile, OSError) as exc:
        raise NvenccInstallError(f"archive NVEnc illisible ({exc})") from exc
    return dest_dir / WINDOWS_BINARY


def _replace_user_root(staging: Path) -> None:
    """Remplace l'installation utilisateur par ``staging`` (même dossier parent)."""
    root = user_root()
    previous = root.with_name(root.name + ".previous")
    if previous.exists():
        shutil.rmtree(previous, ignore_errors=True)
    try:
        if root.exists():
            root.rename(previous)
        staging.rename(root)
    except OSError as exc:
        if previous.exists() and not root.exists():
            previous.rename(root)
        raise NvenccInstallError(f"installation impossible dans {root} ({exc}) : NVEncC encore utilisé ?") from exc
    shutil.rmtree(previous, ignore_errors=True)


def _user_install(
    archive: Path | None, progress: ProgressFn | None, cancel: threading.Event | None, workdir: Path,
) -> Path:
    """Installation sans droits dans le dossier utilisateur (Linux : binaire du .deb ; Windows : archive ZIP)."""
    windows = sys.platform == "win32"
    if archive is None:
        archive = workdir / ("nvencc.zip" if windows else "nvencc.deb")
        _download(_asset(_WINDOWS_ASSET if windows else _DEB_ASSET), archive, progress, cancel)
    root = user_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".nvencc-", dir=root.parent))
    try:
        _check_cancel(cancel)
        if windows:
            extract_windows_zip(archive, staging, cancel)
        else:
            extract_deb_binary(archive, staging / LINUX_BINARY, cancel)
        # Dernier point d'annulation : ensuite l'échange commence et l'installation est acquise.
        _check_cancel(cancel)
        _replace_user_root(staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return root / binary_name()


def install(
    method: str | None = None,
    *,
    progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    log: LogFn | None = None,
    interactive: bool | None = None,
    elevate: bool = True,
) -> Path:
    """Installe NVEncC (méthode de :func:`plan_method` par défaut) ; chemin du binaire installé.

    apt/dnf refusé (pas d'élévation possible, mot de passe refusé, dépendances) → installation utilisateur.
    ``elevate=False`` : jamais de sudo ni de pkexec, installation utilisateur directe.
    """
    emit: LogFn = log or (lambda _level, _message: None)
    if not supported_platform():
        raise NvenccInstallError("NVEncC n'existe que pour Linux et Windows x86-64")
    method = method or plan_method()
    try:
        return _install(method, progress, cancel, emit, interactive, elevate)
    except OSError as exc:
        raise NvenccInstallError(f"installation impossible ({exc})") from exc


def _install(
    method: str, progress: ProgressFn | None, cancel: threading.Event | None, emit: LogFn, interactive: bool | None,
    elevate: bool = True,
) -> Path:
    # Téléchargement sur disque, à côté de l'installation (/tmp souvent en RAM ; paquet ≈ 100 Mo).
    base = user_root().parent
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".nvencc-download-", dir=base) as tmp:
        workdir = Path(tmp)
        if method in (METHOD_APT, METHOD_DNF):
            privilege = privilege_prefix(interactive) if elevate else None
            if not elevate:
                emit("INFO", "NVEncC : élévation désactivée : installation dans le dossier utilisateur.")
            elif privilege is None:
                emit("WARN", "NVEncC : ni sudo (terminal) ni pkexec disponibles : installation dans le dossier utilisateur.")
            else:
                deb = method == METHOD_APT
                archive = workdir / ("nvencc.deb" if deb else "nvencc.rpm")
                _download(_asset(_DEB_ASSET if deb else _RPM_ASSET), archive, progress, cancel)
                installed = _native_install(method, archive, privilege, emit, cancel)
                if installed is not None:
                    return installed
                emit("WARN", "NVEncC : installation par le gestionnaire de paquets impossible : "
                             "installation dans le dossier utilisateur.")
                if deb:
                    return _user_install(archive, progress, cancel, workdir)
        return _user_install(None, progress, cancel, workdir)


def remove_user() -> bool:
    """Supprime l'installation utilisateur ; ``False`` si des fichiers sont encore utilisés."""
    root = user_root()
    if not root.exists():
        return True
    try:
        shutil.rmtree(root)
    except OSError:
        return False
    return True


def first_run_step(log: LogFn | None = None) -> Path | None:
    """Étape NVEncC du premier lancement (AllInc NoVidia) : rien sans GPU NVIDIA ni NVEncC déjà présent."""
    emit: LogFn = log or (lambda _level, _message: None)
    if not supported_platform():
        return None
    if not nvidia_present():
        emit("INFO", "NVEncC : aucun GPU NVIDIA détecté, étape ignorée.")
        return None
    existing = user_binary() or next(
        (Path(found) for name in ("nvencc", "NVEncC", WINDOWS_BINARY) if (found := shutil.which(name))), None,
    )
    if existing is not None:
        emit("OK", f"NVEncC déjà présent ({existing}).")
        return existing
    emit("INFO", "GPU NVIDIA détecté : installation de NVEncC…")
    try:
        installed = install(log=emit)
    except NvenccInstallError as exc:
        emit("WARN", f"NVEncC non installé ({exc}) : installation possible depuis la page Extensions.")
        return None
    emit("OK", f"NVEncC installé : {installed}")
    return installed


__all__ = [
    "METHOD_APT", "METHOD_DNF", "METHOD_USER", "METHOD_WINDOWS", "NvenccCancelled", "NvenccInstallError",
    "NvenccTool", "PROVENANCE_BUNDLED", "PROVENANCE_SYSTEM", "PROVENANCE_USER", "describe", "extract_deb_binary",
    "extract_windows_zip", "first_run_step", "install", "is_atomic_distro", "latest_version", "linux_distro",
    "nvidia_present", "plan_method", "privilege_prefix", "read_version", "remove_user", "supported_platform",
    "user_binary", "user_root",
]
