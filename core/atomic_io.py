"""
core/atomic_io.py — Écriture atomique de petits fichiers (JSON utilisateur, caches).

Candidat unique dans le dossier cible, ``fsync``, puis ``os.replace`` : une
interruption laisse l'ancienne version intacte, jamais un fichier partiel.
"""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path

#: Longueur maximale d'un nom de fichier (octets, POSIX et NTFS en pratique).
_NAME_MAX_BYTES = 255


def atomic_write_text(path: Path, content: str, *, mode: int | None = None) -> None:
    """Remplace ``path`` par ``content`` (UTF-8) de façon atomique."""
    atomic_write_bytes(path, content.encode("utf-8"), mode=mode)


def atomic_write_bytes(path: Path, content: bytes, *, mode: int | None = None) -> None:
    """Remplace ``path`` par ``content`` de façon atomique.

    ``mode`` : droits du fichier publié (le candidat temporaire est créé en 0600) ; documents
    destinés à être partagés (rapports), sinon droits privés.
    """
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_rewrite_text(path: Path, content: str) -> None:
    """Réécrit un fichier de configuration modifié sur place : remplacement atomique, droits existants conservés."""
    path = Path(path)
    try:
        mode: int | None = path.stat().st_mode & 0o777
    except OSError:
        mode = None
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content, mode=mode)


def _ordinary_candidate(path: Path) -> tuple[int, Path]:
    """Candidat caché à côté de ``path``, créé comme un fichier ordinaire (``0o666`` filtré par l'umask)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(64):
        tail = f".{secrets.token_hex(4)}.tmp"
        budget = _NAME_MAX_BYTES - len(tail.encode("utf-8")) - 1
        stem = path.name.encode("utf-8")[:max(1, budget)].decode("utf-8", errors="ignore") or "file"
        candidate = path.with_name(f".{stem}{tail}")
        try:
            return os.open(candidate, flags, 0o666), candidate
        except FileExistsError:
            continue
    raise FileExistsError(f"Aucun nom de candidat libre à côté de {path}")


def atomic_publish_bytes(path: Path, content: bytes) -> None:
    """Remplace ``path`` de façon atomique (document destiné à l'utilisateur : rapport, job, résumé).

    Dossiers parents créés ; droits du fichier remplacé conservés, sinon ceux d'une création ordinaire
    (umask), jamais le 0600 d'un temporaire privé.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode: int | None = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = None
    fd, candidate = _ordinary_candidate(path)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(candidate, mode)
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def atomic_publish_text(path: Path, content: str) -> None:
    """:func:`atomic_publish_bytes` d'un texte UTF-8."""
    atomic_publish_bytes(path, content.encode("utf-8"))


__all__ = ["atomic_publish_bytes", "atomic_publish_text", "atomic_rewrite_text", "atomic_write_bytes",
           "atomic_write_text"]
