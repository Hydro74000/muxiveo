"""Métadonnées HDR10+ générées depuis le Dolby Vision de la source (option « Générer HDR10+ »).

Provenance des statistiques par image, de la plus exacte à la plus approchée (``resolve_generation_method``) :

- ``fel_statistics`` : image FEL reconstruite, mesurée par le plugin mvo-fel pendant l'encodage (sortie HEVC) ;
- ``measure_reconstructed`` : la même image mesurée avant l'encodage (sortie AV1 : JSON exigé au départ) ;
- ``measure`` : image de la source (couche ``base`` : image encodée hors FEL), mesure complète du plugin
  (≥ 0.4.0) ; plans = drapeaux scene-refresh du RPU (``scene_start`` des statistiques) ;
- ``l1`` : repli sans mesure (extension absente, profil 5) : répartition de maxRGB modélisée depuis le L1 du RPU
  (min / moyenne / max en PQ) — approximatif.

Le document produit (profil A, mêmes règles d'agrégation que le HDR10+ recalculé FEL, ``core/fel/hdr10plus.py``)
est indexé sur les images de la source, comme un HDR10+ extrait par ``hdr10plus_tool`` : l'expansion
d'interpolation, l'audit des trames et l'injection s'appliquent ensuite sans distinction.

Annulation : ``InterruptedError`` (lecteurs et boucles Python), ``FelCancelled`` (plugin) ; jamais de repli L1.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from core.dovi_profile_detector import DoviSubProfile
from core.fel.hdr10plus import FrameRecord, FrameStatistics, SceneRecord, describe, write_hdr10plus_json
from core.hdr_inspection.cache import InspectionCache
from core.hdr_inspection.dovi import DoviAnalysis, parse_dovi_export
from core.hdr_inspection.measure import ImageMeasurement, measure_track
from core.hdr_inspection.pq import pq_to_nits
from core.i18n import translate_text

METHOD_FEL_STATISTICS = "fel_statistics"
METHOD_MEASURE_RECONSTRUCTED = "measure_reconstructed"
METHOD_MEASURE = "measure"
METHOD_L1 = "l1"
#: Méthodes dont le JSON est écrit avant l'encodage (``fel_statistics`` : après, par le runtime).
PREPARED_METHODS = frozenset({METHOD_MEASURE_RECONSTRUCTED, METHOD_MEASURE, METHOD_L1})
#: Outil inscrit dans ``ToolInfo`` du JSON généré.
TOOL_NAME = "Muxiveo (DV → HDR10+)"

#: Exposant des hautes lumières du modèle L1 (provisoire : à étalonner sur des films mesurés, RPU + mesure).
L1_HIGHLIGHT_EXPONENT = 3.0
#: Positions des percentiles de maxRGB des statistiques (1, 25, 50, 75, 90, 95, 99,98 %).
_PERCENTILE_POSITIONS = np.array([0.01, 0.25, 0.5, 0.75, 0.90, 0.95, 0.9998])
#: Quantiles médians de l'intégration de la moyenne linéaire et de la part ≤ 100 nits.
_GRID = (np.arange(128) + 0.5) / 128
_CHUNK = 8192


class GenerationError(RuntimeError):
    """HDR10+ impossible à générer (mesure inexploitable, RPU sans L1)."""


@dataclass(frozen=True)
class MethodChoice:
    method: str
    #: Raison lisible (journal), déjà traduite.
    reason: str

    @property
    def approximate(self) -> bool:
        return self.method == METHOD_L1


@dataclass(frozen=True)
class GeneratedHdr10Plus:
    path: Path
    method: str
    frames: int
    scenes: int
    #: Résumé du journal (images, plans, MaxCLL/MaxFALL, pic).
    summary: str


def _check(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise InterruptedError


def engine_measure_layers() -> frozenset[str]:
    """Couches mesurables par l'extension mvo-fel installée (vide : absente, incompatible ou < 0.4.0)."""
    try:
        from core.fel.engine import FelEngine, measure_supported

        engine = FelEngine.installed()
    except Exception:  # noqa: BLE001 — extension absente ou illisible : repli L1 annoncé par l'appelant
        return frozenset()
    return frozenset(layer for layer in ("base", "reconstructed") if measure_supported(engine, layer))


