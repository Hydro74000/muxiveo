"""
core/update_apply.py — Mise à jour par échange de dossiers : archive source, application macOS.

Bibliothèque standard seule : importé par l'application (`prepare`, `collect_results`) et exécuté seul, après
la sortie de l'application, copié hors du dossier remplacé (`python -I update_apply.py commit …`) ou par
l'exécutable figé (`Muxiveo --apply-update commit …`).

- prepare (application, avant de quitter) : extraction bornée de l'archive dont le SHA-256 a été vérifié en amont,
  ou copie du bundle depuis l'image disque ; structure et version contrôlées (`--cli version`) ; éléments de
  l'utilisateur à reporter (tout ce que l'archive installée ne livrait pas, d'après son marqueur).
- commit (processus détaché) : attente de la sortie de l'application, report des éléments de l'utilisateur,
  échange des dossiers, contrôle de la version installée, relance. Échec du report ou du contrôle : retour
  à l'ancienne version. Échec du compte rendu après validation : sauvegarde conservée et relance tentée.

L'ancienne version reste dans le dossier de travail (`.muxiveo-update-*`, à côté de l'installation) ; résultat
dans `result.json`, relu au démarrage suivant (`collect_results`). Aucune commande shell.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import plistlib
import re
import shutil
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from collections.abc import Callable

MARKER_NAME = ".muxiveo-installation"
WORKDIR_PREFIX = ".muxiveo-update-"
#: Marqueur des dossiers de travail des versions antérieures (propriétaire lu dans `prepared.json`).
WORKDIR_TAG = ".muxiveo-update"
#: Marqueur des transactions courantes : JSON `{"install": <racine résolue>}`, invisible aux collecteurs des
#: versions antérieures (qui ne peuvent ni le signaler ni le supprimer).
WORKDIR_TAG_V2 = ".muxiveo-update-v2"
#: Journal des comptes rendus non signalés, par installation : `<parent>/.muxiveo-update.reports/<empreinte>.json`
#: (hors du préfixe des dossiers de travail : jamais pris pour une transaction).
REPORTS_DIR = ".muxiveo-update.reports"
#: Attente maximale du verrou du journal (secondes).
JOURNAL_LOCK_TIMEOUT_S = 10.0
KIND_SOURCE = "source"
KIND_MACOS = "macos-app"
MACOS_APP_NAME = "Muxiveo.app"
# Livrés comme modèles mais propres à l'utilisateur : config.ini (Windows, mode sources : configuration écrite par le
# setup et le panneau Paramètres à la racine du projet) — toujours conservés, jamais remplacés par la nouvelle version.
PRESERVED_FILES = frozenset({"config.ini"})
ROOT_TOKEN = "{root}"  # nosec B105  # jeton de gabarit, pas un secret

MAX_BYTES = 2 * 1024**3
MAX_ENTRIES = 50_000
MAX_METADATA_BYTES = 1024**2
WAIT_EXIT_S = 120.0
PROBE_TIMEOUT_S = 120.0
READY_FILE = "ready"
TRANSACTION_FILE = "transaction.json"
RESULT_FILE = "result.json"
PREPARED_FILE = "prepared.json"
REPORTED_FILE = "reported"
CLEANUP_PREFIX = "muxiveo-update-cleanup-"
CLEANUP_FILE = "cleanup.json"
_ANNOUNCED_VERSION_RE = re.compile(r"^Muxiveo (\S+)$")
_RESERVED_NAME_RE = re.compile(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?")
_WINDOWS_SHARING_ERRORS = {5, 32, 33}   # accès refusé, fichier ou verrou utilisé par un autre processus


class UpdateApplyError(Exception):
    """Mise à jour impossible ou interrompue (message destiné à l'utilisateur)."""


class SwapStateError(UpdateApplyError):
    """Échange de dossiers interrompu sans retour possible à l'état initial : installation à vérifier."""


# --------------------------------------------------------------------------- extraction


def safe_relative(name: str) -> PurePosixPath:
    """Chemin d'archive sans échappement du dossier cible ni nom ambigu (Windows compris)."""
    path = PurePosixPath(name)
    if not name or name.startswith("/") or "\\" in name or ":" in name or not path.parts or ".." in path.parts:
        raise UpdateApplyError(f"Chemin d'archive refusé : {name!r}")
    for part in path.parts:
        if part.endswith((" ", ".")) or any(ord(c) < 32 for c in part) or _RESERVED_NAME_RE.fullmatch(part):
            raise UpdateApplyError(f"Nom ambigu dans l'archive : {name!r}")
    return path


class _BoundedTarInfo(tarfile.TarInfo):
    """En-têtes étendus (PAX, noms longs GNU) bornés avant leur lecture complète ; fichiers creux refusés."""

    def _proc_member(self, tarfile_obj):
        if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                         tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK) and self.size > 65536:
            raise UpdateApplyError("Métadonnées d'archive trop volumineuses.")
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise UpdateApplyError("Fichiers creux non pris en charge.")
        return super()._proc_member(tarfile_obj)


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise UpdateApplyError("Mise à jour annulée.")


