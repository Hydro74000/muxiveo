"""Correction HDR headless (hdr.patch), recommandations et moments FEL de hdr.inspect ; outils simulés."""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from cli.hdr_patch import add_hdr_patch_parser, build_request
from core.hdr_inspection import InspectionReport, build_snapshot
from core.hdr_inspection.advice import advice_data, build_advice
from core.hdr_inspection.measure import read_measurement
from core.hdr_inspection.patch import MasteringEdit
from core.hdr_inspection.realign import Proposal, Realignment, chosen_edits, recompute_scenes
from core.hdr_inspection.report import render_html, report_data
from core.jobs.errors import JobError
from core.service import ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.ops import hdr_patch as ops
from core.service.ops.hdr_patch import HdrPatchRequest, resolve_plan
from tests.test_hdr_edit import write_statistics
from tests.test_hdr_inspection import _MASTERING, _analyses, _info, _rpu, _write_export


def _report(tmp_path: Path, *, dovi: bool = True, hdr10plus: bool = True, cm40: bool = False) -> InspectionReport:
    analyses = _analyses(tmp_path)
    rpu = analyses[0]
    if cm40:
        from core.hdr_inspection import parse_dovi_export
        rpu = parse_dovi_export(_write_export(tmp_path / "rpu40.json", [_rpu(i, cm40=True) for i in range(10)]))
    if dovi:
        info = _info(tmp_path / "film.mkv")
    else:
        from core.inspector import HDRType
        info = _info(tmp_path / "film.mkv", side_data=[_MASTERING],
                     hdr_type=HDRType.HDR10PLUS if hdr10plus else HDRType.HDR10)
    return InspectionReport(build_snapshot(info), rpu if dovi else None, analyses[1] if hdr10plus else None,
                            dynamic=True)


def _request(tmp_path: Path, **fields: Any) -> HdrPatchRequest:
    return HdrPatchRequest(source=tmp_path / "film.mkv", output=tmp_path / "fix.mkv", **fields)


def _measurement(tmp_path: Path, peaks: list[float] | None = None, **kwargs: Any):
    path = write_statistics(tmp_path / "s.jsonl", peaks or [300.0] * 5 + [2500.0] * 5, scenes=[0, 5], **kwargs)
    return read_measurement(path, total_frames=10, fps=24)


# --- Résolution du plan ------------------------------------------------------------------------


def test_manual_values_and_mastering_sync(tmp_path):
    report = _report(tmp_path)
    request = _request(tmp_path, light_levels=ops.LightLevelsPatch(max_cll=1200, max_fall=300),
                       mastering=ops.MasteringPatch(primaries="BT.2020", min_nits=0.005, max_nits=4000))
    plan, warnings = resolve_plan(request, report, None, display_peak=1000)
    assert plan.static is not None and plan.static.light_levels == (1200, 300)
    assert plan.static.mastering == MasteringEdit("BT.2020", 0.005, 4000)
    # « Garder identiques » : L6 (MaxCLL/MaxFALL du L6 actuel) et écran du RPU recopiés.
    assert plan.rpu is not None and plan.rpu.l6 == (4000, 0.005, 1000, 400)
    assert plan.rpu.source_display == (0.005, 4000.0) and warnings == []
    unsynced, _ = resolve_plan(dataclasses.replace(request, sync_mastering=False), report, None, display_peak=1000)
    assert unsynced.rpu is None


def test_suggested_values_follow_the_studio(tmp_path):
    report = _report(tmp_path)
    request = _request(tmp_path, light_levels=ops.LightLevelsPatch(mode="suggested"),
                       mastering=ops.MasteringPatch(mode="suggested"), sync_mastering=False)
    plan, _ = resolve_plan(request, report, None, display_peak=1000)
    assert plan.static is not None and plan.static.light_levels == (400, 38)  # HDR10+ (valeurs saturées écartées)
    assert plan.static.mastering == MasteringEdit("Display P3", 0.005, 1000)  # L6 du RPU, primaires du fichier
    overridden, _ = resolve_plan(dataclasses.replace(
        request, light_levels=ops.LightLevelsPatch(mode="suggested", max_fall=20)), report, None, display_peak=1000)
    assert overridden.static is not None and overridden.static.light_levels == (400, 20)


