"""
core/output_commit.py — Candidat unique, réservation de destination et publication.

- :func:`reserve_candidate` crée un candidat neuf (``O_EXCL``) à côté de la
  destination : même volume (remplacement atomique), jamais partagé entre
  deux jobs, et seul fichier que le job a le droit de supprimer.
- :class:`OutputReservation` verrouille une destination pour la durée d'un
  job (verrou OS libéré à la mort du processus) et relève son état initial :
  absente, ou présente et acceptée pour écrasement (confirmation GUI,
  ``--force`` CLI).
- :func:`publish_candidate` publie le candidat. Destination absente au
  départ : publication sans écrasement. Présente : remplacement seulement si
  c'est toujours le fichier relevé au départ. Sinon :class:`OutputChangedError`
  et la destination comme le candidat sont conservés.
- :class:`ExistingPolicy` décide, sous le verrou de la réservation, du sort
  d'une destination présente : remplacement admis (``--force``), refus
  (:class:`OutputExistsError`), ou état confirmé par l'utilisateur exigé
  (:class:`OutputStateChangedError` si la destination a changé depuis).
- :func:`publish_document`, :func:`publish_file`, :func:`publish_unique` :
  documents et copies publiés sous réservation (jamais d'écrasement non
  autorisé, même entre processus concurrents).
"""

from __future__ import annotations

import errno
import ctypes
import hashlib
import os
import secrets
import sys
import threading
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from core.file_lock import FileLock

CANDIDATE_SUFFIX = ".partial"
_NAME_MAX_BYTES = 255


class OutputBusyError(RuntimeError):
    """Destination déjà réservée par un autre job (ce processus ou un autre)."""

    def __init__(self, output: Path) -> None:
        self.output = Path(output)
        super().__init__(
            f"Sortie déjà en cours d'écriture par un autre traitement : {output}. "
            "Attendez sa fin ou choisissez une autre destination."
        )


class OutputCommitError(RuntimeError):
    """Publication refusée : le candidat doit être conservé pour récupération."""


class OutputChangedError(OutputCommitError):
    """Destination apparue ou modifiée pendant le job : publication refusée."""

    def __init__(self, output: Path, candidate: Path) -> None:
        self.output = Path(output)
        self.candidate = Path(candidate)
        super().__init__(
            f"La destination {output} a été créée ou modifiée pendant le traitement ; "
            f"elle est conservée. Résultat disponible dans {candidate}."
        )


class OutputPublicationError(OutputCommitError):
    """Publication atomique sans écrasement indisponible sur cette destination."""

    def __init__(self, output: Path, candidate: Path) -> None:
        self.output, self.candidate = Path(output), Path(candidate)
        super().__init__(
            f"Publication sûre impossible vers {output} sur ce système de fichiers. "
            f"Résultat conservé dans {candidate}."
        )


class OutputRefusedError(RuntimeError):
    """Réservation refusée avant toute écriture (aucun candidat créé)."""

    def __init__(self, output: Path, message: str) -> None:
        self.output = Path(output)
        super().__init__(message)


class OutputExistsError(OutputRefusedError):
    """Destination présente sans autorisation de remplacement."""

    def __init__(self, output: Path, message: str | None = None) -> None:
        super().__init__(output, message or f"Sortie déjà existante : {output} (utiliser --force)")


class OutputStateChangedError(OutputRefusedError):
    """Destination créée, modifiée ou reciblée depuis sa confirmation par l'utilisateur."""

    def __init__(self, output: Path) -> None:
        super().__init__(output, f"La destination {output} a changé depuis la confirmation : rien n'a été écrit.")


def candidate_pattern(output: Path) -> Path:
    """Forme des candidats d'une sortie (affichage des plans, sans création)."""
    output = Path(output)
    return output.with_name(f"{output.stem}.<unique>{output.suffix}{CANDIDATE_SUFFIX}")


