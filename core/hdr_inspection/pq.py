"""Courbe PQ (SMPTE ST 2084) vectorisée."""
from __future__ import annotations

import numpy as np

_M1, _M2 = 2610 / 16384, 2523 / 4096 * 128
_C1, _C2, _C3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32


def pq_to_nits(signal: np.ndarray | float) -> np.ndarray:
    """Signal PQ normalisé (0–1) → nits."""
    value = np.clip(np.asarray(signal, dtype=np.float64), 0.0, 1.0)
    power = value ** (1.0 / _M2)
    return 10000.0 * (np.maximum(power - _C1, 0.0) / (_C2 - _C3 * power)) ** (1.0 / _M1)


def nits_to_pq(nits: np.ndarray | float) -> np.ndarray:
    """Nits → signal PQ normalisé (0–1)."""
    y = np.clip(np.asarray(nits, dtype=np.float64) / 10000.0, 0.0, 1.0) ** _M1
    return ((_C1 + _C2 * y) / (1.0 + _C3 * y)) ** _M2


def pq12_to_nits(codes: np.ndarray | int) -> np.ndarray:
    """Codes PQ 12 bits des métadonnées Dolby Vision (L1, L2, L6…) → nits."""
    return pq_to_nits(np.asarray(codes, dtype=np.float64) / 4095.0)


def nice_nits(nits: float) -> int:
    """Nits arrondis ; dizaine ronde si elle a le même code PQ 12 bits (écart de quantification)."""
    value = float(nits)
    tens = round(value / 10.0) * 10
    if value >= 100 and int(round(float(nits_to_pq(tens)) * 4095)) == int(round(float(nits_to_pq(value)) * 4095)):
        return int(tens)
    return int(round(value))
