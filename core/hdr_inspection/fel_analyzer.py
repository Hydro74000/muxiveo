"""FEL Analyzer : apport de la couche d'amélioration Dolby Vision (FEL) image par image.

Comparaison de l'image reconstruite (RPU + EL) à la couche de base passée par les courbes du RPU
sans résiduel (« mapped » : ce qu'affiche une TV Dolby Vision sans FEL, P8.1). Les deux images
sont produites par le plugin mvo-fel en Y'CbCr 4:2:0 10 bits, sans tramage, aux mêmes instants
échantillonnés (rafales de deux images pour la stabilité temporelle). Sept catégories, seuils
provisoires (à étalonner sur le corpus FEL) ; survie à l'encodage mesurée par un vrai encodage.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import IO, TYPE_CHECKING

import numpy as np

from core.hdr_inspection.fel_layer import LayerFacts
from core.hdr_inspection.pq import pq_to_nits
from core.i18n import translate_text
from core.subprocess_utils import subprocess_windows_no_window_kwargs

if TYPE_CHECKING:
    from core.fel.engine import CaptureSession
    from core.hdr_inspection.dovi import DoviAnalysis

#: Seuil de visibilité (JND) de chaque catégorie, dans son unité, en conditions critiques :
#: - ton, ombres : ΔE ITP (ITU-R BT.2124, 1 = JND) d'un code de luma 10 bits PQ plage limitée ≈ 0,82 ;
#: - couleur : ΔE ITP de l'écart moyen Cb/Cr filtré ≈ 2,2 par code (1 code Cb ≈ 1,1 JND, Cr ≈ 1,7) ;
#: - hautes lumières : 1 code ≈ 1,05 % au-delà de 500 nits, petite surface et compression de la TV (≈ × 2) ;
#: - détail : bande d − moyenne 5 × 5 pondérée par la CSF de Barten (spectre en 1/f, 1 hauteur d'image
#:   en UHD) : sensibilité ≈ 0,48 du pic → 1 JND ≈ 2,5 codes RMS (3,9 à 1,5 H, 10 à 3 H) ;
#: - grain : discrimination de contraste au-dessus du seuil (loi de puissance de Legge) ≈ 10 % ;
#: - stabilité : saut de luminance de toute l'image d'une image à la suivante ≈ 0,7 code.
JND: dict[str, float] = {"tone": 1.2, "highlights": 2.5, "detail": 2.5, "grain": 10.0, "colour": 0.45,
                         "shadows": 1.2, "temporal": 0.7}
#: Catégorie : libellé, unité, seuils faible/moyen et moyen/fort (valeur absolue) : moyen ≈ 1,5 JND
#: (perceptible en comparaison directe), fort ≈ 4 JND (perceptible sans référence).
CATEGORIES: tuple[tuple[str, str, str, tuple[float, float]], ...] = (
    ("tone", "Ton / exposition", "codes", (2.0, 5.0)),
    ("highlights", "Hautes lumières", "%", (4.0, 10.0)),
    ("detail", "Détail / texture", "codes RMS", (4.0, 10.0)),
    ("grain", "Grain", "%", (15.0, 40.0)),
    ("colour", "Couleur", "codes", (0.7, 1.8)),
    ("shadows", "Ombres", "codes", (2.0, 5.0)),
    ("temporal", "Stabilité temporelle", "codes", (1.0, 3.0)),
)
_STRONG = {key: thresholds[1] for key, _label, _unit, thresholds in CATEGORIES}
#: Apport global : écarts cumulés à chaque instant (sommation de probabilités, Minkowski β = 3 sur les
#: écarts en JND) ; catégories recouvrantes réduites à leur maximum (luminance : ton, ombres, hautes
#: lumières ; hautes fréquences : détail, grain) pour ne pas compter deux fois les mêmes pixels.
POOLING_BETA = 3.0
POOLING_GROUPS: tuple[tuple[str, ...], ...] = (("tone", "shadows", "highlights"), ("colour",), ("detail", "grain"),
                                               ("temporal",))
OVERALL_THRESHOLDS = (1.5, 4.0)
JND_NOTE = ("JND : seuil de visibilité en conditions critiques (ΔE ITP, ITU-R BT.2124 ; CSF de Barten pour le "
            "détail, vu à une hauteur d'image). Moyen ≈ 1,5 JND : perceptible en comparaison directe ; "
            "fort ≈ 4 JND : perceptible sans référence. Apport global : écarts cumulés au même instant "
            "(Minkowski β = 3), ombres et hautes lumières comptées avec le ton, grain avec le détail.")
LEVELS = ("faible", "moyen", "fort")
#: Plage limitée 10 bits : noir et blanc nominaux de la luma.
_BLACK, _RANGE = 64.0, 876.0
#: Hautes lumières mesurées seulement si le percentile 99,9 % atteint ce niveau.
_HIGHLIGHT_NITS = 100.0
#: Bandes de lignes des mesures (traitées en parallèle) : mémoire bornée et tous les cœurs occupés.
_BANDS = 16
#: Version des formules de mesure : un changement invalide les analyses en cache.
FEL_METRICS_VERSION = 1


@dataclass(frozen=True)
class Frame:
    """Image Y'CbCr 4:2:0 10 bits (codes)."""

    y: np.ndarray
    cb: np.ndarray
    cr: np.ndarray


@dataclass(frozen=True)
class FelSample:
    time_s: float
    metrics: dict[str, float]


@dataclass(frozen=True)
class CategorySummary:
    key: str
    median: float
    p90: float
    level: str
    #: p90 en multiples du seuil de visibilité (JND) de la catégorie.
    jnd: float = 0.0


@dataclass(frozen=True, eq=False)
class Moment:
    time_s: float
    score: float
    #: Vignettes RGB 8 bits (H × W × 3) : couche mapped, reconstruite, carte de l'écart.
    mapped: np.ndarray
    reconstructed: np.ndarray
    difference: np.ndarray


@dataclass(frozen=True)
class Survival:
    encoder: str
    bitrate_kbps: int
    #: Part de l'apport conservée après encodage (projection), basses et hautes fréquences.
    tone_kept: float
    detail_kept: float
    #: Erreur d'encodage (RMS haute fréquence, codes) face à l'apport de détail.
    encode_error: float
    detail: float


