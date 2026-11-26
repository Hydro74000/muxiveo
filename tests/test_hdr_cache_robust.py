"""
tests/test_hdr_cache_robust.py — Cache HDR robuste (D6, C8 ; plan de remédiation 5.2.0, lot 3).

Une entrée illisible (tronquée, archive corrompue) ou de structure inattendue (``meta`` qui n'est pas un objet,
tableaux incohérents) est traitée comme absente par les trois lecteurs, jamais propagée ni supprimée ; elle est
remplacée au prochain enregistrement.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest

from core.hdr_inspection import InspectionCache
from core.hdr_inspection.fel_analyzer import FelReport, FelSample, summarise
from core.hdr_inspection.measure import ImageMeasurement


def _measurement(frames: int = 10) -> ImageMeasurement:
    return ImageMeasurement("base", np.full((frames, 3), 100, np.float32), np.full(frames, 50, np.float32),
                            np.full(frames, 90, np.float32), np.zeros(frames, np.float32),
                            np.zeros((frames, 7), np.float32), np.array([0, 4], np.int32),
                            np.zeros(1024, np.int64), (0, 0, 16, 16))


def _setup(tmp_path: Path) -> tuple[Path, InspectionCache, str]:
    source = tmp_path / "film.mkv"
    source.write_bytes(b"x" * 64)
    cache = InspectionCache(tmp_path / "cache")
    key = cache.key(source, 0)
    assert key is not None
    return source, cache, key


def _write_npz(path: Path, meta: object, **arrays: np.ndarray) -> None:
    buffer = io.BytesIO()
    np.savez_compressed(buffer, meta=np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8), **arrays)
    path.write_bytes(buffer.getvalue())


def _readers(cache: InspectionCache, source: Path):
    return {"mesure": lambda: cache.load_measurement(source, 0), "dynamique": lambda: cache.load(source, 0),
            "fel": lambda: cache.load_fel(source, 0)}


def _entries(root: Path, key: str) -> dict[str, Path]:
    return {"mesure": root / f"{key}.base.npz", "dynamique": root / f"{key}.npz", "fel": root / f"{key}.fel.npz"}


def test_truncated_entries_read_as_absent_and_are_kept(tmp_path):
    """R3 du contre-audit : une entrée tronquée levait ``BadZipFile`` dans les trois lecteurs."""
    source, cache, key = _setup(tmp_path)
    cache.store_measurement(source, 0, _measurement())
    whole = _entries(cache._root, key)["mesure"].read_bytes()
    for name, entry in _entries(cache._root, key).items():
        entry.write_bytes(whole[:40] if name == "mesure" else b"PK\x03\x04tronque")
    for name, read in _readers(cache, source).items():
        assert read() is None, name
    assert all(entry.exists() for entry in _entries(cache._root, key).values())  # jamais supprimée par nom


def test_meta_that_is_not_an_object_reads_as_absent(tmp_path):
    source, cache, key = _setup(tmp_path)
    for entry in _entries(cache._root, key).values():
        entry.parent.mkdir(parents=True, exist_ok=True)
        _write_npz(entry, [], average=np.zeros(3, np.float32))
    for name, read in _readers(cache, source).items():
        assert read() is None, name


@pytest.mark.parametrize("change", [
    {"max_rgb": np.zeros((9, 3), np.float32)},
    {"percentiles": np.zeros((10, 6), np.float32)},
    {"histogram": np.zeros(512, np.int64)},
    {"scene_starts": np.array([0, 12], np.int32)},
    {"scene_starts": np.array([4, 2], np.int32)},
    {"average": np.zeros((10, 1), np.float32)},
    {"window": [0, 0, 16]},
])
def test_inconsistent_measurement_reads_as_absent(tmp_path, change):
    source, cache, key = _setup(tmp_path)
    m = _measurement()
    arrays = {"max_rgb": m.max_rgb, "average": m.average, "y9999": m.y9999, "y100": m.y100,
              "percentiles": m.percentiles, "scene_starts": m.scene_starts, "histogram": m.histogram}
    meta = {"version": 1, "layer": "base", "window": list(m.window), "sampled": 0}
    if "window" in change:
        meta["window"] = change.pop("window")
    arrays.update(change)
    entry = _entries(cache._root, key)["mesure"]
    entry.parent.mkdir(parents=True, exist_ok=True)
    _write_npz(entry, meta, **arrays)
    assert cache.load_measurement(source, 0) is None


def test_valid_measurement_round_trip(tmp_path):
    """Non-régression : une mesure cohérente est relue telle quelle."""
    source, cache, _key = _setup(tmp_path)
    cache.store_measurement(source, 0, _measurement())
    loaded = cache.load_measurement(source, 0)
    assert loaded is not None and loaded.frames == 10 and loaded.window == (0, 0, 16, 16)


def test_inconsistent_fel_entry_reads_as_absent(tmp_path):
    source, cache, key = _setup(tmp_path)
    entry = _entries(cache._root, key)["fel"]
    entry.parent.mkdir(parents=True, exist_ok=True)
    from core.hdr_inspection.fel_analyzer import FEL_METRICS_VERSION

    _write_npz(entry, {"version": 1, "metrics_version": FEL_METRICS_VERSION, "metrics": ["a", "b"],
                       "moments": [], "facts": None},
               times=np.array([1.0, 2.0]), values=np.zeros((2, 3)))  # 3 colonnes pour 2 mesures nommées
    assert cache.load_fel(source, 0) is None


def test_store_fel_after_corruption_replaces_the_entry(tmp_path):
    source, cache, key = _setup(tmp_path)
    entry = _entries(cache._root, key)["fel"]
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_bytes(b"PK\x03\x04tronque")
    samples = (FelSample(10.0, {"ton": 1.0}),)
    cache.store_fel(source, 0, FelReport(None, samples, summarise(list(samples)), ()))
    loaded = cache.load_fel(source, 0)
    assert loaded is not None and [sample.time_s for sample in loaded.samples] == [10.0]


def test_hdr10plus_generation_remeasures_over_a_corrupted_cache(tmp_path):
    """Génération HDR10+ depuis le DV : mesure en cache corrompue → nouvelle mesure, jamais d'échec."""
    from core.hdr10plus_generation import measured_statistics
    from tests.test_hdr10plus_generation import _Engine
    from tests.test_hdr_edit import write_statistics

    source, cache, key = _setup(tmp_path)
    entry = _entries(cache._root, key)["mesure"]
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_bytes(b"PK\x03\x04tronque")
    engine = _Engine(write_statistics(tmp_path / "full.jsonl", [100.0] * 4, scenes=[0, 2]))
    work = tmp_path / "work"
    work.mkdir()
    statistics, origin = measured_statistics(engine, source, 0, work, cache=cache)
    assert engine.calls == [("base", "all")] and len(statistics.frames) == 4 and origin == "cpu"
    reread = cache.load_measurement(source, 0)
    assert reread is not None and reread.frames == 4  # entrée remplacée par la mesure neuve