@pytest.mark.parametrize("fields, message", [
    ({}, "Aucune modification"),
    ({"light_levels": ops.LightLevelsPatch(max_cll=1000)}, "MaxFALL"),
    ({"light_levels": ops.LightLevelsPatch(max_cll=100, max_fall=400)}, "100/400"),
    ({"mastering": ops.MasteringPatch(min_nits=0.005)}, "max_nits"),
    ({"hdr10plus": ops.Hdr10PlusPatch(remove=True, cap_nits=1000)}, "remove"),
    ({"hdr10plus": ops.Hdr10PlusPatch(recompute=True)}, "mesure complète"),
    ({"realign": ("all",)}, "mesure de l'image requise"),
    ({"rpu": ops.RpuPatch(remove_cmv4=True)}, "CM v4.0"),
])
def test_plan_refusals(tmp_path, fields, message):
    with pytest.raises(JobError, match=message) as caught:
        resolve_plan(_request(tmp_path, **fields), _report(tmp_path), None, display_peak=1000)
    assert caught.value.exit_code == 3


def test_metadata_absent_from_the_track_is_refused(tmp_path):
    hdr10 = _report(tmp_path, dovi=False, hdr10plus=False)
    for fields in ({"rpu": ops.RpuPatch(remove_cmv4=True)}, {"hdr10plus": ops.Hdr10PlusPatch(profile_a=True)}):
        with pytest.raises(JobError, match="n'a pas"):
            resolve_plan(_request(tmp_path, **fields), hdr10, None, display_peak=1000)
    with pytest.raises(JobError) as caught:
        resolve_plan(HdrPatchRequest(source=tmp_path / "film.mkv", output=tmp_path / "film.mkv",
                                     light_levels=ops.LightLevelsPatch(max_cll=1, max_fall=1)), hdr10, None,
                     display_peak=1000)
    assert caught.value.exit_code == 2


def test_l11_warns_on_cm29_and_hdr10plus_recompute(tmp_path):
    l11 = ops.RpuPatch(l11=ops.L11Patch(content_type=1))
    _plan, warnings = resolve_plan(_request(tmp_path, rpu=l11), _report(tmp_path), None, display_peak=1000)
    assert warnings and "CM v4.0" in warnings[0]
    _plan, warnings = resolve_plan(_request(tmp_path, rpu=l11), _report(tmp_path, cm40=True), None, display_peak=1000)
    assert warnings == []
    measurement = _measurement(tmp_path)
    plan, _ = resolve_plan(_request(tmp_path, hdr10plus=ops.Hdr10PlusPatch(recompute=True, cap_nits=2000)),
                           _report(tmp_path), measurement, display_peak=1000)
    assert plan.hdr10plus is not None and plan.hdr10plus.recompute is not None and plan.hdr10plus.drop_bezier
    fast = _measurement(tmp_path, times=[i / 24 for i in range(10)])
    with pytest.raises(JobError, match="mesure complète"):
        resolve_plan(_request(tmp_path, hdr10plus=ops.Hdr10PlusPatch(recompute=True)), _report(tmp_path), fast,
                     display_peak=1000)


def test_realignment_is_retained_like_the_edition_tab(tmp_path):
    report = _report(tmp_path)
    measurement = _measurement(tmp_path)
    plan, _ = resolve_plan(_request(tmp_path, realign=("all",)), report, measurement, display_peak=1000)
    described = ops.describe_plan(plan)
    assert described["static"] is not None or described["rpu"] is not None or described["hdr10plus"] is not None
    explicit = ops.LightLevelsPatch(max_cll=3000, max_fall=500)
    plan, _ = resolve_plan(_request(tmp_path, realign=("static",), light_levels=explicit), report, measurement,
                           display_peak=1000)
    assert plan.static is not None and plan.static.light_levels == (3000, 500)  # valeur explicite prioritaire


