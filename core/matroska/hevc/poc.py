"""
core/matroska/hevc/poc.py — Ordre d'affichage d'un flux HEVC brut (Annex B) et images sans RPU Dolby Vision.

Un HEVC brut n'a pas d'horodatage : le rang d'affichage d'une image se déduit de son POC (H.265 § 8.3.1),
calculé depuis l'en-tête de sa première tranche (``slice_pic_order_cnt_lsb``) et les jeux de paramètres
(SPS : ``log2_max_pic_order_cnt_lsb_minus4`` ; PPS : bits d'en-tête supplémentaires, ``output_flag``). Le
rang est l'ordre (séquence codée, POC) ; chaque séquence codée commence à une image IRAP sans RASL à sortir
(IDR, BLA, CRA en tête de flux ou après une fin de séquence). Lecture en mémoire bornée, annulable.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Callable

from core.matroska.hevc.access_units import HevcStreamCancelled, iter_hevc_access_units

_SPS, _PPS, _EOS = 33, 34, 36
_BLA_W_LP, _IDR_W_RADL, _IDR_N_LP, _CRA, _IRAP_LAST = 16, 19, 20, 21, 23
_RADL_N, _RASL_R = 6, 9


class PocError(ValueError):
    """Flux HEVC illisible pour le calcul de l'ordre d'affichage."""


class _Bits:
    """Lecture bit à bit d'une charge RBSP (octets d'émulation retirés)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def u(self, count: int) -> int:
        value = 0
        for _ in range(count):
            byte = self._pos >> 3
            if byte >= len(self._data):
                raise PocError("en-tête HEVC tronqué")
            value = (value << 1) | ((self._data[byte] >> (7 - (self._pos & 7))) & 1)
            self._pos += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise PocError("code Exp-Golomb invalide")
        return (1 << zeros) - 1 + self.u(zeros)


def _rbsp(payload: bytes, limit: int = 512) -> bytes:
    """Charge du NAL sans son en-tête de 2 octets, octets d'émulation (00 00 03) retirés (début seulement)."""
    out = bytearray()
    zeros = 0
    for byte in payload[2:2 + limit]:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _skip_profile_tier_level(bits: _Bits, max_sub_layers_minus1: int) -> None:
    bits.u(8 + 32 + 48)          # espace, palier, profil, compatibilités, contraintes
    bits.u(8)                    # general_level_idc
    present = [(bits.u(1), bits.u(1)) for _ in range(max_sub_layers_minus1)]
    if max_sub_layers_minus1 > 0:
        bits.u(2 * (8 - max_sub_layers_minus1))
    for profile_present, level_present in present:
        if profile_present:
            bits.u(88)
        if level_present:
            bits.u(8)


def _parse_sps(payload: bytes) -> tuple[int, int, bool]:
    """(sps_id, log2_max_poc_lsb, separate_colour_plane)."""
    bits = _Bits(_rbsp(payload))
    bits.u(4)
    max_sub_layers_minus1 = bits.u(3)
    bits.u(1)
    _skip_profile_tier_level(bits, max_sub_layers_minus1)
    sps_id = bits.ue()
    separate = False
    if bits.ue() == 3:           # chroma_format_idc
        separate = bool(bits.u(1))
    bits.ue()
    bits.ue()                    # largeur, hauteur
    if bits.u(1):                # fenêtre de conformité
        for _ in range(4):
            bits.ue()
    bits.ue()
    bits.ue()                    # profondeurs
    return sps_id, bits.ue() + 4, separate


def _parse_pps(payload: bytes) -> tuple[int, int, bool, bool, int]:
    """(pps_id, sps_id, dependent_slices, output_flag_present, num_extra_slice_header_bits)."""
    bits = _Bits(_rbsp(payload))
    pps_id, sps_id = bits.ue(), bits.ue()
    dependent = bool(bits.u(1))
    output_flag = bool(bits.u(1))
    return pps_id, sps_id, dependent, output_flag, bits.u(3)