@dataclass(frozen=True, eq=False)
class FelReport:
    facts: LayerFacts | None
    samples: tuple[FelSample, ...]
    summary: tuple[CategorySummary, ...]
    moments: tuple[Moment, ...]
    survival: Survival | None = None
    notes: tuple[str, ...] = field(default=())

    def level(self, key: str) -> str:
        return next((s.level for s in self.summary if s.key == key), "faible")

    @property
    def overall(self) -> CategorySummary:
        """Apport global (JND cumulés par instant) : médiane, p90 et niveau."""
        return overall_summary(self.samples)


def _box(image: np.ndarray, radius: int) -> np.ndarray:
    """Moyenne glissante (2r + 1)² par sommes cumulées, bords répliqués."""
    padded = np.pad(image.astype(np.float64), radius + 1, mode="edge")
    c = padded.cumsum(0).cumsum(1)
    k = 2 * radius + 1
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k])[:image.shape[0], :image.shape[1]] / (k * k)


def _nits(codes: np.ndarray | float) -> np.ndarray:
    return pq_to_nits(np.clip((np.asarray(codes, dtype=np.float64) - _BLACK) / _RANGE, 0, 1))


def active_rows(frame: Frame, area: tuple[int, int, int, int] | None = None) -> slice:
    """Lignes actives : zone L5 (haut, bas) si connue, sinon lignes dont la luma dépasse le noir."""
    height = frame.y.shape[0]
    if area is not None and area[2] + area[3] < height:
        return slice(area[2], height - area[3])
    bright = np.flatnonzero(frame.y.mean(axis=1) > _BLACK + 4)
    return slice(int(bright[0]), int(bright[-1]) + 1) if bright.size else slice(0, height)


def _band_box_sums(image: np.ndarray, radius: int, lo: int, hi: int, dtype: type) -> np.ndarray:
    """Sommes (2r+1)² des lignes [lo, hi) d'une image entière, bords répliqués (comme ``_box``), exactes."""
    height, width = image.shape
    k = 2 * radius + 1
    vertical = np.cumsum(image[np.clip(np.arange(lo - radius, hi + radius), 0, height - 1)], axis=0, dtype=dtype)
    window = vertical[k - 1:].copy()
    window[1:] -= vertical[:-k]
    horizontal = np.cumsum(window[:, np.clip(np.arange(-radius, width + radius), 0, width - 1)], axis=1, dtype=dtype)
    sums = horizontal[:, k - 1:].copy()
    sums[:, 1:] -= horizontal[:, :-k]
    return sums


def _hist_percentile(histogram: np.ndarray, q: float) -> float:
    """``np.percentile`` (interpolation linéaire) de valeurs entières, exact, depuis leur histogramme."""
    count = int(histogram.sum())
    position = (count - 1) * q / 100.0
    below = int(np.floor(position))
    cumulative = np.cumsum(histogram)
    low = int(np.searchsorted(cumulative, below + 1))
    high = int(np.searchsorted(cumulative, min(below + 2, count)))
    return low + (high - low) * (position - below)


def _merged_std(parts: list[tuple[int, float, float]]) -> float:
    """Écart type de l'union de groupes (effectif, moyenne, somme des carrés des écarts) : algorithme de Chan."""
    count = sum(n for n, _mean, _m2 in parts)
    if count == 0:
        return 0.0
    mean = sum(n * mu for n, mu, _m2 in parts) / count
    m2 = sum(m2 + n * (mu - mean) ** 2 for n, mu, m2 in parts)
    return float(np.sqrt(m2 / count))


def _moments(values: np.ndarray) -> tuple[int, float, float]:
    if not values.size:
        return 0, 0.0, 0.0
    mean = float(values.mean())
    return int(values.size), mean, float(((values - mean) ** 2).sum())


