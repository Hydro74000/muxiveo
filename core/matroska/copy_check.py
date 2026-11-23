"""
core/matroska/copy_check.py — Comparaison d'un fichier Matroska et de sa copie réassemblée.

Sert à valider un réassemblage natif (réparation de l'entrelacement d'une sortie
FFmpeg) **avant** de le substituer à l'original : la copie doit porter exactement
les mêmes médias et métadonnées, aux seuls changements autorisés près
(identifiants régénérés et remappés, statistiques, Cues, SeekHead, MuxingApp /
WritingApp, Duration recalculée, TimestampScale 1 ms).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .reader import MatroskaReader, payload_children

#: Éléments du TrackEntry régénérés par le writer (jamais comparés).
_TRACK_ID_FIELDS = frozenset({bytes.fromhex("d7"), bytes.fromhex("73c5"), bytes.fromhex("ec")})
#: Statistiques de piste régénérées par l'assemblage.
_STATISTICS_TAGS = frozenset({
    "BPS", "DURATION", "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES",
    "_STATISTICS_WRITING_APP", "_STATISTICS_WRITING_DATE_UTC", "_STATISTICS_TAGS",
})


class CopyCheckCancelled(Exception):
    """Comparaison interrompue par l'annulation du job."""


def block_fingerprints(path: Path, *, cancelled: Callable[[], bool] | None = None) -> tuple[str, ...]:
    """Empreinte SHA-256 de la suite des blocs de chaque piste (dans l'ordre des pistes).

    Couvre horodatage (1 ms), durée, drapeaux (clé, invisible, discardable),
    ReferenceBlock, CodecState, BlockAdditions, DiscardPadding, nombre de laces
    et payload brut (lacing compris). Un bloc portant un élément de BlockGroup
    non représenté lève ``ValueError`` : sa copie ne serait pas fidèle.
    """
    reader = MatroskaReader(path)
    numbers = [track.number for track in reader.tracks()]
    digests = {number: hashlib.sha256() for number in numbers}
    blocks = reader.blocks(first_lace_only=True)
    try:
        for count, block in enumerate(blocks):
            if cancelled is not None and count % 4096 == 0 and cancelled():
                raise CopyCheckCancelled()
            if block.unrepresented_group_elements:
                raise ValueError("Bloc portant un élément de BlockGroup non représenté.")
            digest = digests.get(block.track_number)
            if digest is None:
                raise ValueError(f"Bloc d'une piste absente (#{block.track_number}).")
            timestamp = block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000
            duration = block.duration_ns if block.duration_ns is not None else (
                block.duration_ms * 1_000_000 if block.duration_ms is not None else None
            )
            references = block.references_ns or tuple(value * 1_000_000 for value in block.references)
            header = (
                round(timestamp / 1_000_000),
                None if duration is None else round(duration / 1_000_000),
                bool(block.is_keyframe), bool(block.flags & 0x08), bool(block.flags & 0x01),
                tuple(round(value / 1_000_000) for value in references),
                block.discard_padding_ns, block.lace_count,
            )
            digest.update(repr(header).encode())
            for part in (block.codec_state, block.block_additions, block.encoded_frames_payload or block.payload):
                digest.update(len(part).to_bytes(8, "big"))
                digest.update(part)
    finally:
        close = getattr(blocks, "close", None)
        if callable(close):
            close()
    return tuple(digests[number].hexdigest() for number in numbers)


def _track_entries(reader: MatroskaReader) -> list[tuple[tuple[bytes, bytes], ...]]:
    return [
        tuple(child for child in payload_children(track.raw_entry) if child[0] not in _TRACK_ID_FIELDS)
        for track in reader.tracks()
    ]


def _normalized_tags(reader: MatroskaReader) -> list[tuple]:
    """Tags hors statistiques, cibles ramenées au rang de la piste / pièce jointe."""
    track_rank = {track.uid: rank for rank, track in enumerate(reader.tracks())}
    attachment_rank = {attachment.uid: rank for rank, attachment in enumerate(reader.attachments())}
    result = []
    for tag in reader.tags():
        values = tuple(value for value in tag.values if value[0].upper() not in _STATISTICS_TAGS)
        if not values:
            continue
        targets: list[tuple[str, object]] = []
        for key, value in sorted(tag.targets.items()):
            if key == "63c5":
                targets.append((key, ("piste", track_rank.get(value))))
            elif key == "63c6":
                targets.append((key, ("pièce jointe", attachment_rank.get(value))))
            else:
                targets.append((key, value))
        result.append((tuple(targets), values))
    return sorted(result, key=repr)


def _info_passthrough(reader: MatroskaReader) -> tuple[bytes, ...]:
    from .assembly import segment_info_passthrough

    return segment_info_passthrough(reader)


def compare_matroska_copy(
    original: Path, copy: Path, *, cancelled: Callable[[], bool] | None = None,
) -> list[str]:
    """Écarts entre un fichier et sa copie (vide : copie fidèle).

    Les blocs des deux fichiers sont lus séquentiellement, en parallèle.
    """
    differences: list[str] = []
    source, target = MatroskaReader(original), MatroskaReader(copy)
    if _track_entries(source) != _track_entries(target):
        differences.append("TrackEntry différents (hors numéros et UID).")
    if source.segment_title() != target.segment_title():
        differences.append("Titre du segment différent.")
    try:
        if _info_passthrough(source) != _info_passthrough(target):
            differences.append("Segment Info différent (DateUTC, autres champs recopiés).")
    except ValueError as exc:
        differences.append(str(exc))
    if [(a.name, a.media_type, a.description, a.data) for a in source.attachments()] != [
        (a.name, a.media_type, a.description, a.data) for a in target.attachments()
    ]:
        differences.append("Pièces jointes différentes.")
    chapters_id = bytes.fromhex("1043a770")
    if source.raw_top_level(chapters_id) != target.raw_top_level(chapters_id):
        differences.append("Chapitres différents.")
    if _normalized_tags(source) != _normalized_tags(target):
        differences.append("Tags différents (cibles remappées, hors statistiques).")
    if differences:
        return differences
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="matroska-copy-check") as pool:
        first = pool.submit(block_fingerprints, original, cancelled=cancelled)
        second = pool.submit(block_fingerprints, copy, cancelled=cancelled)
        source_blocks, target_blocks = first.result(), second.result()
    for rank, (ours, theirs) in enumerate(zip(source_blocks, target_blocks), start=1):
        if ours != theirs:
            differences.append(f"Blocs de la piste {rank} différents.")
    return differences


__all__ = ["CopyCheckCancelled", "block_fingerprints", "compare_matroska_copy"]