def reserve_candidate(output: Path) -> Path:
    """Crée un candidat vide et unique à côté de ``output`` et retourne son chemin.

    Droits par défaut du système (``0o666`` filtré par l'umask), comme un
    fichier créé par FFmpeg. Terminaison ``<suffixe>.partial`` conservée
    (``film.mkv`` → ``film.<aléa>.mkv.partial``).
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(64):
        tail = f".{secrets.token_hex(4)}{output.suffix}{CANDIDATE_SUFFIX}"
        budget = _NAME_MAX_BYTES - len(tail.encode("utf-8"))
        stem = output.stem.encode("utf-8")[:max(1, budget)].decode("utf-8", errors="ignore") or "output"
        candidate = output.with_name(stem + tail)
        try:
            fd = os.open(candidate, flags, 0o666)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise FileExistsError(errno.EEXIST, "Aucun nom de candidat libre", str(output))


def default_lock_dir() -> Path:
    """Dossier des verrous de destination (cache utilisateur de l'application)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "muxiveo" / "locks"


def destination_key(output: Path) -> str:
    """Clé canonique d'une destination : chemin réel, casse repliée hors Linux."""
    canonical = os.path.normcase(os.path.realpath(Path(output).expanduser()))
    return canonical.casefold() if sys.platform in ("win32", "darwin") else canonical


@dataclass(frozen=True)
class _Snapshot:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    entry_ino: int
    resolved: str
    #: Changement d'état (POSIX : toute écriture ou modification des droits, non réglable par ``utime``) et droits :
    #: une réécriture de même taille au mtime restauré reste détectée.
    ctime_ns: int = 0
    mode: int = 0


def _snapshot(path: Path) -> _Snapshot | None:
    try:
        st = os.stat(path)
        entry = os.lstat(path)
    except FileNotFoundError:
        return None
    return _Snapshot(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, entry.st_ino, destination_key(path),
                     st.st_ctime_ns, st.st_mode)


@dataclass(frozen=True)
class ConfirmedState:
    """État d'une destination tel que montré à l'utilisateur : chemin saisi, destination canonique (même
    absente) et instantané du fichier (None : absent). Un lien reciblé change ``resolved`` ou l'entrée."""

    path: Path
    resolved: str
    snapshot: _Snapshot | None


def capture_state(path: Path) -> ConfirmedState:
    """État actuel de ``path`` (à relever avant de demander la confirmation de remplacement)."""
    absolute = Path(path).expanduser().absolute()
    return ConfirmedState(absolute, destination_key(absolute), _snapshot(absolute))


@dataclass(frozen=True)
class ExistingPolicy:
    """Sort d'une destination présente, décidé sous le verrou de la réservation."""

    mode: Literal["allow", "refuse", "expect"] = "allow"
    confirmed: ConfirmedState | None = field(default=None, compare=False)

    @classmethod
    def allow(cls) -> "ExistingPolicy":
        """Remplacement admis (``--force``, comportement historique des workflows)."""
        return cls("allow")

    @classmethod
    def refuse(cls) -> "ExistingPolicy":
        """Destination présente refusée (:class:`OutputExistsError`)."""
        return cls("refuse")

    @classmethod
    def expect(cls, confirmed: ConfirmedState) -> "ExistingPolicy":
        """État confirmé par l'utilisateur exigé jusqu'à la publication."""
        return cls("expect", confirmed)

    @classmethod
    def from_force(cls, force: bool) -> "ExistingPolicy":
        return cls.allow() if force else cls.refuse()

    @property
    def refuses_existing(self) -> bool:
        """Vrai si une destination déjà présente est refusée d'avance (pré-contrôle rapide permis)."""
        return self.mode == "refuse"


_ACTIVE_LOCK = threading.Lock()
# Réservations détenues par ce processus, par clé de destination : la
# publication y retrouve l'état initial relevé au lancement du job.
_ACTIVE: dict[str, "OutputReservation"] = {}
_ACTIVE_PATHS: dict[str, "OutputReservation"] = {}


def _path_key(output: Path) -> str:
    """Identité stable du chemin fourni, même si un lien change de cible."""
    key = os.path.normcase(os.path.abspath(Path(output).expanduser()))
    return key.casefold() if sys.platform in ("win32", "darwin") else key


class OutputReservation:
    """Destination verrouillée pour un job ; à libérer par :meth:`release`."""

    def __init__(self, output: Path, *, lock_dir: Path | None = None) -> None:
        self.output = Path(output).expanduser().absolute()
        self.key = destination_key(self.output)
        self.path_key = _path_key(self.output)
        self._lock_dir = Path(lock_dir) if lock_dir is not None else default_lock_dir()
        self._locks: list[FileLock] = []
        self.initial: _Snapshot | None = None
        self.policy = ExistingPolicy.allow()
        self._confirmed: ConfirmedState | None = None

    @classmethod
    def acquire(cls, output: Path, *, lock_dir: Path | None = None,
                existing: ExistingPolicy | None = None) -> "OutputReservation":
        """Réserve ``output`` ou lève :class:`OutputBusyError` sans attendre.

        ``existing`` (défaut : remplacement admis) : :class:`OutputExistsError` ou
        :class:`OutputStateChangedError` levées sous le verrou, avant toute écriture.
        """
        reservation = cls(output, lock_dir=lock_dir)
        reservation.policy = existing or ExistingPolicy.allow()
        reservation._acquire()
        return reservation

    def _lock_keys(self) -> list[str]:
        keys = {f"path:{self.key}", f"path:{self.path_key}"}
        snapshot = _snapshot(self.output)
        if snapshot is not None and snapshot.ino:
            # Alias d'un fichier existant (lien physique, autre chemin de montage).
            keys.add(f"file:{snapshot.dev}:{snapshot.ino}")
        return sorted(keys)

    def _acquire(self) -> None:
        try:
            self._lock_dir.mkdir(parents=True, exist_ok=True)
            for key in self._lock_keys():
                name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".lock"
                lock = FileLock(self._lock_dir / name)
                if not lock.try_acquire():
                    raise OutputBusyError(self.output)
                self._locks.append(lock)
            self.initial = _snapshot(self.output)
            self._check_policy()
            with _ACTIVE_LOCK:
                if self.key in _ACTIVE or self.path_key in _ACTIVE_PATHS:
                    raise OutputBusyError(self.output)
                _ACTIVE[self.key] = self
                _ACTIVE_PATHS[self.path_key] = self
        except BaseException:
            # Hors du mutex : release() prend lui-même ce verrou.
            self.release()
            raise

    def _check_policy(self) -> None:
        policy = self.policy
        if policy.mode == "refuse" and self.initial is not None:
            raise OutputExistsError(self.output)
        if policy.mode == "expect":
            confirmed = policy.confirmed
            # Destination effectivement réservée = cible confirmée, et chemin confirmé inchangé
            # (fichier, entrée d'un lien, cible d'un lien de dossier).
            if (confirmed is None or self.key != confirmed.resolved
                    or capture_state(confirmed.path) != confirmed):
                raise OutputStateChangedError(self.output)
            self._confirmed = confirmed

    def release(self) -> None:
        """Libère la destination (idempotent, appelable depuis tout thread)."""
        with _ACTIVE_LOCK:
            if _ACTIVE.get(self.key) is self:
                del _ACTIVE[self.key]
            if _ACTIVE_PATHS.get(self.path_key) is self:
                del _ACTIVE_PATHS[self.path_key]
        locks, self._locks = self._locks, []
        for lock in locks:
            lock.release(unlink=True)

    def __enter__(self) -> "OutputReservation":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def publish(self, candidate: Path) -> None:
        """Publie ``candidate`` sur la destination selon l'état relevé au départ."""
        candidate = Path(candidate)
        if destination_key(self.output) != self.key:
            raise OutputChangedError(self.output, candidate)
        if self._confirmed is not None and capture_state(self._confirmed.path) != self._confirmed:
            # Chemin confirmé reciblé ou modifié pendant le traitement : rien n'est publié.
            raise OutputChangedError(self.output, candidate)
        current = _snapshot(self.output)
        if self.initial is None:
            if current is not None:
                raise OutputChangedError(self.output, candidate)
            _publish_no_clobber(candidate, self.output)
        else:
            if current != self.initial:
                raise OutputChangedError(self.output, candidate)
            os.replace(candidate, self.output)
        # La destination est désormais celle produite par ce job.
        self.initial = _snapshot(self.output)
        if self._confirmed is not None:
            self._confirmed = capture_state(self._confirmed.path)


def active_reservation(output: Path) -> OutputReservation | None:
    with _ACTIVE_LOCK:
        return _ACTIVE_PATHS.get(_path_key(output)) or _ACTIVE.get(destination_key(output))


def publish_candidate(candidate: Path, output: Path) -> None:
    """Publie ``candidate`` sur ``output``.

    Sous réservation active de ce processus : règles de :meth:`OutputReservation.publish`.
    Sans réservation (intermédiaires d'un workspace, appels historiques) :
    remplacement atomique simple.
    """
    reservation = active_reservation(output)
    if reservation is None:
        os.replace(candidate, output)
        return
    reservation.publish(candidate)


_NO_HARDLINK_ERRNOS = frozenset(
    code for code in (
        getattr(errno, "EPERM", None), getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None), getattr(errno, "EXDEV", None),
        getattr(errno, "EMLINK", None), getattr(errno, "ENOSYS", None),
        getattr(errno, "EACCES", None),
    ) if code is not None
)


