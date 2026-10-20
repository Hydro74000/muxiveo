"""HDR Studio, lot 2 : mesure de l'image, simulation du tone mapping, correction des métadonnées."""
from __future__ import annotations

import dataclasses
import json
import shutil
import struct
import threading
from pathlib import Path

import numpy as np
import pytest

from core.hdr_inspection import InspectionCache, InspectionReport, Severity, audit, build_snapshot
from core.hdr_inspection.measure import MeasurementError, histogram_share_above, read_measurement
from core.hdr_inspection.patch import (
    Hdr10PlusEdit,
    MasteringEdit,
    MetadataPatcher,
    PatchCancelled,
    PatchError,
    PatchPlan,
    RpuEdit,
    StaticEdit,
    suggested_mastering,
    transform_hdr10plus,
)
from core.hdr_inspection.pq import nits_to_pq
from core.hdr_inspection.tonemap import (
    TvModel,
    analyse,
    eetf,
    frame_levels,
    knee_nits,
    model_source_peak,
    suggested_light_levels,
)
from core.i18n import current_language, set_current_language
from tests.test_hdr_inspection import _analyses, _hdr10plus_entry, _info, _write_hdr10plus


@pytest.fixture(autouse=True)
def _french():
    previous = current_language()
    set_current_language("fra")
    yield
    set_current_language(previous)


def _code_bin(nits: float) -> int:
    return int(float(nits_to_pq(nits)) * 65535) >> 6


