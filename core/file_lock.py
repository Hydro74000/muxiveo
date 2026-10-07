"""
core/file_lock.py — Verrou exclusif non bloquant sur un fichier.

POSIX : ``flock`` ; Windows : ``msvcrt.locking`` sur le premier octet.
Le système libère le verrou à la mort du processus détenteur : un verrou
libre signifie qu'aucun processus vivant ne détient la ressource. Deux
ouvertures du même fichier, y compris dans un même processus, s'excluent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "win32":  # pragma: no cover - dépend de la plateforme
    import msvcrt
else:
    import fcntl


class FileLock:
    """Verrou exclusif sur ``path`` (fichier créé au besoin, jamais lu)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Prend le verrou sans attendre ; False s'il est détenu ailleurs."""
        if self._fd is not None:
            return True
        # Le fichier peut être retiré par son détenteur entre l'ouverture et
        # le verrouillage : on vérifie que le verrou porte bien sur le fichier
        # encore présent à ce chemin, sinon on recommence.
        for _ in range(16):
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            try:
                if not _lock_fd(fd):
                    return False
                try:
                    current = os.stat(self.path)
                except FileNotFoundError:
                    continue
                opened = os.fstat(fd)
                if sys.platform == "win32" or (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
                    self._fd = fd
                    return True
            finally:
                if self._fd != fd:
                    _unlock_fd(fd)
                    os.close(fd)
        return False

    def release(self, *, unlink: bool = False) -> None:
        """Libère le verrou ; ``unlink`` retire le fichier de verrou (best effort)."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if unlink and sys.platform != "win32":
            # Retrait sous verrou : un concurrent qui a ouvert l'ancien fichier
            # détecte le changement d'inode et recommence.
            try:
                os.unlink(self.path)
            except OSError:
                pass
        try:
            _unlock_fd(fd)
        finally:
            os.close(fd)
        if unlink and sys.platform == "win32":
            # Windows refuse de supprimer un fichier ouvert : retrait après
            # fermeture, ignoré si un autre processus l'a ouvert entre-temps.
            try:
                os.unlink(self.path)
            except OSError:
                pass


def lock_is_free(path: Path) -> bool:
    """Vrai si aucun détenteur vivant (verrou pris puis relâché aussitôt)."""
    probe = FileLock(path)
    try:
        if not probe.try_acquire():
            return False
    except OSError:
        return False
    probe.release()
    return True


def _lock_fd(fd: int) -> bool:
    try:
        if sys.platform == "win32":  # pragma: no cover - dépend de la plateforme
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock_fd(fd: int) -> None:
    try:
        if sys.platform == "win32":  # pragma: no cover - dépend de la plateforme
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


__all__ = ["FileLock", "lock_is_free"]
