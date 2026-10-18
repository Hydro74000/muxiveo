"""HDR Studio : réalignement des métadonnées sur la mesure de l'image, HDR10+ recalculé depuis la mesure."""
from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from core.fel.hdr10plus import statistics_from_measurement, write_hdr10plus_json
from core.hdr_inspection import InspectionReport, audit, build_snapshot
from core.hdr_inspection.hdr10p import parse_hdr10plus_json
from core.hdr_inspection.measure import ImageMeasurement
from core.hdr_inspection.patch import Hdr10PlusEdit, MetadataPatcher, PatchPlan
from core.hdr_inspection.pq import nits_to_pq
from core.hdr_inspection.realign import realign, scene_cuts
from core.i18n import current_language, set_current_language
from tests.test_hdr_inspection import _analyses, _info, _write_hdr10plus


@pytest.fixture(autouse=True)
def _french():
    previous = current_language()
    set_current_language("fra")
    yield
    set_current_language(previous)


def _measurement(peak: float, average: float, frames: int = 10, starts=(0, 5)) -> ImageMeasurement:
    peaks = np.full(frames, peak, dtype=np.float32)
    percentiles = np.tile(np.array([0.1, 5, 20, 60, 120, 200, peak * 0.95], dtype=np.float32), (frames, 1))
    return ImageMeasurement("base", np.stack([peaks] * 3, axis=1), np.full(frames, average, dtype=np.float32),
                            np.full(frames, peak * 0.9, dtype=np.float32), np.full(frames, 0.8, dtype=np.float32),
                            percentiles, np.asarray(starts, dtype=np.int32), np.zeros(1024, np.uint64),
                            (0, 0, 3840, 2160))


def _report(tmp_path, *, max_pq: int, maxscl=(40000, 39000, 38000)) -> InspectionReport:
    """MaxScl en dixièmes de nit (JSON HDR10+)."""
    dovi, _ = _analyses(tmp_path, max_pq=max_pq)
    hdr10plus = parse_hdr10plus_json(_write_hdr10plus(tmp_path / "h2.json", [5, 5], maxscl=maxscl))
    snapshot = build_snapshot(_info(tmp_path / "a.mkv"))
    return InspectionReport(snapshot, dovi, hdr10plus, audit(snapshot, dovi, hdr10plus), dynamic=True)


def test_realign_proposes_only_visible_deviations(tmp_path):
    # Image à 1600 nits : MaxCLL 1000 du fichier écrête sur un écran de 1000 nits, L6 contredit,
    # MaxScl HDR10+ à 4000 (image assombrie), L1 (≈ 1000 nits) sous l'image.
    report = _report(tmp_path, max_pq=3079)
    measurement = _measurement(1600, 320)
    result = realign(report, measurement, 1000)
    keys = [p.key for p in result.proposals]
    assert keys[:1] == ["static"] and "l6" in keys and "hdr10plus" in keys and "l1" in keys
    assert result.light_levels == (1600, 320)
    assert result.l6 is not None and result.l6[2:] == (1600, 320)
    assert result.hdr10plus_scenes is not None and result.hdr10plus_scenes.tolist() == [0, 5]
    assert not next(p for p in result.proposals if p.key == "l1").applicable
    assert "écrêtage" in next(p for p in result.proposals if p.key == "static").impact
    assert [p.key for p in result.applicable()] == [k for k in keys if k != "l1"]


def test_realign_estimate_never_lowers_values_nor_recomputes_hdr10plus(tmp_path):
    report = _report(tmp_path, max_pq=3079)
    estimate = dataclasses.replace(_measurement(1600, 100), sampled=3)
    result = realign(report, estimate, 1000)
    keys = [p.key for p in result.proposals]
    assert result.light_levels == (1600, 400)  # MaxFALL du fichier (400) conservée : estimation plus basse
    assert "hdr10plus" not in keys and result.hdr10plus_scenes is None
    assert all("mesure rapide" in p.impact for p in result.proposals)


def test_realign_silent_when_metadata_fits_the_picture(tmp_path):
    # Image à 950 nits : MaxCLL 1000/400, MDCV 1000, L6 1000/400, MaxScl et L1 ≈ image : rien à proposer.
    report = _report(tmp_path, max_pq=int(round(float(nits_to_pq(950)) * 4095)), maxscl=(9500, 9000, 8500))
    result = realign(report, _measurement(950, 400), 1000)
    assert result.proposals == ()


def test_statistics_from_measurement_and_scene_cuts(tmp_path):
    measurement = _measurement(800, 100, frames=6, starts=(0,))
    statistics = statistics_from_measurement(measurement, np.array([0, 3]))
    assert [(s.first, s.frames) for s in statistics.scenes] == [(0, 3), (3, 3)]
    path = tmp_path / "h.json"
    assert write_hdr10plus_json(statistics, path) == 2
    document = json.loads(path.read_text())
    assert document["JSONInfo"]["HDR10plusProfile"] == "A" and len(document["SceneInfo"]) == 6
    assert document["SceneInfo"][0]["LuminanceParameters"]["MaxScl"] == [8000, 8000, 8000]
    flat = dataclasses.replace(measurement, average=np.array([10] * 3 + [400] * 21, dtype=np.float32),
                               max_rgb=np.ones((24, 3), np.float32), scene_starts=np.array([0], np.int32))
    assert scene_cuts(flat).tolist() == [0]  # coupe trop proche du début (< 12 images) ignorée
    longer = dataclasses.replace(flat, average=np.array([10] * 20 + [400] * 20, dtype=np.float32))
    assert scene_cuts(longer).tolist() == [0, 20]


class _Tools:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.injected: dict | None = None

    def __call__(self, cmd):
        cmd = [str(c) for c in cmd]
        self.commands.append(cmd)
        if Path(cmd[0]).name == "hdr10plus_tool" and cmd[1] == "inject":
            self.injected = json.loads(Path(cmd[cmd.index("-j") + 1]).read_text())
        if Path(cmd[0]).name == "hdr10plus_tool" and cmd[1] in ("remove", "inject"):
            shutil.copy(cmd[cmd.index("-i") + 1], cmd[cmd.index("-o") + 1])
        return ""


def test_patcher_injects_hdr10plus_recomputed_from_measurement(tmp_path):
    source = tmp_path / "film.hevc"
    source.write_bytes(b"\x00\x00\x00\x01" + b"\x40\x01" + b"\x00" * 64)
    work = tmp_path / "work"
    work.mkdir()
    tools = _Tools()
    patcher = MetadataPatcher(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool", hdr10plus_bin="hdr10plus_tool",
                              run_cmd=tools, work_dir=work)
    edit = Hdr10PlusEdit(targeted_display=600, recompute=(_measurement(700, 50, frames=4, starts=(0,)), np.array([0, 2])))
    assert not edit.is_empty()
    patcher.run(PatchPlan(source, 0, tmp_path / "out.hevc", hdr10plus=edit))
    verbs = [(Path(c[0]).name, c[1]) for c in tools.commands]
    assert verbs == [("hdr10plus_tool", "remove"), ("hdr10plus_tool", "inject")]  # aucune extraction
    assert tools.injected is not None and tools.injected["JSONInfo"]["HDR10plusProfile"] == "A"
    assert tools.injected["SceneInfoSummary"]["SceneFrameNumbers"] == [2, 2]
    assert {i["TargetedSystemDisplayMaximumLuminance"] for i in tools.injected["SceneInfo"]} == {600}
    assert not list(work.iterdir())