def resolve_generation_method(*, sub_profile: DoviSubProfile, fel_bake: bool, fel_statistics: bool,
                              hevc_output: bool, measure_layers: frozenset[str]) -> MethodChoice:
    """Provenance des statistiques HDR10+ d'une piste (règle unique : préparation, aperçu, journal).

    ``fel_bake`` : FEL intégré à l'image encodée ; ``fel_statistics`` : le plugin FEL mesure l'image produite
    (capacité ``frame_statistics``) ; ``measure_layers`` : couches mesurables avant l'encodage.
    """
    if fel_bake:
        if hevc_output and fel_statistics:
            return MethodChoice(METHOD_FEL_STATISTICS, translate_text(
                "image FEL reconstruite, mesurée pendant l'encodage"))
        if "reconstructed" in measure_layers:
            return MethodChoice(METHOD_MEASURE_RECONSTRUCTED, translate_text(
                "image FEL reconstruite, mesurée avant l'encodage"))
        # Le L1 décrit le master Dolby Vision, donc l'image reconstruite : plus juste que la mesure de la BL.
        return MethodChoice(METHOD_L1, translate_text(
            "estimation depuis le L1 du RPU (mesure de l'image reconstruite indisponible)"))
    if sub_profile == DoviSubProfile.P5:
        return MethodChoice(METHOD_L1, translate_text(
            "estimation depuis le L1 du RPU (profil 5 : aucune couche HDR10 à mesurer)"))
    if "base" in measure_layers:
        return MethodChoice(METHOD_MEASURE, translate_text("mesure de chaque image de la source"))
    return MethodChoice(METHOD_L1, translate_text(
        "estimation depuis le L1 du RPU (extension mvo-fel 0.4.0 ou plus récente absente)"))


def with_scenes(statistics: FrameStatistics, starts: object) -> FrameStatistics:
    """Statistiques re-découpées aux débuts de plans ``starts`` (0 ajouté, hors bornes écartés)."""
    count = len(statistics.frames)
    first = sorted({int(s) for s in np.asarray(starts).tolist() if 0 <= int(s) < count} | {0})
    bounds = [*first, count]
    scenes = tuple(SceneRecord(start, end - start) for start, end in zip(bounds, bounds[1:]))
    marks = set(first)
    frames = tuple(replace(frame, scene_start=index in marks) for index, frame in enumerate(statistics.frames))
    return FrameStatistics(statistics.width, statistics.height, frames, scenes)


def statistics_from_measurement_scenes(measurement: ImageMeasurement, *,
                                       cancelled: Callable[[], bool] | None = None) -> FrameStatistics:
    """Statistiques d'une mesure complète, plans du RPU (sinon coupes détectées dans la mesure)."""
    from core.fel.hdr10plus import statistics_from_measurement
    from core.hdr_inspection.realign import scene_cuts

    if measurement.is_estimate:
        raise GenerationError(translate_text(
            "Mesure rapide (images clés) : une mesure complète est requise pour générer le HDR10+."))
    return statistics_from_measurement(measurement, scene_cuts(measurement), cancelled=cancelled)


def _l1_quantiles(low: np.ndarray, median: np.ndarray, high: np.ndarray, positions: np.ndarray,
                  exponent: float) -> np.ndarray:
    """Quantiles PQ du modèle (images × positions) : linéaire du min à la médiane, puis vers le max en
    puissance ``exponent`` (hautes lumières rares)."""
    p = positions[np.newaxis, :]
    lower = low[:, None] + (median - low)[:, None] * np.minimum(p, 0.5) * 2.0
    upper = median[:, None] + (high - median)[:, None] * np.clip(2.0 * p - 1.0, 0.0, None) ** exponent
    return np.where(p <= 0.5, lower, upper)


