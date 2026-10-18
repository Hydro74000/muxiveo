"""
core/atomic_io.py — Écriture atomique de petits fichiers (JSON utilisateur, caches).

Candidat unique dans le dossier cible, ``fsync``, puis ``os.replace`` : une
interruption laisse l'ancienne version intacte, jamais un fichier partiel.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    """Remplace ``path`` par ``content`` (UTF-8) de façon atomique."""
    atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Remplace ``path`` par ``content`` de façon atomique."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


__all__ = ["atomic_write_bytes", "atomic_write_text"]
