"""Simulation du tone mapping d'un téléviseur HDR10 (EETF BT.2390) selon MaxCLL / MDCV.

Un téléviseur HDR10 choisit sa courbe d'après le pic annoncé du contenu (MaxCLL, à défaut le
MDCV) et sa propre luminosité. Annoncer trop bas écrête les hautes lumières au-delà ; trop haut
les compresse sans nécessité. La référence « dynamique » suit le pic réel de chaque image
(ce que font Dolby Vision, HDR10+ et les TV à analyse dynamique). Modèle de référence
documenté, pas le comportement exact d'un téléviseur donné.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from core.hdr_inspection.audit import Severity
from core.hdr_inspection.hdr10p import SATURATED_NITS
from core.hdr_inspection.inspector import InspectionReport
from core.hdr_inspection.measure import ImageMeasurement, histogram_share_above
from core.hdr_inspection.pq import nits_to_pq, pq12_to_nits, pq_to_nits

#: Assombrissement (EV, ≈ −10 % de luminance) à partir duquel une image compte comme « hautes lumières assombries ».
DARKENING_EV = 0.15


class TvModel(str, Enum):
    MAXCLL = "maxcll"    # courbe calée sur MaxCLL (MDCV à défaut)
    MDCV = "mdcv"        # MaxCLL ignorée, courbe calée sur le MDCV
    DYNAMIC = "dynamic"  # pic réel de chaque image (référence)


@dataclass(frozen=True, eq=False)
class FrameLevels:
    """Niveaux par image utilisés par la simulation (nits ; NaN : image ignorée)."""

    source: str        # "measure" | "hdr10plus" | "l1"
    peak: np.ndarray   # maxRGB maximal
    high: np.ndarray   # percentile 99,98 % de maxRGB (pic robuste)
    histogram: np.ndarray | None
    scene_starts: np.ndarray

    @property
    def frames(self) -> int:
        return int(self.peak.size)


def frame_levels(report: InspectionReport | None, measurement: ImageMeasurement | None = None) -> FrameLevels | None:
    """Mesure de l'image si disponible, sinon HDR10+ (MaxScl, 99,98 %), sinon L1 Dolby Vision (robuste)."""
    if measurement is not None and measurement.frames:
        return FrameLevels("measure", measurement.peaks().astype(np.float64),
                           measurement.percentiles[:, -1].astype(np.float64), measurement.histogram,
                           measurement.scene_starts)
    if report is None:
        return None
    hdr10plus = report.hdr10plus
    if hdr10plus is not None and hdr10plus.frames:
        peak = hdr10plus.max_scl.astype(np.float64)
        high = np.minimum(hdr10plus.p99.astype(np.float64), peak)
        saturated = peak >= SATURATED_NITS
        peak[saturated] = np.nan  # valeurs saturées : jamais un pic crédible
        high[saturated] = np.nan
        return FrameLevels("hdr10plus", peak, high, None, hdr10plus.scene_starts)
    dovi = report.dovi
    if dovi is not None and dovi.frames:
        peak = pq12_to_nits(dovi.l1_max)
        return FrameLevels("l1", peak, peak.copy(), None, dovi.scene_starts)
    return None


def eetf(nits: np.ndarray | float, source_peak: np.ndarray | float, display_peak: float) -> np.ndarray:
    """Luminance affichée (BT.2390, noir à 0) : genou à 1,5·m − 0,5, spline jusqu'au pic de l'écran.

    Au-delà du pic annoncé ``source_peak``, le signal est écrêté ; écran au moins aussi lumineux que
    le pic annoncé : aucune compression, écrêtage au pic de l'écran.
    """
    x = np.asarray(nits, dtype=np.float64)
    source = np.broadcast_to(np.asarray(source_peak, dtype=np.float64), x.shape)
    s = nits_to_pq(np.maximum(source, 1e-6))
    d = float(nits_to_pq(display_peak))
    max_lum = np.minimum(d / s, 1.0)
    e = np.clip(nits_to_pq(x) / s, 0.0, 1.0)
    ks = 1.5 * max_lum - 0.5
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(max_lum < 1.0, (e - ks) / (1.0 - ks), 0.0)
    spline = ((2 * t**3 - 3 * t**2 + 1) * ks + (t**3 - 2 * t**2 + t) * (1 - ks) + (-2 * t**3 + 3 * t**2) * max_lum)
    mapped = np.where((max_lum < 1.0) & (e >= ks), spline, e)
    shown = pq_to_nits(mapped * s)
    return np.where(max_lum >= 1.0, np.minimum(x, display_peak), shown)


def knee_nits(source_peak: float, display_peak: float) -> float | None:
    """Début de la compression des hautes lumières (nits), None sans compression."""
    s, d = float(nits_to_pq(source_peak)), float(nits_to_pq(display_peak))
    if d >= s:
        return None
    return float(pq_to_nits(max(0.0, 1.5 * d / s - 0.5) * s))