def statistics_from_l1(dovi: DoviAnalysis, *, exponent: float = L1_HIGHLIGHT_EXPONENT,
                       cancelled: Callable[[], bool] | None = None) -> FrameStatistics:
    """Statistiques approchées depuis le L1 (min, moyenne, max de maxRGB en codes PQ 12 bits par image).

    Modèle par image : maxRGB réparti en PQ du min à la médiane linéairement, puis de la médiane au max en
    puissance ``exponent`` ; médiane choisie pour que la moyenne PQ du modèle égale le L1 moyen. MaxScl = L1
    max (trois canaux), Y 99,99 % = L1 max ; moyenne de maxRGB = intégrale de l'EOTF sur le modèle (jamais
    l'EOTF de la moyenne PQ, sous-estimée) ; part ≤ 100 nits lue sur le modèle.
    """
    frames = int(dovi.frames)
    if frames <= 0:
        raise GenerationError(translate_text("RPU Dolby Vision vide : HDR10+ impossible à générer."))
    if int(dovi.l1_missing) >= frames:
        raise GenerationError(translate_text("RPU Dolby Vision sans L1 : HDR10+ impossible à générer."))
    low_all = np.asarray(dovi.l1_min, dtype=np.float64)[:frames] / 4095.0
    high_all = np.maximum(np.asarray(dovi.l1_max, dtype=np.float64)[:frames] / 4095.0, low_all)
    mean_all = np.clip(np.asarray(dovi.l1_avg, dtype=np.float64)[:frames] / 4095.0, low_all, high_all)
    tail = 1.0 / (2.0 * (exponent + 1.0))
    median_all = np.clip((mean_all - low_all / 4.0 - high_all * tail) / (0.75 - tail), low_all, high_all)
    starts = {int(s) for s in np.asarray(dovi.scene_starts).tolist() if 0 <= int(s) < frames} | {0}
    records: list[FrameRecord] = []
    for begin in range(0, frames, _CHUNK):
        _check(cancelled)
        end = min(frames, begin + _CHUNK)
        low, median, high = low_all[begin:end], median_all[begin:end], high_all[begin:end]
        grid = pq_to_nits(_l1_quantiles(low, median, high, _GRID, exponent))
        average = grid.mean(axis=1)
        y100 = (grid <= 100.0).mean(axis=1)
        percentiles = pq_to_nits(_l1_quantiles(low, median, high, _PERCENTILE_POSITIONS, exponent))
        peaks = pq_to_nits(high)
        for offset in range(end - begin):
            peak = float(peaks[offset])
            records.append(FrameRecord(begin + offset in starts, (peak, peak, peak), float(average[offset]), peak,
                                       float(y100[offset]), tuple(float(v) for v in percentiles[offset])))
    bounds = [*sorted(starts), frames]
    return FrameStatistics(0, 0, tuple(records),
                           tuple(SceneRecord(start, end - start) for start, end in zip(bounds, bounds[1:])))


def l1_statistics(rpu: Path, *, dovi_tool_bin: str, run_cmd: Callable[[list[str]], object], work_dir: Path,
                  cancelled: Callable[[], bool] | None = None) -> FrameStatistics:
    """Statistiques L1 d'un RPU complet (une entrée par image affichée : images sans RPU complétées par
    l'appelant, sinon les statistiques suivantes seraient décalées)."""
    export = Path(work_dir) / "hdr10plus_l1_export.json"
    try:
        run_cmd([dovi_tool_bin, "export", "-i", str(rpu), "-d", f"all={export}"])
        _check(cancelled)
        dovi = parse_dovi_export(export, cancelled=cancelled)
    finally:
        export.unlink(missing_ok=True)
    return statistics_from_l1(dovi, cancelled=cancelled)


def measured_statistics(engine: object, path: Path, stream_index: int, work_dir: Path, *, layer: str = "base",
                        threads: int = 8, total_frames: int | None = None, fps: float | None = None,
                        progress: Callable[[int], None] | None = None, cancel: threading.Event | None = None,
                        cache: InspectionCache | None = None,
                        cache_write: bool = True) -> tuple[FrameStatistics, str]:
    """Statistiques de la mesure complète de ``layer`` : (statistiques, origine pour le journal).

    Cache du HDR Studio relu (mesure complète seulement) et alimenté si ``cache_write`` (jamais pour un segment
    d'aperçu)."""
    cancelled = cancel.is_set if cancel is not None else None
    measurement = cache.load_measurement(path, stream_index, layer, complete_only=True) if cache is not None else None
    origin = translate_text("cache du HDR Studio")
    if measurement is None:
        measurement, origin = measure_track(engine, path, stream_index, work_dir, layer=layer, threads=threads,
                                            total_frames=total_frames, fps=fps, progress=progress, cancel=cancel)
        if cache is not None and cache_write:
            cache.store_measurement(path, stream_index, measurement)
    return statistics_from_measurement_scenes(measurement, cancelled=cancelled), origin


def write_generated(statistics: FrameStatistics, output: Path, method: str, *,
                    cancelled: Callable[[], bool] | None = None) -> GeneratedHdr10Plus:
    """Écrit le JSON HDR10+ généré (profil A, indexé sur les images de la source)."""
    if not statistics.frames:
        raise GenerationError(translate_text("Aucune image mesurée : HDR10+ impossible à générer."))
    scenes = write_hdr10plus_json(statistics, output, tool=TOOL_NAME, cancelled=cancelled)
    return GeneratedHdr10Plus(Path(output), method, len(statistics.frames), scenes, describe(statistics))