# ── Corrections de la relecture (lot 3) ───────────────────────────────────────


def test_dynamic_entry_without_its_analyses_reads_as_absent(tmp_path):
    source, cache, key = _setup(tmp_path)
    entry = _entries(cache._root, key)["dynamique"]
    entry.parent.mkdir(parents=True, exist_ok=True)
    _write_npz(entry, {"version": 1})
    assert cache.load(source, 0) is None


def test_dolby_vision_analysis_with_empty_l1_reads_as_absent(tmp_path):
    from core.hdr_inspection.dovi import parse_dovi_export
    from tests.test_hdr_inspection import _rpu, _write_export

    source, cache, key = _setup(tmp_path)
    dovi = parse_dovi_export(_write_export(tmp_path / "rpu.json", [_rpu(i) for i in range(5)]))
    cache.store(source, 0, dovi, None)
    assert cache.load(source, 0) is not None  # entrée cohérente relue
    entry = _entries(cache._root, key)["dynamique"]
    with np.load(entry, allow_pickle=False) as data:
        arrays = {name: data[name] for name in data.files}
    arrays["dovi.l1_max"] = np.zeros(0, np.uint16)  # 5 images annoncées, aucun L1
    meta = arrays.pop("meta")
    buffer = io.BytesIO()
    np.savez_compressed(buffer, meta=meta, **arrays)
    entry.write_bytes(buffer.getvalue())
    assert cache.load(source, 0) is None


def test_fel_moment_thumbnails_of_another_shape_read_as_absent(tmp_path):
    from core.hdr_inspection.fel_analyzer import Moment

    source, cache, _key = _setup(tmp_path)
    good = np.zeros((4, 6, 3), np.uint8)
    samples = (FelSample(10.0, {"ton": 1.0}),)
    moment = Moment(10.0, 1.0, good, good, np.zeros((4, 5, 3), np.uint8))  # carte d'écart d'une autre taille
    cache.store_fel(source, 0, FelReport(None, samples, summarise(list(samples)), (moment,)))
    assert cache.load_fel(source, 0) is None
    cache.store_fel(source, 0, FelReport(None, samples, summarise(list(samples)),
                                         (Moment(10.0, 1.0, good, good, good),)))
    assert cache.load_fel(source, 0) is not None