def _slice_poc_lsb(payload: bytes, nal_type: int, pps: dict[int, tuple[int, bool, bool, int]],
                   sps: dict[int, tuple[int, bool]]) -> int:
    """``slice_pic_order_cnt_lsb`` de la première tranche d'une image (0 pour une IDR)."""
    bits = _Bits(_rbsp(payload))
    if not bits.u(1):
        raise PocError("première tranche attendue")
    if _BLA_W_LP <= nal_type <= _IRAP_LAST:
        bits.u(1)                # no_output_of_prior_pics_flag
    pps_id = bits.ue()
    if pps_id not in pps:
        raise PocError(f"PPS {pps_id} absent")
    sps_id, _dependent, output_flag, extra_bits = pps[pps_id]
    if sps_id not in sps:
        raise PocError(f"SPS {sps_id} absent")
    log2_max_lsb, separate = sps[sps_id]
    bits.u(extra_bits)
    bits.ue()                    # slice_type
    if output_flag:
        bits.u(1)
    if separate:
        bits.u(2)
    if nal_type in (_IDR_W_RADL, _IDR_N_LP):
        return 0
    return bits.u(log2_max_lsb)


def iter_pictures(path: Path, *, cancelled: Callable[[], bool] | None = None,
                  progress: Callable[[int], None] | None = None) -> Iterator[tuple[tuple[int, int], frozenset[int]]]:
    """Images d'un HEVC brut en ordre de décodage : ((séquence codée, POC), types de NAL de l'unité).

    Le tri sur la clé donne l'ordre d'affichage. ``cancelled`` : :class:`HevcStreamCancelled` levée par le
    parcours ; :class:`PocError` si le flux est illisible.
    """
    sps: dict[int, tuple[int, bool]] = {}
    pps: dict[int, tuple[int, bool, bool, int]] = {}
    cvs, prev_tid0_poc, after_eos = -1, 0, True
    for unit in iter_hevc_access_units(Path(path), cancel_cb=cancelled, progress_cb=progress):
        kinds: set[int] = set()
        first_slice = None
        for nal in unit.nal_units:
            kinds.add(nal.nal_type)
            if nal.nal_type == _SPS:
                sps_id, log2, separate = _parse_sps(nal.payload)
                sps[sps_id] = (log2, separate)
            elif nal.nal_type == _PPS:
                pps_id, sps_id, dependent, output_flag, extra = _parse_pps(nal.payload)
                pps[pps_id] = (sps_id, dependent, output_flag, extra)
            elif nal.nal_type < 32 and nal.first_slice_in_pic and first_slice is None:
                first_slice = nal
            elif nal.nal_type == _EOS:
                after_eos = True
        if first_slice is None:
            continue                            # unité sans image (paramètres seuls)
        nal_type = first_slice.nal_type
        temporal_id = (first_slice.payload[1] & 0x07) - 1
        lsb = _slice_poc_lsb(first_slice.payload, nal_type, pps, sps)
        max_lsb = 1 << sps[pps[_slice_pps_id(first_slice.payload, nal_type)][0]][0]
        if _BLA_W_LP <= nal_type <= _IRAP_LAST and (nal_type != _CRA or after_eos or cvs < 0):
            cvs += 1                            # IRAP sans RASL à sortir : nouvelle séquence, MSB nul
            poc = lsb
        else:
            prev_lsb = prev_tid0_poc & (max_lsb - 1)
            prev_msb = prev_tid0_poc - prev_lsb
            if lsb < prev_lsb and prev_lsb - lsb >= max_lsb // 2:
                msb = prev_msb + max_lsb
            elif lsb > prev_lsb and lsb - prev_lsb > max_lsb // 2:
                msb = prev_msb - max_lsb
            else:
                msb = prev_msb
            poc = msb + lsb
        after_eos = False
        sub_layer_non_reference = nal_type <= 14 and nal_type % 2 == 0
        if temporal_id == 0 and not (_RADL_N <= nal_type <= _RASL_R) and not sub_layer_non_reference:
            prev_tid0_poc = poc
        yield (cvs, poc), frozenset(kinds)


def _slice_pps_id(payload: bytes, nal_type: int) -> int:
    bits = _Bits(_rbsp(payload, 16))
    bits.u(1)
    if _BLA_W_LP <= nal_type <= _IRAP_LAST:
        bits.u(1)
    return bits.ue()


__all__ = ["HevcStreamCancelled", "PocError", "iter_pictures"]
