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

from .assembly import quantize_block
from .ids import DEFAULT_TIMESTAMP_SCALE_NS
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

    Couvre horodatage, durée et références quantifiés comme l'assemblage
    (``quantize_block``, 1 ms), drapeaux (clé, invisible, discardable),
    CodecState, BlockAdditions, DiscardPadding, mode et nombre de laces, et
    payload brut (en-tête de laçage et tailles compris). Un bloc portant un élément de BlockGroup
    non représenté lève ``ValueError`` : sa copie ne serait pas fidèle. Sous-titres :
    suite prise dans l'ordre de présentation (horodatage, puis rang), celui où
    l'assemblage écrit les blocs indexés d'une source qui les range en retard.
    """
    reader = MatroskaReader(path)
    tracks = reader.tracks()
    numbers = [track.number for track in tracks]
    digests = {number: hashlib.sha256() for number in numbers}
    # Piste de sous-titres → (horodatage, rang, empreinte du bloc), triés en fin de lecture.
    subtitles: dict[int, list[tuple[int, int, bytes]]] = {
        track.number: [] for track in tracks if track.track_type == 17
    }
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
            # Même normalisation que l'assemblage (extrémités quantifiées à 1 ms).
            block = quantize_block(block, DEFAULT_TIMESTAMP_SCALE_NS)
            timestamp = block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000
            duration = block.duration_ns if block.duration_ns is not None else (
                block.duration_ms * 1_000_000 if block.duration_ms is not None else None
            )
            references = block.references_ns or tuple(value * 1_000_000 for value in block.references)
            header = (
                timestamp, duration,
                bool(block.is_keyframe), bool(block.flags & 0x08), bool(block.flags & 0x01),
                tuple(references), block.discard_padding_ns, block.lace_count, block.lacing_mode,
            )
            pending = subtitles.get(block.track_number)
            target = hashlib.sha256() if pending is not None else digest
            target.update(repr(header).encode())
            for part in (block.codec_state, block.block_additions, block.encoded_frames_payload or block.payload):
                target.update(len(part).to_bytes(8, "big"))
                target.update(part)
            if pending is not None:
                pending.append((timestamp, len(pending), target.digest()))
    finally:
        close = getattr(blocks, "close", None)
        if callable(close):
            close()
    for number, pending in subtitles.items():
        for _timestamp, _rank, block_digest in sorted(pending):
            digests[number].update(block_digest)
    return tuple(digests[number].hexdigest() for number in numbers)


def _track_entries(reader: MatroskaReader) -> list[tuple[tuple[bytes, bytes], ...]]:
    return [
        tuple(child for child in payload_children(track.raw_entry) if child[0] not in _TRACK_ID_FIELDS)
        for track in reader.tracks()
    ]


_TAGS_ID = bytes.fromhex("1254c367")
_TAG_ID = bytes.fromhex("7373")
_TARGETS_ID = bytes.fromhex("63c0")
_SIMPLE_TAG_ID = bytes.fromhex("67c8")
_TAG_NAME_ID = bytes.fromhex("45a3")
_TAG_TRACK_UID_ID = bytes.fromhex("63c5")
_TAG_ATTACHMENT_UID_ID = bytes.fromhex("63c6")


def _normalized_tags(reader: MatroskaReader) -> list[tuple]:
    """Tags comparés en EBML : cibles de piste / pièce jointe ramenées à leur rang,
    statistiques régénérées retirées, tout le reste (langues, valeurs binaires,
    tags imbriqués, cibles répétées) conservé octet pour octet."""
    track_rank = {track.uid: rank for rank, track in enumerate(reader.tracks())}
    attachment_rank = {attachment.uid: rank for rank, attachment in enumerate(reader.attachments())}
    result: list[tuple] = []
    for raw in reader.raw_top_level(_TAGS_ID):
        for element_id, payload in payload_children(raw):
            if element_id != _TAGS_ID:
                continue
            for tag_id, tag_payload in payload_children(payload):
                if tag_id != _TAG_ID:
                    result.append(("élément", tag_id, tag_payload))
                    continue
                targets: list[tuple] = []
                simple: list[bytes] = []
                for child_id, child_payload in payload_children(tag_payload):
                    if child_id == _TARGETS_ID:
                        for target_id, target_payload in payload_children(child_payload):
                            value = int.from_bytes(target_payload, "big")
                            # Rang d'une cible connue, sinon UID brut : jamais confondus.
                            if target_id == _TAG_TRACK_UID_ID:
                                targets.append((target_id, *_ranked(track_rank, value, "piste")))
                            elif target_id == _TAG_ATTACHMENT_UID_ID:
                                targets.append((target_id, *_ranked(attachment_rank, value, "pièce jointe")))
                            else:
                                targets.append((target_id, target_payload))
                    elif child_id == _SIMPLE_TAG_ID and _simple_tag_name(child_payload) in _STATISTICS_TAGS:
                        continue
                    else:
                        simple.append(child_id + child_payload)
                if simple:
                    result.append((tuple(targets), tuple(simple)))
    return sorted(result, key=repr)


def _ranked(ranks: dict[int, int], value: int, kind: str) -> tuple[str, int]:
    return (f"{kind} (rang)", ranks[value]) if value in ranks else (f"{kind} (UID brut)", value)


def _simple_tag_name(payload: bytes) -> str:
    for child_id, child_payload in payload_children(payload):
        if child_id == _TAG_NAME_ID:
            return child_payload.decode("utf-8", "replace").upper()
    return ""


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
