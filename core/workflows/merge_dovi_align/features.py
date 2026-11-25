"""Vignettes ramenées sur une bande commune aux deux films, puis normalisées (structure seule).

Coordonnées d'image : origine au centre, unités en fraction de largeur (deux images de même largeur
affichée se superposent quelle que soit leur résolution). Hypothèse « même image, recadrée » : la bande
commune est l'intersection des zones actives ; un décalage vertical éventuel est estimé par
:func:`best_shift`.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .probe import VIGNETTE_H, VIGNETTE_W, Signatures

BAND_ROWS, BAND_COLS = 8, 16
#: Écart type (codes 10 bits) sous lequel une vignette est sans information (noire, uniforme).
MIN_STD = 6.0
#: Écart type sous lequel une vignette est plate (noire, uniforme) : seul un écart « informative / plate » est un
#: désaccord ; entre les deux (image sombre peu contrastée, étalonnage plus sombre de l'autre film), la paire
#: est jugée sur sa corrélation.
FLAT_STD = 2.0
#: Corrélation à partir de laquelle une image plate et une image informative montrent la même structure.
FLAT_CORRELATION = 0.5
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

    def intersect(self, other: "Band", margin: float = 0.01, margin_x: float | None = None) -> "Band":
        mx = margin if margin_x is None else margin_x
        return Band(max(self.top, other.top) + margin, min(self.bottom, other.bottom) - margin,
                    max(self.left, other.left) + mx, min(self.right, other.right) - mx)

    def shifted(self, dy: float, dx: float = 0.0) -> "Band":
        return Band(self.top + dy, self.bottom + dy, self.left + dx, self.right + dx)


def cell_margins(*signatures: Signatures) -> tuple[float, float]:
    """Marges (verticale, horizontale) d'une cellule de vignette, en fraction de largeur : une bande réduite
    d'autant n'interpole aucune cellule à cheval sur une barre."""
    vertical = max(sig.height / VIGNETTE_H / sig.width for sig in signatures)
    horizontal = max(1.0 / VIGNETTE_W for _sig in signatures)
    return vertical, horizontal


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
    features, contrast = band_features_contrast(signatures, band)
    return features, contrast >= MIN_STD


def pair_masks(first: np.ndarray, second: np.ndarray,
               correlation: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(paires comparables, paires en désaccord) d'après les contrastes des deux images : comparable si l'une est
    informative et l'autre non plate ; désaccord si l'une est informative et l'autre plate, sauf si la corrélation
    montre la même structure (``FLAT_CORRELATION`` : image très sombre d'un étalonnage plus sombre, comparable ;
    une image noire ne corrèle pas avec du contenu)."""
    strong1, strong2 = first >= MIN_STD, second >= MIN_STD
    flat1, flat2 = first < FLAT_STD, second < FLAT_STD
    comparable = (strong1 & ~flat2) | (strong2 & ~flat1)
    opposed = (strong1 & flat2) | (strong2 & flat1)
    if correlation is not None:
        structured = opposed & (correlation >= FLAT_CORRELATION)
        comparable |= structured
        opposed &= ~structured
    return comparable, opposed


def band_features_contrast(signatures: Signatures, band: Band) -> tuple[np.ndarray, np.ndarray]:
    """(vecteurs normalisés ``(N, 8 × 16)``, écart type de la vignette sur la bande en codes 10 bits)."""
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
    contrast = np.zeros(count, dtype=np.float32)
    for start in range(0, count, _CHUNK):  # mémoire bornée sur un film entier
        values = signatures.vignettes[start:start + _CHUNK].astype(np.float32) * 4.0  # codes 10 bits
        flat = np.einsum("rh,nhw,cw->nrc", wr, values, wc).reshape(values.shape[0], -1)
        flat -= flat.mean(axis=1, keepdims=True)
        norms = np.linalg.norm(flat, axis=1)
        contrast[start:start + _CHUNK] = norms / np.sqrt(flat.shape[1])
        features[start:start + _CHUNK] = flat / np.where(norms > 1e-6, norms, 1.0)[:, None]
    return features, contrast


def _pair_scores(base: np.ndarray, informative: np.ndarray, second: Signatures, band: Band,
                 shifts: list[tuple[float, float]]) -> np.ndarray:
    """Corrélation par image (images × décalages) de ``second`` décalé face à ``base``."""
    out = np.full((base.shape[0], len(shifts)), np.nan, dtype=np.float32)
    for column, (dx, dy) in enumerate(shifts):
        other, informative_other = band_features(second, band.shifted(dy, dx))
        mask = informative & informative_other
        out[mask, column] = np.sum(base[mask] * other[mask], axis=1)
    return out


def _column_means(scores: np.ndarray) -> np.ndarray:
    valid = ~np.isnan(scores)
    counts = valid.sum(axis=0)
    sums = np.where(valid, scores, 0.0).sum(axis=0)
    return np.where(counts > 0, sums / np.maximum(counts, 1), -1.0)


def best_shift(first: Signatures, second: Signatures, band: Band, groups: list[np.ndarray], *,
               steps: int = 4, step: float = 0.01) -> tuple[float, float, float, float]:
    """Translation ``(dx, dy)`` de ``second`` (fraction de largeur) maximisant la corrélation avec ``first``
    sur des paires d'images de même rang (recherche à ``step`` puis affinée au quart de pas), et cohérence
    d'une paire à l'autre.

    ``groups`` : indices des images de chaque paire (une coupe). Rend (dx, dy, corrélation moyenne, part des
    paires dont le meilleur décalage grossier est à un pas au plus du décalage global).
    """
    base, informative = band_features(first, band)
    coarse = [(kx * step, ky * step) for ky in range(-steps, steps + 1) for kx in range(-steps, steps + 1)]
    scores = _pair_scores(base, informative, second, band, coarse)
    totals = _column_means(scores)
    best = int(np.argmax(totals))
    cx, cy = coarse[best]
    fine_step = step / 4
    fine = [(cx + kx * fine_step, cy + ky * fine_step) for ky in range(-3, 4) for kx in range(-3, 4)]
    fine_totals = _column_means(_pair_scores(base, informative, second, band, fine))
    fine_best = int(np.argmax(fine_totals))
    dx, dy = fine[fine_best]
    agree = 0
    counted = 0
    for group in groups:
        local = scores[group]
        if not np.any(~np.isnan(local)):
            continue
        counted += 1
        lx, ly = coarse[int(np.argmax(_column_means(local)))]
        if abs(lx - cx) <= step + 1e-9 and abs(ly - cy) <= step + 1e-9:
            agree += 1
    return dx, dy, float(fine_totals[fine_best]), (agree / counted if counted else 0.0)