def model_source_peak(model: TvModel, max_cll: int | None, mdcv_max: float | None) -> float | None:
    """Pic supposé par le téléviseur ; None pour le modèle dynamique."""
    mdcv = mdcv_max if mdcv_max and mdcv_max > 0 else 1000.0
    if model is TvModel.MDCV:
        return float(mdcv)
    if model is TvModel.MAXCLL:
        return float(max_cll) if max_cll and max_cll > 0 else float(mdcv)
    return None


@dataclass(frozen=True, eq=False)
class ToneMapRisk:
    model: TvModel
    source_peak: float | None
    display_peak: float
    knee: float | None
    #: Seuil d'écrêtage : max(pic supposé, pic de l'écran) ; inf pour le modèle dynamique.
    clip_level: float
    frames: int
    clipped_frames: int          # au moins un pixel au-delà du seuil
    visible_clip_frames: int     # plus de 0,02 % des pixels au-delà (percentile 99,98 %)
    clipped_scenes: int
    clipped_pixels: float | None  # part des pixels du film (mesure de l'image seulement)
    worst_clip_ev: float
    darkened_frames: int
    darkening_median_ev: float
    darkening_p90_ev: float
    #: Pic affiché par image (nits), pour la courbe.
    displayed_peak: np.ndarray

    def severity(self) -> Severity:
        visible = self.visible_clip_frames / self.frames if self.frames else 0.0
        darkened = self.darkened_frames / self.frames if self.frames else 0.0
        if visible > 0.01 or self.worst_clip_ev > 1.0:
            return Severity.ERROR
        if self.clipped_frames or darkened > 0.05:
            return Severity.WARN
        return Severity.OK


def analyse(levels: FrameLevels, *, model: TvModel, max_cll: int | None, mdcv_max: float | None,
            display_peak: float) -> ToneMapRisk:
    """Écrêtage et assombrissement des hautes lumières face à la référence dynamique."""
    peak, high = levels.peak, levels.high
    valid = np.isfinite(peak) & np.isfinite(high)
    source = model_source_peak(model, max_cll, mdcv_max)
    dynamic_source = np.maximum(np.where(valid, peak, display_peak), 1e-3)
    ideal = eetf(np.where(valid, high, 0.0), dynamic_source, display_peak)
    if source is None:
        clip_level = float("inf")
        shown_high = ideal
        shown_peak = eetf(np.where(valid, peak, 0.0), dynamic_source, display_peak)
    else:
        clip_level = max(source, display_peak)
        shown_high = eetf(np.where(valid, high, 0.0), source, display_peak)
        shown_peak = eetf(np.where(valid, peak, 0.0), source, display_peak)
    clipped = valid & (peak > clip_level)
    visible = valid & (high > clip_level)
    scenes = 0
    if clipped.any() and levels.scene_starts.size:
        scenes = int(np.unique(np.searchsorted(levels.scene_starts, np.nonzero(clipped)[0], side="right") - 1).size)
    with np.errstate(divide="ignore", invalid="ignore"):
        darkening = np.where(valid & (ideal > 0.05) & (shown_high > 0), np.log2(ideal / shown_high), 0.0)
    darkened = darkening > DARKENING_EV
    worst = float(np.log2(np.nanmax(peak[clipped]) / clip_level)) if clipped.any() else 0.0
    return ToneMapRisk(
        model=model, source_peak=source, display_peak=display_peak,
        knee=knee_nits(source, display_peak) if source is not None else None,
        clip_level=clip_level, frames=int(valid.sum()),
        clipped_frames=int(clipped.sum()), visible_clip_frames=int(visible.sum()), clipped_scenes=scenes,
        clipped_pixels=(histogram_share_above(levels.histogram, clip_level)
                        if levels.histogram is not None and np.isfinite(clip_level) else None),
        worst_clip_ev=worst,
        darkened_frames=int(darkened.sum()),
        darkening_median_ev=float(np.median(darkening[darkened])) if darkened.any() else 0.0,
        darkening_p90_ev=float(np.percentile(darkening[darkened], 90)) if darkened.any() else 0.0,
        displayed_peak=np.where(valid, shown_peak, np.nan).astype(np.float32),
    )


def suggested_light_levels(report: InspectionReport | None,
                           measurement: ImageMeasurement | None = None) -> tuple[int, int | None, str] | None:
    """MaxCLL/MaxFALL proposés : mesure de l'image (exacts), sinon HDR10+ (hors saturation), sinon pic L1.

    MaxFALL inconnu (None) d'après le L1, moyenne en PQ et non en lumière linéaire.
    """
    if measurement is not None and measurement.frames:
        return measurement.max_cll(), measurement.max_fall(), "measure"
    if report is None:
        return None
    hdr10plus = report.hdr10plus
    if hdr10plus is not None and hdr10plus.frames:
        credible = hdr10plus.max_scl < SATURATED_NITS
        if credible.any():
            fall = float(hdr10plus.average_rgb[credible].max())
            return int(round(hdr10plus.credible_peak_nits())), int(round(fall)), "hdr10plus"
    if report.dovi is not None and report.dovi.frames:
        return int(round(report.dovi.l1_peak_nits())), None, "l1"
    return None
