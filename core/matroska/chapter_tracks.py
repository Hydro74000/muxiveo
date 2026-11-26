"""
core/matroska/chapter_tracks.py — Chapitres copiés d'une source : ``ChapterTrack`` remappé vers les pistes de sortie.

Un chapitre peut viser des pistes (``ChapterTrack`` → ``ChapterTrackUID``) ; copié tel quel, il viserait des UID
qui n'existent plus dans la sortie. Chaque UID source est remplacé par les UID de sortie de la piste (plusieurs
pour une piste dupliquée) ; un chapitre dont **toutes** les pistes ciblées sont absentes de la sortie est retiré
(décision DF : le garder sans ``ChapterTrack`` élargirait sa portée à toutes les pistes), chapitres imbriqués
compris. Une édition vidée est retirée, ``Chapters`` vidé aussi.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .ebml import element, uint_element
from .reader import payload_children

CHAPTERS_ID = bytes.fromhex("1043a770")
EDITION_ENTRY_ID = bytes.fromhex("45b9")
CHAPTER_ATOM_ID = bytes.fromhex("b6")
CHAPTER_TRACK_ID = bytes.fromhex("8f")
CHAPTER_TRACK_UID_ID = bytes.fromhex("89")


def _remap_atom(payload: bytes, uid_map: Mapping[int, Sequence[int]]) -> tuple[bytes | None, int]:
    """Chapitre réécrit (``None`` : retiré) et nombre de chapitres retirés (lui-même et ses enfants compris)."""
    parts: list[bytes] = []
    removed = 0
    for element_id, value in payload_children(payload):
        if element_id == CHAPTER_TRACK_ID:
            targets: list[int] = []
            for child_id, child_value in payload_children(value):
                if child_id == CHAPTER_TRACK_UID_ID:
                    targets.extend(uid_map.get(int.from_bytes(child_value, "big"), ()))
            if not targets:
                # Toutes les pistes ciblées absentes : chapitre retiré avec ses enfants (décision DF).
                return None, 1 + _count_atoms(payload)
            parts.append(element(CHAPTER_TRACK_ID, b"".join(uint_element(CHAPTER_TRACK_UID_ID, uid)
                                                             for uid in dict.fromkeys(targets))))
        elif element_id == CHAPTER_ATOM_ID:
            child, count = _remap_atom(value, uid_map)
            removed += count
            if child is not None:
                parts.append(child)
        else:
            parts.append(element(element_id, value))
    return element(CHAPTER_ATOM_ID, b"".join(parts)), removed


def _count_atoms(payload: bytes) -> int:
    """Chapitres imbriqués d'un chapitre (récursif)."""
    return sum(1 + _count_atoms(value) for element_id, value in payload_children(payload)
               if element_id == CHAPTER_ATOM_ID)


def remap_chapter_tracks(chapters_payload: bytes, uid_map: Mapping[int, Sequence[int]]) -> tuple[bytes, int]:
    """Élément ``Chapters`` complet (charge utile ``chapters_payload``) réécrit pour la sortie : ``ChapterTrackUID``
    remappés par ``uid_map`` (UID source → UID de sortie), chapitres sans cible retirés. Renvoie (élément, ``b""`` si
    plus aucun chapitre ; nombre de chapitres retirés)."""
    editions: list[bytes] = []
    removed = 0
    for element_id, value in payload_children(chapters_payload):
        if element_id != EDITION_ENTRY_ID:
            editions.append(element(element_id, value))
            continue
        parts: list[bytes] = []
        atoms = 0
        for edition_id, edition_value in payload_children(value):
            if edition_id == CHAPTER_ATOM_ID:
                atom, count = _remap_atom(edition_value, uid_map)
                removed += count
                if atom is not None:
                    parts.append(atom)
                    atoms += 1
            else:
                parts.append(element(edition_id, edition_value))
        if atoms:
            editions.append(element(EDITION_ENTRY_ID, b"".join(parts)))
    if not any(item.startswith(EDITION_ENTRY_ID) for item in editions):
        return b"", removed
    return element(CHAPTERS_ID, b"".join(editions)), removed


__all__ = ["remap_chapter_tracks"]
