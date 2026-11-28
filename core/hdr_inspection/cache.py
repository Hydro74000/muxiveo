"""Cache des analyses dynamiques, par fichier (chemin, taille, date) et piste."""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
import zlib
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, cast

import numpy as np

from core.atomic_io import atomic_write_bytes
from core.hdr_inspection.dovi import ActiveArea, DoviAnalysis, L6Set
from core.hdr_inspection.fel_analyzer import FEL_METRICS_VERSION, FelReport, FelSample, Moment, summarise
from core.hdr_inspection.fel_layer import LayerFacts
from core.hdr_inspection.hdr10p import Hdr10PlusAnalysis
from core.hdr_inspection.measure import PERCENTILES, FrameCheck, ImageMeasurement

#: À incrémenter à chaque changement des analyses : les anciennes entrées sont ignorées.
CACHE_VERSION = 1
_MAX_ENTRIES = 64
#: Cases de l'histogramme d'une mesure (code PQ 16 bits >> 6).
_HISTOGRAM_BINS = 1024

#: Toute erreur de lecture ou de décodage d'une entrée (fichier tronqué, archive ou flux zlib corrompu, tableau ou
#: métadonnée inattendus) : l'entrée est traitée comme absente, jamais propagée.
_READ_ERRORS: tuple[type[BaseException], ...] = (
    OSError, EOFError, ValueError, KeyError, TypeError, AttributeError, IndexError, zipfile.BadZipFile, zlib.error)


class _InvalidEntry(ValueError):
    """Entrée lisible mais de structure inattendue."""


def _require(condition: bool) -> None:
    if not condition:
        raise _InvalidEntry


def _open_entry(entry: Path | None) -> tuple[dict, dict[str, np.ndarray]] | None:
    """Métadonnées (objet JSON, version courante) et tableaux d'une entrée, entièrement décodés ; ``None`` si
    l'entrée manque ou ne se lit pas. Aucune suppression : un autre processus peut l'avoir remplacée entre-temps,
    elle sera réécrite (atomiquement) au prochain enregistrement ou retirée par le ménage."""
    if entry is None or not entry.is_file():
        return None
    try:
        with np.load(entry, allow_pickle=False) as data:
            meta = json.loads(bytes(data["meta"]).decode("utf-8"))
            _require(isinstance(meta, dict) and meta.get("version") == CACHE_VERSION)
            arrays = {name: np.asarray(data[name]) for name in data.files if name != "meta"}
        return meta, arrays
    except _READ_ERRORS:
        return None


def _touch(entry: Path) -> None:
    """Date d'accès rafraîchie (ménage du plus ancien) ; entrée retirée entre-temps : sans effet."""
    try:
        os.utime(entry)
    except OSError:
        pass


def _int_list(value: object, length: int) -> list[int]:
    _require(isinstance(value, list) and len(value) == length
             and all(isinstance(item, int) and not isinstance(item, bool) for item in value))
    return cast(list[int], value)


def _frames_of(values: dict) -> int:
    frames = values.get("frames")
    _require(isinstance(frames, int) and not isinstance(frames, bool) and frames >= 0)
    return cast(int, frames)


def _finite(array: np.ndarray) -> bool:
    """Tableau numérique sans valeur non finie (NaN, infini)."""
    if not np.issubdtype(array.dtype, np.number) or np.iscomplexobj(array):
        return False
    return bool(np.all(np.isfinite(array))) if np.issubdtype(array.dtype, np.floating) else True


def _scene_starts(starts: np.ndarray, frames: int) -> bool:
    """Débuts de plans : entiers strictement croissants dans [0, frames) (comparés en entiers signés : un entier
    non signé décroissant ferait déborder la différence)."""
    if starts.ndim != 1 or not np.issubdtype(starts.dtype, np.integer):
        return False
    values = starts.astype(np.int64)
    return bool(np.all(values >= 0)) and bool(np.all(values < max(frames, 1))) and bool(np.all(np.diff(values) > 0))


