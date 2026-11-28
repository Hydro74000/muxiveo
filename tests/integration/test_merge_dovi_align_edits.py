"""Merge DoVi — alignement : application réelle des plans d'édition (dovi_tool, hdr10plus_tool)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from core.workflows.merge_dovi_align.active_area import ActiveAreaPlan
from core.workflows.merge_dovi_align.align import AlignmentMap, Segment, Uncovered
from core.workflows.merge_dovi_align.edits import apply_hdr10plus, apply_rpu, build_edit_plan
from core.workflows.merge_dovi_align.signals import hdr10plus_digests, rpu_digests

pytestmark = pytest.mark.skipif(not (shutil.which("dovi_tool") and shutil.which("hdr10plus_tool")),
                                reason="dovi_tool ou hdr10plus_tool absent")


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"{' '.join(cmd)}\n{result.stderr}"
    return result.stdout


def _marked_rpu(root: Path, frames: int) -> Path:
    config = root / "gen.json"
    config.write_text(json.dumps({
        "cm_version": "V29", "length": frames,
        "level6": {"max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
                   "max_content_light_level": 449, "max_frame_average_light_level": 331},
        "shots": [{"start": start, "duration": min(10, frames - start)} for start in range(0, frames, 10)],
    }), encoding="utf-8")
    raw = root / "raw.bin"
    run(["dovi_tool", "generate", "-j", str(config), "-o", str(raw)])
    marks = root / "marks.json"
    marks.write_text(json.dumps({"active_area": {
        "crop": False, "presets": [{"id": i, "left": 0, "right": 0, "top": 200 + i, "bottom": 276} for i in range(frames)],
        "edits": {f"{i}-{i}": i for i in range(frames)}}}), encoding="utf-8")
    marked = root / "marked.bin"
    run(["dovi_tool", "editor", "-i", str(raw), "-j", str(marks), "-o", str(marked)])
    return marked


def _marked_hdr10plus(root: Path, frames: int) -> Path:
    from core.fel.hdr10plus import FrameRecord, FrameStatistics, SceneRecord, hdr10plus_document

    records = tuple(FrameRecord(True, (100.0, 100.0, 100.0), float(i + 1), 100.0, 0.0,
                                (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0)) for i in range(frames))
    path = root / "h.json"
    path.write_text(json.dumps(hdr10plus_document(FrameStatistics(64, 64, records,
                                                                  tuple(SceneRecord(i, 1) for i in range(frames))))),
                    encoding="utf-8")
    return path


def test_imax_like_plan_is_applied_and_checked(tmp_path: Path) -> None:
    rpu = _marked_rpu(tmp_path, 140)
    hdr = _marked_hdr10plus(tmp_path, 140)
    # Film 1 = images 16…115 de Film 2, trois images noires de Film 1 sans correspondance en 50…52.
    gap = Uncovered(50, 3, True, 65, True)
    alignment = AlignmentMap(103, 140, (Segment(0, 50, 16), Segment(53, 50, 66)), ((0, 16), (116, 24)), (gap,),
                             np.asarray([0, 4, 14, 24, 34, 44, 50, 53, 57, 67, 77, 87, 97]))
    area = ActiveAreaPlan(False, ((0, 103, (0, 0, 68, 68)),), "test")
    level6 = {"max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
              "max_content_light_level": 452, "max_frame_average_light_level": 312}
    plan = build_edit_plan(alignment, active_area=area, level6=level6)
    out = apply_rpu(plan, rpu, tmp_path, dovi_tool="dovi_tool", run=run)
    assert len(rpu_digests(out)) == 103
    summary = run(["dovi_tool", "info", "-i", str(out), "--summary"])
    assert "MaxCLL: 452" in summary and "top=68" in summary
    edited = apply_hdr10plus(plan, hdr, tmp_path, hdr10plus_tool="hdr10plus_tool", run=run)
    source = hdr10plus_digests(hdr)
    assert hdr10plus_digests(edited) == [source[j] for j in plan.sources.tolist()]


def test_kept_l5_follows_the_projection(tmp_path: Path) -> None:
    rpu = _marked_rpu(tmp_path, 60)
    alignment = AlignmentMap(50, 60, (Segment(0, 50, 5),), ((0, 5), (55, 5)), (), np.asarray([0, 5, 15, 25, 35, 45]))
    plan = build_edit_plan(alignment, active_area=ActiveAreaPlan(True, (), "test"), level6=None)
    out = apply_rpu(plan, rpu, tmp_path, dovi_tool="dovi_tool", run=run)
    assert len(rpu_digests(out)) == 50
