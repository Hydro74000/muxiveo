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
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

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


class OutputChangedError(RuntimeError):
    """Destination apparue ou modifiée pendant le job : publication refusée."""

    def __init__(self, output: Path, candidate: Path) -> None:
        self.output = Path(output)
        self.candidate = Path(candidate)
        super().__init__(
            f"La destination {output} a été créée ou modifiée pendant le traitement ; "
            f"elle est conservée. Résultat disponible dans {candidate}."
        )


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


def _snapshot(path: Path) -> _Snapshot | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return _Snapshot(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


_ACTIVE_LOCK = threading.Lock()
# Réservations détenues par ce processus, par clé de destination : la
# publication y retrouve l'état initial relevé au lancement du job.
_ACTIVE: dict[str, "OutputReservation"] = {}


class OutputReservation:
    """Destination verrouillée pour un job ; à libérer par :meth:`release`."""

    def __init__(self, output: Path, *, lock_dir: Path | None = None) -> None:
        self.output = Path(output).expanduser().absolute()
        self.key = destination_key(self.output)
        self._lock_dir = Path(lock_dir) if lock_dir is not None else default_lock_dir()
        self._locks: list[FileLock] = []
        self.initial: _Snapshot | None = None

    @classmethod
    def acquire(cls, output: Path, *, lock_dir: Path | None = None) -> "OutputReservation":
        """Réserve ``output`` ou lève :class:`OutputBusyError` sans attendre."""
        reservation = cls(output, lock_dir=lock_dir)
        reservation._acquire()
        return reservation

    def _lock_keys(self) -> list[str]:
        keys = [f"path:{self.key}"]
        snapshot = _snapshot(self.output)
        if snapshot is not None and snapshot.ino:
            # Alias d'un fichier existant (lien physique, autre chemin de montage).
            keys.append(f"file:{snapshot.dev}:{snapshot.ino}")
        return sorted(keys)

    def _acquire(self) -> None:
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        for key in self._lock_keys():
            name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".lock"
            lock = FileLock(self._lock_dir / name)
            if not lock.try_acquire():
                self.release()
                raise OutputBusyError(self.output)
            self._locks.append(lock)
        self.initial = _snapshot(self.output)
        with _ACTIVE_LOCK:
            if self.key in _ACTIVE:
                self.release()
                raise OutputBusyError(self.output)
            _ACTIVE[self.key] = self

    def release(self) -> None:
        """Libère la destination (idempotent, appelable depuis tout thread)."""
        with _ACTIVE_LOCK:
            if _ACTIVE.get(self.key) is self:
                del _ACTIVE[self.key]
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


def active_reservation(output: Path) -> OutputReservation | None:
    with _ACTIVE_LOCK:
        return _ACTIVE.get(destination_key(output))


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
        # Système de fichiers sans liens physiques (FAT, certains partages) :
        # le verrou de destination exclut les autres jobs Muxiveo ; dernier
        # contrôle avant le remplacement.
        if os.path.lexists(output):
            raise OutputChangedError(output, candidate) from exc
        os.replace(candidate, output)
        return
    os.unlink(candidate)


__all__ = [
    "CANDIDATE_SUFFIX", "OutputBusyError", "OutputChangedError", "OutputReservation",
    "active_reservation", "candidate_pattern", "default_lock_dir", "destination_key",
    "publish_candidate", "reserve_candidate",
]
