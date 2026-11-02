"""HDR10+ et HDR10 statique recalculés sur l'image reconstruite (statistiques du plugin mvo-fel).

Le plugin mesure chaque image produite (capacité ``frame_statistics``, format
``docs/statistiques-image.md`` du dépôt des extensions) et cumule l'histogramme de maxRGB
par plan Dolby Vision. Ce module en tire, plan par plan, les métadonnées HDR10+ au format
JSON de ``hdr10plus_tool`` (profil A : sans courbe de Bézier, la TV construit son tone mapping
depuis les statistiques), et les MaxCLL/MaxFALL exacts de la sortie.

Distribution (9 valeurs, convention de la spécification HDR10+ LLC) : percentiles de maxRGB
1, 25, 50, 75, 90, 95 % (moyenne sur le plan des percentiles de chaque image) et 99,98 % (leur
maximum) ; la position « 5 » porte DistributionY99 (maximum sur le plan du percentile 99,99 % de
la luminance de chaque image) et la position « 10 » DistributionY100nit (moyenne sur le plan du
pourcentage de pixels de luminance ≤ 100 nits). Unités : 0,1 cd/m². Règles d'agrégation
vérifiées face au HDR10+ d'un disque UHD (même définition appliquée à sa BL).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DISTRIBUTION_INDEX = (1, 5, 10, 25, 50, 75, 90, 95, 99)
# Ordre des percentiles de maxRGB émis par le plugin (1, 25, 50, 75, 90, 95, 99,98 %).
_PERCENTILE_INDEX = (1, 25, 50, 75, 90, 95, 99)
_BINS = 1024
_M1, _M2 = 2610 / 16384, 2523 / 4096 * 128
_C1, _C2, _C3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32


class FrameStatisticsError(ValueError):
    """Fichier de statistiques absent, incomplet ou incohérent."""


def fel_statistics_path(video: object) -> Path | None:
    """Statistiques de l'image du plugin FEL (HDR10+ recalculé) ; None : HDR10+ de la source conservé."""
    context = getattr(video, "fel_context", None)
    statistics = getattr(context, "statistics", None) if context is not None else None
    return statistics if isinstance(statistics, Path) else None


def hdr10plus_recomputed(video: object) -> bool:
    """HDR10+ recalculé sur l'image reconstruite : statistiques du plugin et choix « recompute ».

    Les statistiques peuvent aussi servir aux seuls MaxCLL/MaxFALL (HDR10+ de la source conservé).
    """
    return bool(fel_statistics_path(video) is not None and getattr(video, "copy_hdr10plus", False)
                and getattr(video, "fel_hdr10plus", "recompute") == "recompute")


#: Provenances des MaxCLL/MaxFALL mesurés pendant l'encodage (statistiques de l'image produite).
MEASURED_LIGHT_LEVEL_SOURCES = frozenset({"fel_frame_statistics", "restore_frame_statistics"})


def restore_statistics_path(video: object) -> Path | None:
    """Statistiques de l'image restaurée (moteur muxiveo-rife ``--frame-statistics``) ; None sans mesure."""
    statistics = getattr(video, "restore_statistics", None)
    return statistics if isinstance(statistics, Path) else None


def light_level_statistics_path(video: object) -> Path | None:
    """Statistiques des MaxCLL/MaxFALL : image restaurée (dernière transformation), sinon image reconstruite (FEL)."""
    return restore_statistics_path(video) or fel_statistics_path(video)


def light_level_origin(video: object) -> str:
    """Image mesurée, pour le journal."""
    return "l'image restaurée" if restore_statistics_path(video) is not None else "l'image reconstruite"


def deferred_light_levels(video: object) -> bool:
    """MaxCLL/MaxFALL différés jusqu'aux statistiques (aucune valeur transmise à l'encodeur)."""
    return bool(getattr(video, "inject_hdr_meta", False)
                and getattr(video, "static_hdr_light_level_source", "") in MEASURED_LIGHT_LEVEL_SOURCES)


