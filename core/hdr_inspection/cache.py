"""Cache des analyses dynamiques, par fichier (chemin, taille, date) et piste."""
from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import asdict, fields
from pathlib import Path

import numpy as np

from core.atomic_io import atomic_write_bytes
from core.hdr_inspection.dovi import ActiveArea, DoviAnalysis, L6Set
from core.hdr_inspection.hdr10p import Hdr10PlusAnalysis
from core.hdr_inspection.measure import ImageMeasurement

#: À incrémenter à chaque changement des analyses : les anciennes entrées sont ignorées.
CACHE_VERSION = 1
_MAX_ENTRIES = 64


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
        if entry is None or not entry.is_file():
            return None
        try:
            with np.load(entry, allow_pickle=False) as data:
                meta = json.loads(bytes(data["meta"]).decode("utf-8"))
                if meta.get("version") != CACHE_VERSION:
                    return None
                arrays = {name: data[name] for name in data.files if name != "meta"}
            dovi = hdr10plus = None
            if meta.get("dovi") is not None:
                values = dict(meta["dovi"])
                values["active_areas"] = tuple(ActiveArea(**a) for a in values["active_areas"])
                values["l6"] = tuple(L6Set(**s) for s in values["l6"])
                for name in ("l2_targets", "l8_targets", "l10_targets", "l9_primaries", "l11_content", "source_display"):
                    values[name] = _tuple(values[name])
                dovi = DoviAnalysis(**values, **{k[5:]: v for k, v in arrays.items() if k.startswith("dovi.")})
            if meta.get("hdr10plus") is not None:
                values = dict(meta["hdr10plus"])
                for name in ("windows", "targeted_display"):
                    values[name] = _tuple(values[name])
                hdr10plus = Hdr10PlusAnalysis(**values, **{k[10:]: v for k, v in arrays.items()
                                                           if k.startswith("hdr10plus.")})
            os.utime(entry)
            return dovi, hdr10plus
        except (OSError, ValueError, KeyError, TypeError):
            return None

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
        np.savez_compressed(buffer, **arrays)
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(self._root / f"{key}.npz", buffer.getvalue())
            self._prune()
        except OSError:
            pass

    def load_measurement(self, path: Path, stream_index: int, layer: str = "base") -> ImageMeasurement | None:
        """Mesure de l'image en cache (fichier distinct des métadonnées dynamiques) : complète de préférence,
        sinon mesure rapide (images clés)."""
        for suffix in ("", "-keyframes"):
            measurement = self._load_measurement(path, stream_index, layer, suffix)
            if measurement is not None:
                return measurement
        return None

    def _load_measurement(self, path: Path, stream_index: int, layer: str, suffix: str) -> ImageMeasurement | None:
        key = self.key(path, stream_index)
        entry = self._root / f"{key}.{layer}{suffix}.npz" if key else None
        if entry is None or not entry.is_file():
            return None
        try:
            with np.load(entry, allow_pickle=False) as data:
                meta = json.loads(bytes(data["meta"]).decode("utf-8"))
                if meta.get("version") != CACHE_VERSION or meta.get("layer") != layer:
                    return None
                measurement = ImageMeasurement(
                    layer=layer, window=tuple(meta["window"]), sampled=int(meta.get("sampled", 0)),
                    **{name: data[name] for name in ("max_rgb", "average", "y9999", "y100", "percentiles",
                                                     "scene_starts", "histogram")})
            os.utime(entry)
            return measurement
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def store_measurement(self, path: Path, stream_index: int, measurement: ImageMeasurement) -> None:
        key = self.key(path, stream_index)
        if key is None:
            return
        meta = {"version": CACHE_VERSION, "layer": measurement.layer, "window": list(measurement.window),
                "sampled": measurement.sampled}
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

    def _prune(self) -> None:
        entries = sorted(self._root.glob("*.npz"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in entries[_MAX_ENTRIES:]:
            stale.unlink(missing_ok=True)