def frame_metrics(reconstructed: Frame, mapped: Frame, rows: slice, *,
                  pool: Executor | None = None) -> dict[str, float]:
    """Mesures d'une paire d'images (même instant).

    Moyennes glissantes en sommes entières exactes, bandes de lignes traitées en parallèle (``pool``) et
    percentiles tirés d'histogrammes : mêmes valeurs que le calcul en float64 sur l'image entière, à
    l'ordre des additions près (≈ 1e-16 relatif).
    """
    r_full = reconstructed.y[rows].astype(np.int32)
    m_full = mapped.y[rows].astype(np.int32)
    d_full = r_full - m_full
    m2_full = m_full.astype(np.int64) ** 2
    height = r_full.shape[0]
    bands = max(1, min(_BANDS, height // 32))
    bounds = np.linspace(0, height, bands + 1, dtype=int)
    codes = int(max(r_full.max(initial=0), m_full.max(initial=0))) + 1

    def band(index: int) -> dict:
        lo, hi = int(bounds[index]), int(bounds[index + 1])
        r, m, d = r_full[lo:hi], m_full[lo:hi], d_full[lo:hi]
        low = _band_box_sums(d_full, 8, lo, hi, np.int32) / 289.0
        sum_r = _band_box_sums(r_full, 2, lo, hi, np.int32)
        sum_m = _band_box_sums(m_full, 2, lo, hi, np.int32)
        high = d - (sum_r - sum_m) / 25.0  # box(d) = box(r) − box(m), sommes entières exactes
        mean_m = sum_m / 25.0
        flat = _band_box_sums(m2_full, 2, lo, hi, np.int64) / 25.0 - mean_m ** 2 < 36.0
        dark = m < 300
        abs_low = np.abs(low)
        return {"tone": float(abs_low.sum()), "detail": float((high * high).sum()), "mean": int(d.sum(dtype=np.int64)),
                "r_hist": np.bincount(r.ravel(), minlength=codes), "m_hist": np.bincount(m.ravel(), minlength=codes),
                "flat": int(flat.sum()), "noise_r": _moments((r - sum_r / 25.0)[flat]),
                "noise_m": _moments((m - mean_m)[flat]), "dark": int(dark.sum()),
                "shadows": float(abs_low[dark].sum())}

    parts = list(pool.map(band, range(bands)) if pool is not None else map(band, range(bands)))
    size = d_full.size
    metrics = {
        "tone": sum(p["tone"] for p in parts) / size,
        "detail": float(np.sqrt(sum(p["detail"] for p in parts) / size)),
        "mean": sum(p["mean"] for p in parts) / size,
    }
    # Hautes lumières : percentile 99,9 % de la luma, en nits (images sans hautes lumières écartées).
    r_hist, m_hist = sum(p["r_hist"] for p in parts), sum(p["m_hist"] for p in parts)
    pr, pm = float(_nits(_hist_percentile(r_hist, 99.9))), float(_nits(_hist_percentile(m_hist, 99.9)))
    metrics["highlights"] = 100.0 * (pr - pm) / pm if pm >= _HIGHLIGHT_NITS else 0.0
    # Grain : bruit haute fréquence dans les aplats (faible variance locale de la couche mapped).
    if sum(p["flat"] for p in parts) / size > 0.05:
        noise_r = _merged_std([p["noise_r"] for p in parts])
        noise_m = _merged_std([p["noise_m"] for p in parts])
        metrics["grain"] = 100.0 * (noise_r / noise_m - 1.0) if noise_m > 0.05 else 0.0
    else:
        metrics["grain"] = 0.0
    chroma_rows = slice(rows.start // 2, (rows.stop + 1) // 2)

    def colour(pair: tuple[np.ndarray, np.ndarray]) -> float:
        a, b = pair
        diff = a[chroma_rows].astype(np.int32) - b[chroma_rows].astype(np.int32)
        return float(np.abs(_band_box_sums(diff, 4, 0, diff.shape[0], np.int32)).sum()) / (81.0 * diff.size)

    pairs = ((reconstructed.cb, mapped.cb), (reconstructed.cr, mapped.cr))
    metrics["colour"] = sum(pool.map(colour, pairs) if pool is not None else map(colour, pairs)) / 2
    dark = sum(p["dark"] for p in parts)
    metrics["shadows"] = sum(p["shadows"] for p in parts) / dark if dark / size > 0.02 else 0.0
    return metrics


def classify(value: float, thresholds: tuple[float, float]) -> str:
    value = abs(value)
    return LEVELS[0] if value < thresholds[0] else LEVELS[1] if value < thresholds[1] else LEVELS[2]


def summarise(samples: Sequence[FelSample]) -> tuple[CategorySummary, ...]:
    """Médiane et p90 par catégorie (valeurs absolues) ; niveau d'après le p90 (moments où le FEL agit)."""
    summary = []
    for key, _label, _unit, thresholds in CATEGORIES:
        values = np.array([abs(s.metrics.get(key, 0.0)) for s in samples], dtype=np.float64)
        if not values.size:
            summary.append(CategorySummary(key, 0.0, 0.0, LEVELS[0]))
            continue
        p90 = float(np.percentile(values, 90))
        summary.append(CategorySummary(key, float(np.median(values)), p90, classify(p90, thresholds), p90 / JND[key]))
    return tuple(summary)


def pooled_jnd(metrics: dict[str, float]) -> float:
    """Écart cumulé d'un instant, en JND (voir ``POOLING_GROUPS``)."""
    groups = [max(abs(metrics.get(key, 0.0)) / JND[key] for key in group) for group in POOLING_GROUPS]
    return float(sum(value ** POOLING_BETA for value in groups) ** (1 / POOLING_BETA))


def overall_summary(samples: Sequence[FelSample]) -> CategorySummary:
    values = np.array([pooled_jnd(s.metrics) for s in samples], dtype=np.float64)
    if not values.size:
        return CategorySummary("overall", 0.0, 0.0, LEVELS[0])
    p90 = float(np.percentile(values, 90))
    return CategorySummary("overall", float(np.median(values)), p90, classify(p90, OVERALL_THRESHOLDS), p90)


#: Codes stables des verdicts (CLI, scripts), du plus utile au moins utile.
VERDICTS = ("useful", "useful_if_bitrate", "useful_high_bitrate", "moderate", "useless")
#: Codes stables des niveaux (JSON) : faible / moyen / fort.
LEVEL_KEYS = {"faible": "weak", "moyen": "medium", "fort": "strong"}


def recommendation(report: FelReport) -> tuple[str, str]:
    """Verdict et explication (texte traduit)."""
    return verdict(report)[1:]


def verdict(report: FelReport) -> tuple[str, str, str]:
    """Code stable (``VERDICTS``), verdict et explication (texte traduit)."""
    if not report.samples and report.notes:
        # Rien mesuré (MEL, FEL vide) : la raison tient lieu d'explication.
        return "useless", translate_text("Intégrer le FEL : inutile"), report.notes[0]
    strong = [key for key, *_ in CATEGORIES if report.level(key) == LEVELS[2]]
    medium = [key for key, *_ in CATEGORIES if report.level(key) == LEVELS[1]]
    labels = {key: translate_text(label) for key, label, *_ in CATEGORIES}
    # Seuls le détail fin et le grain dépendent du débit d'encodage ; le reste (ton, couleur, stabilité) se voit.
    visible = [key for key in strong if key not in ("detail", "grain")]
    if visible:
        return ("useful", translate_text("Intégrer le FEL : utile"),
                translate_text("Apport fort : {categories}.", categories=", ".join(labels[k] for k in strong)))
    if strong:
        survival = report.survival
        if survival is not None and survival.detail_kept < 0.5:
            return ("useful_high_bitrate", translate_text("Intégrer le FEL : utile seulement à haut débit"),
                    translate_text("Apport de détail fin, à {kept} % conservé à {bitrate} kbit/s ({encoder}).",
                                   kept=round(survival.detail_kept * 100), bitrate=survival.bitrate_kbps,
                                   encoder=survival.encoder))
        return ("useful_if_bitrate", translate_text("Intégrer le FEL : utile si le débit le permet"),
                translate_text("Apport fort en détail fin ({categories}) ; tester la survie à l'encodage.",
                               categories=", ".join(labels[k] for k in strong)))
    # Catégories faibles prises une à une mais cumulées au même instant (moyennes comprises).
    overall = report.overall
    contributors = [s.key for s in report.summary if s.jnd >= 0.5]
    cumulated = ", ".join(labels[k] for k in contributors) or "—"
    if overall.level == LEVELS[2]:
        return ("useful", translate_text("Intégrer le FEL : utile"),
                translate_text("Apport global fort ({jnd} JND) par cumul : {categories}.", jnd=f"{overall.p90:.1f}",
                               categories=cumulated))
    if medium:
        return ("moderate", translate_text("Intégrer le FEL : apport modéré"),
                translate_text("Apport moyen : {categories}.", categories=", ".join(labels[k] for k in medium)))
    if overall.level == LEVELS[1]:
        return ("moderate", translate_text("Intégrer le FEL : apport modéré"),
                translate_text("Apport global moyen ({jnd} JND) par cumul : {categories}.",
                               jnd=f"{overall.p90:.1f}", categories=cumulated))
    return ("useless", translate_text("Intégrer le FEL : inutile"),
            translate_text("Apport négligeable : ce FEL se comporte comme un MEL."))


# ── Vignettes ────────────────────────────────────────────────────────────────
def _downscale(plane: np.ndarray, factor: int) -> np.ndarray:
    h, w = (plane.shape[0] // factor) * factor, (plane.shape[1] // factor) * factor
    return plane[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))


def _rgb_pq(y: np.ndarray, cb: np.ndarray, cr: np.ndarray) -> np.ndarray:
    """Y'CbCr BT.2020 (codes 10 bits plage limitée) → R'G'B' PQ normalisé (0 – 1)."""
    yn, cbn, crn = (y - _BLACK) / _RANGE, (cb - 512) / 896, (cr - 512) / 896
    rgb = np.stack([yn + 1.4746 * crn, yn - 0.16455 * cbn - 0.57135 * crn, yn + 1.8814 * cbn], axis=-1)
    return np.clip(rgb, 0, 1)


def _display(nits: np.ndarray, exposure_ev: float = 0.0) -> np.ndarray:
    """Aperçu SDR : exposition, compression douce des hautes lumières vers 100 nits, gamma 2,4 (RGB 8 bits)."""
    lifted = nits * 2.0 ** exposure_ev
    sdr = lifted / (1.0 + lifted / 300.0) / 75.0
    return (np.clip(sdr, 0, 1) ** (1 / 2.4) * 255).astype(np.uint8)


def thumbnail(frame: Frame, rows: slice, width: int = 480) -> np.ndarray:
    """Vignette SDR (RGB 8 bits) d'une image PQ : BT.2020 → luminance compressée vers 100 nits, gamma 2,4."""
    factor = max(2, frame.y.shape[1] // width)
    factor -= factor % 2
    y = _downscale(frame.y[rows].astype(np.float64), factor)
    chroma_rows = slice(rows.start // 2, (rows.stop + 1) // 2)
    cb = _downscale(frame.cb[chroma_rows].astype(np.float64), factor // 2)[:y.shape[0], :y.shape[1]]
    cr = _downscale(frame.cr[chroma_rows].astype(np.float64), factor // 2)[:y.shape[0], :y.shape[1]]
    return _display(pq_to_nits(_rgb_pq(y, cb, cr)))


#: Palette de l'écart absolu : noir → violet → jaune sur 0 – 16 codes, par pas de 1/16 de code.
_DIFFERENCE_STEPS = 16
_t = np.linspace(0.0, 1.0, 16 * _DIFFERENCE_STEPS + 1)
_DIFFERENCE_LUT = (np.stack([np.clip(2 * _t, 0, 1), np.clip(2 * _t - 1, 0, 1) * 0.9,
                             np.clip(1.2 * _t, 0, 1) * (1 - _t)], axis=-1) * 255).astype(np.uint8)
del _t


def _difference_colours(d: np.ndarray) -> np.ndarray:
    """Écart absolu (codes) → noir → violet → jaune, 16 codes au maximum."""
    index = np.minimum(np.abs(np.asarray(d, dtype=np.float32)) * _DIFFERENCE_STEPS, len(_DIFFERENCE_LUT) - 1)
    return _DIFFERENCE_LUT[index.astype(np.intp)]


def difference_map(reconstructed: Frame, mapped: Frame, rows: slice, width: int = 480) -> np.ndarray:
    """Carte de l'écart (basses fréquences, valeur absolue) : noir → violet → jaune, 16 codes au maximum."""
    factor = max(2, reconstructed.y.shape[1] // width)
    factor -= factor % 2
    return _difference_colours(_downscale(reconstructed.y[rows].astype(np.float64)
                                          - mapped.y[rows].astype(np.float64), factor))


# ── Comparateur pleine définition ────────────────────────────────────────────
@dataclass(frozen=True, eq=False)
class MomentImages:
    """Images pleine définition d'un moment : R'G'B' PQ sur 16 bits (H × W × 3) et carte de l'écart."""

    time_s: float
    mapped: np.ndarray
    reconstructed: np.ndarray
    difference: np.ndarray


def pq_codes(frame: Frame, rows: slice) -> np.ndarray:
    """R'G'B' PQ 16 bits pleine définition (chroma 4:2:0 étendue sur 2 × 2 pixels) des lignes actives."""
    start, stop = rows.start - rows.start % 2, min(frame.y.shape[0], rows.stop + rows.stop % 2)
    y = frame.y[start:stop].astype(np.float32)
    y -= _BLACK
    y /= _RANGE
    height, width = y.shape
    cb = (frame.cb[start // 2:stop // 2].astype(np.float32) - 512) / 896
    cr = (frame.cr[start // 2:stop // 2].astype(np.float32) - 512) / 896
    # Termes de chroma calculés en demi-définition, ajoutés par diffusion sur les blocs 2 × 2.
    blocks = np.empty((height // 2, 2, width // 2, 2, 3), np.float32)
    luma = y.reshape(height // 2, 2, width // 2, 2)
    for channel, term in enumerate((1.4746 * cr, -0.16455 * cb - 0.57135 * cr, 1.8814 * cb)):
        np.add(luma, term[:, None, :, None], out=blocks[..., channel])
    rgb = blocks.reshape(height, width, 3)[rows.start - start:rows.stop - start]
    np.clip(rgb, 0, 1, out=rgb)
    rgb *= 65535
    np.rint(rgb, out=rgb)
    return rgb.astype(np.uint16)


def _box3(image: np.ndarray) -> np.ndarray:
    """Moyenne 3 × 3 (float32, bords répliqués)."""
    padded = np.pad(image, 1, mode="edge")
    rows = padded[:-2] + padded[1:-1] + padded[2:]
    return (rows[:, :-2] + rows[:, 1:-1] + rows[:, 2:]) / 9


def moment_images(reconstructed: Frame, mapped: Frame, rows: slice, time_s: float) -> MomentImages:
    """Paire pleine définition du comparateur ; écart de luma lissé sur 3 × 3 pixels."""
    d = reconstructed.y[rows].astype(np.float32) - mapped.y[rows].astype(np.float32)
    return MomentImages(time_s, pq_codes(mapped, rows), pq_codes(reconstructed, rows), _difference_colours(_box3(d)))


@lru_cache(maxsize=16)
def _display_lut(exposure_ev: float) -> np.ndarray:
    return _display(pq_to_nits(np.arange(65536, dtype=np.float64) / 65535), exposure_ev)


def render_sdr(codes: np.ndarray, exposure_ev: float = 0.0) -> np.ndarray:
    """Aperçu SDR (RGB 8 bits) de codes R'G'B' PQ 16 bits, par table de correspondance (exposition en EV)."""
    return _display_lut(float(exposure_ev))[codes]


def luminance_nits(codes: np.ndarray) -> float:
    """Luminance BT.2020 (nits) d'un pixel R'G'B' PQ 16 bits."""
    rgb = pq_to_nits(np.asarray(codes, dtype=np.float64) / 65535)
    return float(0.2627 * rgb[0] + 0.6780 * rgb[1] + 0.0593 * rgb[2])


# ── Capture par le plugin ────────────────────────────────────────────────────
CaptureLayer = Callable[[str, float, int], list[Frame]]


class SessionCapture:
    """Capture par session du plugin (≥ 0.6.0) : les deux couches d'un seul décodage, source et GPU ouverts
    une fois. Rendu « exact » (certifié identique au CPU au bit près) : mêmes octets que la capture par
    couche. ``close`` libère la session."""

    def __init__(self, session: CaptureSession, width: int, height: int,
                 cancelled: threading.Event | None = None) -> None:
        self._session, self._width, self._height, self._cancelled = session, width, height, cancelled

    def pair(self, time_s: float, count: int) -> tuple[list[Frame], list[Frame]]:
        if self._cancelled is not None and self._cancelled.is_set():
            raise InterruptedError
        buffer = self._session.capture_pair(time_s, count)
        size = self._session.frame_size

        def frame(index: int) -> Frame:
            return _frames(buffer[index * size:(index + 1) * size], self._width, self._height)

        return [frame(2 * i) for i in range(count)], [frame(2 * i + 1) for i in range(count)]

    def __call__(self, layer: str, time_s: float, count: int) -> list[Frame]:
        if layer not in ("reconstructed", "mapped"):
            raise ValueError(f"Couche {layer} non capturée par la session")
        reconstructed, mapped = self.pair(time_s, count)
        return reconstructed if layer == "reconstructed" else mapped

    def backend(self) -> str:
        return self._session.backend()

    def close(self) -> None:
        self._session.close()


def close_capture(capture: object) -> None:
    """Libère la session d'une capture (sans effet pour la capture par couche)."""
    close = getattr(capture, "close", None)
    if callable(close):
        close()


def plugin_capture(engine: object, path: Path, stream: int, *, ffmpeg_bin: str, width: int, height: int,
                   frame_rate: str, chroma_location: str = "topleft", device: str = "cpu",
                   precision: str = "fast", threads: int = 8,
                   cancelled: threading.Event | None = None) -> CaptureLayer:
    """Fonction de capture : ``count`` images de la couche ``layer`` à partir de ``time_s``.

    Plugin ≥ 0.6.0 : session (``SessionCapture``) sur GPU en précision exacte (« cpu » demandé : choix
    automatique, l'initialisation du GPU n'étant payée qu'une fois), libérée par ``close_capture``.
    Sinon, une production par appel, CPU par défaut : pour quelques images, l'initialisation d'un
    contexte GPU coûte plus que le rendu (mesuré 0,7 s contre 1,7 s pour deux images UHD).
    """
    from core.fel.engine import CaptureSession, FelSource, capture_supported

    if capture_supported(engine):
        session = CaptureSession(engine, path, stream, frame_rate=frame_rate,  # type: ignore[arg-type]
                                 chroma_location=chroma_location, device="auto" if device == "cpu" else device,
                                 precision="exact", threads=threads)
        return SessionCapture(session, width, height, cancelled)

    frame_size = width * height * 3 // 2 * 2

    class _Plan:
        def acquire(self) -> tuple[str, Callable[[], None]]:
            return device, lambda: None

        def log(self, _level: str, _message: str) -> None:
            return None

    def capture(layer: str, time_s: float, count: int) -> list[Frame]:
        reader = subprocess.Popen(  # nosec B603 (outil configuré, arguments séparés)
            [ffmpeg_bin, "-v", "error", "-nostdin", "-f", "nut", "-i", "pipe:0", "-frames:v", str(count),
             "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            **subprocess_windows_no_window_kwargs(include_stdin=False))
        assert reader.stdin is not None and reader.stdout is not None
        source = FelSource(engine=engine, path=path, stream=stream, threads=threads,  # type: ignore[arg-type]
                           frame_rate=frame_rate, device_plan=_Plan(),  # type: ignore[arg-type]
                           start_us=int(round(max(0.0, time_s) * 1e6)), pixel_format="yuv420p10le",
                           chroma_location=chroma_location, dither="none", timestamps="zero",
                           precision=precision, layer=layer)
        producer = source.start(reader.stdin, cancelled or threading.Event())
        frames: list[Frame] = []
        try:
            for _ in range(count):
                data = reader.stdout.read(frame_size)
                if len(data) < frame_size:
                    break
                frames.append(_frames(data, width, height))
        finally:
            producer.cancel()
            reader.kill()
            reader.wait()
            producer.finish()
        if len(frames) < count:
            producer.check_error()
            raise RuntimeError(translate_text("Couche {layer} : images manquantes à {time} s.", layer=layer,
                                              time=f"{time_s:.1f}"))
        return frames

    return capture


def analyse(capture: CaptureLayer, times: Sequence[float], *,
            area: Callable[[float], tuple[int, int, int, int] | None] = lambda _t: None,
            facts: LayerFacts | None = None, progress: Callable[[float], None] | None = None,
            cancelled: Callable[[], bool] | None = None, moments: int = 5,
            previous: FelReport | None = None) -> FelReport:
    """Mesure chaque instant (rafale de deux images par couche) ; vignettes des moments les plus marqués.

    ``previous`` : instants déjà mesurés (cache, passe plus grossière de la grille emboîtée), repris tels
    quels et complétés par ``times``.
    """
    samples: list[FelSample] = list(previous.samples) if previous is not None else []
    # Vignettes calculées dès l'entrée d'un instant parmi les plus marqués (images pleines libérées).
    kept: list[Moment] = sorted(previous.moments, key=lambda item: -item.score) if previous is not None else []

    def pair(time_s: float) -> tuple[list[Frame], list[Frame]]:
        both = getattr(capture, "pair", None)
        if callable(both):  # plugin capable de livrer les deux couches d'un seul décodage
            return both(time_s, 2)
        return capture("reconstructed", time_s, 2), capture("mapped", time_s, 2)

    # Capture de l'instant suivant pendant les mesures (parallèles) de l'instant courant.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="fel-capture") as prefetch, \
            ThreadPoolExecutor(max_workers=_workers(), thread_name_prefix="fel-mesure") as pool:
        pending = prefetch.submit(pair, times[0]) if times else None
        for index, time_s in enumerate(times):
            if cancelled is not None and cancelled():
                if pending is not None:
                    pending.cancel()
                raise InterruptedError
            assert pending is not None
            reconstructed, mapped = pending.result()
            pending = prefetch.submit(pair, times[index + 1]) if index + 1 < len(times) else None
            metrics, score, rows = _sample_metrics(reconstructed, mapped, area(time_s), pool)
            samples.append(FelSample(time_s, metrics))
            if len(kept) < moments or score > kept[-1].score:
                recon, mapped_frame = reconstructed[0], mapped[0]
                kept.append(Moment(time_s, score, thumbnail(mapped_frame, rows), thumbnail(recon, rows),
                                   difference_map(recon, mapped_frame, rows)))
                kept = sorted(kept, key=lambda item: -item.score)[:moments]
            if progress is not None:
                progress((index + 1) / max(1, len(times)))
    samples.sort(key=lambda item: item.time_s)
    if facts is None and previous is not None:
        facts = previous.facts
    return FelReport(facts, tuple(samples), summarise(samples), tuple(sorted(kept, key=lambda m: m.time_s)))


def analyse_progressive(capture: CaptureLayer, duration_s: float, interval_s: float, *,
                        area: Callable[[float], tuple[int, int, int, int] | None] = lambda _t: None,
                        cached: FelReport | None = None,
                        facts: Callable[[], LayerFacts | None] | None = None,
                        progress: Callable[[float], None] | None = None,
                        partial: Callable[[FelReport], None] | None = None,
                        cancelled: Callable[[], bool] | None = None, moments: int = 5) -> FelReport:
    """Analyse par passes emboîtées (60 s, 30 s, puis la densité demandée) : instants en cache repris.

    ``partial`` reçoit le verdict provisoire après chaque passe sauf la dernière ; ``facts`` (relevé
    des couches, lancé en parallèle par l'appelant) n'est attendu qu'à la fin.
    """
    levels = sample_levels(duration_s, interval_s)
    grid = {time_s for level in levels for time_s in level}
    known = {s.time_s: s for s in (cached.samples if cached is not None else ()) if s.time_s in grid}
    reused = sorted(known.values(), key=lambda s: s.time_s)
    report = FelReport(None, tuple(reused), summarise(reused),
                       tuple(m for m in (cached.moments if cached is not None else ()) if m.time_s in grid))
    total = sum(1 for level in levels for time_s in level if time_s not in known)
    done = 0
    for index, level in enumerate(levels):
        todo = [time_s for time_s in level if time_s not in known]
        if todo:
            offset = done

            def level_progress(fraction: float, offset: int = offset, size: int = len(todo)) -> None:
                if progress is not None:
                    progress((offset + fraction * size) / max(1, total))

            report = analyse(capture, todo, area=area, previous=report, progress=level_progress,
                             cancelled=cancelled, moments=moments)
            done += len(todo)
        # Verdict provisoire seulement s'il reste des instants à mesurer (rien à attendre sinon).
        if partial is not None and report.samples and done < total:
            partial(report)
    layer_facts = facts() if facts is not None else None
    if layer_facts is None and cached is not None:
        layer_facts = cached.facts
    if progress is not None:
        progress(1.0)
    return dataclasses.replace(report, facts=layer_facts)


def _workers() -> int:
    return max(2, min(_BANDS, os.cpu_count() or 4))


def _sample_metrics(reconstructed: list[Frame], mapped: list[Frame], area: tuple[int, int, int, int] | None,
                    pool: Executor | None = None) -> tuple[dict[str, float], float, slice]:
    """Mesures d'un instant (première image de la rafale) et indice des moments marquants."""
    rows = active_rows(mapped[0], area)
    first = frame_metrics(reconstructed[0], mapped[0], rows, pool=pool)
    metrics = {key: first[key] for key in first if key != "mean"}
    # Stabilité : variation de l'écart moyen d'une image à la suivante (pompage corrigé par le FEL).
    following_diff = reconstructed[1].y[rows].astype(np.int64) - mapped[1].y[rows].astype(np.int64)
    following = float(following_diff.sum()) / following_diff.size
    metrics["temporal"] = abs(following - first["mean"])
    score = (metrics["tone"] / _STRONG["tone"] + metrics["detail"] / _STRONG["detail"]
             + abs(metrics["highlights"]) / _STRONG["highlights"])
    return metrics, score, rows


def area_at(dovi: DoviAnalysis | None, fps: float) -> Callable[[float], tuple[int, int, int, int] | None]:
    """Zone active L5 (gauche, droite, haut, bas) en vigueur à un instant, None sans L5."""

    def area(time_s: float) -> tuple[int, int, int, int] | None:
        if dovi is None or not dovi.active_areas:
            return None
        frame = int(time_s * (fps or 24))
        current = dovi.active_areas[0]
        for item in dovi.active_areas:
            if item.first_frame > frame:
                break
            current = item
        return (current.left, current.right, current.top, current.bottom)

    return area


def nothing_to_measure(dovi: DoviAnalysis) -> str | None:
    """Nature de la couche quand l'image reconstruite égale la lecture P8.1 (« MEL », « FEL vide »), sinon None."""
    if dovi.el_type == "FEL" and dovi.residual_active_frames:
        return None
    return "MEL" if dovi.el_type == "MEL" else translate_text("FEL vide")


#: Densités emboîtées : la grille de 10 s contient celle de 30 s, qui contient celle de 60 s.
COARSE_INTERVALS = (60, 30)


def sample_times(duration_s: float, interval_s: float, margin_s: float = 5.0) -> list[float]:
    """Instants de la grille ``marge + k × intervalle`` (marges du début et de la fin écartées).

    Grilles de même origine : celle d'un intervalle multiple d'un autre en est un sous-ensemble, ce qui
    permet de reprendre les instants déjà mesurés (cache, passe plus grossière).
    """
    if duration_s <= 2 * margin_s:
        return [max(0.0, duration_s / 2)]
    step = max(1.0, float(interval_s))
    count = max(1, int((duration_s - 2 * margin_s) // step) + 1)
    return [round(margin_s + index * step, 3) for index in range(count)]


def sample_levels(duration_s: float, interval_s: float, margin_s: float = 5.0) -> list[list[float]]:
    """Passes de la plus grossière à la plus fine : instants nouveaux de chaque densité emboîtée."""
    intervals = [coarse for coarse in COARSE_INTERVALS if coarse > interval_s and coarse % interval_s == 0]
    seen: set[float] = set()
    levels: list[list[float]] = []
    for step in [*intervals, interval_s]:
        level = [time_s for time_s in sample_times(duration_s, step, margin_s) if time_s not in seen]
        seen.update(level)
        if level:
            levels.append(level)
    return levels


# ── Survie à l'encodage ──────────────────────────────────────────────────────
def _encoder_args(encoder: str) -> tuple[list[str], list[str]]:
    """Options avant l'entrée (périphérique) et de sortie (format, préréglage) de l'encodeur de test."""
    if encoder in ("hevc_vaapi", "hevc_qsv"):
        from core.workflows.encode.hw_devices import select_linux_hwaccel_device

        node = select_linux_hwaccel_device(encoder) if sys.platform.startswith("linux") else None
        if encoder == "hevc_vaapi":
            return (["-vaapi_device", node] if node else []), ["-vf", "format=p010le,hwupload"]
        return (["-qsv_device", node] if node else []), ["-pix_fmt", "p010le"]
    if encoder == "hevc_nvenc":
        return [], ["-pix_fmt", "p010le", "-preset", "p5"]
    return [], ["-pix_fmt", "yuv420p10le", "-preset", "medium", "-x265-params", "log-level=error"]


def _frames(data: bytes, width: int, height: int) -> Frame:
    planes = np.frombuffer(data, dtype="<u2")
    luma = width * height
    return Frame(planes[:luma].reshape(height, width), planes[luma:luma * 5 // 4].reshape(height // 2, width // 2),
                 planes[luma * 5 // 4:].reshape(height // 2, width // 2))


def survival_indices(total: int) -> list[int]:
    """Huit images réparties dans la rafale (toutes si elle est plus courte) : base du calcul de survie."""
    return sorted({round(i * (total - 1) / 7) for i in range(8)}) if total > 8 else list(range(total))


def _streamed(cmd: list[str], feed: Callable[[IO[bytes]], object], consume: Callable[[IO[bytes]], object]) -> None:
    """Exécute ``cmd`` : entrée écrite par un fil, sortie lue par l'appelant, erreurs collectées."""
    process = subprocess.Popen(  # nosec B603 (outil configuré, arguments séparés)
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **subprocess_windows_no_window_kwargs(include_stdin=False))
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stdin, stderr = process.stdin, process.stderr
    errors: list[bytes] = []

    def write() -> None:
        try:
            feed(stdin)
        except (BrokenPipeError, OSError, ValueError):
            pass  # processus arrêté : son code de sortie et son journal font foi
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=write, name="fel-survival-in", daemon=True),
               threading.Thread(target=lambda: errors.append(stderr.read()), name="fel-survival-err", daemon=True)]
    for thread in threads:
        thread.start()
    try:
        consume(process.stdout)
    except BaseException:
        process.kill()
        raise
    finally:
        process.stdout.close()
        for thread in threads:
            thread.join()
        process.wait()
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, cmd[0], stderr=b"".join(errors))


def encode_round_trip(frames: Sequence[Frame], *, ffmpeg_bin: str, encoder: str, bitrate_kbps: int,
                      frame_rate: str, keep: Sequence[int] | None = None) -> list[Frame]:
    """Encode puis décode les images (HEVC 10 bits au débit demandé) ; mêmes dimensions.

    ``keep`` : rangs des images décodées conservées (toutes par défaut) ; les autres sont lues puis
    oubliées, et les images d'entrée transmises une à une (mémoire bornée en UHD).
    """
    height, width = frames[0].y.shape
    device, output = _encoder_args(encoder)
    common = ["-v", "error", "-nostdin"]
    chunks: list[bytes] = []

    def feed_frames(pipe: IO[bytes]) -> None:
        for frame in frames:
            for plane in (frame.y, frame.cb, frame.cr):
                pipe.write(plane.astype("<u2").tobytes())

    _streamed([ffmpeg_bin, *common, *device, "-f", "rawvideo", "-pix_fmt", "yuv420p10le", "-s", f"{width}x{height}",
               "-r", frame_rate, "-i", "pipe:0", *output, "-c:v", encoder, "-b:v", f"{bitrate_kbps}k",
               "-maxrate", f"{bitrate_kbps * 2}k", "-bufsize", f"{bitrate_kbps * 2}k", "-f", "hevc", "pipe:1"],
              feed_frames, lambda pipe: chunks.append(pipe.read()))
    encoded = b"".join(chunks)
    wanted = set(range(len(frames)) if keep is None else keep)
    size = width * height * 3 // 2 * 2
    result: list[Frame] = []

    def read_frames(pipe: IO[bytes]) -> None:
        index = 0
        while len(data := pipe.read(size)) == size:
            if index in wanted:
                result.append(_frames(data, width, height))
            index += 1

    _streamed([ffmpeg_bin, *common, "-f", "hevc", "-i", "pipe:0", "-f", "rawvideo", "-pix_fmt", "yuv420p10le",
               "pipe:1"], lambda pipe: pipe.write(encoded), read_frames)
    return result


def run_survival(capture: CaptureLayer, fel: FelReport, *, ffmpeg_bin: str, encoder: str, bitrate_kbps: int,
                 fps: float, area: Callable[[float], tuple[int, int, int, int] | None],
                 duration_s: float | None = None, progress: Callable[[str, int], None] | None = None) -> Survival:
    """Encode une seconde du moment le plus détaillé et mesure la part de l'apport conservée.

    La rafale reste dans la source (``duration_s``) : avancée ou raccourcie près de la fin d'un extrait.
    ``subprocess.CalledProcessError`` : encodeur indisponible (stderr dans l'exception).
    """
    from fractions import Fraction

    if not fel.samples:
        raise ValueError(translate_text("Aucun instant mesuré : analyser d'abord l'apport du FEL."))
    moment = max(fel.samples, key=lambda s: s.metrics.get("detail", 0.0))
    fps = fps or 24000 / 1001
    burst = max(4, int(round(fps)))
    start = moment.time_s
    if duration_s:
        # Marge de deux images : la dernière image annoncée par le conteneur n'est pas toujours décodable.
        burst = max(2, min(burst, int(duration_s * fps) - 2))
        start = max(0.0, min(start, duration_s - (burst + 2) / fps))
    if progress is not None:
        progress(translate_text("Capture des images…"), 10)
    both = getattr(capture, "pair", None)
    if callable(both):  # session : les deux couches d'un seul décodage
        reconstructed, burst_mapped = both(start, burst)
    else:
        reconstructed = capture("reconstructed", start, burst)
        burst_mapped = capture("mapped", start, burst)
    picked = survival_indices(len(reconstructed))
    mapped = [burst_mapped[i] for i in picked]
    del burst_mapped
    if progress is not None:
        progress(translate_text("Encodage de test…"), 50)
    rate = Fraction(fps).limit_denominator(1001)
    encoded = encode_round_trip(reconstructed, ffmpeg_bin=ffmpeg_bin, encoder=encoder, bitrate_kbps=bitrate_kbps,
                                frame_rate=f"{rate.numerator}/{rate.denominator}", keep=picked)
    return survival([reconstructed[i] for i in picked], mapped, encoded, encoder=encoder, bitrate_kbps=bitrate_kbps,
                    rows=active_rows(mapped[0], area(moment.time_s)))


def survival(reconstructed: Sequence[Frame], mapped: Sequence[Frame], encoded: Sequence[Frame], *,
             encoder: str, bitrate_kbps: int, rows: slice) -> Survival:
    """Part de l'apport du FEL conservée après encodage (projection de E − M sur R − M).

    Images alignées rang par rang : en pratique celles de ``survival_indices`` (l'encodage, lui,
    porte sur la rafale entière).
    """
    tone_num = tone_den = detail_num = detail_den = error = 0.0
    count = min(len(reconstructed), len(mapped), len(encoded))
    if count == 0:
        raise ValueError("Aucune image décodée après l'encodage de test.")
    with ThreadPoolExecutor(max_workers=_workers(), thread_name_prefix="fel-survie") as pool:
        for r_frame, m_frame, e_frame in zip(reconstructed, mapped, encoded):
            m = m_frame.y[rows].astype(np.int32)
            contribution = r_frame.y[rows].astype(np.int32) - m
            kept = e_frame.y[rows].astype(np.int32) - m
            height = contribution.shape[0]
            bands = max(1, min(_BANDS, height // 32))
            bounds = np.linspace(0, height, bands + 1, dtype=int)

            def band(index: int, contribution: np.ndarray = contribution, kept: np.ndarray = kept,
                     bounds: np.ndarray = bounds) -> tuple[float, float, float, float, float]:
                # Sommes entières exactes (même filtre que ``_box``), produits en float64.
                lo, hi = int(bounds[index]), int(bounds[index + 1])
                low_c = _band_box_sums(contribution, 8, lo, hi, np.int32) / 289.0
                low_k = _band_box_sums(kept, 8, lo, hi, np.int32) / 289.0
                high_c = contribution[lo:hi] - _band_box_sums(contribution, 2, lo, hi, np.int32) / 25.0
                high_k = kept[lo:hi] - _band_box_sums(kept, 2, lo, hi, np.int32) / 25.0
                noise = high_k - high_c
                return (float((low_c * low_k).sum()), float((low_c * low_c).sum()),
                        float((high_c * high_k).sum()), float((high_c * high_c).sum()), float((noise * noise).sum()))

            parts = list(pool.map(band, range(bands)))
            tone_num += sum(p[0] for p in parts)
            tone_den += sum(p[1] for p in parts)
            detail_num += sum(p[2] for p in parts)
            detail_den += sum(p[3] for p in parts)
            # Bruit d'encodage haute fréquence : (E − R) filtré = high_k − high_c (filtre linéaire).
            error += float(np.sqrt(sum(p[4] for p in parts) / contribution.size))
    detail = float(np.sqrt(detail_den / (count * reconstructed[0].y[rows].size)))
    return Survival(encoder, bitrate_kbps,
                    tone_kept=max(0.0, min(1.0, tone_num / tone_den)) if tone_den > 0 else 1.0,
                    detail_kept=max(0.0, min(1.0, detail_num / detail_den)) if detail_den > 0 else 1.0,
                    encode_error=error / count, detail=detail)