def write_statistics(path: Path, peaks: list[float], *, averages: list[float] | None = None,
                     scenes: list[int] | None = None, pixels: int = 1000, times: list[float] | None = None) -> Path:
    """Statistiques au format du plugin : une ligne par image, puis histogramme par plan.

    ``times`` : mesure par images clés (horodatage de chaque image mesurée).
    """
    averages = averages or [p / 20 for p in peaks]
    scenes = scenes or [0]
    header = {"format": "mvo-fel-frame-statistics", "version": 1, "bins": 1024, "width": 64, "height": 32}
    if times is not None:
        header["sampling"] = "keyframes"
    lines = [json.dumps(header)]
    bounds = [*scenes, len(peaks)]
    frame = 0
    for first, end in zip(bounds, bounds[1:]):
        hist: dict[int, int] = {}
        for _ in range(first, end):
            peak = peaks[frame]
            lines.append(json.dumps({
                "frame": frame, "scene_start": frame == first and frame > 0,
                "max_nits": [peak, peak * 0.8, peak * 0.5], "avg_nits": averages[frame], "y9999_nits": peak * 0.9,
                "y100": 0.9, "maxrgb_percentiles": [0, 1, 5, 20, 50, 80, peak * 0.95], "window": [0, 0, 64, 32],
                **({"time_s": times[frame]} if times is not None else {})}))
            # 999 pixels à 10 nits, 1 au pic.
            for nits, count in ((10.0, pixels - 1), (peak, 1)):
                hist[_code_bin(nits)] = hist.get(_code_bin(nits), 0) + count
            frame += 1
        lines.append(json.dumps({"scene_first": first, "frames": end - first,
                                 "maxrgb_hist": sorted([k, v] for k, v in hist.items())}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ── Mesure ───────────────────────────────────────────────────────────────────
def test_read_measurement_and_levels(tmp_path):
    measurement = read_measurement(write_statistics(tmp_path / "s.jsonl", [100.0, 800.0, 1500.0, 200.0],
                                                    scenes=[0, 2]))
    assert measurement.frames == 4 and measurement.scene_starts.tolist() == [0, 2]
    assert measurement.max_cll() == 1500 and measurement.max_fall() == 75
    assert measurement.percentiles.shape == (4, 7) and measurement.window == (0, 0, 64, 32)
    assert int(measurement.histogram.sum()) == 4000
    # 4 pixels de pic sur 4000, dont 2 au-delà de 700 nits.
    assert histogram_share_above(measurement.histogram, 700) == pytest.approx(2 / 4000, rel=0.05)
    assert histogram_share_above(measurement.histogram, 5) == pytest.approx(1.0)


def test_read_measurement_rejects_gaps(tmp_path):
    path = write_statistics(tmp_path / "s.jsonl", [100.0, 200.0, 300.0], scenes=[0, 2])
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(line for line in lines if '"frame": 1,' not in line) + "\n", encoding="utf-8")
    with pytest.raises(MeasurementError):
        read_measurement(path)
    path.write_text('{"format": "autre"}\n', encoding="utf-8")
    with pytest.raises(MeasurementError):
        read_measurement(path)


def test_keyframe_measurement_holds_values_and_cache_prefers_complete(tmp_path):
    path = write_statistics(tmp_path / "kf.jsonl", [100.0, 900.0, 300.0], scenes=[0, 2], times=[0.0, 2.0, 4.0])
    measurement = read_measurement(path, total_frames=120, fps=24)
    assert measurement.is_estimate and measurement.sampled == 3 and measurement.frames == 120
    peaks = measurement.peaks()
    assert (peaks[0], peaks[47], peaks[48], peaks[95], peaks[96], peaks[119]) == (100, 100, 900, 900, 300, 300)
    assert measurement.scene_starts.tolist() == [0, 96] and measurement.max_cll() == 900
    with pytest.raises(MeasurementError):
        read_measurement(path)  # cadence requise
    media = tmp_path / "film.mkv"
    media.write_bytes(b"x")
    cache = InspectionCache(tmp_path / "cache")
    cache.store_measurement(media, 0, measurement)
    cached = cache.load_measurement(media, 0)
    assert cached is not None and cached.is_estimate and cached.sampled == 3 and cached.frames == 120
    cache.store_measurement(media, 0, read_measurement(write_statistics(tmp_path / "all.jsonl", [100.0] * 120)))
    assert not cache.load_measurement(media, 0).is_estimate  # mesure complète préférée


def test_measurement_cache_round_trip(tmp_path):
    measurement = read_measurement(write_statistics(tmp_path / "s.jsonl", [100.0, 900.0], scenes=[0, 1]))
    media = tmp_path / "film.mkv"
    media.write_bytes(b"x")
    cache = InspectionCache(tmp_path / "cache")
    assert cache.load_measurement(media, 0) is None
    cache.store_measurement(media, 0, measurement)
    loaded = cache.load_measurement(media, 0)
    assert loaded is not None and loaded.max_cll() == 900 and loaded.window == (0, 0, 64, 32)
    assert np.array_equal(loaded.histogram, measurement.histogram)
    assert cache.load_measurement(media, 0, "reconstructed") is None
    assert cache.load(media, 0) is None  # métadonnées dynamiques : fichier distinct


# ── Tone mapping ─────────────────────────────────────────────────────────────
def test_eetf_reference_points():
    values = np.array([1.0, 100.0, 400.0, 1000.0, 4000.0, 8000.0])
    # Écran au moins aussi lumineux que le pic annoncé : identité puis écrêtage au pic de l'écran.
    assert eetf(values, 1000, 1000).tolist() == pytest.approx([1, 100, 400, 1000, 1000, 1000])
    shown = eetf(values, 4000, 1000)
    assert shown[:2] == pytest.approx([1, 100], rel=1e-6)  # sous le genou : inchangé
    assert np.all(np.diff(shown) >= 0) and shown[-2] == pytest.approx(1000) and shown[-1] == pytest.approx(1000)
    assert 400 < knee_nits(4000, 1000) < 600 and knee_nits(800, 1000) is None
    assert model_source_peak(TvModel.MAXCLL, 0, 4000) == 4000 and model_source_peak(TvModel.MAXCLL, 1200, None) == 1200
    assert model_source_peak(TvModel.MDCV, 1200, None) == 1000 and model_source_peak(TvModel.DYNAMIC, 1200, 4000) is None


def test_analyse_clipping_and_needless_compression(tmp_path):
    measurement = read_measurement(write_statistics(tmp_path / "s.jsonl", [300.0, 1500.0, 1500.0, 300.0],
                                                    scenes=[0, 1, 3]))
    levels = frame_levels(None, measurement)
    assert levels is not None and levels.source == "measure"
    # MaxCLL 600 sur un écran de 600 nits : images à 1500 nits écrêtées.
    low = analyse(levels, model=TvModel.MAXCLL, max_cll=600, mdcv_max=1000, display_peak=600)
    assert low.clipped_frames == 2 and low.clipped_scenes == 1 and low.clip_level == 600
    assert low.worst_clip_ev == pytest.approx(np.log2(1500 / 600), rel=1e-3) and low.clipped_pixels > 0
    assert low.severity() is Severity.ERROR
    # MaxCLL 10 000 : aucune image écrêtée, hautes lumières assombries face à la référence dynamique.
    high = analyse(levels, model=TvModel.MAXCLL, max_cll=10000, mdcv_max=1000, display_peak=600)
    assert high.clipped_frames == 0 and high.darkened_frames >= 2 and high.darkening_median_ev > 0.15
    assert np.all(high.displayed_peak <= 600 + 1e-3)
    dynamic = analyse(levels, model=TvModel.DYNAMIC, max_cll=None, mdcv_max=None, display_peak=600)
    assert dynamic.clipped_frames == 0 and dynamic.darkened_frames == 0 and dynamic.severity() is Severity.OK


def test_levels_and_suggestions_from_metadata(tmp_path):
    dovi, hdr10plus = _analyses(tmp_path, max_pq=3000)
    snapshot = build_snapshot(_info(tmp_path / "a.mkv"))
    report = InspectionReport(snapshot, dovi, hdr10plus, dynamic=True)
    levels = frame_levels(report)
    assert levels is not None and levels.source == "hdr10plus" and levels.frames == 10
    cll, fall, source = suggested_light_levels(report)
    assert (cll, fall, source) == (400, 38, "hdr10plus")
    only_dovi = frame_levels(InspectionReport(snapshot, dovi, None, dynamic=True))
    assert only_dovi is not None and only_dovi.source == "l1"
    assert suggested_light_levels(InspectionReport(snapshot, dovi, None))[1:] == (None, "l1")
    assert frame_levels(InspectionReport(snapshot)) is None


def test_saturated_hdr10plus_frames_ignored_by_simulation(tmp_path):
    path = tmp_path / "h.json"
    infos = [_hdr10plus_entry(i, i, maxscl=(4000, 4000, 4000)) for i in range(3)]
    infos += [_hdr10plus_entry(3, 0, maxscl=(100000, 100000, 100000))]
    path.write_text(json.dumps({"JSONInfo": {"HDR10plusProfile": "B"}, "SceneInfo": infos}), encoding="utf-8")
    from core.hdr_inspection import parse_hdr10plus_json
    report = InspectionReport(build_snapshot(_info(tmp_path / "a.mkv", frames=4)), None, parse_hdr10plus_json(path))
    levels = frame_levels(report)
    assert levels is not None and np.isnan(levels.peak[3]) and levels.peak[0] == pytest.approx(400)
    risk = analyse(levels, model=TvModel.MAXCLL, max_cll=300, mdcv_max=1000, display_peak=300)
    assert risk.clipped_frames == 3 and risk.frames == 3


def test_audit_uses_measurement(tmp_path):
    measurement = read_measurement(write_statistics(tmp_path / "s.jsonl", [300.0, 420.0]))
    snapshot = build_snapshot(_info(tmp_path / "a.mkv", frames=2))  # 1000/400 déclarés
    titles = [f.title for f in audit(snapshot, measurement=measurement)]
    assert any("très au-dessus de la valeur mesurée (420 nits)" in t for t in titles)
    low = read_measurement(write_statistics(tmp_path / "t.jsonl", [300.0, 2000.0]))
    findings = audit(snapshot, measurement=low)
    assert any(f.severity is Severity.WARN and "sous la valeur mesurée (2000 nits)" in f.title for f in findings)


# ── Correction ───────────────────────────────────────────────────────────────
def test_edits_validation_and_editor_config():
    with pytest.raises(ValueError):
        StaticEdit(300, 400)
    StaticEdit(0, 0)
    assert RpuEdit().is_empty() and Hdr10PlusEdit().is_empty()
    config = RpuEdit(l6=(1000, 0.005, 700, 200), active_area=(0, 0, 140, 140), l11=(1, 0, True),
                     source_display=(0.005, 1000), remove_cmv4=True).editor_config()
    assert config["level6"] == {"max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 50,
                                "max_content_light_level": 700, "max_frame_average_light_level": 200}
    assert config["active_area"]["presets"] == [{"id": 0, "left": 0, "right": 0, "top": 140, "bottom": 140}]
    assert config["level11"] == {"content_type": 1, "whitepoint": 0, "reference_mode_flag": True}
    assert config["max_pq"] == 3079 and config["min_pq"] == 62 and config["remove_cmv4"] is True


def test_mastering_edit_and_suggestion(tmp_path):
    from types import SimpleNamespace

    edit = MasteringEdit("Display P3", 0.0001, 1000)
    assert edit.master_display() == "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)"
    assert edit.colour() == (0.68, 0.32, 0.265, 0.69, 0.15, 0.06, 0.3127, 0.329, 1000.0, 0.0001)
    assert MasteringEdit("BT.2020", 0.005, 4000).master_display().startswith("G(8500,39850)B(6550,2300)R(35400,14600)")
    for bad in (("sRGB", 0.0001, 1000), ("BT.2020", 1000, 1000), ("BT.2020", 0.0001, 20000)):
        with pytest.raises(ValueError):
            MasteringEdit(*bad)
    static = StaticEdit(mastering=edit)
    assert static.light_levels is None and not static.is_empty() and StaticEdit().is_empty()
    with pytest.raises(ValueError):
        StaticEdit(100, None)
    dovi, hdr10plus = _analyses(tmp_path)
    snapshot = SimpleNamespace(mastering=None)
    assert suggested_mastering(SimpleNamespace(snapshot=snapshot, dovi=dovi, hdr10plus=None)) == (
        MasteringEdit("Display P3", 0.005, 1000), "l6")
    without_l6 = dataclasses.replace(dovi, l6=(), l9_primaries=(2,))
    edit, source = suggested_mastering(SimpleNamespace(snapshot=snapshot, dovi=without_l6, hdr10plus=None))
    assert source == "source_display" and edit.primaries == "BT.2020" and edit.max_nits == dovi.source_display[0][1]
    measured = SimpleNamespace(frames=10, max_cll=lambda: 1500)
    assert suggested_mastering(SimpleNamespace(snapshot=snapshot, dovi=None, hdr10plus=hdr10plus), measured) == (
        MasteringEdit("Display P3", 0.005, 4000), "standard")
    current = SimpleNamespace(mastering=SimpleNamespace(primaries="BT.2020"))
    assert suggested_mastering(SimpleNamespace(snapshot=current, dovi=None, hdr10plus=None)) == (
        MasteringEdit("BT.2020", 0.0001, 1000), "standard")


def test_static_sei_mastering_replaced_and_colour_written(tmp_path):
    from core.matroska.ebml import element, uint_element
    from core.matroska.hevc.payload_rewriter import _with_light_levels
    from core.matroska.ids import COLOUR_ID, LUMINANCE_MAX_ID, MASTERING_METADATA_ID, MAX_CLL_ID
    from core.matroska.reader import payload_children
    from core.workflows.hevc_static_hdr_metadata import (
        _build_static_hdr_sei_nal, _sei_messages, replace_static_hdr_sei_file,
    )
    mdcv_cll, _ = _build_static_hdr_sei_nal(
        master_display="G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)", max_cll="900,300")
    start = b"\x00\x00\x00\x01"
    idr = bytes([19 << 1, 1, 0x80, 0x11])
    source, output = tmp_path / "in.hevc", tmp_path / "out.hevc"
    source.write_bytes(start + mdcv_cll + start + idr)
    edit = MasteringEdit("BT.2020", 0.005, 4000)
    assert replace_static_hdr_sei_file(source, output, master_display=edit.master_display()).applied
    messages = [m for n in output.read_bytes().split(start) if n and (n[0] >> 1) & 0x3F == 39 for m in _sei_messages(n)]
    assert sorted(t for t, _ in messages) == [137, 144]
    mdcv = next(p for t, p in messages if t == 137)
    assert int.from_bytes(mdcv[:2], "big") == 8500 and int.from_bytes(mdcv[16:20], "big") == 40000000
    cll = next(p for t, p in messages if t == 144)
    assert int.from_bytes(cll[:2], "big") == 900  # CLL conservé
    video = element(COLOUR_ID, uint_element(MAX_CLL_ID, 1000) + element(MASTERING_METADATA_ID, b""))
    colour = dict(payload_children(dict(payload_children(_with_light_levels(video, None, edit.colour())))[COLOUR_ID]))
    assert int.from_bytes(colour[MAX_CLL_ID], "big") == 1000
    mastering = dict(payload_children(colour[MASTERING_METADATA_ID]))
    assert struct.unpack(">d", mastering[LUMINANCE_MAX_ID])[0] == 4000.0


def test_static_sei_rewrite_reports_progress_and_cancels(tmp_path):
    from core.workflows.hevc_static_hdr_metadata import replace_static_hdr_sei_file
    start = b"\x00\x00\x00\x01"
    source, output = tmp_path / "in.hevc", tmp_path / "out.hevc"
    source.write_bytes((start + bytes([19 << 1, 1, 0x80, 0x11])) * 200)
    fractions: list[float] = []
    assert replace_static_hdr_sei_file(source, output, max_cll="900,300", progress_cb=fractions.append).applied
    assert len(fractions) == 3 and fractions == sorted(fractions) and 0 < fractions[-1] < 1

    def cancel(_fraction: float) -> None:
        raise PatchCancelled

    with pytest.raises(PatchCancelled):
        replace_static_hdr_sei_file(source, output, max_cll="900,300", progress_cb=cancel)

def test_transform_hdr10plus(tmp_path):
    source = _write_hdr10plus(tmp_path / "h.json", [2, 3], maxscl=(50000, 40000, 30000), avg=6000)
    target = tmp_path / "out.json"
    assert transform_hdr10plus(source, target, Hdr10PlusEdit(targeted_display=1000, cap_nits=4000)) == 5
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["JSONInfo"]["HDR10plusProfile"] == "B"
    assert document["SceneInfoSummary"] == {"SceneFirstFrameIndex": [0, 2], "SceneFrameNumbers": [2, 3]}
    first = document["SceneInfo"][0]
    assert first["TargetedSystemDisplayMaximumLuminance"] == 1000 and first["MaxScl" if False else "LuminanceParameters"]
    luminance = first["LuminanceParameters"]
    assert luminance["MaxScl"] == [40000, 40000, 30000] and luminance["AverageRGB"] == 6000
    values = dict(zip(luminance["LuminanceDistributions"]["DistributionIndex"],
                      luminance["LuminanceDistributions"]["DistributionValues"]))
    assert values[10] == 87 and values[99] == 3594  # pourcentage jamais plafonné
    transform_hdr10plus(source, target, Hdr10PlusEdit(drop_bezier=True))
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["JSONInfo"]["HDR10plusProfile"] == "A"
    assert all("BezierCurveData" not in info and info["TargetedSystemDisplayMaximumLuminance"] == 0
               for info in document["SceneInfo"])


class _FakeTools:
    """dovi_tool / hdr10plus_tool simulés : copient le flux et écrivent les fichiers attendus."""

    def __init__(self, hdr10plus_json: Path) -> None:
        self.hdr10plus_json = hdr10plus_json
        self.commands: list[list[str]] = []

    def __call__(self, cmd):
        cmd = list(cmd)
        self.commands.append(cmd)
        tool, verb = Path(cmd[0]).name, cmd[1]
        if tool == "dovi_tool" and verb == "extract-rpu":
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"rpu")
        elif tool == "dovi_tool" and verb == "editor":
            assert json.loads(Path(cmd[cmd.index("-j") + 1]).read_text())["level6"]["max_content_light_level"] == 700
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"rpu2")
        elif tool == "dovi_tool" and verb == "inject-rpu":
            shutil.copy(cmd[cmd.index("-i") + 1], cmd[cmd.index("-o") + 1])
        elif tool == "hdr10plus_tool" and verb == "extract":
            shutil.copy(self.hdr10plus_json, cmd[cmd.index("-o") + 1])
        elif tool == "hdr10plus_tool" and verb in ("remove", "inject"):
            shutil.copy(cmd[cmd.index("-i") + 1], cmd[cmd.index("-o") + 1])
        return ""


