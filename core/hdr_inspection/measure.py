"""Mesure de l'image (niveau 3) : statistiques de chaque image calculées par le plugin mvo-fel.

Couche « base » : la BL lue comme un signal HDR10 (ce que voit une TV HDR10, ce que décrivent
MaxCLL/MaxFALL) ; « reconstructed » : image Dolby Vision reconstruite (RPU + EL). Format des
statistiques : ``docs/statistiques-image.md`` du dépôt des extensions.
"""
from __future__ import annotations

import dataclasses
import json
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: Percentiles de maxRGB émis par le plugin pour chaque image.
PERCENTILES = (1.0, 25.0, 50.0, 75.0, 90.0, 95.0, 99.98)
_BINS = 1024


class MeasurementError(ValueError):
    """Statistiques absentes, incomplètes ou incohérentes."""


@dataclass(frozen=True, eq=False)
class ImageMeasurement:
    layer: str
    #: Nits par image : maxima de R, G, B (N × 3), moyenne de maxRGB, Y à 99,99 %, part ≤ 100 nits.
    max_rgb: np.ndarray
    average: np.ndarray
    y9999: np.ndarray
    y100: np.ndarray
    #: Percentiles de maxRGB (N × 7, ``PERCENTILES``).
    percentiles: np.ndarray
    scene_starts: np.ndarray
    #: Histogramme de maxRGB cumulé du film (1024 cases : code PQ 16 bits >> 6).
    histogram: np.ndarray
    #: Fenêtre mesurée de la première image (x, y, largeur, hauteur).
    window: tuple[int, int, int, int]
    #: Mesure rapide (images clés) : nombre d'images réellement mesurées, valeurs tenues jusqu'à l'image
    #: clé suivante ; 0 : mesure complète.
    sampled: int = 0

    @property
    def is_estimate(self) -> bool:
        return self.sampled > 0

    @property
    def frames(self) -> int:
        return int(self.average.size)

    def peaks(self) -> np.ndarray:
        """maxRGB maximal de chaque image (nits)."""
        return self.max_rgb.max(axis=1) if self.frames else np.zeros(0, dtype=np.float32)

    def max_cll(self) -> int:
        return min(10000, int(round(float(self.peaks().max())))) if self.frames else 0

    def max_fall(self) -> int:
        return min(10000, int(round(float(self.average.max())))) if self.frames else 0