def extract_tar(archive: Path, destination: Path, cancelled: Callable[[], bool] | None = None) -> None:
    """Extrait une archive .tar.gz : fichiers et dossiers seulement, taille et nombre d'entrées bornés."""
    destination.mkdir()
    names: set[str] = set()
    total = 0
    with tarfile.open(archive, "r|gz", tarinfo=_BoundedTarInfo) as tar:
        for member in tar:
            _check_cancelled(cancelled)
            relative = safe_relative(member.name)
            key = relative.as_posix().casefold()
            if key in names:
                raise UpdateApplyError(f"Entrée en double dans l'archive : {member.name!r}")
            names.add(key)
            total += max(member.size, 0)
            if member.size < 0 or total > MAX_BYTES or len(names) > MAX_ENTRIES:
                raise UpdateApplyError("Archive trop volumineuse.")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise UpdateApplyError(f"Entrée spéciale refusée (lien, périphérique) : {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise UpdateApplyError(f"Entrée illisible : {member.name!r}")
            remaining = member.size
            with target.open("xb") as out:
                while remaining:
                    _check_cancelled(cancelled)
                    data = source.read(min(1024 * 1024, remaining))
                    if not data:
                        raise UpdateApplyError(f"Entrée tronquée : {member.name!r}")
                    out.write(data)
                    remaining -= len(data)
            target.chmod((member.mode & 0o755) | 0o600)


def extract_app_from_dmg(dmg: Path, destination: Path, app_name: str = MACOS_APP_NAME,
                         cancelled: Callable[[], bool] | None = None) -> Path:
    """Copie le bundle de l'image disque (montée en lecture seule, sans fenêtre) dans `destination`."""
    destination.mkdir()
    mount = destination.parent / "dmg-mount"
    mount.mkdir()
    _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-noverify", "-mountpoint", str(mount),
          str(dmg)], "montage de l'image disque")
    try:
        source = mount / app_name
        if source.is_symlink() or not source.is_dir():
            raise UpdateApplyError(f"{app_name} absent de l'image disque.")
        _check_cancelled(cancelled)
        _run(["ditto", str(source), str(destination / app_name)], "copie de l'application", cancelled=cancelled)
    finally:
        subprocess.run(["hdiutil", "detach", str(mount), "-force"], check=False,  # nosec B603 B607
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    return destination / app_name


def _no_window_kwargs() -> dict:
    """Windows : aucune console ouverte par les contrôles (l'assistant détaché n'en a pas).
    Équivalent de `core/subprocess_utils.py`, que ce module autonome ne peut pas importer."""
    return {"creationflags": 0x08000000} if os.name == "nt" else {}      # CREATE_NO_WINDOW


def _run_process(command: list[str], *, cancelled: Callable[[], bool] | None = None,
                 timeout: float, **kwargs) -> subprocess.CompletedProcess:
    """Attend le sous-processus en surveillant l'annulation, puis le récolte toujours."""
    _check_cancelled(cancelled)
    if cancelled is None:
        # Commandes internes de contrôle/copie, arguments séparés, aucun shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        return subprocess.run(command, timeout=timeout, check=False, **kwargs)  # nosec B603
    capture = kwargs.pop("capture_output", False)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
    with subprocess.Popen(command, **kwargs) as process:  # nosec B603
        deadline = time.monotonic() + timeout
        try:
            while True:
                _check_cancelled(cancelled)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # Exception construite, aucune exécution.
                    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                    _check_cancelled(cancelled)
                    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()


def _run(command: list[str], label: str, timeout: float = 600.0,
         cancelled: Callable[[], bool] | None = None) -> None:
    try:
        # Argv construit par ce module (outils système), sans shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        result = _run_process(command, capture_output=True, text=True,
                              timeout=timeout, cancelled=cancelled, **_no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateApplyError(f"Échec de {label} : {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [f"code {result.returncode}"]
        raise UpdateApplyError(f"Échec de {label} : {detail[0]}")


# --------------------------------------------------------------------------- contrôles


def read_marker(root: Path) -> dict | None:
    """Marqueur d'une installation issue d'une archive source (liste des fichiers livrés), None s'il manque."""
    path = root / MARKER_NAME
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_METADATA_BYTES * 8 + 1)
    except OSError:
        return None
    if len(raw) > MAX_METADATA_BYTES * 8 or path.is_symlink():
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        return None
    files = doc.get("files")
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        return None
    return doc


def carry_over_plan(install: Path, shipped: set[str], cancelled: Callable[[], bool] | None = None) -> list[str]:
    """Éléments de l'utilisateur : tout ce que l'archive installée ne livrait pas (environnement virtuel…) et
    `PRESERVED_FILES`. Dossier inconnu reporté en bloc ; caches Python ignorés (recompilés).
    """
    shipped_dirs = {parent.as_posix() for name in shipped for parent in PurePosixPath(name).parents}
    plan: list[str] = []

    def walk(relative: PurePosixPath) -> None:
        with os.scandir(install.joinpath(*relative.parts)) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                _check_cancelled(cancelled)
                rel = relative / entry.name
                key = rel.as_posix()
                if entry.name == "__pycache__" or key == MARKER_NAME:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if key in shipped_dirs:
                        walk(rel)
                    else:
                        plan.append(key)
                elif key in PRESERVED_FILES or (key not in shipped and not entry.name.endswith(".pyc")):
                    plan.append(key)

    walk(PurePosixPath())
    return plan


def expand(template: list[str], root: Path) -> list[str]:
    """Commande enregistrée → commande réelle (`{root}` = dossier contrôlé)."""
    return [arg.replace(ROOT_TOKEN, str(root)) for arg in template]


def probe(template: list[str], root: Path, version: str, cancelled: Callable[[], bool] | None = None) -> None:
    """`--cli version` de la version à installer : elle démarre et annonce la version attendue."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    try:
        # Commande enregistrée par l'application, dossier contrôlé, sans shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        result = _run_process(expand(template, root) + ["--cli", "version"], cwd=root,
                                capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, env=env, cancelled=cancelled,
                                **_no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateApplyError(f"La nouvelle version ne démarre pas : {exc}") from exc
    announced = [m.group(1) for line in result.stdout.splitlines() if (m := _ANNOUNCED_VERSION_RE.match(line.strip()))]
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [f"code {result.returncode}"]
        raise UpdateApplyError(f"La nouvelle version ne démarre pas correctement : {detail[0]}")
    if version not in announced:     # égalité exacte : 12.0.0 n'est pas 2.0.0
        raise UpdateApplyError(
            f"La nouvelle version ne démarre pas correctement : version annoncée {announced[0] if announced else '?'}"
            f" au lieu de {version}."
        )


def _source_candidate(unpacked: Path) -> Path:
    roots = [entry for entry in unpacked.iterdir()]
    if len(roots) != 1 or not roots[0].is_dir() or roots[0].is_symlink():
        raise UpdateApplyError("Archive source inattendue : un seul dossier racine attendu.")
    root = roots[0]
    for required in ("main.py", "launcher.py", "core/version.py"):
        if not (root / required).is_file():
            raise UpdateApplyError(f"Archive source incomplète : {required} absent.")
    if read_marker(root) is None:
        raise UpdateApplyError("Archive source sans marqueur d'installation.")
    return root


def _check_app_bundle(bundle: Path, bundle_id: str) -> None:
    """Identifiant attendu et exécutable déclaré (`CFBundleExecutable`, casse exacte : volumes sensibles à la casse)."""
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as source:
            info = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise UpdateApplyError(f"Application inattendue : Info.plist illisible ({exc}).") from exc
    identifier = info.get("CFBundleIdentifier")
    if identifier != bundle_id:
        raise UpdateApplyError(f"Application inattendue : identifiant {identifier!r}.")
    name = info.get("CFBundleExecutable")
    macos = bundle / "Contents" / "MacOS"
    try:
        listed = isinstance(name, str) and name in os.listdir(macos)
    except OSError:
        listed = False
    executable = macos / name if listed else None
    if executable is None or executable.is_symlink() or not executable.is_file():
        raise UpdateApplyError("Application inattendue : exécutable absent.")


# --------------------------------------------------------------------------- préparation


def prepare(
    kind: str,
    archive: Path,
    work: Path,
    install: Path,
    version: str,
    probe_command: list[str],
    relaunch_command: list[str],
    bundle_id: str = "",
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Prépare l'échange : candidat contrôlé dans `work`, plan de report ; écrit `prepared.json`."""
    _check_cancelled(cancelled)
    install = install.resolve()
    if install.parent != work.resolve().parent:
        raise UpdateApplyError("Dossier de travail hors du dossier parent de l'installation.")
    carry: list[str] = []
    requirements_changed = False
    if kind == KIND_SOURCE:
        extract_tar(archive, work / "unpacked", cancelled=cancelled)
        candidate = _source_candidate(work / "unpacked")
        old = read_marker(install)
        if old is None:
            raise UpdateApplyError("Installation actuelle sans marqueur : mise à jour intégrée impossible.")
        carry = carry_over_plan(install, set(old["files"]), cancelled=cancelled)
        old_requirements = install / "requirements.txt"
        new_requirements = candidate / "requirements.txt"
        requirements_changed = (
            old_requirements.is_file() and new_requirements.is_file()
            and old_requirements.read_bytes() != new_requirements.read_bytes()
        )
    elif kind == KIND_MACOS:
        candidate = extract_app_from_dmg(archive, work / "unpacked", cancelled=cancelled)
        _check_app_bundle(candidate, bundle_id)
    else:
        raise UpdateApplyError(f"Mode de mise à jour inconnu : {kind}")
    _check_cancelled(cancelled)
    probe(probe_command, candidate, version, cancelled=cancelled)
    _check_cancelled(cancelled)
    archive.unlink(missing_ok=True)
    doc = {
        "schema": 1, "kind": kind, "install": str(install), "candidate": str(candidate), "version": version,
        "carry": carry, "probe": probe_command, "relaunch": relaunch_command,
        "requirements_changed": requirements_changed,
    }
    (work / PREPARED_FILE).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


# --------------------------------------------------------------------------- échange


def _atomic_exchange(left: Path, right: Path) -> bool:
    """Échange atomique de deux chemins (Linux renameat2, macOS renamex_np) ; False si indisponible."""
    if sys.platform.startswith("linux"):
        name, args = "renameat2", (-100, os.fsencode(left), -100, os.fsencode(right), 2)   # RENAME_EXCHANGE
    elif sys.platform == "darwin":
        name, args = "renamex_np", (os.fsencode(left), os.fsencode(right), 2)               # RENAME_SWAP
    else:
        return False
    try:
        function = getattr(ctypes.CDLL(None, use_errno=True), name)
    except (OSError, AttributeError):
        return False
    if function(*args) == 0:
        return True
    errno = ctypes.get_errno()
    if errno in (22, 38, 45, 78, 95):   # EINVAL, ENOSYS, ENOTSUP (macOS), ENOSYS (macOS), EOPNOTSUPP
        return False
    raise OSError(errno, os.strerror(errno), str(left))


def exchange(left: Path, right: Path, spare: Path) -> None:
    """Échange le contenu de deux dossiers ; à défaut d'échange atomique, trois renommages.

    Échec : état initial rétabli puis exception d'origine ; `SwapStateError` si ce retour échoue lui aussi.
    """
    if _atomic_exchange(left, right):
        return
    os.rename(left, spare)
    try:
        os.rename(right, left)
    except BaseException as exc:
        _undo_exchange(exc, [(spare, left)])
        raise
    try:
        os.rename(spare, right)
    except BaseException as exc:
        _undo_exchange(exc, [(left, right), (spare, left)])
        raise


def _undo_exchange(cause: BaseException, renames: list[tuple[Path, Path]]) -> None:
    """Annule un échange inachevé ; `SwapStateError` (avec l'emplacement de chaque dossier) si c'est impossible."""
    for source, target in renames:
        try:
            os.rename(source, target)
        except OSError as exc:
            raise SwapStateError(
                f"Échange de dossiers interrompu ({cause}) puis retour impossible ({exc}) : {source} n'a pas pu "
                f"reprendre la place de {target}."
            ) from exc


def _describe_os_error(exc: OSError, install: Path) -> str:
    if getattr(exc, "winerror", None) in _WINDOWS_SHARING_ERRORS:
        return (f"Dossier {install} utilisé par un autre programme (terminal, explorateur…) : fermez-le puis "
                "relancez la mise à jour.")
    return str(exc)


def _wait_for_exit(pid: int, work: Path, timeout: float = WAIT_EXIT_S) -> bool:
    """Signale `ready` puis attend la fin de l'application (handle pris avant, Windows : pas de réemploi du PID)."""
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x00100000, False, pid)    # SYNCHRONIZE
        error = ctypes.get_last_error() if not handle else 0  # type: ignore[attr-defined]
        (work / READY_FILE).touch()
        if not handle:
            # ERROR_INVALID_PARAMETER : plus de processus de ce PID, application terminée. Tout autre
            # refus (accès…) ne prouve pas la fermeture : mise à jour abandonnée, rien n'est modifié.
            return error == 87
        try:
            return kernel.WaitForSingleObject(ctypes.c_void_p(handle), int(timeout * 1000)) == 0
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    (work / READY_FILE).touch()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.1)
    return False