def pq_eotf(signal: float) -> float:
    """EOTF PQ (SMPTE ST 2084) : signal normalisé → nits."""
    p = signal ** (1 / _M2)
    return 10000 * (max(p - _C1, 0.0) / (_C2 - _C3 * p)) ** (1 / _M1)


@dataclass(frozen=True)
class FrameRecord:
    scene_start: bool
    max_nits: tuple[float, float, float]
    avg_nits: float
    y9999_nits: float
    y100: float
    percentiles: tuple[float, ...]  # maxRGB à 1, 25, 50, 75, 90, 95, 99,98 % (nits)


@dataclass(frozen=True)
class SceneRecord:
    first: int
    frames: int


@dataclass(frozen=True)
class FrameStatistics:
    width: int
    height: int
    frames: tuple[FrameRecord, ...]
    scenes: tuple[SceneRecord, ...]

    def static_light_levels(self) -> str:
        """MaxCLL,MaxFALL (CTA-861.3) : maximum du canal le plus lumineux, maximum de la moyenne de maxRGB."""
        max_cll = max(max(frame.max_nits) for frame in self.frames)
        max_fall = max(frame.avg_nits for frame in self.frames)
        return f"{min(round(max_cll), 10000)},{min(round(max_fall), 10000)}"


def read_frame_statistics(path: Path) -> FrameStatistics:
    """Lit et contrôle le fichier JSON Lines du plugin (images contiguës, plans couvrant tout le flux)."""
    try:
        lines = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        raise FrameStatisticsError(f"Statistiques de l'image illisibles ({Path(path).name}) : {exc}") from exc
    if not lines or lines[0].get("format") != "mvo-fel-frame-statistics" or lines[0].get("version") != 1 \
            or lines[0].get("bins") != _BINS:
        raise FrameStatisticsError("Statistiques de l'image : format inconnu.")
    frames: list[FrameRecord] = []
    scenes: list[SceneRecord] = []
    for line in lines[1:]:
        if "frame" in line:
            if line["frame"] != len(frames):
                raise FrameStatisticsError(f"Statistiques de l'image : image {line['frame']} hors séquence.")
            percentiles = tuple(float(v) for v in line.get("maxrgb_percentiles") or ())
            if len(percentiles) != len(_PERCENTILE_INDEX):
                raise FrameStatisticsError("Statistiques de l'image : percentiles de maxRGB absents.")
            frames.append(FrameRecord(bool(line["scene_start"]), tuple(float(v) for v in line["max_nits"]),  # type: ignore[arg-type]
                                      float(line["avg_nits"]), float(line["y9999_nits"]), float(line["y100"]),
                                      percentiles))
        elif "scene_first" in line:
            # Histogramme cumulé du plan : diagnostic du plugin, non utilisé (percentiles par image).
            scenes.append(SceneRecord(int(line["scene_first"]), int(line["frames"])))
    if not frames:
        raise FrameStatisticsError("Statistiques de l'image : aucune image.")
    expected = 0
    for scene in scenes:
        if scene.first != expected or scene.frames <= 0:
            raise FrameStatisticsError("Statistiques de l'image : plans discontinus.")
        expected += scene.frames
    if expected != len(frames):
        raise FrameStatisticsError(f"Statistiques de l'image : {expected} images en plans pour {len(frames)} mesurées.")
    return FrameStatistics(int(lines[0]["width"]), int(lines[0]["height"]), tuple(frames), tuple(scenes))


#: Écart de MaxFALL entre la source et l'image restaurée tenu pour invisible (moyenne de l'image, arrondis).
FALL_TOLERANCE = 0.01