def _patcher(tmp_path, tools, **kwargs) -> MetadataPatcher:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return MetadataPatcher(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool", hdr10plus_bin="hdr10plus_tool",
                           run_cmd=tools, work_dir=work, **kwargs)


def test_patcher_raw_hevc_flow_and_publication(tmp_path):
    source = tmp_path / "film.hevc"
    source.write_bytes(b"\x00\x00\x00\x01" + b"\x40\x01" + b"\x00" * 64)
    tools = _FakeTools(_write_hdr10plus(tmp_path / "h.json", [3]))
    steps: list[tuple[str, float, float]] = []
    output = tmp_path / "out.hevc"
    plan = PatchPlan(source, 0, output, rpu=RpuEdit(l6=(1000, 0.0001, 700, 200)),
                     hdr10plus=Hdr10PlusEdit(targeted_display=600))
    assert _patcher(tmp_path, tools, on_step=lambda *step: steps.append(step)).run(plan) == output
    assert output.read_bytes() == source.read_bytes()
    verbs = [(Path(c[0]).name, c[1]) for c in tools.commands]
    assert verbs == [("dovi_tool", "extract-rpu"), ("dovi_tool", "editor"), ("dovi_tool", "inject-rpu"),
                     ("hdr10plus_tool", "extract"), ("hdr10plus_tool", "remove"), ("hdr10plus_tool", "inject")]
    assert len({label for label, _, _ in steps}) == 3 and not list((tmp_path / "work").iterdir())
    # Une fenêtre par balayage outil : extract-rpu, inject-rpu ×2 | extract, remove, inject ×2 | écriture.
    starts = [round(start, 6) for _, start, _ in steps]
    assert starts == sorted(starts) and steps[-1][1] + steps[-1][2] == pytest.approx(1.0)
    assert steps[2][1] + 2 * steps[2][2] == pytest.approx(steps[3][1])
    assert steps[-2][1] + 2 * steps[-2][2] == pytest.approx(steps[-1][1])
    assert not list(tmp_path.glob("*.partial"))
    # Sortie existante : remplacée (destination relevée au départ) ; la source n'est jamais la sortie.
    _patcher(tmp_path, _FakeTools(tmp_path / "h.json")).run(plan)
    with pytest.raises(PatchError):
        _patcher(tmp_path, tools).run(PatchPlan(source, 0, source, static=StaticEdit(100, 50)))
    with pytest.raises(PatchError):
        _patcher(tmp_path, tools).run(PatchPlan(source, 0, output))
    with pytest.raises(PatchError):
        _patcher(tmp_path, tools).run(PatchPlan(tmp_path / "film.mp4", 0, output, static=StaticEdit(1, 1)))


