"""HDR10+ généré depuis le Dolby Vision : choix de la méthode, modèle L1, mesure, écriture (cœur Qt-free)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pytest

from core.dovi_profile_detector import DoviSubProfile
from core.fel.hdr10plus import read_frame_statistics
from core.hdr10plus_generation import (
    METHOD_FEL_STATISTICS,
    METHOD_L1,
    METHOD_MEASURE,
    METHOD_MEASURE_RECONSTRUCTED,
    TOOL_NAME,
    GenerationError,
    l1_statistics,
    measured_statistics,
    resolve_generation_method,
    statistics_from_l1,
    statistics_from_measurement_scenes,
    with_scenes,
    write_generated,
)
from core.hdr_inspection import InspectionCache
from core.hdr_inspection.dovi import parse_dovi_export
from core.hdr_inspection.measure import read_measurement
from core.hdr_inspection.pq import nits_to_pq, pq_to_nits
from tests.test_hdr_edit import write_statistics
from tests.test_hdr_inspection import _rpu, _write_export

_BOTH = frozenset({"base", "reconstructed"})


def _method(**kwargs) -> str:
    base = {"sub_profile": DoviSubProfile.P8_1, "fel_bake": False, "fel_statistics": False, "hevc_output": True,
            "measure_layers": _BOTH}
    return resolve_generation_method(**{**base, **kwargs}).method


# ── Choix de la méthode ──────────────────────────────────────────────────────
def test_method_measure_when_extension_measures_base():
    assert _method() == METHOD_MEASURE
    assert _method(sub_profile=DoviSubProfile.P7_FEL) == METHOD_MEASURE  # FEL non intégré : la BL est encodée


def test_method_l1_without_extension_or_for_p5():
    assert _method(measure_layers=frozenset()) == METHOD_L1
    assert _method(sub_profile=DoviSubProfile.P5) == METHOD_L1
    assert resolve_generation_method(sub_profile=DoviSubProfile.P5, fel_bake=False, fel_statistics=False,
                                     hevc_output=True, measure_layers=_BOTH).approximate


def test_method_fel_prefers_encode_statistics_then_reconstructed_measure_then_l1():
    fel = {"sub_profile": DoviSubProfile.P7_FEL, "fel_bake": True}
    assert _method(**fel, fel_statistics=True) == METHOD_FEL_STATISTICS
    # Sortie AV1 : JSON exigé avant l'encodage, statistiques FEL réservées à HEVC.
    assert _method(**fel, fel_statistics=True, hevc_output=False) == METHOD_MEASURE_RECONSTRUCTED
    assert _method(**fel, fel_statistics=False, measure_layers=frozenset({"base"})) == METHOD_L1


# ── Modèle L1 ────────────────────────────────────────────────────────────────
def _dovi(tmp_path: Path, l1: list[tuple[int, int, int]], scenes: set[int], *, missing: set[int] | None = None):
    rpus = []
    for frame, (low, high, avg) in enumerate(l1):
        entry = _rpu(frame, scene=frame in scenes, max_pq=high, l1=frame not in (missing or set()))
        for block in entry["vdr_dm_data"]["cmv29_metadata"]["ext_metadata_blocks"]:
            if "Level1" in block:
                block["Level1"] = {"min_pq": low, "max_pq": high, "avg_pq": avg}
        rpus.append(entry)
    return parse_dovi_export(_write_export(tmp_path / "rpu.json", rpus))


def _code(nits: float) -> int:
    return int(round(float(nits_to_pq(nits)) * 4095))


def test_l1_model_keeps_peak_scenes_and_orders_percentiles(tmp_path):
    l1 = [(_code(0.01), _code(1000), _code(80))] * 3 + [(_code(0.1), _code(400), _code(20))] * 2
    statistics = statistics_from_l1(_dovi(tmp_path, l1, {0, 3}))
    assert [(s.first, s.frames) for s in statistics.scenes] == [(0, 3), (3, 2)]
    first = statistics.frames[0]
    assert first.max_nits == pytest.approx((1000.0,) * 3, rel=2e-3)
    assert first.y9999_nits == pytest.approx(1000.0, rel=2e-3)
    assert list(first.percentiles) == sorted(first.percentiles)
    assert first.percentiles[-1] <= first.max_nits[0] + 1e-6
    assert 0.0 <= first.y100 <= 1.0


def test_l1_model_mean_is_linear_mean_not_eotf_of_pq_mean(tmp_path):
    low, high, avg = _code(0.005), _code(4000), _code(100)
    record = statistics_from_l1(_dovi(tmp_path, [(low, high, avg)], {0})).frames[0]
    # La moyenne linéaire d'une répartition dépasse l'EOTF de sa moyenne PQ (EOTF convexe).
    assert record.avg_nits > float(pq_to_nits(avg / 4095))
    assert record.avg_nits < 4000


def test_l1_model_inconsistent_codes_are_clamped(tmp_path):
    record = statistics_from_l1(_dovi(tmp_path, [(2000, 1500, 3000)], {0})).frames[0]
    assert np.isfinite(record.avg_nits) and list(record.percentiles) == sorted(record.percentiles)


def test_l1_absent_everywhere_is_refused(tmp_path):
    dovi = _dovi(tmp_path, [(7, 3000, 1200)] * 3, {0}, missing={0, 1, 2})
    with pytest.raises(GenerationError):
        statistics_from_l1(dovi)


def test_l1_statistics_runs_export_and_cleans_up(tmp_path):
    rpus = [_rpu(i, scene=i in (0, 2)) for i in range(4)]
    calls: list[list[str]] = []

    def run(cmd: list[str]) -> str:
        calls.append(cmd)
        target = Path(cmd[-1].split("=", 1)[1])
        _write_export(target, rpus)
        return ""

    statistics = l1_statistics(tmp_path / "rpu.bin", dovi_tool_bin="dovi_tool", run_cmd=run, work_dir=tmp_path)
    assert calls[0][:4] == ["dovi_tool", "export", "-i", str(tmp_path / "rpu.bin")]
    assert len(statistics.frames) == 4 and [s.first for s in statistics.scenes] == [0, 2]
    assert not (tmp_path / "hdr10plus_l1_export.json").exists()


def test_l1_cancellation_raises_interrupted(tmp_path):
    dovi = _dovi(tmp_path, [(7, 3000, 1200)] * 3, {0})
    with pytest.raises(InterruptedError):
        statistics_from_l1(dovi, cancelled=lambda: True)


# ── Mesure ───────────────────────────────────────────────────────────────────
def test_measurement_scenes_follow_rpu_flags(tmp_path):
    measurement = read_measurement(write_statistics(tmp_path / "s.jsonl", [100.0, 800.0, 1500.0, 200.0],
                                                    scenes=[0, 2]))
    statistics = statistics_from_measurement_scenes(measurement)
    assert [(s.first, s.frames) for s in statistics.scenes] == [(0, 2), (2, 2)]


def test_measurement_estimate_is_refused(tmp_path):
    measurement = read_measurement(write_statistics(tmp_path / "k.jsonl", [100.0, 900.0], times=[0.0, 2.0]),
                                   total_frames=48, fps=24)
    with pytest.raises(GenerationError):
        statistics_from_measurement_scenes(measurement)


class _Engine:
    """Plugin simulé : écrit les statistiques demandées."""

    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls: list[tuple[str, str]] = []

    def measure(self, path, stream, statistics_path, *, layer, threads, progress, cancelled, sampling,
                device="auto", precision="fast"):
        self.calls.append((layer, sampling))
        Path(statistics_path).write_text(self.source.read_text(encoding="utf-8"), encoding="utf-8")
        return "cpu"


def test_measured_statistics_ignores_keyframe_cache_and_stores_full_measure(tmp_path):
    media = tmp_path / "film.mkv"
    media.write_bytes(b"x" * 64)
    cache = InspectionCache(tmp_path / "cache")
    cache.store_measurement(media, 0, read_measurement(
        write_statistics(tmp_path / "k.jsonl", [100.0, 900.0], times=[0.0, 1.0]), total_frames=48, fps=24))
    engine = _Engine(write_statistics(tmp_path / "full.jsonl", [100.0] * 4, scenes=[0, 2]))
    work = tmp_path / "work"
    work.mkdir()
    statistics, origin = measured_statistics(engine, media, 0, work, cache=cache)
    assert engine.calls == [("base", "all")] and origin == "cpu"
    assert len(statistics.frames) == 4 and not list(work.iterdir())
    assert not cache.load_measurement(media, 0).is_estimate  # mesure complète gardée
    statistics, origin = measured_statistics(_Engine(tmp_path / "absent"), media, 0, work, cache=cache)
    assert len(statistics.frames) == 4 and origin != "cpu"  # relue du cache


def test_measured_statistics_without_cache_write(tmp_path):
    media = tmp_path / "segment.mkv"
    media.write_bytes(b"x" * 64)
    cache = InspectionCache(tmp_path / "cache")
    engine = _Engine(write_statistics(tmp_path / "full.jsonl", [100.0] * 3))
    measured_statistics(engine, media, 0, tmp_path, cache=cache, cache_write=False)
    assert cache.load_measurement(media, 0) is None


def test_measured_statistics_rejects_cache_with_other_frame_count(tmp_path):
    """Mesure en cache d'un autre nombre d'images (premier GOP perdu par mvo-fel ≤ 0.6.1) : remesurée."""
    media = tmp_path / "film.mkv"
    media.write_bytes(b"x" * 64)
    cache = InspectionCache(tmp_path / "cache")
    cache.store_measurement(media, 0, read_measurement(write_statistics(tmp_path / "short.jsonl", [100.0] * 3)))
    engine = _Engine(write_statistics(tmp_path / "full.jsonl", [100.0] * 5, scenes=[0, 2]))
    counted: list[bool] = []

    def source_frames(exact: bool) -> int:
        counted.append(exact)
        return 5

    statistics, origin = measured_statistics(engine, media, 0, tmp_path, cache=cache, source_frames=source_frames)
    assert engine.calls == [("base", "all")] and len(statistics.frames) == 5
    assert origin.startswith("cpu") and origin != "cpu"  # mesure neuve, cache écarté annoncé
    assert cache.load_measurement(media, 0).frames == 5  # cache remplacé
    assert True in counted  # écart confirmé par le compte exact avant d'écarter le cache


def test_measured_statistics_refuses_fresh_measurement_with_other_frame_count(tmp_path):
    media = tmp_path / "film.mkv"
    media.write_bytes(b"x" * 64)
    cache = InspectionCache(tmp_path / "cache")
    engine = _Engine(write_statistics(tmp_path / "short.jsonl", [100.0] * 3))
    with pytest.raises(GenerationError, match="3 images pour 5"):
        measured_statistics(engine, media, 0, tmp_path, cache=cache, source_frames=lambda exact: 5)
    assert cache.load_measurement(media, 0) is None  # jamais mise en cache


def test_measured_statistics_quick_count_mismatch_settled_by_exact_count(tmp_path):
    """Compte rapide faux, compte exact égal à la mesure : mesure gardée (pas de faux refus)."""
    media = tmp_path / "film.mkv"
    media.write_bytes(b"x" * 64)
    engine = _Engine(write_statistics(tmp_path / "full.jsonl", [100.0] * 4))
    statistics, _ = measured_statistics(engine, media, 0, tmp_path,
                                        source_frames=lambda exact: 4 if exact else 7)
    assert len(statistics.frames) == 4
    counted: list[bool] = []
    measured_statistics(engine, media, 0, tmp_path, source_frames=lambda exact: counted.append(exact) or 4)
    assert counted == [False]  # compte rapide concordant : aucun recomptage exact


def test_measure_reader_cancellation_is_not_a_read_error(tmp_path):
    path = write_statistics(tmp_path / "s.jsonl", [100.0] * 3)
    with pytest.raises(InterruptedError):
        read_measurement(path, cancelled=lambda: True)
    engine = _Engine(path)
    event = threading.Event()
    event.set()
    with pytest.raises(InterruptedError):
        measured_statistics(engine, tmp_path / "m.mkv", 0, tmp_path, cancel=event)


# ── Écriture ─────────────────────────────────────────────────────────────────
def test_write_generated_profile_a_one_entry_per_frame(tmp_path):
    measurement = read_measurement(write_statistics(tmp_path / "s.jsonl", [100.0, 800.0, 1500.0], scenes=[0, 1]))
    result = write_generated(statistics_from_measurement_scenes(measurement), tmp_path / "g.json", METHOD_MEASURE)
    document = json.loads((tmp_path / "g.json").read_text(encoding="utf-8"))
    assert document["JSONInfo"]["HDR10plusProfile"] == "A" and document["ToolInfo"]["Tool"] == TOOL_NAME
    assert len(document["SceneInfo"]) == 3 == result.frames and result.scenes == 2
    assert document["SceneInfoSummary"] == {"SceneFirstFrameIndex": [0, 1], "SceneFrameNumbers": [1, 2]}


def test_write_generated_cancellation(tmp_path):
    measurement = read_measurement(write_statistics(tmp_path / "s.jsonl", [100.0, 800.0]))
    statistics = statistics_from_measurement_scenes(measurement)
    with pytest.raises(InterruptedError):
        write_generated(statistics, tmp_path / "g.json", METHOD_MEASURE, cancelled=lambda: True)


def test_with_scenes_resplits_and_marks_starts(tmp_path):
    statistics = read_frame_statistics(write_statistics(tmp_path / "s.jsonl", [100.0] * 5))
    split = with_scenes(statistics, [3, 99, -1])
    assert [(s.first, s.frames) for s in split.scenes] == [(0, 3), (3, 2)]
    assert [frame.scene_start for frame in split.frames] == [True, False, False, True, False]


def test_write_cancelled_mid_scene_removes_the_partial_file(tmp_path):
    from core.fel.hdr10plus import FrameStatistics as Stats
    from core.fel.hdr10plus import write_hdr10plus_json

    one = read_frame_statistics(write_statistics(tmp_path / "s.jsonl", [100.0]))
    long_scene = with_scenes(Stats(0, 0, one.frames * 5000, ()), [0])  # un seul plan de 5000 images
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # annulation après l'agrégation du plan, pendant ses entrées

    output = tmp_path / "g.json"
    with pytest.raises(InterruptedError):
        write_hdr10plus_json(long_scene, output, cancelled=cancelled)
    assert not output.exists()


def test_service_classifies_measure_read_cancellation_as_cancelled(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from core.fel import engine as engine_module
    from core.service.errors import ErrorCode, error_code
    from core.service.ops.hdr import measure_image

    source = write_statistics(tmp_path / "s.jsonl", [100.0] * 3)
    event = threading.Event()

    class _CancellingEngine(_Engine):
        capabilities = {"measure": ["base"]}

        def measure(self, *args, **kwargs):
            backend = super().measure(*args, **kwargs)
            event.set()  # annulation demandée juste après la mesure, pendant la lecture
            return backend

    monkeypatch.setattr(engine_module.FelEngine, "installed", classmethod(lambda cls: _CancellingEngine(source)))
    snapshot = SimpleNamespace(codec="hevc", is_pq=True, dovi=None, fps=24.0, frames=3, frames_exact=True,
                               duration_s=0.125,
                               path=str(tmp_path / "m.mkv"), stream_index=0)
    logger = SimpleNamespace(emit=lambda *a, **k: None)
    with pytest.raises(InterruptedError) as caught:
        measure_image(SimpleNamespace(), SimpleNamespace(snapshot=snapshot), 2, tmp_path, logger, cancel=event)
    assert error_code(caught.value) == ErrorCode.CANCELLED


@pytest.mark.parametrize("peak,p9998,y9999,expected", [
    (4000.0, 500.0, 600.0, 1000.0),   # pixels isolés : plancher de 1000 nits
    (4000.0, 900.0, 950.0, 1800.0),   # 2 × 99,98 %
    (5000.0, 600.0, 1500.0, 1500.0),  # jamais sous Y 99,99 % (distribution cohérente)
    (800.0, 100.0, 120.0, 800.0),     # sous le plancher : pic absolu gardé (points lumineux sur fond sombre)
    (1500.0, 900.0, 950.0, 1500.0),   # pic proche du contenu : inchangé
])
def test_maxscl_caps_isolated_pixels_only(peak, p9998, y9999, expected):
    from core.fel.hdr10plus import FrameRecord, FrameStatistics, SceneRecord, scene_parameters, maxscl_peaks

    record = FrameRecord(True, (peak, peak * 0.9, peak * 0.8), 50.0, y9999, 0.5, (1.0, 5.0, 10.0, 20.0, 50.0, 80.0, p9998))
    statistics = FrameStatistics(0, 0, (record,), (SceneRecord(0, 1),))
    assert scene_parameters(statistics, statistics.scenes[0])["MaxScl"][0] == round(expected * 10)
    assert float(maxscl_peaks(np.array([[peak, peak * 0.9, peak * 0.8]]), [p9998], [y9999])[0]) == expected
