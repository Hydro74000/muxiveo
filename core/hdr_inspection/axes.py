"""Axes des courbes de luminance (graphique du HDR Studio, rapport exporté) : plages et graduations.

Échelles : ``pq`` (PQ, plage ajustée aux valeurs), ``pq_fixed`` (PQ 0,1 – 10 000 nits), ``linear``
(linéaire, plage ajustée). Position verticale exprimée en part de la hauteur (0 = bas, 1 = haut).
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from core.hdr_inspection.pq import nits_to_pq

SCALES = (("pq", "PQ dynamique"), ("pq_fixed", "PQ fixe (0,1 – 10 000 nits)"), ("linear", "Linéaire dynamique"))
FIXED_TICKS = (0.1, 1, 10, 100, 200, 400, 600, 1000, 2000, 4000, 10000)
#: Graduations candidates (nits) des plages ajustées, éclaircies selon la place disponible.
NICE_TICKS = (0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 150, 200, 300, 400, 500, 600, 800,
              1000, 1200, 1500, 2000, 3000, 4000, 5000, 6000, 8000, 10000)


def fit_range(values: np.ndarray, scale: str, markers: Sequence[float] = ()) -> tuple[float, float]:
    """Plage (nits) couvrant les valeurs ; repères au plus deux fois au-dessus des données inclus."""
    if scale == "pq_fixed":
        return 0.1, 10000.0
    finite = np.asarray(values, dtype=np.float64)
    finite = finite[np.isfinite(finite) & (finite > 0)]
    if not finite.size:
        return (0.0 if scale == "linear" else 0.1), 1000.0
    high = float(finite.max())
    high = max([high] + [m for m in markers if m <= 2 * high])
    high = next((t for t in NICE_TICKS if t >= high * 1.05), 10000.0)
    if scale == "linear":
        return 0.0, high
    # Bas : 1er centile des valeurs (images très sombres ramenées au bord plutôt que d'écraser la plage).
    floor = float(np.percentile(finite, 1)) / 1.5
    low = next((t for t in reversed(NICE_TICKS) if t <= floor), NICE_TICKS[0])
    return min(low, high / 10), high


def position(nits: np.ndarray | float, low: float, high: float, scale: str) -> np.ndarray:
    """Part de la hauteur (0 – 1) ; valeurs hors plage ramenées aux bords."""
    values = np.clip(np.asarray(nits, dtype=np.float64), low, high)
    if scale == "linear":
        return (values - low) / max(high - low, 1e-9)
    base, top = float(nits_to_pq(low)), float(nits_to_pq(high))
    return (np.asarray(nits_to_pq(values)) - base) / max(top - base, 1e-9)


def ticks(low: float, high: float, scale: str, min_gap: float) -> list[float]:
    """Graduations dans la plage, espacées d'au moins ``min_gap`` (part de la hauteur)."""
    if scale == "linear":
        # Pas régulier (1, 2, 2,5 ou 5 × 10^n) : cinq à huit graduations.
        raw = (high - low) / 6
        power = 10 ** np.floor(np.log10(raw)) if raw > 0 else 1.0
        step = next((m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw), 10 * power)
        first = np.ceil(low / step) * step
        candidates = [round(float(v), 6) for v in np.arange(first, high + step / 2, step) if v <= high]
    else:
        candidates = [t for t in (FIXED_TICKS if scale == "pq_fixed" else NICE_TICKS) if low <= t <= high]
    kept: list[float] = []
    for tick in candidates:
        if not kept or float(position(tick, low, high, scale)) - float(position(kept[-1], low, high, scale)) >= min_gap:
            kept.append(tick)
    return kept