def _publish_no_clobber(candidate: Path, output: Path) -> None:
    """Publie sans jamais écraser un fichier apparu entre-temps."""
    if sys.platform == "win32":
        # MoveFileEx sans remplacement : échoue si la destination existe.
        try:
            os.rename(candidate, output)
        except FileExistsError as exc:
            raise OutputChangedError(output, candidate) from exc
        return
    try:
        os.link(candidate, output)
    except FileExistsError as exc:
        raise OutputChangedError(output, candidate) from exc
    except OSError as exc:
        if exc.errno not in _NO_HARDLINK_ERRNOS:
            raise
        _rename_no_clobber(candidate, output)
        return
    os.unlink(candidate)


def _rename_no_clobber(candidate: Path, output: Path) -> None:
    """Repli POSIX atomique ; jamais de contrôle suivi d'un os.replace()."""
    libc = ctypes.CDLL(None, use_errno=True)
    source, target = os.fsencode(candidate), os.fsencode(output)
    try:
        if sys.platform.startswith("linux"):
            rename = libc.renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(-100, source, -100, target, 1)  # AT_FDCWD, RENAME_NOREPLACE
        elif sys.platform == "darwin":
            rename = libc.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            result = rename(source, target, 4)  # RENAME_EXCL
        else:
            raise OutputPublicationError(output, candidate)
    except AttributeError as exc:
        raise OutputPublicationError(output, candidate) from exc
    if result != 0:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise OutputChangedError(output, candidate)
        raise OutputPublicationError(output, candidate) from OSError(code, os.strerror(code))


