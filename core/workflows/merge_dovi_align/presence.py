"""Présence des métadonnées dynamiques image par image, relevée **avant** extraction.

Les extracteurs masquent les trous : ``dovi_tool extract-rpu`` indexe les RPU par leur nombre
rencontré, ``hdr10plus_tool extract`` comble les images manquantes en ordre de décodage. Les comptes
extraits ne prouvent donc rien ; seule la lecture de chaque unité d'accès (blocs Matroska ou flux
Annex B, sans décodage) dit quelles images portent un RPU / un SEI HDR10+.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from core.i18n import translate_text
from core.workflows.encode.hdr10plus_output import _HDR10PLUS_T35

_RPU_NAL = 62
_SEI_NALS = (39, 40)

#: Prédicat sur les NAL d'une image : (type, contenu sans en-tête de longueur).
NalPredicate = Callable[[Iterable[tuple[int, bytes]]], bool]


def has_rpu(nals: Iterable[tuple[int, bytes]]) -> bool:
    """L'image porte une NAL RPU Dolby Vision (type 62)."""
    return any(kind == _RPU_NAL for kind, _payload in nals)


def has_hdr10plus(nals: Iterable[tuple[int, bytes]]) -> bool:
    """L'image porte un SEI T.35 HDR10+ (SMPTE ST 2094-40)."""
    return any(kind in _SEI_NALS and _HDR10PLUS_T35 in payload for kind, payload in nals)


class PresenceError(RuntimeError):
    """Rang d'affichage d'une image sans métadonnée impossible à établir sans ambiguïté."""


@dataclass(frozen=True)
class PresenceGaps:
    """Images sans la métadonnée cherchée (rangs comptés depuis 0)."""

    frames: int
    #: Rangs d'affichage des images sans la métadonnée (croissants).
    missing: tuple[int, ...]
    #: Toutes les images manquantes sont les dernières du flux en ordre de décodage.
    trailing: bool

    @property
    def complete(self) -> bool:
        return not self.missing


def _length_prefixed_nals(payload: bytes, length_size: int) -> list[tuple[int, bytes]]:
    nals: list[tuple[int, bytes]] = []
    pos = 0
    while pos + length_size <= len(payload):
        size = int.from_bytes(payload[pos:pos + length_size], "big")
        start = pos + length_size
        if size < 2 or start + size > len(payload):
            break
        nals.append(((payload[start] >> 1) & 0x3F, payload[start:start + size]))
        pos = start + size
    return nals


def _gaps(order_keys: list, missing_decode: list[int]) -> PresenceGaps:
    """Rangs d'affichage des images manquantes ; clés d'affichage égales avec un trou : :class:`PresenceError`
    (départager par l'ordre de décodage pourrait viser une autre image que celle qui manque)."""
    if missing_decode and len(set(order_keys)) != len(order_keys):
        raise PresenceError(translate_text(
            "Horodatages d'images égaux : rang des images sans métadonnée indéterminé."))
    order = sorted(range(len(order_keys)), key=lambda index: order_keys[index])
    rank = {decode: display for display, decode in enumerate(order)}
    trailing = bool(missing_decode) and missing_decode == list(
        range(len(order_keys) - len(missing_decode), len(order_keys)))
    return PresenceGaps(len(order_keys), tuple(sorted(rank[index] for index in missing_decode)), trailing)


def scan_matroska_presences(path: Path, track_index: int, predicates: dict[str, NalPredicate], *,
                            cancelled: Callable[[], bool] | None = None) -> dict[str, PresenceGaps]:
    """Blocs vidéo d'un MKV lus **une fois** : images sans chaque métadonnée, rang d'affichage par horodatage.

    Horodatages égaux avec un trou : :class:`PresenceError`. ``InterruptedError`` si ``cancelled``.
    """
    from core.matroska.reader import MatroskaReader

    reader = MatroskaReader(Path(path))
    reader.segment()
    tracks = reader.tracks()
    if not 0 <= track_index < len(tracks) or tracks[track_index].track_type != 1:
        raise ValueError(f"Piste vidéo #{track_index} absente.")
    track = tracks[track_index]
    private = track.codec_private
    length_size = (private[21] & 0x03) + 1 if len(private) >= 23 and private[0] == 1 else 4
    stamps: list[int] = []
    missing: dict[str, list[int]] = {name: [] for name in predicates}
    for block in reader.blocks(track_numbers={track.number}):
        index = len(stamps)
        if cancelled is not None and index % 2048 == 0 and cancelled():
            raise InterruptedError
        stamps.append(block.timestamp_ns if block.timestamp_ns is not None else block.timestamp_ms * 1_000_000)
        nals = _length_prefixed_nals(block.payload, length_size)
        for name, present in predicates.items():
            if not present(nals):
                missing[name].append(index)
    return {name: _gaps(stamps, values) for name, values in missing.items()}


def scan_matroska_presence(path: Path, track_index: int, present: NalPredicate, *,
                           cancelled: Callable[[], bool] | None = None) -> PresenceGaps:
    """Comme :func:`scan_matroska_presences` pour une seule métadonnée."""
    return scan_matroska_presences(path, track_index, {"x": present}, cancelled=cancelled)["x"]


def scan_annexb_presences(path: Path, predicates: dict[str, NalPredicate], *,
                          cancelled: Callable[[], bool] | None = None) -> dict[str, PresenceGaps]:
    """HEVC brut (Annex B) lu **une fois** : images sans chaque métadonnée, rang d'affichage déduit du POC.

    Annulation : ``HevcStreamCancelled`` ; flux illisible : ``PocError``.
    """
    from core.matroska.hevc.poc import iter_picture_units

    keys: list[tuple[int, int]] = []
    missing: dict[str, list[int]] = {name: [] for name in predicates}
    for key, _kinds, unit in iter_picture_units(Path(path), cancelled=cancelled):
        nals = [(nal.nal_type, nal.payload) for nal in unit.nal_units]
        for name, present in predicates.items():
            if not present(nals):
                missing[name].append(len(keys))
        keys.append(key)
    return {name: _gaps(keys, values) for name, values in missing.items()}


def scan_annexb_presence(path: Path, present: NalPredicate, *,
                         cancelled: Callable[[], bool] | None = None) -> PresenceGaps:
    """Comme :func:`scan_annexb_presences` pour une seule métadonnée."""
    return scan_annexb_presences(path, {"x": present}, cancelled=cancelled)["x"]
