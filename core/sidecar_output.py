"""
core/sidecar_output.py — Sorties annexes d'une requête : rapports, calibrations, exact-jobs, résumés, recettes,
NFO, images.

Une annexe n'est jamais écrite sur un fichier que la requête lit ou produit (source, document de la requête,
sortie planifiée, autre annexe). :class:`SidecarGuard` reçoit ces fichiers protégés puis réserve chaque annexe
**avant** la première écriture et le premier job : collision → :class:`SidecarConflictError`, rien n'est écrit.
Une annexe existante sans lien avec la requête est remplacée (décision DA, 2026-11-26).

Identité d'un chemin : clé canonique (liens symboliques résolus, casse repliée hors Linux,
:func:`core.output_commit.destination_key`) et, pour un fichier présent, périphérique + inode (liens physiques,
autre chemin de montage). Elle est recalculée juste avant l'écriture : un fichier protégé créé entre-temps
(sortie produite) ou un lien reciblé sont vus.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_publish_bytes, atomic_publish_text
from core.jobs.constants import EXIT_ARGS, EXIT_WORKFLOW
from core.jobs.errors import JobError
from core.output_commit import OutputBusyError, OutputReservation, destination_key
from core.service.errors import ApiError, ErrorCode

#: Suffixe des candidats cachés d'une publication différée (images).
CANDIDATE_SUFFIX = ".partial"


class SidecarConflictError(JobError):
    """Annexe désignant un fichier protégé de la requête ou une autre annexe (``invalid_request``)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_ARGS)


def identity_keys(path: Path) -> frozenset[str]:
    """Clés d'identité de ``path`` : chemin canonique, et fichier présent (périphérique + inode)."""
    path = Path(path).expanduser()
    keys = {f"path:{destination_key(path)}"}
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return frozenset(keys)
    if st.st_ino:
        keys.add(f"file:{st.st_dev}:{st.st_ino}")
    return frozenset(keys)


@dataclass
class _Entry:
    path: Path
    role: str
    keys: frozenset[str]

    def current(self) -> frozenset[str]:
        """Clés relevées à l'enregistrement complétées de l'état actuel (fichier créé, lien reciblé)."""
        return self.keys | identity_keys(self.path)


def _absolute(path: Path | str) -> Path:
    return Path(path).expanduser().absolute()


class SidecarGuard:
    """Fichiers protégés d'une requête et annexes réservées.

    ``protected`` : couples (chemin, rôle) ; rôle = libellé français du fichier (« source », « document de la
    requête », « sortie »…), repris dans le message de refus.
    """

    def __init__(self, protected: Iterable[tuple[Path | str, str]] = ()) -> None:
        self._protected: list[_Entry] = []
        self._claimed: list[_Entry] = []
        for path, role in protected:
            self.protect(path, role)

    def protect(self, path: Path | str | None, role: str) -> None:
        """Ajoute un fichier lu ou produit par la requête (``None`` ou vide : ignoré)."""
        if path is None or str(path) == "":
            return
        absolute = _absolute(path)
        self._protected.append(_Entry(absolute, role, identity_keys(absolute)))

    def protect_many(self, paths: Iterable[Path | str | None], role: str) -> None:
        for path in paths:
            self.protect(path, role)

    def _conflict(self, keys: frozenset[str], *, skip: _Entry | None = None) -> _Entry | None:
        for entry in (*self._protected, *self._claimed):
            if entry is not skip and keys & entry.current():
                return entry
        return None

    @staticmethod
    def _refusal(role: str, path: Path, hit: _Entry) -> SidecarConflictError:
        return SidecarConflictError(
            f"{role} : {path} désigne {hit.role} de la requête ({hit.path}) ; rien n'a été écrit.")

    def claim(self, path: Path | str, role: str) -> Path:
        """Réserve l'annexe ``path`` (chemin absolu renvoyé) ; collision → :class:`SidecarConflictError`."""
        absolute = _absolute(path)
        keys = identity_keys(absolute)
        hit = self._conflict(keys)
        if hit is not None:
            raise self._refusal(role, absolute, hit)
        self._claimed.append(_Entry(absolute, role, keys))
        return absolute

    def _entry(self, path: Path) -> _Entry:
        absolute = _absolute(path)
        key = destination_key(absolute)
        for entry in self._claimed:
            if destination_key(entry.path) == key:
                return entry
        raise ValueError(f"Annexe non réservée : {absolute}")

    def verify(self, path: Path | str) -> Path:
        """Contrôle revérifié juste avant l'écriture d'une annexe réservée (état actuel des fichiers)."""
        entry = self._entry(Path(path))
        hit = self._conflict(entry.current(), skip=entry)
        if hit is not None:
            raise self._refusal(entry.role, entry.path, hit)
        return entry.path

    def write_text(self, path: Path | str, content: str) -> Path:
        """Écrit l'annexe réservée ``path`` (atomique, droits du fichier remplacé conservés sinon umask)."""
        target = self.verify(path)
        atomic_publish_text(target, content)
        return target

    def write_bytes(self, path: Path | str, content: bytes) -> Path:
        target = self.verify(path)
        atomic_publish_bytes(target, content)
        return target

    def write_json(self, path: Path | str, payload: Any) -> Path:
        from core.jobs.json_io import json_text

        return self.write_text(path, json_text(payload))