def _light_pair(value: str) -> tuple[int, int] | None:
    """MaxCLL,MaxFALL lus ; None si absents ou nuls (0,0 : inconnus)."""
    parts = [part.strip() for part in str(value or "").split(",")]
    if len(parts) != 2 or not all(part.isdigit() for part in parts) or not int(parts[0]):
        return None
    return int(parts[0]), int(parts[1])


def _peak(statistics: FrameStatistics) -> float:
    return max(max(frame.max_nits) for frame in statistics.frames)


def _fall(statistics: FrameStatistics) -> float:
    return max(frame.avg_nits for frame in statistics.frames)


def isolated_light_level_change(source: FrameStatistics, restored: FrameStatistics) -> bool:
    """Vrai si la Restauration ne change MaxCLL / MaxFALL que par des pixels isolés, invisibles : dans chaque image
    de l'image au pic le plus haut, moins de 0,02 % des pixels dépassent le pic de l'autre (percentile 99,98 % de
    maxRGB sous ce pic, critère d'écrêtage visible du HDR Studio) ; MaxFALL à 1 % près."""
    source_peak, restored_peak = _peak(source), _peak(restored)
    higher, lower_peak = (source, restored_peak) if source_peak >= restored_peak else (restored, source_peak)
    robust = max(frame.percentiles[-1] for frame in higher.frames)
    source_fall, restored_fall = _fall(source), _fall(restored)
    return robust <= lower_peak and abs(restored_fall - source_fall) <= FALL_TOLERANCE * max(source_fall, 1.0)


def written_light_levels(video: object, statistics: FrameStatistics) -> tuple[str, str]:
    """MaxCLL,MaxFALL à écrire après l'encodage et message du journal.

    Image reconstruite (FEL) : toujours la mesure (les valeurs de la source décrivent la BL). Image restaurée : les
    valeurs de la source restent intactes si la Restauration ne les change que par des pixels isolés
    (``isolated_light_level_change``, trames lues mesurées par le moteur avant la Restauration) ; sinon, ou sans
    valeurs dans la source, la mesure de l'image restaurée.
    """
    exact = statistics.static_light_levels()
    declared = _light_pair(getattr(video, "restore_source_light_levels", ""))
    source_path = getattr(video, "restore_source_statistics", None)
    if restore_statistics_path(video) is None or declared is None or not isinstance(source_path, Path):
        return exact, f"MaxCLL/MaxFALL exacts de {light_level_origin(video)} : {exact}."
    kept = f"{declared[0]},{declared[1]}"
    try:
        source = read_frame_statistics(source_path)
    except FrameStatisticsError as exc:
        return exact, f"MaxCLL/MaxFALL exacts de l'image restaurée : {exact} (source non comparée : {exc})."
    measured_source = source.static_light_levels()
    if isolated_light_level_change(source, statistics):
        return kept, (f"MaxCLL/MaxFALL de la source conservés : {kept} (image restaurée {exact}, source mesurée "
                      f"{measured_source} : écart limité à des pixels isolés, invisible).")
    return exact, (f"MaxCLL/MaxFALL exacts de l'image restaurée : {exact} (source mesurée {measured_source} : "
                   "écart visible).")


def _tenths(nits: float) -> int:
    return max(0, min(100000, round(nits * 10)))


def scene_parameters(statistics: FrameStatistics, scene: SceneRecord) -> dict:
    """LuminanceParameters HDR10+ d'un plan (format JSON de hdr10plus_tool)."""
    frames = statistics.frames[scene.first:scene.first + scene.frames]
    values: dict[int, int] = {}
    for position, index in enumerate(_PERCENTILE_INDEX):
        series = [frame.percentiles[position] for frame in frames]
        # 99,98 % : maximum des images (comme Y99) ; 1 à 95 % : moyenne des images.
        values[index] = _tenths(max(series) if index == 99 else sum(series) / len(series))
    values[5] = _tenths(max(frame.y9999_nits for frame in frames))
    values[10] = max(0, min(100, round(100 * sum(frame.y100 for frame in frames) / len(frames))))
    return {
        "AverageRGB": _tenths(sum(frame.avg_nits for frame in frames) / len(frames)),
        "LuminanceDistributions": {
            "DistributionIndex": list(DISTRIBUTION_INDEX),
            "DistributionValues": [values[index] for index in DISTRIBUTION_INDEX],
        },
        "MaxScl": [_tenths(max(frame.max_nits[channel] for frame in frames)) for channel in range(3)],
    }