def spawn_detached(command: list[str], cwd: Path) -> subprocess.Popen:
    """Processus détaché de celui-ci : sans flux, nouvelle session (POSIX) ou nouveau groupe sans
    console, hors de l'objet job éventuel du terminal qui le tuerait avec son parent (Windows)."""
    kwargs: dict = {"cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        detached = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        # Argv construit par l'application (interpréteur ou exécutable installé), sans shell.
        try:
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            return subprocess.Popen(command, creationflags=detached | 0x01000000, **kwargs)  # nosec B603
        except OSError:                             # CREATE_BREAKAWAY_FROM_JOB refusé par l'objet job
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            return subprocess.Popen(command, creationflags=detached, **kwargs)  # nosec B603
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
    return subprocess.Popen(command, start_new_session=True, **kwargs)  # nosec B603


def launch(template: list[str], root: Path) -> None:
    """Lance une version installée, détachée de ce processus (commande vide : aucune relance, CLI)."""
    if not template:
        return
    spawn_detached(expand(template, root), root)


def _write_journal(path: Path, doc: dict) -> None:
    """Publie un document complet ; une erreur laisse l'ancien journal intact."""
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(doc, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_result(work: Path, **fields: object) -> None:
    _write_journal(work / RESULT_FILE, {"schema": 1, "finished": time.time(), **fields})


def commit(work: Path, pid: int, wait_timeout: float = WAIT_EXIT_S) -> int:
    """Après la sortie de l'application : report, échange, contrôle, puis compte rendu et relance ;
    retour arrière jusqu'au contrôle réussi, puis conservation de la sauvegarde si la finalisation échoue."""
    work = work.resolve()
    doc = json.loads((work / PREPARED_FILE).read_text(encoding="utf-8"))
    install, candidate = Path(doc["install"]), Path(doc["candidate"])
    if not candidate.resolve().is_relative_to(work) or install.parent != work.parent:
        _write_result(work, ok=False, version=doc.get("version", ""), error="Chemins de mise à jour incohérents.")
        return 1
    if not _wait_for_exit(pid, work, wait_timeout):
        _write_result(work, ok=False, version=doc["version"],
                      cleanup_safe=True,
                      error="L'application ne s'est pas fermée : mise à jour abandonnée, rien n'a été modifié.")
        return 1
    moved: list[str] = []
    kept: list[str] = []
    swapped = False
    spare = work / "swap"
    _write_journal(work / TRANSACTION_FILE, {"phase": "moving"})
    try:
        for rel in doc["carry"]:
            source = install.joinpath(*safe_relative(rel).parts)
            target = candidate.joinpath(*safe_relative(rel).parts)
            if rel in PRESERVED_FILES:
                os.replace(source, target)           # remplace le modèle livré par la nouvelle version
            elif os.path.lexists(target):
                kept.append(rel)          # livré aussi par la nouvelle version : ancien conservé dans la sauvegarde
                continue
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(source, target)
            moved.append(rel)
        exchange(install, candidate, spare)
        swapped = True
        _write_journal(work / TRANSACTION_FILE, {"phase": "swapped"})
        probe(doc["probe"], install, doc["version"])
    except BaseException as exc:  # noqa: BLE001 — tout échec ramène l'ancienne version
        error = _describe_os_error(exc, install) if isinstance(exc, OSError) else str(exc)
        # Échange interrompu sans retour possible : ne plus rien déplacer, tout conserver pour l'utilisateur.
        incomplete = isinstance(exc, SwapStateError)
        if incomplete:
            error += f" — fichiers conservés dans {work}"
        else:
            try:
                if swapped:
                    exchange(install, candidate, spare)
                for rel in reversed(moved):
                    os.rename(candidate.joinpath(*safe_relative(rel).parts),
                              install.joinpath(*safe_relative(rel).parts))
            except (OSError, SwapStateError) as rollback_error:
                incomplete = True
                error += f" — retour arrière incomplet ({rollback_error}) : fichiers conservés dans {work}"
        _write_result(work, ok=False, version=doc["version"], error=error, rollback_incomplete=incomplete,
                      cleanup_safe=not incomplete)
        try:
            launch(doc["relaunch"], install)
        except OSError:
            pass
        return 1
    # Ancienne version sous le nom de l'installation (le candidat porte encore celui de la nouvelle) :
    # un bundle macOS n'est reconnu, donc relançable, que par son suffixe `.app`.
    backup = work / "previous" / install.name
    try:
        backup.parent.mkdir()
        os.rename(candidate, backup)
    except OSError:
        backup = candidate
    result = {"ok": True, "version": doc["version"], "backup": str(backup), "kept": kept,
              "requirements_changed": bool(doc.get("requirements_changed"))}
    # Compte rendu écrit avant la relance : la nouvelle version le lit dès son démarrage.
    journal_error = None
    try:
        _write_result(work, **result)
    except OSError as exc:
        # L'installation a réussi : conserver la transaction et tenter la relance malgré le journal.
        journal_error = str(exc)
        result.update(preserve_backup=True, error=f"Compte rendu incomplet : {exc}")
        try:
            _write_result(work, **result)
        except OSError:
            pass
    try:
        launch(doc["relaunch"], install)
    except OSError as exc:
        try:
            _write_result(work, **result, relaunch_error=str(exc))
        except OSError:
            pass
    return 1 if journal_error else 0


# --------------------------------------------------------------------------- suivi


def install_key(install: Path | str) -> str:
    """Empreinte de la racine résolue d'une installation (nom du journal de ses comptes rendus)."""
    import hashlib

    return hashlib.sha256(os.path.realpath(str(install)).encode("utf-8")).hexdigest()[:32]


def write_workdir_tag(work: Path, install: Path) -> None:
    """Marque `work` comme transaction de l'installation `install` (format courant)."""
    (work / WORKDIR_TAG_V2).write_text(json.dumps({"install": os.path.realpath(str(install))}), encoding="utf-8")


def _tag(work: Path) -> str | None:
    for name in (WORKDIR_TAG_V2, WORKDIR_TAG):
        path = work / name
        if path.is_file() and not path.is_symlink():
            return name
    return None


def workdir_owner(work: Path) -> str | None:
    """Racine résolue de l'installation d'une transaction ; `None` : inconnue (dossier ancien sans
    `prepared.json`, marqueur illisible)."""
    tag = _tag(work)
    source = work / (WORKDIR_TAG_V2 if tag == WORKDIR_TAG_V2 else PREPARED_FILE)
    try:
        if source.is_symlink() or source.stat().st_size > MAX_METADATA_BYTES:
            return None
        doc = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    install = doc.get("install") if isinstance(doc, dict) else None
    return os.path.realpath(install) if isinstance(install, str) and install else None


def _workdirs(parent: Path) -> list[Path]:
    try:
        entries = list(parent.iterdir())
    except OSError:
        return []
    return sorted(
        (p for p in entries if p.name.startswith(WORKDIR_PREFIX) and p.is_dir() and not p.is_symlink()
         and _tag(p) is not None),
        key=lambda p: p.stat().st_mtime,
    )


def _remove_workdir(work: Path) -> None:
    """Garde le marqueur (de son format) jusqu'à la dernière suppression pour reprendre un ménage interrompu."""
    tag = _tag(work)
    try:
        for child in work.iterdir():
            if child.name == tag:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        if tag is not None:
            (work / tag).unlink(missing_ok=True)
        work.rmdir()
    except OSError:
        pass


class JournalLock:
    """Verrou OS non bloquant du journal d'une installation (`fcntl.flock` / `msvcrt.locking`), attente bornée :
    toute lecture-modification-écriture du journal est sérialisée entre processus (application, auxiliaire
    détaché lancé en `python -I`, ménage). Bibliothèque standard seule."""

    def __init__(self, path: Path, timeout: float = JOURNAL_LOCK_TIMEOUT_S) -> None:
        self._path = path
        self._timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> "JournalLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    os.close(fd)
                    raise
                time.sleep(0.02)
        self._fd = fd
        return self

    def __exit__(self, *_exc: object) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _journal_paths(parent: Path, install: str) -> tuple[Path, Path]:
    key = install_key(install)
    folder = parent / REPORTS_DIR
    return folder / f"{key}.json", folder / f"{key}.lock"


#: Taille maximale lue d'un journal de comptes rendus (plusieurs dizaines de milliers d'entrées).
MAX_REPORTS_BYTES = 64 * 1024**2


def _read_reports(path: Path, *, strict: bool = False) -> list[dict]:
    """Entrées du journal ; absent : liste vide. ``strict`` (avant toute modification) : journal illisible ou
    trop volumineux → ``OSError``, jamais remplacé par une liste vide (comptes rendus en attente perdus)."""
    try:
        if path.is_symlink():
            raise OSError(f"journal des mises à jour invalide : {path}")
        if path.stat().st_size > MAX_REPORTS_BYTES:
            raise OSError(f"journal des mises à jour trop volumineux : {path}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("results"), list):
            raise ValueError("journal sans liste de résultats")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        if strict:
            raise OSError(f"journal des mises à jour illisible ({exc}) : conservé tel quel") from exc
        return []
    return [entry for entry in doc["results"] if isinstance(entry, dict) and isinstance(entry.get("id"), str)]


def _update_reports(parent: Path, install: str, change: Callable[[list[dict]], list[dict]]) -> list[dict]:
    """Lecture-modification-écriture du journal sous verrou ; renvoie les entrées après modification."""
    path, lock = _journal_paths(parent, install)
    with JournalLock(lock):
        entries = _read_reports(path, strict=True)
        updated = change(list(entries))
        if updated != entries:
            document = {"schema": 1, "install": install, "results": updated}
            # Au-delà de la taille lue, le lecteur masquerait tout le journal : ajout refusé (dossier gardé).
            if len(json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8")) > MAX_REPORTS_BYTES:
                raise OSError(f"journal des mises à jour plein ({path}) : compte rendu laissé dans son dossier")
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_journal(path, document)
        return updated


def pending_reports(parent: Path, install: Path | str) -> list[dict]:
    """Comptes rendus non signalés de l'installation conservés par le journal (dossiers déjà supprimés)."""
    path, _lock = _journal_paths(parent, os.path.realpath(str(install)))
    return _read_reports(path)


def _keep_report(parent: Path, owner: str, entry: dict, work: Path | None = None) -> bool:
    """Recopie dans le journal le compte rendu d'un dossier supprimé avant d'avoir été signalé ; ``work`` : dossier
    dont le témoin ``reported`` est relu **sous le verrou** (un signalement concurrent l'a peut-être posé).
    Faux si le journal n'a pas pu être écrit : le dossier, seule copie du compte rendu, doit alors être gardé."""
    def change(entries: list[dict]) -> list[dict]:
        # Sous le verrou : signalé entre-temps, ou dossier déjà retiré par un autre ménage (qui a recopié son compte
        # rendu s'il n'était pas signalé) : rien à garder, jamais de compte rendu ressuscité.
        if work is not None and (not work.is_dir() or (work / REPORTED_FILE).exists()):
            return entries
        return entries if any(item["id"] == entry["id"] for item in entries) else [*entries, entry]

    try:
        _update_reports(parent, owner, change)
    except OSError:
        return False
    return True


def mark_reported(parent: Path, install: Path | str, ids: list[str]) -> None:
    """Comptes rendus signalés (écriture confirmée par l'appelant) : dossiers marqués, entrées du journal
    retirées."""
    wanted = set(ids)
    if not wanted:
        return
    owner = os.path.realpath(str(install))

    def change(entries: list[dict]) -> list[dict]:
        # Sous le verrou du journal : témoins posés et entrées retirées ensemble (un ménage concurrent relit le
        # témoin sous ce même verrou avant de recopier).
        for work in _workdirs(parent):
            if work.name in wanted:
                try:
                    (work / REPORTED_FILE).touch()
                except OSError:
                    pass
        return [item for item in entries if item["id"] not in wanted]

    _update_reports(parent, owner, change)


def collect_results(parent: Path, now: float | None = None, *, discard: list[Path] | None = None,
                    report: bool = True, install: Path | str | None = None) -> list[dict]:
    """Comptes rendus non signalés des mises à jour de l'installation `install` ; ménage de ses dossiers de travail.

    Seuls les dossiers de l'installation sont signalés et soumis à la rétention (dernière réussite **par
    installation**) ; propriétaire inconnu : ni signalé ni supprimé, sauf téléchargement abandonné depuis plus
    d'une heure. `install=None` (ménage) : chaque propriétaire connu a sa propre rétention.
    Conservés : dernière sauvegarde réussie, fichiers personnels en collision et états ambigus ou en cours.
    Supprimés : réussites remplacées, abandons/retours arrière confirmés, téléchargements abandonnés depuis
    plus d'une heure. `discard` : dossiers à supprimer ajoutés à cette liste au lieu d'être supprimés
    (sauvegardes volumineuses : suppression laissée à l'appelant). Un dossier supprimable dont le compte rendu
    n'a pas été signalé le recopie d'abord dans le journal de son installation.

    Les comptes rendus renvoyés (`id` : nom du dossier) ne sont marqués signalés que par :func:`mark_reported`,
    appelé une fois leur écriture confirmée.
    """
    now = time.time() if now is None else now
    wanted = os.path.realpath(str(install)) if install is not None else None

    def remove(work: Path, owner: str | None, entry: dict | None) -> None:
        if entry is not None and owner is not None and not (work / REPORTED_FILE).exists():
            if not _keep_report(parent, owner, entry, work):
                return  # journal inaccessible : le dossier reste la seule copie du compte rendu
        if discard is not None:
            discard.append(work)
        else:
            _remove_workdir(work)

    results: list[dict] = []
    finished: dict[str, list[tuple[Path, dict, dict]]] = {}
    for work in _workdirs(parent):
        owner = workdir_owner(work)
        result_path = work / RESULT_FILE
        age = now - work.stat().st_mtime
        mine = owner is not None and (wanted is None or owner == wanted)
        if owner is not None and not mine:
            continue  # autre installation : ses comptes rendus et sa rétention ne regardent qu'elle
        if result_path.is_file():
            if owner is None:
                continue  # propriétaire inconnu : ni signalé ni supprimé
            try:
                doc = json.loads(result_path.read_text(encoding="utf-8"))
                if not isinstance(doc, dict) or type(doc.get("ok")) is not bool:
                    raise ValueError("Résultat invalide")
            except (OSError, ValueError):
                doc = {"ok": False, "error": "Résultat de mise à jour illisible."}
            entry = {**doc, "workdir": str(work), "id": work.name}
            if report and not (work / REPORTED_FILE).exists():
                results.append(entry)
            finished.setdefault(owner, []).append((work, doc, entry))
        elif (work / READY_FILE).exists() or (work / TRANSACTION_FILE).exists():
            if owner is not None and report and age > 86400 and not (work / REPORTED_FILE).exists():
                results.append({"ok": False, "workdir": str(work), "id": work.name,
                                "error": f"Mise à jour interrompue ; fichiers conservés dans {work}."})
        elif age > 3600:
            remove(work, None, None)  # téléchargement abandonné (propriétaire inconnu compris)
    for owner, items in finished.items():
        # Ordre des mises à jour : heure de fin consignée dans le résultat (jamais la date du dossier, modifiée
        # par le témoin ``reported``), sinon date du dossier.
        items.sort(key=lambda item: _finished_at(item[0], item[1]))
        latest_ok = next((work for work, doc, _entry in reversed(items) if doc.get("ok")), None)
        for work, doc, entry in items:
            # État ambigu ou fichiers personnels en collision : seule copie possible, jamais supprimée.
            if (work != latest_ok and not any(doc.get(key) for key in
                    ("rollback_incomplete", "preserve_backup", "kept", "relaunch_error"))
                    and (doc.get("ok") is True or doc.get("cleanup_safe") is True)):
                remove(work, owner, entry)
    if report and wanted is not None:
        seen = {entry["id"] for entry in results}
        results += [entry for entry in pending_reports(parent, wanted) if entry["id"] not in seen]
    return results


def _finished_at(work: Path, doc: dict) -> float:
    finished = doc.get("finished")
    if isinstance(finished, (int, float)) and not isinstance(finished, bool):
        return float(finished)
    try:
        return work.stat().st_mtime
    except OSError:
        return 0.0


def cleanup_stale(paths: list[Path], install: Path | str | None = None) -> None:
    """Revérifie les dossiers confiés à l'auxiliaire : dernière sauvegarde (par installation) et mises à jour
    actives préservées, comptes rendus non signalés recopiés dans le journal avant suppression."""
    requested = {path.absolute() for path in paths}
    for parent in {path.parent for path in requested}:
        eligible: list[Path] = []
        collect_results(parent, discard=eligible, report=False, install=install)
        for work in eligible:
            if work.absolute() in requested:
                _remove_workdir(work)


# --------------------------------------------------------------------------- script


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="update_apply")
    parser.add_argument("phase", choices=["commit", "cleanup"])
    parser.add_argument("work", type=Path)
    parser.add_argument("--pid", type=int)
    args = parser.parse_args(argv)
    if args.phase == "cleanup":
        work = args.work.absolute()
        if work.is_symlink() or not work.name.startswith(CLEANUP_PREFIX):
            raise UpdateApplyError("Dossier de ménage invalide.")
        manifest = work / CLEANUP_FILE
        if manifest.is_symlink() or manifest.stat().st_size > MAX_METADATA_BYTES:
            raise UpdateApplyError("Manifeste de ménage invalide.")
        document = json.loads(manifest.read_text(encoding="utf-8"))
        # Manifeste courant : {"install", "paths"} ; ancien format : liste des dossiers seule.
        paths = document.get("paths") if isinstance(document, dict) else document
        install = document.get("install") if isinstance(document, dict) else None
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths) \
                or not isinstance(install, (str, type(None))):
            raise UpdateApplyError("Manifeste de ménage invalide.")
        # Le dossier courant n'est jamais l'un des dossiers supprimés (Windows).
        os.chdir(work.parent)
        try:
            cleanup_stale([Path(path) for path in paths], install=install)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return 0
    if args.pid is None:
        parser.error("--pid est requis pour commit")
    work = args.work.resolve()
    os.chdir(work)        # Windows : un processus bloque son dossier courant, jamais celui de l'installation
    try:
        return commit(work, args.pid)
    except Exception as exc:  # noqa: BLE001 — trace pour le démarrage suivant
        _write_result(work, ok=False, version="", error=str(exc), preserve_backup=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