def guard_for(ctx: Any, *, sources: Iterable[Path | str | None] = ()) -> SidecarGuard:
    """Garde d'une opération : documents de transport du contexte (``protected_paths``) et ``sources``."""
    guard = SidecarGuard()
    guard.protect_many(getattr(ctx, "protected_paths", ()) or (), "document de la requête")
    guard.protect_many(sources, "source")
    return guard


def nfo_enabled(config: Any, options: Any) -> bool:
    """NFO écrit après le job : option du job (``--nfo`` / ``--no-nfo``), sinon configuration."""
    choice = getattr(options, "nfo", None)
    return bool(getattr(config, "generate_nfo", False)) if choice is None else bool(choice)


def nfo_path(output: Path) -> Path:
    """NFO MediaInfo écrit à côté d'une sortie (même nom, extension ``.nfo``)."""
    return _absolute(output).with_suffix(".nfo")


def nfo_input_conflict(output: Path, inputs: Iterable[Path | str | None]) -> str | None:
    """Message de refus si le NFO de ``output`` désignerait un fichier lu par le job, sinon ``None``."""
    target = nfo_path(output)
    keys = identity_keys(target)
    for item in inputs:
        if item is None or str(item) == "":
            continue
        if keys & identity_keys(_absolute(item)):
            return (f"Le NFO {target} remplacerait un fichier lu par le job ({_absolute(item)}) : "
                    "renommer la sortie ou désactiver le NFO.")
    return None


def check_nfo_against_documents(output: Path, config: Any, options: Any) -> None:
    """Job unitaire : le NFO écrit après le job ne remplace jamais un document du transport (``call --request``,
    ``--config``…) ; refus avant le job (:class:`SidecarConflictError`)."""
    if not nfo_enabled(config, options) or getattr(options, "export_workflow", None):
        return
    target = nfo_path(output)
    keys = identity_keys(target)
    for document in getattr(options, "protected_paths", ()) or ():
        if keys & identity_keys(_absolute(document)):
            raise SidecarConflictError(
                f"NFO : {target} désigne un document de la requête ({_absolute(document)}) ; rien n'a été écrit.")


def remux_input_paths(config: Any) -> list[Path]:
    """Fichiers lus par un remux (sources, pièces jointes externes)."""
    paths = [Path(source.path) for source in getattr(config, "sources", ()) or () if getattr(source, "path", None)]
    paths += [Path(item) for item in getattr(config, "extra_attachments", ()) or ()]
    return paths


def encode_input_paths(config: Any) -> list[Path]:
    """Fichiers lus par un encodage (source, pistes d'autres sources, pièces jointes, balises)."""
    paths: list[Path] = []
    source = getattr(config, "source", None)
    if source:
        paths.append(Path(source))
    for name in ("video_tracks", "audio_tracks", "subtitle_tracks", "attachment_streams"):
        for item in getattr(config, name, ()) or ():
            value = item[0] if isinstance(item, tuple) else getattr(item, "source_path", None)
            if value:
                paths.append(Path(value))
    paths += [Path(item) for item in getattr(config, "extra_attachments", ()) or ()]
    paths += [Path(item) for item in getattr(config, "tag_sources", ()) or ()]
    return paths