def read_measurement(path: Path, layer: str = "base", *, total_frames: int | None = None, fps: float | None = None,
                     cancelled: Callable[[], bool] | None = None) -> ImageMeasurement:
    """Lit le fichier JSON Lines du plugin (images contiguës, plans couvrant toutes les images).

    Mesure par images clés (en-tête ``"sampling":"keyframes"``) : chaque image mesurée est placée à son
    horodatage (``time_s`` × ``fps``) et ses valeurs tenues jusqu'à la suivante, sur ``total_frames`` images.
    """
    max_rgb, average, y9999, y100 = array("f"), array("f"), array("f"), array("f")
    times = array("d")
    sampled = False
    percentiles = array("f")
    starts = array("i")
    histogram = np.zeros(_BINS, dtype=np.uint64)
    window: tuple[int, int, int, int] = (0, 0, 0, 0)
    covered = 0
    frame = 0
    try:
        with Path(path).open(encoding="utf-8") as handle:
            header = json.loads(handle.readline() or "{}")
            if header.get("format") != "mvo-fel-frame-statistics" or header.get("bins") != _BINS:
                raise MeasurementError("Statistiques de l'image : format inconnu.")
            sampled = header.get("sampling") == "keyframes"
            for number, line in enumerate(handle):
                if cancelled is not None and number % 8192 == 0 and cancelled():
                    raise InterruptedError
                if not line.strip():
                    continue
                item = json.loads(line)
                if "frame" in item:
                    if item["frame"] != frame:
                        raise MeasurementError(f"Statistiques de l'image : image {item['frame']} hors séquence.")
                    values = item.get("maxrgb_percentiles") or ()
                    if len(values) != len(PERCENTILES) or len(item["max_nits"]) != 3:
                        raise MeasurementError("Statistiques de l'image : valeurs manquantes.")
                    max_rgb.extend(float(v) for v in item["max_nits"])
                    average.append(float(item["avg_nits"]))
                    y9999.append(float(item["y9999_nits"]))
                    y100.append(float(item["y100"]))
                    percentiles.extend(float(v) for v in values)
                    if sampled:
                        times.append(float(item["time_s"]))
                    if frame == 0:
                        window = tuple(int(v) for v in item.get("window") or (0, 0, 0, 0))[:4]  # type: ignore[assignment]
                    frame += 1
                elif "scene_first" in item:
                    if item["scene_first"] != covered or item["frames"] <= 0:
                        raise MeasurementError("Statistiques de l'image : plans discontinus.")
                    starts.append(int(item["scene_first"]))
                    covered += int(item["frames"])
                    for index, count in item.get("maxrgb_hist") or ():
                        histogram[int(index)] += int(count)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, MeasurementError):
            raise
        raise MeasurementError(f"Statistiques de l'image illisibles ({Path(path).name}) : {exc}") from exc
    if not frame:
        raise MeasurementError("Statistiques de l'image : aucune image.")
    if covered != frame:
        raise MeasurementError(f"Statistiques de l'image : {covered} images en plans pour {frame} mesurées.")
    measurement = ImageMeasurement(
        layer=layer,
        max_rgb=np.frombuffer(max_rgb, dtype=np.float32).reshape(-1, 3).copy(),
        average=np.frombuffer(average, dtype=np.float32).copy(),
        y9999=np.frombuffer(y9999, dtype=np.float32).copy(),
        y100=np.frombuffer(y100, dtype=np.float32).copy(),
        percentiles=np.frombuffer(percentiles, dtype=np.float32).reshape(-1, len(PERCENTILES)).copy(),
        scene_starts=np.frombuffer(starts, dtype=np.int32).copy(),
        histogram=histogram,
        window=window,
    )
    if not sampled:
        return measurement
    if not fps or fps <= 0:
        raise MeasurementError("Statistiques de l'image : cadence requise pour une mesure par images clés.")
    return _expand(measurement, np.frombuffer(times, dtype=np.float64), fps, total_frames)


def _expand(measurement: ImageMeasurement, times: np.ndarray, fps: float, total_frames: int | None) -> ImageMeasurement:
    """Mesure par images clés étendue à toutes les images : valeurs tenues jusqu'à l'image clé suivante."""
    positions = np.rint((times - times[0]) * fps).astype(np.int64)
    if np.any(np.diff(positions) <= 0):
        raise MeasurementError("Statistiques de l'image : horodatages des images clés non croissants.")
    total = max(int(total_frames or 0), int(positions[-1]) + 1)
    # Image i : dernière image clé mesurée à sa position ou avant.
    owner = np.searchsorted(positions, np.arange(total), side="right") - 1
    starts = np.unique(positions[measurement.scene_starts.astype(np.int64)]).astype(np.int32)
    return dataclasses.replace(
        measurement,
        max_rgb=measurement.max_rgb[owner], average=measurement.average[owner], y9999=measurement.y9999[owner],
        y100=measurement.y100[owner], percentiles=measurement.percentiles[owner], scene_starts=starts,
        sampled=int(positions.size))


def histogram_share_above(histogram: np.ndarray, nits: float) -> float:
    """Part des pixels dont maxRGB dépasse ``nits`` (cases de l'histogramme du film)."""
    from core.hdr_inspection.pq import nits_to_pq

    total = float(histogram.sum())
    if total <= 0:
        return 0.0
    # Case k : codes PQ 16 bits [64k, 64k + 63] ; une case à cheval est comptée au prorata.
    position = float(nits_to_pq(nits)) * 65535.0 / 64.0
    first = int(position)
    above = float(histogram[first + 1:].sum()) if first + 1 < histogram.size else 0.0
    if first < histogram.size:
        above += float(histogram[first]) * (1.0 - (position - first))
    return above / total
