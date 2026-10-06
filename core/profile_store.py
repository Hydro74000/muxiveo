"""
core/profile_store.py — Stockage de profils JSON identifiés par leur nom exact.

Le nom affiché (champ JSON) est l'identité du profil ; le nom de fichier n'en
est qu'une dérivation. Deux noms distincts que la normalisation rend
identiques (``Film/4K`` et ``Film:4K``) ne partagent jamais un fichier :
le premier garde le nom normalisé historique, le suivant reçoit un suffixe
stable dérivé de son nom exact. Les fichiers existants ne sont jamais renommés.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_text
from core.file_lock import FileLock
from core.json_documents import JsonDocumentError, read_json_document


@dataclass(frozen=True)
class ProfileLoadError:
    """Profil présent sur disque mais illisible (conservé tel quel)."""

    path: Path
    reason: str


@dataclass
class ProfileStore:
    """Fichiers ``*.json`` d'un dossier, retrouvés par nom exact."""

    directory: Path
    safe_stem: Callable[[str], str]
    #: Nom exact d'un profil chargé (None : profil non nommé).
    name_of: Callable[[dict[str, Any]], str | None]
    load_errors: list[ProfileLoadError] = field(default_factory=list)

    def scan(self) -> list[tuple[Path, dict[str, Any]]]:
        """Profils lisibles, triés par nom de fichier ; erreurs dans ``load_errors``."""
        entries: list[tuple[Path, dict[str, Any]]] = []
        errors: list[ProfileLoadError] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = read_json_document(path)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, JsonDocumentError) as exc:
                errors.append(ProfileLoadError(path, str(exc)))
                continue
            if not isinstance(data, dict):
                errors.append(ProfileLoadError(path, "objet JSON attendu"))
                continue
            entries.append((path, data))
        self.load_errors = errors
        return entries

    def find(self, name: str) -> Path | None:
        """Fichier du profil dont le nom exact est ``name``."""
        for path, data in self.scan():
            if self.name_of(data) == name:
                return path
        return None

    def path_for_name(self, name: str) -> Path:
        """Fichier existant du profil ``name``, sinon nom libre à créer."""
        existing = self.find(name)
        if existing is not None:
            return existing
        base = self.safe_stem(name)
        taken = {path.stem.casefold() for path in self.directory.glob("*.json")}
        if base.casefold() not in taken:
            return self.directory / f"{base}.json"
        stem = f"{base}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]}"
        candidate, index = stem, 2
        while candidate.casefold() in taken:
            candidate, index = f"{stem}-{index}", index + 1
        return self.directory / f"{candidate}.json"

    def write(self, name: str, content: str) -> Path:
        """Écrit atomiquement le profil ``name`` (même fichier s'il existe)."""
        with self._mutation():
            path = self.path_for_name(name)
            atomic_write_text(path, content)
            return path

    def delete(self, name: str) -> bool:
        """Supprime le seul fichier du profil ``name`` ; False s'il n'existe pas."""
        with self._mutation():
            path = self.find(name)
            if path is None:
                return False
            path.unlink(missing_ok=True)
            return True

    @contextmanager
    def _mutation(self):
        """Résolution du nom et écriture indivisibles entre instances."""
        lock = FileLock(self.directory / ".profiles.lock")
        if not lock.try_acquire():
            raise OSError("Un autre traitement modifie les profils ; réessayez.")
        try:
            yield
        finally:
            lock.release(unlink=True)


__all__ = ["ProfileLoadError", "ProfileStore"]