def test_patcher_removes_hdr10plus_and_cancels(tmp_path):
    source = tmp_path / "film.hevc"
    source.write_bytes(b"\x00" * 32)
    tools = _FakeTools(tmp_path / "unused.json")
    output = tmp_path / "out.hevc"
    _patcher(tmp_path, tools).run(PatchPlan(source, 0, output, hdr10plus=Hdr10PlusEdit(remove=True)))
    assert [(Path(c[0]).name, c[1]) for c in tools.commands] == [("hdr10plus_tool", "remove")]
    cancelled = threading.Event()
    cancelled.set()
    output.unlink()
    with pytest.raises(PatchCancelled):
        _patcher(tmp_path, tools, cancelled=cancelled.is_set).run(
            PatchPlan(source, 0, output, hdr10plus=Hdr10PlusEdit(remove=True)))
    assert not output.exists() and not list(tmp_path.glob("*.partial"))


def test_patcher_keeps_result_when_publication_is_refused(tmp_path):
    """Audit Codex G2 : destination apparue pendant le traitement ; le fichier corrigé est conservé."""
    from core.output_commit import OutputChangedError

    source = tmp_path / "film.hevc"
    source.write_bytes(b"\x00" * 32)
    output = tmp_path / "out.hevc"

    class _ToolsCreatingOutput(_FakeTools):
        def __call__(self, cmd):
            result = super().__call__(cmd)
            output.write_bytes(b"autre logiciel")  # destination créée pendant le job
            return result

    with pytest.raises(OutputChangedError) as exc:
        _patcher(tmp_path, _ToolsCreatingOutput(tmp_path / "unused.json")).run(
            PatchPlan(source, 0, output, hdr10plus=Hdr10PlusEdit(remove=True)))
    assert output.read_bytes() == b"autre logiciel"
    assert exc.value.candidate.is_file() and exc.value.candidate.read_bytes() == source.read_bytes()