def job_document_paths(job: dict[str, Any]) -> list[tuple[str, str]]:
    """Chemins d'un document de job (inventaire de :mod:`core.json_documents`) : (chemin, rôle)."""
    from core.json_documents import _map_document_paths

    found: list[tuple[str, str]] = []

    def visit(slot: str, value: str) -> str:
        if value:
            found.append((value, "sortie" if slot == "output" else "source"))
        return value

    _map_document_paths(job, visit, preset_base=None)
    return [(value, role) for value, role in found if not value.endswith(("/", "\\"))]


@dataclass(frozen=True)
class PendingFile:
    """Annexe écrite en candidat caché dans son dossier de destination, publiée plus tard."""

    candidate: Path
    final: Path
    role: str


def stage_bytes(final: Path, content: bytes, *, role: str) -> PendingFile:
    """Écrit ``content`` dans un candidat caché du dossier de ``final`` (même volume : renommage atomique)."""
    final = Path(final).expanduser()  # chemin tel que demandé (affiché), contrôlé sous sa forme absolue
    final.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(64):
        tail = f".{secrets.token_hex(4)}{CANDIDATE_SUFFIX}"
        budget = 255 - len(tail.encode("utf-8")) - 1
        stem = final.name.encode("utf-8")[:max(1, budget)].decode("utf-8", errors="ignore") or "file"
        candidate = final.with_name(f".{stem}{tail}")
        try:
            fd = os.open(candidate, flags, 0o666)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
        return PendingFile(candidate, final, role)
    raise FileExistsError(f"Aucun nom de candidat libre à côté de {final}")


def discard_pending(pending: Iterable[PendingFile]) -> list[Path]:
    """Supprime les candidats ; renvoie ceux qui n'ont pas pu l'être."""
    left: list[Path] = []
    for item in pending:
        try:
            item.candidate.unlink(missing_ok=True)
        except OSError:
            left.append(item.candidate)
    return left


def publish_pending(guard: SidecarGuard, pending: Sequence[PendingFile], *,
                    lock_dir: Path | None = None,
                    rename: Callable[[Path, Path], None] = os.replace) -> list[Path]:
    """Publie des annexes en une passe : tous les noms contrôlés (fichiers protégés, doublons) et réservés avant
    le premier renommage, réservations toujours libérées.

    Collision ou nom réservé ailleurs (``busy``) : candidats supprimés, rien n'est publié. Échec d'un renommage
    en cours de passe : annexes déjà publiées conservées, candidats restants supprimés (tentative), erreur
    ``workflow_failed`` listant publiés, non publiés et candidats restés sur le disque. Aucun fichier protégé n'est
    jamais touché (contrôle préalable). ``rename`` : remplaçable par les tests.
    """
    reservations: list[OutputReservation] = []
    try:
        try:
            for item in pending:
                guard.claim(item.final, item.role)
            for item in pending:
                guard.verify(item.final)
                reservations.append(OutputReservation.acquire(item.final, lock_dir=lock_dir))
        except OutputBusyError as exc:
            discard_pending(pending)
            raise ApiError(ErrorCode.BUSY, f"{exc} ; aucune image publiée.") from exc
        except BaseException:
            discard_pending(pending)
            raise
        published: list[Path] = []
        for position, item in enumerate(pending):
            try:
                rename(item.candidate, item.final)
            except OSError as exc:
                rest = list(pending[position:])
                left = discard_pending(rest)
                unpublished = ", ".join(str(entry.final) for entry in rest)
                message = (f"Publication interrompue ({exc}) : publiées : "
                           f"{', '.join(str(path) for path in published) or 'aucune'} ; non publiées : {unpublished}")
                if left:
                    message += " ; candidats restés sur le disque : " + ", ".join(str(path) for path in left)
                raise JobError(message, EXIT_WORKFLOW) from exc
            published.append(item.final)
        return published
    finally:
        for reservation in reservations:
            reservation.release()


__all__ = [
    "CANDIDATE_SUFFIX", "PendingFile", "SidecarConflictError", "SidecarGuard", "check_nfo_against_documents",
    "discard_pending",
    "encode_input_paths", "guard_for", "identity_keys", "job_document_paths", "nfo_enabled", "nfo_input_conflict",
    "nfo_path",
    "publish_pending", "remux_input_paths", "stage_bytes",
]