def test_chosen_edits_and_recompute_scenes(tmp_path):
    proposals = (Proposal("static", "", "", "", ""), Proposal("l6", "", "", "", ""),
                 Proposal("l1", "", "", "", "", applicable=False))
    realignment = Realignment(proposals, light_levels=(900, 200), mastering=MasteringEdit("BT.2020", 0.0001, 1000),
                              l6=(1000, 0.0001, 900, 200), hdr10plus_scenes=np.array([0, 3]))
    edits = chosen_edits(realignment, ("static", "mdcv", "hdr10plus"))
    assert edits.light_levels == (900, 200) and edits.mastering is None and edits.l6 is None
    assert edits.hdr10plus_scenes is None  # aucune proposition « hdr10plus » applicable
    measurement = _measurement(tmp_path)
    assert recompute_scenes(_report(tmp_path), measurement, realignment).tolist() == [0, 3]
    assert recompute_scenes(_report(tmp_path), measurement).tolist() == [0, 5]  # plans du HDR10+ actuel


# --- Opération et CLI --------------------------------------------------------------------------


def _ctx(tmp_path: Path, sink: ListSink | None = None, **ports: Any) -> ExecutionContext:
    config = SimpleNamespace(app_data_dir=tmp_path / "data", display_peak_nits=1000, tool_ffmpeg="ffmpeg",
                             tool_dovi_tool="dovi_tool", tool_hdr10plus="hdr10plus_tool",
                             ensure_work_dir=lambda: tmp_path / "work")
    report = _report(tmp_path)
    defaults = {"file_inspector": lambda **_k: SimpleNamespace(inspect=lambda path: _info(Path(path))),
                "dynamic_report": lambda *a, **k: report}
    return ExecutionContext.create(config, sink or ListSink(), ports={**defaults, **ports})


def test_operation_dry_run_then_write(tmp_path, monkeypatch):
    payload = {"source": str(tmp_path / "film.mkv"), "output": str(tmp_path / "fix.mkv"),
               "light_levels": {"max_cll": 1000, "max_fall": 400}, "rpu": {"l11": {"content_type": 1}}}
    result: Any = call(CATALOG, "hdr.patch", {**payload, "dry_run": True}, _ctx(tmp_path))
    assert result.dry_run and result.changes["static"]["max_cll"] == 1000
    assert result.changes["rpu"]["level11"]["content_type"] == 1 and result.warnings
    written: list[Any] = []

    class FakePatcher:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def run(self, plan):
            written.append(plan)
            plan.output.write_bytes(b"mkv")
            return plan.output

    monkeypatch.setattr(ops, "MetadataPatcher", FakePatcher)
    sink = ListSink()
    result = call(CATALOG, "hdr.patch", payload, _ctx(tmp_path, sink))
    assert result.output == str(tmp_path / "fix.mkv") and written[0].static.light_levels == (1000, 400)
    assert any(event.type.value == "artifact" for event in sink.events)
    assert not [p for p in (tmp_path / "work").iterdir() if p.is_dir()], "dossier process supprimé"
    with pytest.raises(JobError) as caught:
        call(CATALOG, "hdr.patch", payload, _ctx(tmp_path))
    assert caught.value.exit_code == 5


def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_hdr_patch_parser(parser.add_subparsers())
    return parser.parse_args(["hdr-patch", "-i", "a.mkv", "-o", "b.mkv", *argv])