def hdr10plus_document(statistics: FrameStatistics) -> dict:
    """Document HDR10+ (profil A) : une entrée par image, valeurs constantes sur chaque plan."""
    scene_info: list[dict] = []
    for scene_id, scene in enumerate(statistics.scenes):
        parameters = scene_parameters(statistics, scene)
        for offset in range(scene.frames):
            scene_info.append({
                "LuminanceParameters": parameters,
                "NumberOfWindows": 1,
                "TargetedSystemDisplayMaximumLuminance": 0,
                "SceneFrameIndex": offset,
                "SceneId": scene_id,
                "SequenceFrameIndex": scene.first + offset,
            })
    return {
        "JSONInfo": {"HDR10plusProfile": "A", "Version": "1.0"},
        "SceneInfo": scene_info,
        "SceneInfoSummary": {
            "SceneFirstFrameIndex": [scene.first for scene in statistics.scenes],
            "SceneFrameNumbers": [scene.frames for scene in statistics.scenes],
        },
        "ToolInfo": {"Tool": "Muxiveo (mvo-fel)", "Version": "1.0"},
    }


def statistics_from_measurement(measurement: object, scene_starts: object) -> FrameStatistics:
    """Statistiques par image d'une mesure du HDR Studio (``ImageMeasurement``), découpées en ``scene_starts``."""
    import numpy as np

    max_rgb = np.asarray(getattr(measurement, "max_rgb"), dtype=np.float64)
    average = np.asarray(getattr(measurement, "average"), dtype=np.float64)
    y9999 = np.asarray(getattr(measurement, "y9999"), dtype=np.float64)
    y100 = np.asarray(getattr(measurement, "y100"), dtype=np.float64)
    percentiles = np.asarray(getattr(measurement, "percentiles"), dtype=np.float64)
    count = int(average.size)
    starts = sorted({int(s) for s in np.asarray(scene_starts).tolist() if 0 <= int(s) < count} | {0})
    if not count or percentiles.shape != (count, len(_PERCENTILE_INDEX)):
        raise FrameStatisticsError("Mesure de l'image incomplète.")
    first = set(starts)
    frames = tuple(FrameRecord(index in first, (float(max_rgb[index, 0]), float(max_rgb[index, 1]),
                                                float(max_rgb[index, 2])), float(average[index]),
                               float(y9999[index]), float(y100[index]), tuple(float(v) for v in percentiles[index]))
                   for index in range(count))
    bounds = [*starts, count]
    scenes = tuple(SceneRecord(start, end - start) for start, end in zip(bounds, bounds[1:]))
    window = getattr(measurement, "window", (0, 0, 0, 0))
    return FrameStatistics(int(window[2]), int(window[3]), frames, scenes)


def write_hdr10plus_json(statistics: FrameStatistics, output: Path) -> int:
    """Écrit le JSON HDR10+ ; renvoie le nombre de plans."""
    document = hdr10plus_document(statistics)
    Path(output).write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    return len(statistics.scenes)


def describe(statistics: FrameStatistics) -> str:
    """Résumé pour le journal : images, plans, MaxCLL/MaxFALL, pic de MaxScl."""
    peak = max(max(frame.max_nits) for frame in statistics.frames)
    return (f"{len(statistics.frames)} images, {len(statistics.scenes)} plans, MaxCLL/MaxFALL "
            f"{statistics.static_light_levels()}, pic {round(peak)} nits")