def test_patcher_checks_free_space(tmp_path, monkeypatch):
    import core.hdr_inspection.patch as patch_module

    source = tmp_path / "film.hevc"
    source.write_bytes(b"\x00" * 1024)
    monkeypatch.setattr(patch_module, "_free_bytes", lambda _path: 100)
    with pytest.raises(PatchError, match="Espace insuffisant"):
        _patcher(tmp_path, _FakeTools(tmp_path / "x.json")).run(
            PatchPlan(source, 0, tmp_path / "o.hevc", hdr10plus=Hdr10PlusEdit(remove=True)))


# ── Moteur de mesure (ABI simulée) ──────────────────────────────────────────
class _FakeLib:
    def __init__(self, status: int = 0) -> None:
        self.status, self.calls = status, []

    def mvo_fel_create(self, path, stream, threads, num, den):
        self.calls.append(("create", path, stream, threads))
        return 7

    def mvo_fel_set_frame_statistics(self, handle, path):
        self.stats = Path(path.decode())
        return 0

    def mvo_fel_set_measure(self, handle, layer):
        self.calls.append(("measure", layer))
        return 0

    def mvo_fel_set_device(self, handle, device):
        return 0

    def mvo_fel_set_precision(self, handle, precision):
        return 0

    def mvo_fel_run(self, handle, write, progress, opaque):
        import ctypes
        assert not ctypes.cast(write, ctypes.c_void_p).value  # aucun callback d'écriture
        progress(None, 3)
        return self.status

    def mvo_fel_error(self, handle):
        return b"erreur native"

    def mvo_fel_backend(self, handle):
        return b"vulkan: test"

    def mvo_fel_cancel(self, handle):
        self.calls.append(("cancel",))

    def mvo_fel_destroy(self, handle):
        self.calls.append(("destroy",))