def test_cli_request_parsing():
    request = build_request(_args("--light-levels", "suggested", "--max-fall", "300", "--mdcv", "Display P3:0,0001:1000",
                                  "--l6", "1000:0.005:900:300", "--l5", "0,0,276,276", "--l11", "1:2:ref",
                                  "--rpu-display", "0.005:1000", "--hdr10plus-cap", "1500", "--realign", "static,l6",
                                  "--measure", "fast", "--no-mastering-sync"))
    assert request["light_levels"] == {"mode": "suggested", "max_fall": 300}
    assert request["mastering"] == {"primaries": "Display P3", "min_nits": 0.0001, "max_nits": 1000.0}
    assert request["rpu"]["l6"] == {"max_mastering_nits": 1000, "min_mastering_nits": 0.005, "max_cll": 900,
                                    "max_fall": 300}
    assert request["rpu"]["active_area"] == {"left": 0, "right": 0, "top": 276, "bottom": 276}
    assert request["rpu"]["l11"] == {"content_type": 1, "whitepoint": 2, "reference_mode": True}
    assert request["hdr10plus"] == {"cap_nits": 1500.0} and request["realign"] == ["static", "l6"]
    assert request["measurement"] == "fast" and request["sync_mastering"] is False
    assert build_request(_args("--mdcv", "suggested"))["mastering"] == {"mode": "suggested"}
    for argv in (("--realign", "tout"), ("--l6", "1000:1"), ("--mdcv", "1000")):
        with pytest.raises(JobError):
            build_request(_args(*argv))
    HdrPatchRequest.parse(build_request(_args("--l6", "1000:0.005:900:300")), allow_relative=True)


# --- Recommandations et moments FEL ------------------------------------------------------------


def test_advice_in_json_and_html_only_when_requested(tmp_path):
    report = _report(tmp_path)
    measurement = _measurement(tmp_path)
    advice = build_advice(report, measurement, display_peak=1000)
    data = advice_data(advice)
    assert data["suggested"]["light_levels"] == {"max_cll": 2500, "max_fall": measurement.max_fall(),
                                                 "source": "measure"}
    assert data["tv_simulation"]["file"]["model"] == "maxcll" and data["realignment"] is not None
    assert "advice" not in report_data(report, measurement=measurement)
    assert report_data(report, measurement=measurement, advice=advice)["advice"]["display_peak"] == 1000
    assert "Recommandations" in render_html(report, measurement=measurement, advice=advice) or \
        "Recommendations" in render_html(report, measurement=measurement, advice=advice)


def test_fel_moments_are_written_as_png(tmp_path, monkeypatch):
    from core.fel import engine as fel_engine
    from core.hdr_inspection import fel_analyzer
    from core.hdr_inspection.fel_analyzer import FelReport, MomentImages
    from core.service.ops.hdr import write_fel_moments

    monkeypatch.setattr(fel_engine.FelEngine, "installed", classmethod(lambda cls: object()))
    monkeypatch.setattr(fel_analyzer, "plugin_capture", lambda *a, **k: lambda layer, t, n: [object()])
    monkeypatch.setattr(fel_analyzer, "active_rows", lambda frame, area=None: slice(None))
    codes = np.full((2, 3, 3), 30000, dtype=np.uint16)
    monkeypatch.setattr(fel_analyzer, "moment_images", lambda r, m, rows, t: MomentImages(
        t, codes, codes, np.zeros((2, 3, 3), dtype=np.uint8)))
    report = _report(tmp_path)
    moment = SimpleNamespace(time_s=3725.4)
    fel = FelReport(None, (), (), (moment,))  # type: ignore[arg-type]
    logger = SimpleNamespace(emit=lambda *a, **k: None)
    written = write_fel_moments(report, fel, tmp_path / "m", exposure=1.0, ffmpeg="ffmpeg", threads=2,
                                logger=logger)  # type: ignore[arg-type]
    assert [p.name for p in written] == [f"film.fel-01h02m05s.{s}.png" for s in ("sans-fel", "avec-fel", "ecart")]
    assert all(p.read_bytes().startswith(b"\x89PNG") for p in written)
