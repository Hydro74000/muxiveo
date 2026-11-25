"""Merge DoVi — mode Automatique : intégration de l'alignement au workflow et au panneau (sans média)."""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from core.workflows.merge_dovi import (
    FrameCountResult,
    HDRFlags,
    MergeDoviWorkflow,
    StaticHdrMetadata,
    WorkflowError,
    WorkflowStep,
    _CancelledError,
    _WorkflowPaths,
)
from core.workflows.merge_dovi_align import session as session_mod
from core.workflows.merge_dovi_align.align import AlignmentError, AlignmentMap, Segment
from core.workflows.merge_dovi_align.chapters import project_chapters
from core.workflows.merge_dovi_align.frames import FrameTable, VideoProps
from core.workflows.merge_dovi_align.session import AlignmentMode, AlignmentOptions, AlignmentResult
from core.workflows.merge_dovi_align.static_hdr import resolve_static_hdr


def _paths(tmp_path: Path, film1: Path) -> _WorkflowPaths:
    (tmp_path / "work").mkdir(exist_ok=True)
    (tmp_path / "out").mkdir(exist_ok=True)
    return _WorkflowPaths.from_config(tmp_path / "work", tmp_path / "out", film1, "out")


def test_framecount_does_not_refuse_a_mismatch_in_automatic_mode(tmp_path: Path) -> None:
    film1, film2 = tmp_path / "a.mkv", tmp_path / "b.mkv"
    film1.write_bytes(b"1")
    film2.write_bytes(b"2")
    wf = MergeDoviWorkflow()
    cast(Any, wf)._get_framecount = lambda path: {film1: 208606, film2: 208646}[path]
    with pytest.raises(WorkflowError):
        wf._step_framecount(film1, film2)
    result = wf._step_framecount(film1, film2, aligned=True)
    assert (result.fc1, result.fc2) == (208606, 208646)


def _result(rpu: Path, hdr: Path | None, *, identity: bool = False) -> AlignmentResult:
    segments = (Segment(0, 100, 0),) if identity else (Segment(0, 100, 16),)
    removed = () if identity else ((0, 16), (116, 24))
    table = FrameTable(Path("x.mkv"), VideoProps("hevc", 64, 64, Fraction(1), "tv", "", "", Fraction(1, 1000)),
                       True, "", tuple(range(0, 4200, 42)))
    alignment = AlignmentMap(100, 100 if identity else 140, segments, removed, (), np.asarray([0]),
                             {"points_on_cuts": 5, "points_by_image": 2, "film2_full": False})
    return AlignmentResult(alignment, None if identity else cast(Any, object()), rpu, hdr,
                           resolve_static_hdr(None, None), None, table, table, ["Alignement : test."])


def test_align_step_substitutes_edited_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    film1 = tmp_path / "film1.mkv"
    film2 = tmp_path / "film2.mkv"
    film1.write_bytes(b"x")
    film2.write_bytes(b"x")
    paths = _paths(tmp_path, film1)
    paths.film2_rpu.write_bytes(b"old")
    paths.film2_hdr10plus.write_text("{}")
    edited_rpu = paths.work_dir / "align_rpu_final.bin"
    edited_rpu.write_bytes(b"new")
    edited_json = paths.work_dir / "align_hdr10plus.json"
    edited_json.write_text('{"edited": true}')
    captured: dict = {}

    def fake_run_alignment(**kwargs):
        captured.update(kwargs)
        return _result(edited_rpu, edited_json)

    monkeypatch.setattr("core.workflows.merge_dovi.run_alignment", fake_run_alignment)
    wf = MergeDoviWorkflow()
    steps: list = []
    wf.step_started.connect(steps.append)
    result = wf._step_align(cast(Any, object()), film2, paths, HDRFlags(has_dovi=True, has_hdr10plus=True),
                            FrameCountResult(100, 140, 40), AlignmentOptions(), StaticHdrMetadata(),
                            StaticHdrMetadata())
    assert result is not None and steps == [WorkflowStep.ALIGN]
    assert paths.film2_rpu.read_bytes() == b"new" and "edited" in paths.film2_hdr10plus.read_text()
    assert captured["film2_scan"] == film2 and captured["film1_frames"] == 100