def _engine(lib: _FakeLib, measure: list[str] | None = None):
    from core.fel.engine import FelEngine

    engine = FelEngine.__new__(FelEngine)
    engine.lib = lib
    engine.capabilities = {"measure": measure if measure is not None else ["base", "reconstructed"],
                           "device_selection": 1, "precision": ["exact", "standard", "fast"]}
    return engine


def test_engine_measure_contract(tmp_path):
    from core.fel.engine import FelCancelled, FelError, measure_supported

    lib = _FakeLib()
    frames: list[int] = []
    backend = _engine(lib).measure(tmp_path / "f.mkv", 0, tmp_path / "s.jsonl", progress=frames.append, threads=40)
    assert backend == "vulkan: test" and frames == [3]
    assert lib.calls[0][3] == 16 and ("measure", b"base") in lib.calls and lib.calls[-1] == ("destroy",)
    with pytest.raises(FelError, match="erreur native"):
        _engine(_FakeLib(status=1)).measure(tmp_path / "f.mkv", 0, tmp_path / "s.jsonl")
    with pytest.raises(FelCancelled):
        _engine(_FakeLib(status=3)).measure(tmp_path / "f.mkv", 0, tmp_path / "s.jsonl")
    old = _engine(_FakeLib(), measure=[])
    assert not measure_supported(old)
    with pytest.raises(FelError, match="0.4.0"):
        old.measure(tmp_path / "f.mkv", 0, tmp_path / "s.jsonl")
