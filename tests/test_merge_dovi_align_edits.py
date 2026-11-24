"""Merge DoVi — alignement : L5 de Film 1, HDR10 statique unique, plans d'édition (sans média)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from core.workflows.merge_dovi_align.active_area import l5_per_frame, plan_active_area
from core.workflows.merge_dovi_align.align import AlignmentError, AlignmentMap, Segment, Uncovered
from core.workflows.merge_dovi_align.edits import EditRefused, _cut_ranges, build_edit_plan
from core.workflows.merge_dovi_align.probe import Signatures
from core.workflows.merge_dovi_align.static_hdr import level6_edit, resolve_static_hdr

MDCV_1000 = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"
MDCV_4000 = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,50)"


def test_static_hdr_is_resolved_per_field_with_provenance() -> None:
    film1 = SimpleNamespace(master_display=MDCV_1000, max_cll="")
    film2 = SimpleNamespace(master_display=MDCV_4000, max_cll="1200,300")
    resolved = resolve_static_hdr(film1, film2)
    assert (resolved.master_display, resolved.max_cll) == (MDCV_1000, "1200,300")
    assert (resolved.master_display_from, resolved.max_cll_from) == ("film1", "film2")
    assert "Film 1" in resolved.describe() and "Film 2" in resolved.describe()


def test_level6_follows_the_resolved_static_values() -> None:
    resolved = resolve_static_hdr(SimpleNamespace(master_display=MDCV_1000, max_cll="452,312"), None)
    assert level6_edit(resolved, (1000, 1, 449, 331)) == {
        "max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
        "max_content_light_level": 452, "max_frame_average_light_level": 312}
    assert level6_edit(resolved, (1000, 1, 452, 312)) is None          # déjà identique
    partial = resolve_static_hdr(SimpleNamespace(master_display="", max_cll="600,200"), None)
    assert level6_edit(partial, (4000, 50, 1000, 400))["max_display_mastering_luminance"] == 4000
    assert level6_edit(partial, None) is None                          # écran de mastering inconnu partout


def _signatures(bars: list[tuple[int, int]], *, width: int = 384, height: int = 216) -> Signatures:
    n = len(bars)
    top = np.asarray([b[0] for b in bars], dtype=np.int16)
    bottom = np.asarray([b[1] for b in bars], dtype=np.int16)
    zeros = np.where(top >= 0, 0, -1).astype(np.int16)
    return Signatures(width, height, np.arange(n), top, bottom, zeros, zeros,
                      np.zeros((n, 16, 24), dtype=np.uint8), np.zeros(n, np.float32), np.zeros(n, np.float32),
                      np.zeros(n, np.float32))


def _identity(n: int) -> AlignmentMap:
    return AlignmentMap(n, n, (Segment(0, n, 0),), (), (), np.asarray([0]))


def test_active_area_snaps_dark_shots_and_never_covers_visible_rows() -> None:
    # Plans de 10 images : bords francs à 7 (IMAX 1.90 réduit), un plan sombre (barres mesurées 30 à 40),
    # un plan entièrement noir, un plan à 28 (2.39) sur la moitié du film.
    bars = [(7, 7)] * 40 + [(30 + k % 10, 35) for k in range(10)] + [(-1, -1)] * 10 + [(28, 28)] * 40
    sig = _signatures(bars)
    cuts = np.arange(0, 100, 10)
    plan = plan_active_area(sig, cuts, _identity(100), film2_l5=None, film2_size=(3840, 2160))
    areas = {start: area for start, _stop, area in plan.ranges}
    assert not plan.keep and areas[0] == (0, 0, 7, 7)
    assert areas[40] == (0, 0, 28, 28)       # plan sombre : ramené à 28 (dominante ≤ 30), jamais 30+
    for start, stop, (_l, _r, top, bottom) in plan.ranges:
        visible = sig.top[start:stop] >= 0
        if np.any(visible):
            assert top <= sig.top[start:stop][visible].min() and bottom <= sig.bottom[start:stop][visible].min()


def test_active_area_kept_when_film2_l5_matches_film1_exactly() -> None:
    sig = _signatures([(7, 7)] * 50)
    film2_l5 = np.tile(np.asarray([0, 0, 7, 7]), (50, 1))
    plan = plan_active_area(sig, np.asarray([0, 25]), _identity(50), film2_l5=film2_l5, film2_size=(384, 216))
    assert plan.keep
    covering = np.tile(np.asarray([0, 0, 28, 28]), (50, 1))   # L5 2.39 sur une image IMAX 1.90
    plan = plan_active_area(sig, np.asarray([0, 25]), _identity(50), film2_l5=covering, film2_size=(384, 216))
    assert not plan.keep and "visibles" in plan.reason and plan.ranges[0][2] == (0, 0, 7, 7)
    plan = plan_active_area(sig, np.asarray([0, 25]), _identity(50), film2_l5=film2_l5, film2_size=(3840, 2160))
    assert not plan.keep and "×" in plan.reason


def test_l5_export_is_read_per_frame(tmp_path: Path) -> None:
    path = tmp_path / "l5.json"
    path.write_text(json.dumps({"crop": True, "presets": [{"id": 0, "left": 0, "right": 0, "top": 276, "bottom": 276},
                                                          {"id": 1, "left": 0, "right": 0, "top": 0, "bottom": 0}],
                                "edits": {"0-15": 0, "16-39": 1, "40-99": 0}}), encoding="utf-8")
    areas = l5_per_frame(path, 100)
    assert areas[0].tolist() == [0, 0, 276, 276] and areas[20].tolist() == [0, 0, 0, 0]


def _imax_like() -> AlignmentMap:
    # Film 2 : 16 images en tête, 24 en fin de plus (cas réel, réduit).
    return AlignmentMap(100, 140, (Segment(0, 100, 16),), ((0, 16), (116, 24)), (), np.asarray([0, 30, 70]))


def test_edit_plan_for_a_constant_offset() -> None:
    plan = build_edit_plan(_imax_like(), allow_fill=False, active_area=None, level6=None)
    assert plan.position_edit() == {"remove": ["116-139", "0-15"]}
    assert plan.sources.tolist() == list(range(16, 116)) and not plan.duplicates


def test_black_gap_is_filled_automatically_other_gaps_need_consent() -> None:
    gap = Uncovered(50, 3, True, 65, True)
    alignment = AlignmentMap(100, 113, (Segment(0, 50, 16), Segment(53, 47, 66)), ((0, 16),), (gap,),
                             np.asarray([0, 50, 53]))
    plan = build_edit_plan(alignment, allow_fill=False, active_area=None, level6=None)
    assert plan.duplicates == ((49, 50, 3),) and plan.sources[50:53].tolist() == [65, 65, 65]
    content = Uncovered(50, 3, False, 65, False)
    visible = AlignmentMap(100, 113, alignment.segments, ((0, 16),), (content,), alignment.expected_cuts)
    with pytest.raises(EditRefused) as refused:
        build_edit_plan(visible, allow_fill=False, active_area=None, level6=None)
    assert refused.value.ranges == ((50, 53),)
    accepted = build_edit_plan(visible, allow_fill=True, active_area=None, level6=None)
    assert accepted.warnings and accepted.duplicates == ((49, 50, 3),)


def test_head_gap_copies_the_following_frame() -> None:
    gap = Uncovered(0, 10, True, 0, True)
    alignment = AlignmentMap(110, 100, (Segment(10, 100, 0),), (), (gap,), np.asarray([0, 10]))
    plan = build_edit_plan(alignment, allow_fill=False, active_area=None, level6=None)
    assert plan.duplicates == ((0, 0, 10),) and plan.sources[:10].tolist() == [0] * 10


def test_incoherent_map_is_refused() -> None:
    broken = AlignmentMap(100, 100, (Segment(0, 40, 0), Segment(50, 50, 50)), (), (), np.asarray([0]))
    with pytest.raises(AlignmentError):
        build_edit_plan(broken, allow_fill=False, active_area=None, level6=None)


def test_scene_cut_ranges_cover_every_frame() -> None:
    assert _cut_ranges(np.asarray([0, 3, 4]), 7) == {"0-0": True, "1-2": False, "3-3": True, "4-4": True,
                                                     "5-6": False}
