"""Compositeur Dolby Vision de référence (numpy), indépendant du plugin mvo-fel : oracle des tests d'intégration.

Écrit d'après les champs du RPU exportés par ``dovi_tool info -f N`` (JSON) : courbes de mapping par
composante (polynômes par morceaux ou MMR d'ordre 1 à 3) puis résiduel NLQ à zone morte linéaire. Valeurs
normalisées sur [0, 1] (codes 10 bits / 1023) ; le résultat est rendu en codes 10 bits. Volontairement simple :
la chroma prend la luminance moyenne 2 × 2 de son site et l'EL est suréchantillonné au plus proche ; seuls la
moyenne et la structure sont comparées au plugin, pas l'égalité au code près.
"""
from __future__ import annotations

from typing import Any

import numpy as np

Planes = tuple[np.ndarray, np.ndarray, np.ndarray]


def _value(integer: int, fraction: int, denominator: int) -> float:
    return integer + fraction / denominator


def _pivots(curve: dict[str, Any], bl_max: float) -> np.ndarray:
    return np.cumsum(np.asarray(curve["pivots"], dtype=np.float64)) / bl_max


def _piece(signal: np.ndarray, pivots: np.ndarray, count: int) -> np.ndarray:
    """Morceau de chaque échantillon ; le dernier pivot (fin de plage) peut être implicite."""
    return np.clip(np.searchsorted(pivots, signal, side="right") - 1, 0, max(1, count) - 1)


def _component(curve: dict[str, Any], own: np.ndarray, sites: tuple[np.ndarray, np.ndarray, np.ndarray],
               denominator: int, bl_max: float) -> np.ndarray:
    pieces = len(curve.get("poly_order_minus1") or curve.get("mmr_order_minus1") or [0])
    piece = _piece(own, _pivots(curve, bl_max), pieces)
    out = np.zeros_like(own)
    for index in range(pieces):
        mask = piece == index
        if not mask.any():
            continue
        if curve["mapping_idc"] == "Polynomial":
            coefs = [_value(i, f, denominator) for i, f in zip(curve["poly_coef_int"][index], curve["poly_coef"][index])]
            x = own[mask]
            out[mask] = sum(c * x ** power for power, c in enumerate(coefs))
        else:
            s0, s1, s2 = (site[mask] for site in sites)
            terms = (s0, s1, s2, s0 * s1, s0 * s2, s1 * s2, s0 * s1 * s2)
            result = np.full(s0.shape, _value(curve["mmr_constant_int"][index], curve["mmr_constant"][index],
                                              denominator))
            for order in range(curve["mmr_order_minus1"][index] + 1):
                coefs = [_value(i, f, denominator) for i, f in zip(curve["mmr_coef_int"][index][order],
                                                                    curve["mmr_coef"][index][order])]
                result += sum(c * t ** (order + 1) for c, t in zip(coefs, terms))
            out[mask] = result
    return np.clip(out, 0.0, 1.0)


def mapped(bl: Planes, rpu: dict[str, Any]) -> Planes:
    """BL + courbes du RPU (sans résiduel), en codes 10 bits."""
    header, mapping = rpu["header"], rpu["rpu_data_mapping"]
    denominator = 1 << int(header["coefficient_log2_denom"])
    bl_max = float((1 << (int(header["bl_bit_depth_minus8"]) + 8)) - 1)
    y, u, v = (plane / bl_max for plane in bl)
    y_sites = (y[0::2, 0::2] + y[0::2, 1::2] + y[1::2, 0::2] + y[1::2, 1::2]) / 4
    curves = mapping["curves"]
    out_y = _component(curves[0], y, (y, np.repeat(np.repeat(u, 2, 0), 2, 1), np.repeat(np.repeat(v, 2, 0), 2, 1)),
                       denominator, bl_max)
    out_u = _component(curves[1], u, (y_sites, u, v), denominator, bl_max)
    out_v = _component(curves[2], v, (y_sites, u, v), denominator, bl_max)
    return out_y * 1023, out_u * 1023, out_v * 1023


def residual(el: Planes, rpu: dict[str, Any], shape: tuple[int, int]) -> Planes:
    """Résiduel NLQ (zone morte linéaire) suréchantillonné à la taille de la BL, en codes 10 bits."""
    header, nlq = rpu["header"], rpu["rpu_data_mapping"]["nlq"]
    denominator = 1 << int(header["coefficient_log2_denom"])
    planes = []
    for index, plane in enumerate(el):
        target = shape if index == 0 else (shape[0] // 2, shape[1] // 2)
        factor = (target[0] // plane.shape[0], target[1] // plane.shape[1])
        e = np.repeat(np.repeat(plane, factor[0], 0), factor[1], 1) - nlq["nlq_offset"][index]
        slope = _value(nlq["linear_deadzone_slope_int"][index], nlq["linear_deadzone_slope"][index], denominator)
        threshold = _value(nlq["linear_deadzone_threshold_int"][index], nlq["linear_deadzone_threshold"][index],
                           denominator)
        limit = _value(nlq["vdr_in_max_int"][index], nlq["vdr_in_max"][index], denominator)
        value = np.where(e == 0, 0.0, np.sign(e) * (np.abs(e) * slope + threshold))
        planes.append(np.clip(value, -limit, limit) * 1023)
    return planes[0], planes[1], planes[2]
