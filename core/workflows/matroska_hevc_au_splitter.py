"""
core/workflows/hevc_access_unit_splitter.py

Découpage d'un flux HEVC annexB en access units (= frames Matroska).

Un access unit HEVC contient typiquement :
  - éventuels NAL units delimiter / VPS / SPS / PPS (en début de stream, et
    potentiellement avant chaque keyframe selon la sortie ffmpeg)
  - éventuels SEI prefix (notamment HDR10+ : NAL type 39 ; DOVI RPU : NAL
    type 62 selon la spec ITU-T = "unspecified" mais utilisé en pratique
    par dovi_tool / hdr10plus_tool)
  - les NAL units de slice (types 0..31), avec exactement UN d'entre eux
    ayant ``first_slice_segment_in_pic_flag = 1`` qui marque le début de
    l'image.

Algorithme :
  1. Découper le flux en NAL units (séparateurs 0x000001 ou 0x00000001).
  2. Décoder le header NAL (2 octets pour HEVC).
  3. Marquer un nouvel access unit à chaque NAL slice (types 0..31) qui a
     ``first_slice_segment_in_pic_flag = 1`` après une exception (le 1er AU
     du flux), ou à chaque NAL non-slice si l'AU précédent contenait au
     moins un slice (ce qui détecte la frontière naturelle).

Contrainte du muxer : chaque access unit doit former un seul SimpleBlock
Matroska. Tous les NAL d'un même AU restent collés dans le payload du Block.
La détection des keyframes se fait via le NAL type :
  - 16..21 = IRAP (BLA, IDR, CRA) → keyframe.
  - sinon → non-keyframe.

Référence : ITU-T H.265 §7.3.1.2 (NAL unit syntax), §7.4.2.4.4 (detection
of first VCL NAL unit of an access unit).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field


# Séparateurs annexB
_START_CODE_4 = b"\x00\x00\x00\x01"
_START_CODE_3 = b"\x00\x00\x01"


# Types NAL HEVC pertinents
_NAL_TYPE_TRAIL_N = 0
_NAL_TYPE_TRAIL_R = 1
_NAL_TYPE_BLA_W_LP = 16
_NAL_TYPE_BLA_W_RADL = 17
_NAL_TYPE_BLA_N_LP = 18
_NAL_TYPE_IDR_W_RADL = 19
_NAL_TYPE_IDR_N_LP = 20
_NAL_TYPE_CRA_NUT = 21
_NAL_TYPE_VPS = 32
_NAL_TYPE_SPS = 33
_NAL_TYPE_PPS = 34
_NAL_TYPE_AUD = 35
_NAL_TYPE_EOS = 36
_NAL_TYPE_EOB = 37
_NAL_TYPE_FD = 38
_NAL_TYPE_PREFIX_SEI = 39
_NAL_TYPE_SUFFIX_SEI = 40

_VCL_RANGE = range(0, 32)
_IRAP_TYPES = frozenset({
    _NAL_TYPE_BLA_W_LP, _NAL_TYPE_BLA_W_RADL, _NAL_TYPE_BLA_N_LP,
    _NAL_TYPE_IDR_W_RADL, _NAL_TYPE_IDR_N_LP, _NAL_TYPE_CRA_NUT,
})


@dataclass
class HevcNalUnit:
    """Un NAL unit HEVC + ses métadonnées principales."""
    payload: bytes        # Les octets du NAL (sans le start code annexB).
    nal_type: int
    first_slice_in_pic: bool


@dataclass
class HevcAccessUnit:
    """Un access unit = ensemble de NAL formant 1 frame présentée."""
    payload: bytes = b""
    is_keyframe: bool = False
    nal_units: list[HevcNalUnit] = field(default_factory=list)


def _iter_nal_units(stream: bytes) -> Iterator[HevcNalUnit]:
    """
    Itère sur les NAL units d'un buffer annexB. Tolère les start codes
    courts (3 octets) et longs (4 octets), tels qu'émis par ffmpeg
    ``-f hevc``.
    """
    n = len(stream)
    if n == 0:
        return

    # Localise tous les start codes.
    positions: list[tuple[int, int]] = []  # (start_code_offset, payload_offset)
    i = 0
    while i < n - 2:
        if stream[i] == 0 and stream[i + 1] == 0:
            if i + 2 < n and stream[i + 2] == 1:
                positions.append((i, i + 3))
                i += 3
                continue
            if i + 3 < n and stream[i + 2] == 0 and stream[i + 3] == 1:
                positions.append((i, i + 4))
                i += 4
                continue
        i += 1

    if not positions:
        return

    for idx, (_, payload_off) in enumerate(positions):
        next_off = positions[idx + 1][0] if idx + 1 < len(positions) else n
        nal_bytes = stream[payload_off:next_off]
        if len(nal_bytes) < 2:
            continue
        # Header NAL HEVC (2 octets) :
        #   forbidden_zero_bit (1) + nal_unit_type (6) + nuh_layer_id (6) +
        #   nuh_temporal_id_plus1 (3)
        header_byte_0 = nal_bytes[0]
        nal_type = (header_byte_0 >> 1) & 0x3F

        first_slice = False
        if nal_type in _VCL_RANGE and len(nal_bytes) >= 3:
            # Le 3ème octet contient (entre autres) ``first_slice_segment_in_pic_flag``
            # comme premier bit du RBSP slice_segment_header().
            first_slice = bool(nal_bytes[2] & 0x80)

        yield HevcNalUnit(
            payload=nal_bytes,
            nal_type=nal_type,
            first_slice_in_pic=first_slice,
        )


def split_into_access_units(stream: bytes) -> list[HevcAccessUnit]:
    """
    Découpe ``stream`` (HEVC annexB) en access units. Chaque AU correspond
    à exactement une frame présentée et porte tous ses NAL non-slice
    précédents (VPS, SPS, PPS, SEI prefix dont DOVI RPU et HDR10+).

    Logique de séparation : un AU se termine quand on rencontre soit
    a) un NAL slice avec ``first_slice_segment_in_pic_flag = 1`` (= début
    d'une nouvelle frame), précédé d'au moins un slice dans l'AU courant ;
    soit b) un NAL non-slice qui suit un slice (les NAL non-slice "préfixe"
    appartiennent au prochain AU).
    """
    aus: list[HevcAccessUnit] = []
    current = HevcAccessUnit()
    has_slice = False

    def _flush() -> None:
        nonlocal current, has_slice
        if current.nal_units:
            current.payload = b"".join(_emit_nal(n) for n in current.nal_units)
            aus.append(current)
        current = HevcAccessUnit()
        has_slice = False

    for nal in _iter_nal_units(stream):
        is_slice = nal.nal_type in _VCL_RANGE
        # Frontière A : nouveau slice "first in pic" alors qu'on en avait déjà un.
        if is_slice and nal.first_slice_in_pic and has_slice:
            _flush()
        # Frontière B : NAL "prefix" (VPS/SPS/PPS/AUD/SEI prefix) qui suit
        # au moins un slice → appartient au prochain AU.
        if not is_slice and has_slice and nal.nal_type in {
            _NAL_TYPE_VPS, _NAL_TYPE_SPS, _NAL_TYPE_PPS,
            _NAL_TYPE_AUD, _NAL_TYPE_PREFIX_SEI,
            # Les NAL_TYPE 62 (RPU) et 39 (HDR10+) : 39 est déjà inclus,
            # 62 = NAL réservé/unspecified utilisé pour DV ; on le traite
            # comme prefix s'il apparaît avant le 1er slice du prochain AU.
            62, 63,
        }:
            _flush()

        current.nal_units.append(nal)
        if is_slice:
            has_slice = True
            if nal.nal_type in _IRAP_TYPES:
                current.is_keyframe = True

    _flush()
    return aus


def _emit_nal(nal: HevcNalUnit) -> bytes:
    """Réémet un NAL avec un start code annexB long (4 octets)."""
    return _START_CODE_4 + nal.payload


__all__ = [
    "HevcAccessUnit",
    "HevcNalUnit",
    "split_into_access_units",
]