def _per_frame(arrays: dict[str, np.ndarray], prefix: str, names: tuple[str, ...], frames: int) -> None:
    """Tableaux d'une analyse dynamique : une valeur numérique finie par image ; débuts de plans croissants dans
    [0, frames)."""
    for name in names:
        array = arrays[f"{prefix}.{name}"]
        _require(array.shape == (frames,) and _finite(array))
    _require(_scene_starts(arrays[f"{prefix}.scene_starts"], frames))


def _checked_moment(data: dict[str, np.ndarray], index: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vignettes d'un moment FEL : trois images RGB 8 bits (H × W × 3) de même taille."""
    images = tuple(data[f"moment{index}.{name}"] for name in ("mapped", "reconstructed", "difference"))
    shape = images[0].shape
    _require(len(shape) == 3 and shape[2] == 3 and shape[0] > 0 and shape[1] > 0)
    _require(all(image.shape == shape and image.dtype == np.uint8 for image in images))
    return images[0], images[1], images[2]


def _checked_measurement(layer: str, meta: dict, arrays: dict[str, np.ndarray]) -> ImageMeasurement:
    """Mesure relue, tableaux cohérents entre eux (sinon :class:`_InvalidEntry`)."""
    _require(meta.get("layer") == layer)
    window = _int_list(meta.get("window"), 4)
    sampled = meta.get("sampled", 0)
    _require(isinstance(sampled, int) and not isinstance(sampled, bool) and sampled >= 0)
    average = arrays["average"]
    frames = int(average.size)
    _require(average.ndim == 1 and frames > 0)
    _require(arrays["max_rgb"].shape == (frames, 3))
    _require(arrays["y9999"].shape == (frames,) and arrays["y100"].shape == (frames,))
    _require(arrays["percentiles"].shape == (frames, len(PERCENTILES)))
    _require(arrays["histogram"].shape == (_HISTOGRAM_BINS,))
    _require(_scene_starts(arrays["scene_starts"], frames))
    for name in ("max_rgb", "average", "y9999", "y100", "percentiles", "histogram"):
        _require(_finite(arrays[name]))
    verified = meta.get("verified_frames")
    # Compte exact relevé à l'enregistrement : retenu seulement s'il décrit bien cette mesure (anciennes entrées :
    # absent, statut recalculé).
    verified = verified if isinstance(verified, int) and not isinstance(verified, bool) and verified == frames \
        and not sampled else None
    return ImageMeasurement(layer=layer, window=cast(tuple[int, int, int, int], tuple(window)), sampled=sampled,
                            verified_frames=verified,
                            **{name: arrays[name] for name in ("max_rgb", "average", "y9999", "y100", "percentiles",
                                                               "scene_starts", "histogram")})


def _split(value: object) -> tuple[dict, dict[str, np.ndarray]]:
    meta: dict = {}
    arrays: dict[str, np.ndarray] = {}
    for item in fields(value):  # type: ignore[arg-type]
        data = getattr(value, item.name)
        if isinstance(data, np.ndarray):
            arrays[item.name] = data
        elif isinstance(data, tuple) and data and hasattr(data[0], "__dataclass_fields__"):
            meta[item.name] = [asdict(entry) for entry in data]
        else:
            meta[item.name] = data
    return meta, arrays


def _tuple(values: list | tuple | None) -> tuple:
    return tuple(tuple(v) if isinstance(v, list) else v for v in values or ())


class InspectionCache:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @staticmethod
    def key(path: Path, stream_index: int) -> str | None:
        try:
            stat = Path(path).stat()
        except OSError:
            return None
        identity = f"{CACHE_VERSION}|{Path(path).resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{stream_index}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    def load(self, path: Path, stream_index: int) -> tuple[DoviAnalysis | None, Hdr10PlusAnalysis | None] | None:
        key = self.key(path, stream_index)
        entry = self._root / f"{key}.npz" if key else None
        opened = _open_entry(entry)
        if entry is None or opened is None:
            return None
        meta, arrays = opened
        try:
            dovi = hdr10plus = None
            # Les deux clés sont toujours écrites par ``store`` : une entrée qui en manque est invalide.
            _require("dovi" in meta and "hdr10plus" in meta)
            _require(isinstance(meta["dovi"], (dict, type(None))) and isinstance(meta["hdr10plus"], (dict, type(None))))
            _require(meta["dovi"] is not None or meta["hdr10plus"] is not None)
            if meta.get("dovi") is not None:
                values = dict(meta["dovi"])
                _per_frame(arrays, "dovi", ("l1_min", "l1_max", "l1_avg"), _frames_of(values))
                values["active_areas"] = tuple(ActiveArea(**a) for a in values["active_areas"])
                values["l6"] = tuple(L6Set(**s) for s in values["l6"])
                for name in ("l2_targets", "l8_targets", "l10_targets", "l9_primaries", "l11_content", "source_display"):
                    values[name] = _tuple(values[name])
                fields: dict[str, Any] = {**values, **{k[5:]: v for k, v in arrays.items() if k.startswith("dovi.")}}
                dovi = DoviAnalysis(**fields)
            if meta.get("hdr10plus") is not None:
                values = dict(meta["hdr10plus"])
                _per_frame(arrays, "hdr10plus", ("max_scl", "average_rgb", "p99"), _frames_of(values))
                for name in ("windows", "targeted_display"):
                    values[name] = _tuple(values[name])
                fields = {**values, **{k[10:]: v for k, v in arrays.items() if k.startswith("hdr10plus.")}}
                hdr10plus = Hdr10PlusAnalysis(**fields)
        except _READ_ERRORS:
            return None
        _touch(entry)
        return dovi, hdr10plus

    def store(self, path: Path, stream_index: int, dovi: DoviAnalysis | None,
              hdr10plus: Hdr10PlusAnalysis | None) -> None:
        key = self.key(path, stream_index)
        if key is None:
            return
        meta: dict = {"version": CACHE_VERSION, "path": str(path), "stream_index": stream_index,
                      "dovi": None, "hdr10plus": None}
        arrays: dict[str, np.ndarray] = {}
        for name, analysis in (("dovi", dovi), ("hdr10plus", hdr10plus)):
            if analysis is not None:
                meta[name], parts = _split(analysis)
                arrays.update({f"{name}.{k}": v for k, v in parts.items()})
        buffer = io.BytesIO()
        arrays["meta"] = np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8)
        np.savez_compressed(buffer, **cast(dict[str, Any], arrays))  # stubs numpy récents : allow_pickle typé
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(self._root / f"{key}.npz", buffer.getvalue())
            self._prune()
        except OSError:
            pass

    def load_measurement(self, path: Path, stream_index: int, layer: str = "base", *,
                         complete_only: bool = False, check: FrameCheck | None = None) -> ImageMeasurement | None:
        """Mesure de l'image en cache (fichier distinct des métadonnées dynamiques) : complète de préférence,
        sinon mesure rapide (images clés), jamais avec ``complete_only`` (valeurs tenues entre images clés).

        ``check`` (``frame_count_check``) : mesure complète d'un autre nombre d'images que la piste écartée
        (mesure faite par mvo-fel ≤ 0.6.1 sur un flux commençant par une NAL invalide)."""
        for suffix in ("",) if complete_only else ("", "-keyframes"):
            measurement = self._load_measurement(path, stream_index, layer, suffix)
            if measurement is not None and (check is None or check(measurement) is None):
                return measurement
        return None

    def _load_measurement(self, path: Path, stream_index: int, layer: str, suffix: str) -> ImageMeasurement | None:
        key = self.key(path, stream_index)
        entry = self._root / f"{key}.{layer}{suffix}.npz" if key else None
        opened = _open_entry(entry)
        if entry is None or opened is None:
            return None
        try:
            measurement = _checked_measurement(layer, *opened)
        except _READ_ERRORS:
            return None
        _touch(entry)
        return measurement

    def store_measurement(self, path: Path, stream_index: int, measurement: ImageMeasurement) -> None:
        key = self.key(path, stream_index)
        if key is None:
            return
        meta = {"version": CACHE_VERSION, "layer": measurement.layer, "window": list(measurement.window),
                "sampled": measurement.sampled, "verified_frames": measurement.verified_frames}
        buffer = io.BytesIO()
        np.savez_compressed(buffer, meta=np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8),
                            max_rgb=measurement.max_rgb, average=measurement.average, y9999=measurement.y9999,
                            y100=measurement.y100, percentiles=measurement.percentiles,
                            scene_starts=measurement.scene_starts, histogram=measurement.histogram)
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            suffix = "-keyframes" if measurement.is_estimate else ""
            atomic_write_bytes(self._root / f"{key}.{measurement.layer}{suffix}.npz", buffer.getvalue())
            self._prune()
        except OSError:
            pass

    def load_fel(self, path: Path, stream_index: int) -> FelReport | None:
        """Instants du FEL Analyzer déjà mesurés (toutes densités confondues), sans test de survie."""
        key = self.key(path, stream_index)
        entry = self._root / f"{key}.fel.npz" if key else None
        opened = _open_entry(entry)
        if entry is None or opened is None:
            return None
        meta, data = opened
        try:
            _require(meta.get("metrics_version") == FEL_METRICS_VERSION)
            names = meta["metrics"]
            _require(isinstance(names, list) and all(isinstance(name, str) for name in names))
            times, values = data["times"], data["values"]
            _require(times.ndim == 1 and values.shape == (times.size, len(names)))
            _require(isinstance(meta["moments"], list) and all(isinstance(item, dict) for item in meta["moments"]))
            _require(isinstance(meta.get("facts"), (dict, type(None))))
            samples = [FelSample(float(time_s), {name: float(value) for name, value in zip(names, row)})
                       for time_s, row in zip(times, values)]
            moments = tuple(Moment(float(item["time_s"]), float(item["score"]), *_checked_moment(data, i))
                            for i, item in enumerate(meta["moments"]))
            facts = LayerFacts(**{**meta["facts"], "el_size": _tuple(meta["facts"]["el_size"]) or None}) \
                if meta.get("facts") else None
            report = FelReport(facts, tuple(samples), summarise(samples), moments)
        except _READ_ERRORS:
            return None
        _touch(entry)
        return report

    def store_fel(self, path: Path, stream_index: int, report: FelReport, *, moments: int = 5) -> None:
        """Ajoute les instants de ``report`` à ceux déjà en cache (mêmes instants : nouvelle mesure retenue)."""
        key = self.key(path, stream_index)
        if key is None or not report.samples:
            return
        previous = self.load_fel(path, stream_index)  # entrée précédente illisible ou invalide : absente
        by_time = {s.time_s: s for s in (previous.samples if previous is not None else ())}
        by_time.update({s.time_s: s for s in report.samples})
        samples = sorted(by_time.values(), key=lambda s: s.time_s)
        names = sorted({name for s in samples for name in s.metrics})
        kept = {m.time_s: m for m in (previous.moments if previous is not None else ())}
        kept.update({m.time_s: m for m in report.moments})
        best = sorted(kept.values(), key=lambda m: -m.score)[:moments]
        facts = report.facts or (previous.facts if previous is not None else None)
        meta = {"version": CACHE_VERSION, "metrics_version": FEL_METRICS_VERSION, "metrics": names,
                "moments": [{"time_s": m.time_s, "score": m.score} for m in best],
                "facts": None if facts is None else asdict(facts)}
        arrays: dict[str, np.ndarray] = {
            "times": np.array([s.time_s for s in samples], dtype=np.float64),
            "values": np.array([[s.metrics.get(name, 0.0) for name in names] for s in samples], dtype=np.float64)}
        for index, moment in enumerate(best):
            arrays[f"moment{index}.mapped"] = moment.mapped
            arrays[f"moment{index}.reconstructed"] = moment.reconstructed
            arrays[f"moment{index}.difference"] = moment.difference
        arrays["meta"] = np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8)
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **cast(dict[str, Any], arrays))
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(self._root / f"{key}.fel.npz", buffer.getvalue())
            self._prune()
        except OSError:
            pass

    def _prune(self) -> None:
        entries = sorted(self._root.glob("*.npz"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in entries[_MAX_ENTRIES:]:
            stale.unlink(missing_ok=True)
