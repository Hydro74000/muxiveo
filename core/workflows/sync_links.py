"""Liens de synchro entre sources (références en chaîne), partagés par le panneau Conteneur et les jobs.

Une source mesurée face à la piste d'une autre source suit sa référence : la mesure (``relative``) vaut
face à l'audio d'origine de la référence ; la synchro appliquée est
``compose_calibrations(relative, position de la référence dans la sortie)``. Toute référence déplacée
recalcule ses dépendantes ; une boucle (référence synchronisée à partir de sa cible) est refusée.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypeVar

from core.lang_tags import language_key
from core.workflows.sync_calibration import (
    NEGLIGIBLE_SYNC_MS,
    SyncCalibration,
    compose_calibrations,
    effective_calibration,
)


@dataclass(frozen=True)
class SyncLink:
    """Synchro d'une source mesurée face à la piste d'une autre source : elle suit sa référence.

    ``relative`` : source → audio d'origine de la référence ; la synchro appliquée est
    ``compose_calibrations(relative, position de la référence dans la sortie)``.
    """

    reference_file_id: str
    reference_entry_id: str
    relative: SyncCalibration


class SyncLoopError(ValueError):
    """Liens de synchro en boucle (une source suit, même indirectement, sa propre cible)."""


def depends_on(links: Mapping[str, SyncLink], file_id: str, other_file_id: str) -> bool:
    """La synchro de ``file_id`` découle-t-elle, même indirectement, de celle de ``other_file_id`` ?"""
    seen: set[str] = set()
    link = links.get(file_id)
    while link is not None and link.reference_file_id not in seen:
        if link.reference_file_id == other_file_id:
            return True
        seen.add(link.reference_file_id)
        link = links.get(link.reference_file_id)
    return False


def follows_track(links: Mapping[str, SyncLink], file_id: str, entry_id: str) -> bool:
    """La synchro de ``file_id`` découle-t-elle, même indirectement, de la position de la piste ``entry_id`` ?"""
    seen: set[str] = set()
    link = links.get(file_id)
    while link is not None and link.reference_file_id not in seen:
        if link.reference_entry_id == entry_id:
            return True
        seen.add(link.reference_file_id)
        link = links.get(link.reference_file_id)
    return False


def dependency_order(links: Mapping[str, SyncLink]) -> list[str]:
    """Sources liées dans l'ordre de résolution (chaque référence avant ses cibles) ; boucle → ``SyncLoopError``.

    Parcours itératif (une seule référence par source : on suit la chaîne puis on la résout à rebours), en temps
    linéaire, indépendant de la limite de récursion de Python, quelle que soit la longueur de la chaîne.
    """
    order: list[str] = []
    state: dict[str, int] = {}  # 1 : sur la chaîne en cours, 2 : résolue
    for start in links:
        path: list[str] = []
        file_id = start
        while state.get(file_id) != 2:
            if state.get(file_id) == 1:
                loop = path[path.index(file_id):] + [file_id]
                raise SyncLoopError("Synchro en boucle : " + " → ".join(loop))
            state[file_id] = 1
            path.append(file_id)
            link = links.get(file_id)
            if link is None:
                break
            if link.reference_file_id == file_id:
                raise SyncLoopError(f"Synchro en boucle : {file_id} → {file_id}")
            file_id = link.reference_file_id
        for resolved in reversed(path):  # la référence la plus profonde d'abord
            state[resolved] = 2
            if resolved in links:
                order.append(resolved)
    return order


def same_applied_sync(first: SyncCalibration, second: SyncCalibration) -> bool:
    """Même synchro une fois appliquée : décalage arrondi à la ms, ou coupures et cadence identiques
    (méthode et mode demandé compris : les analyses suivantes en dépendent)."""
    first_stored, second_stored = effective_calibration(first), effective_calibration(second)
    if first_stored is None and second_stored is None:
        return round(first.segments[0].shift_ms) == round(second.segments[0].shift_ms)
    return (first_stored is not None and second_stored is not None
            and first_stored.segments == second_stored.segments
            and first_stored.cadence_mismatch == second_stored.cadence_mismatch
            and first_stored.cadence_audio_method == second_stored.cadence_audio_method
            and first_stored.cadence_pitch_factor == second_stored.cadence_pitch_factor
            and (first_stored.cadence_mismatch is None
                 or first_stored.cadence_method_requested == second_stored.cadence_method_requested))


def is_negligible(calibration: SyncCalibration) -> bool:
    """Décalage constant sous le seuil de perception (jamais appliqué)."""
    return (effective_calibration(calibration) is None
            and abs(round(calibration.segments[0].shift_ms)) <= NEGLIGIBLE_SYNC_MS)


def propagate(
    links: Mapping[str, SyncLink],
    changed: Iterable[str],
    *,
    reference_position: Callable[[SyncLink], SyncCalibration],
    current: Callable[[str], SyncCalibration],
    apply: Callable[[str, SyncCalibration, bool], object],
    same: Callable[[SyncCalibration, SyncCalibration], bool] = same_applied_sync,
) -> list[tuple[str, str, int]]:
    """Recalcule les sources liées aux sources ``changed`` (en cascade) ; elles suivent leur référence.

    ``apply(source, synchro, négligeable)`` est appelé pour chaque synchro modifiée ; retourne les
    changements ``(source, référence, décalage appliqué en ms)``.
    """
    queue = [file_id for file_id in changed if file_id]
    done = set(queue)
    updates: list[tuple[str, str, int]] = []
    while queue:
        reference_file_id = queue.pop(0)
        for file_id, link in list(links.items()):
            if link.reference_file_id != reference_file_id or file_id in done:
                continue
            done.add(file_id)
            queue.append(file_id)
            calibration = compose_calibrations(link.relative, reference_position(link))
            negligible = is_negligible(calibration)
            if same(SyncCalibration.linear(0) if negligible else calibration, current(file_id)):
                continue
            apply(file_id, calibration, negligible)
            updates.append((file_id, reference_file_id, 0 if negligible else round(calibration.segments[0].shift_ms)))
    return updates


class _Track(Protocol):
    file_id: str
    track_type: str
    language: str
    flag_commentary: bool


T = TypeVar("T", bound=_Track)


def measure_track(target: T, reference: _Track | None, tracks: Iterable[T], *,
                  usable: Callable[[T], bool] = lambda _track: True) -> T:
    """Piste analysée pour synchroniser ``target`` : une piste audio de sa source dans la langue de la
    référence (hors commentaire, ``usable``), sinon ``target``. Même chronologie : la synchro vaut pour la source.
    """
    reference_language = language_key(reference.language) if reference is not None else ""
    if not reference_language or language_key(target.language) == reference_language:
        return target
    return next((
        track for track in tracks
        if track.file_id == target.file_id and track.track_type == "audio"
        and not track.flag_commentary and usable(track) and language_key(track.language) == reference_language
    ), target)