def publish_document(path: Path, content: bytes, *, force: bool, mode: int | None = None) -> Path:
    """Écrit un petit document (JSON, texte) sous réservation : sans ``force``, jamais d'écrasement, même entre
    écrivains concurrents (:class:`OutputBusyError`, :class:`OutputExistsError`).

    Candidat ``fsync`` puis publication ; supprimé sur toute erreur (document régénérable). Destination apparue
    pendant l'écriture (écrivain externe sans verrou) : conservée, :class:`OutputExistsError`. ``mode`` : droits
    du document publié (défaut 0600, comme :func:`core.atomic_io.atomic_write_bytes`).
    """
    path = Path(path).expanduser()
    with OutputReservation.acquire(path, existing=ExistingPolicy.from_force(force)) as reservation:
        candidate = reserve_candidate(path)
        try:
            with open(candidate, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(candidate, 0o600 if mode is None else mode)
            try:
                reservation.publish(candidate)
            except OutputChangedError as exc:
                raise OutputExistsError(path, f"{path} est apparu pendant l'écriture : il est conservé.") from exc
        finally:
            candidate.unlink(missing_ok=True)
    return path


def publish_file(source: Path, path: Path, *, force: bool = False) -> Path:
    """Copie ``source`` vers ``path`` sous réservation (règles de :func:`publish_document`) ; source intacte."""
    path = Path(path).expanduser()
    with OutputReservation.acquire(path, existing=ExistingPolicy.from_force(force)) as reservation:
        candidate = reserve_candidate(path)
        try:
            shutil.copyfile(source, candidate)
            shutil.copystat(source, candidate)
            try:
                reservation.publish(candidate)
            except OutputChangedError as exc:
                raise OutputExistsError(path, f"{path} est apparu pendant l'écriture : il est conservé.") from exc
        finally:
            candidate.unlink(missing_ok=True)
    return path


def publish_unique(source: Path, directory: Path, name: str, *, attempts: int = 10_000) -> Path:
    """Copie ``source`` dans ``directory`` sous ``name``, sinon ``<stem>-1<suffixe>``, ``-2``… : nom jamais pris
    à un autre fichier ni à une livraison concurrente. La source reste en place (à supprimer par l'appelant)."""
    directory = Path(directory)
    first = directory / name
    for index in range(attempts):
        target = first if index == 0 else directory / f"{first.stem}-{index}{first.suffix}"
        try:
            return publish_file(source, target)
        except (OutputExistsError, OutputBusyError):
            continue
    raise FileExistsError(errno.EEXIST, "Aucun nom libre", str(first))


__all__ = [
    "CANDIDATE_SUFFIX", "ConfirmedState", "ExistingPolicy", "OutputBusyError", "OutputChangedError",
    "OutputCommitError", "OutputExistsError", "OutputPublicationError", "OutputRefusedError", "OutputReservation",
    "OutputStateChangedError", "active_reservation", "candidate_pattern", "capture_state", "default_lock_dir",
    "destination_key", "publish_candidate", "publish_document", "publish_file", "publish_unique", "reserve_candidate",
]
