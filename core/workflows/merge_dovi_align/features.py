"""Vignettes ramenées sur une bande commune aux deux films, puis normalisées (structure seule).

Coordonnées d'image : origine au centre, unités en fraction de largeur (deux images de même largeur
affichée se superposent quelle que soit leur résolution). Hypothèse « même image, recadrée » : la bande
commune est l'intersection des zones actives ; un décalage vertical éventuel est estimé par
:func:`best_vertical_shift`.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .probe import VIGNETTE_H, VIGNETTE_W, Signatures

BAND_ROWS, BAND_COLS = 8, 16
#: Écart type (codes 10 bits) sous lequel une vignette est sans information (noire, uniforme).
MIN_STD = 6.0
_CHUNK = 8192


@dataclass(frozen=True)
class Band:
    """Rectangle en coordonnées d'image (fraction de largeur, centre en 0)."""

    top: float
    bottom: float
    left: float
    right: float

    @property
    def empty(self) -> bool:
        return self.bottom - self.top <= 0.02 or self.right - self.left <= 0.02

    def intersect(self, other: "Band", margin: float = 0.01) -> "Band":
        return Band(max(self.top, other.top) + margin, min(self.bottom, other.bottom) - margin,
                    max(self.left, other.left) + margin, min(self.right, other.right) - margin)

    def shifted(self, dy: float) -> "Band":
        return Band(self.top + dy, self.bottom + dy, self.left, self.right)


def active_band(signatures: Signatures) -> Band:
    """Zone active habituelle d'un film : barres atteintes par au moins 10 % des images non noires."""
    valid = signatures.top >= 0
    width, height = signatures.width, signatures.height
    if not np.any(valid):
        return Band(-height / (2 * width), height / (2 * width), -0.5, 0.5)

    def edge(values: np.ndarray) -> float:
        return float(np.percentile(values[valid], 10))

    top, bottom = edge(signatures.top), edge(signatures.bottom)
    left, right = edge(signatures.left), edge(signatures.right)
    return Band((top - height / 2) / width, (height / 2 - bottom) / width,
                (left - width / 2) / width, (width / 2 - right) / width)


def _interp_matrix(centers: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Matrice (cibles × sources) d'interpolation linéaire, bornée aux sources extrêmes."""
    eye = np.eye(centers.size, dtype=np.float64)
    return np.stack([np.interp(targets, centers, eye[:, k]) for k in range(centers.size)], axis=1)


def band_features(signatures: Signatures, band: Band) -> tuple[np.ndarray, np.ndarray]:
    """(vecteurs normalisés ``(N, 8 × 16)``, informatif par image) sur ``band`` pour ces signatures."""
    width, height = signatures.width, signatures.height
    rows = (np.arange(VIGNETTE_H) + 0.5) * height / VIGNETTE_H
    cols = (np.arange(VIGNETTE_W) + 0.5) * width / VIGNETTE_W
    row_centers = (rows - height / 2) / width
    col_centers = (cols - width / 2) / width
    target_rows = band.top + (np.arange(BAND_ROWS) + 0.5) * (band.bottom - band.top) / BAND_ROWS
    target_cols = band.left + (np.arange(BAND_COLS) + 0.5) * (band.right - band.left) / BAND_COLS
    wr = _interp_matrix(row_centers, target_rows).astype(np.float32)
    wc = _interp_matrix(col_centers, target_cols).astype(np.float32)
    count = len(signatures)
    features = np.zeros((count, BAND_ROWS * BAND_COLS), dtype=np.float32)
    informative = np.zeros(count, dtype=bool)
    for start in range(0, count, _CHUNK):  # mémoire bornée sur un film entier
        values = signatures.vignettes[start:start + _CHUNK].astype(np.float32) * 4.0  # codes 10 bits
        flat = np.einsum("rh,nhw,cw->nrc", wr, values, wc).reshape(values.shape[0], -1)
        flat -= flat.mean(axis=1, keepdims=True)
        norms = np.linalg.norm(flat, axis=1)
        informative[start:start + _CHUNK] = norms / np.sqrt(flat.shape[1]) >= MIN_STD
        features[start:start + _CHUNK] = flat / np.where(norms > 1e-6, norms, 1.0)[:, None]
    return features, informative


def best_vertical_shift(first: Signatures, second: Signatures, band: Band, *,
                        steps: int = 6, step: float = 0.01) -> tuple[float, float]:
    """Décalage vertical de ``second`` (fraction de largeur) maximisant la corrélation moyenne avec ``first``
    sur des paires d'images de même rang ; (décalage, corrélation)."""
    base, informative = band_features(first, band)
    best = (0.0, -1.0)
    for k in range(-steps, steps + 1):
        moved = band.shifted(k * step)
        other, informative_other = band_features(second, moved)
        mask = informative & informative_other
        if not np.any(mask):
            continue
        score = float(np.mean(np.sum(base[mask] * other[mask], axis=1)))
        if score > best[1] + 1e-6 or (abs(score - best[1]) <= 1e-6 and abs(k * step) < abs(best[0])):
            best = (k * step, score)
    return best