def test_align_step_refusal_stops_or_falls_back_to_hdr10(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    film1 = tmp_path / "film1.mkv"
    film2 = tmp_path / "film2.mkv"
    film1.write_bytes(b"x")
    film2.write_bytes(b"x")
    paths = _paths(tmp_path, film1)
    paths.film2_rpu.write_bytes(b"old")

    def refuse(**_kwargs):
        raise AlignmentError("contenus différents")

    monkeypatch.setattr("core.workflows.merge_dovi.run_alignment", refuse)
    wf = MergeDoviWorkflow()
    args = (cast(Any, object()), film2, paths, HDRFlags(has_dovi=True), FrameCountResult(100, 140, 40),
            AlignmentOptions(), StaticHdrMetadata(), StaticHdrMetadata())
    with pytest.raises(WorkflowError, match="contenus différents"):
        wf._step_align(*args)
    assert wf._step_align(*args, allow_hdr10_fallback=True) is None


def test_align_step_cancellation_is_a_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    film1 = tmp_path / "film1.mkv"
    film2 = tmp_path / "film2.mkv"
    film1.write_bytes(b"x")
    film2.write_bytes(b"x")
    paths = _paths(tmp_path, film1)
    paths.film2_rpu.write_bytes(b"old")

    def cancelled(**_kwargs):
        raise InterruptedError

    monkeypatch.setattr("core.workflows.merge_dovi.run_alignment", cancelled)
    with pytest.raises(_CancelledError):
        MergeDoviWorkflow()._step_align(cast(Any, object()), film2, paths, HDRFlags(has_dovi=True),
                                        FrameCountResult(100, 100, 0), AlignmentOptions(), StaticHdrMetadata(),
                                        StaticHdrMetadata())


def test_canonical_shift_is_measured_on_the_video(tmp_path: Path) -> None:
    wf = MergeDoviWorkflow()
    values = {tmp_path / "a.mp4": 1.5, tmp_path / "b.mkv": 0.0}
    cast(Any, wf)._first_video_pts = lambda path: values[path]
    assert wf._canonical_shift_ms(tmp_path / "a.mp4", tmp_path / "b.mkv") == -1500
    cast(Any, wf)._first_video_pts = lambda path: None
    assert wf._canonical_shift_ms(tmp_path / "a.mp4", tmp_path / "b.mkv") == 0


def _table(frames: int, start: int = 0) -> FrameTable:
    props = VideoProps("hevc", 64, 64, Fraction(1), "tv", "", "", Fraction(1, 1000))
    return FrameTable(Path("x.mkv"), props, True, "", tuple(start + 40 * k for k in range(frames)))


def _chapter(start_s: float, end_s: float | None, name: str, uid: int, children=()):
    return SimpleNamespace(start_ns=int(start_s * 1e9), end_ns=None if end_s is None else int(end_s * 1e9),
                           displays=((name, "fra"),), uid=uid, children=tuple(children))


def test_film2_chapters_are_projected_through_the_alignment() -> None:
    # Film 2 : 16 images en tête et 24 en fin de plus que Film 1 (40 ms par image).
    alignment = AlignmentMap(100, 140, (Segment(0, 100, 16),), ((0, 16), (116, 24)), (), np.asarray([0]))
    film1, film2 = _table(100), _table(140)
    edition = SimpleNamespace(chapters=(
        _chapter(0.0, 2.0, "Logo", 1),                  # dans la plage retirée → début de Film 1
        _chapter(2.0, 4.64, "Film", 2),                 # image 50 de Film 2 → image 34 de Film 1
        _chapter(4.64, 5.6, "Générique final", 3),      # plage finale retirée → supprimé
    ))
    projected, dropped = project_chapters((edition,), alignment, film1, film2)
    assert projected is not None and dropped == 1
    assert [(c.name, round(c.timecode_s, 3)) for c in projected] == [("Logo", 0.0), ("Film", 1.36)]
    assert projected[1].end_s == pytest.approx(4.0)     # fin bornée à la fin de la dernière image conservée
    assert project_chapters((edition, edition), alignment, film1, film2) == (None, 0)


def test_panel_alignment_options(tmp_path, qt_app, monkeypatch) -> None:
    import ui.panels.merge_dovi_panel as panel_mod
    from tests.test_merge_dovi_panel import _DummyConfig, _FakeWorkflow

    monkeypatch.setattr(panel_mod, "MergeDoviWorkflow", lambda **kw: _FakeWorkflow(**kw))
    panel = panel_mod.MergeDoviPanel(cast(Any, _DummyConfig(tmp_path)))
    section = panel._config_section
    assert section.alignment_options == AlignmentOptions()
    section._fill_cb.setChecked(True)
    section._verify_cb.setChecked(True)
    assert section.alignment_options == AlignmentOptions(AlignmentMode.AUTO, True, True)
    section._align_combo.setCurrentIndex(1)
    assert section.alignment_options == AlignmentOptions(AlignmentMode.STRICT, False, False)
    assert not section._fill_cb.isEnabled() and not section._verify_cb.isEnabled()


def test_cuda_decode_needs_an_nvidia_driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(session_mod.sys, "platform", "darwin")
    assert not session_mod.cuda_decode_available("ffmpeg")
    monkeypatch.setattr(session_mod.sys, "platform", "linux")
    monkeypatch.setattr(session_mod, "Path", lambda *a: tmp_path / "absent")
    assert not session_mod.cuda_decode_available("ffmpeg")
