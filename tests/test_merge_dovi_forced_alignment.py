"""
tests/test_merge_dovi_forced_alignment.py — Film 2 indécodable (N8, décision DC ; plan de remédiation 5.2.0,
lot 12) : refus immédiat avec la cause d'origine, prioritaire sur le repli HDR10 seul ; alignement forcé sur les
seules coupes des métadonnées (aucune lecture de Film 2, jamais annoncé vérifié) proposé par le panneau.
"""

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
    MergeDoviFailure,
    MergeDoviWorkflow,
    StaticHdrMetadata,
    WorkflowError,
    WorkflowStep,
)
from core.workflows.merge_dovi_align import session as session_mod
from core.workflows.merge_dovi_align.align import (
    AlignmentError,
    AlignmentMap,
    Film2Frames,
    Film2UndecodableError,
    Segment,
    align,
)
from core.workflows.merge_dovi_align.frames import FrameTable, FrameTableError, VideoProps
from core.workflows.merge_dovi_align.probe import ProbeError
from core.workflows.merge_dovi_align.process import ProcessHooks
from core.workflows.merge_dovi_align.session import AlignmentOptions, AlignmentResult
from core.workflows.merge_dovi_align.signals import Film2Metadata
from core.workflows.merge_dovi_align.static_hdr import resolve_static_hdr
from tests.test_merge_dovi_align_engine import Scene, film, metadata_for, render, whole
from tests.test_merge_dovi_alignment_workflow import _paths


@pytest.fixture(autouse=True)
def _french(french_messages):
    """Messages comparés dans leur langue d'origine."""


def _never(*_args: Any, **_kwargs: Any):
    pytest.fail("Film 2 lu en alignement forcé")


def _forced(film1, film2, **kwargs) -> AlignmentMap:
    meta = metadata_for(film2)
    source = Film2Frames(meta.frames, window=_never, full_loader=_never)
    return align(render(film1, 4), meta, source, frame_rate=0.5, metadata_only=True, **kwargs)


def _verified(film1, film2) -> AlignmentMap:
    sig2 = render(film2, 18)
    meta = metadata_for(film2)
    source = Film2Frames(meta.frames, window=lambda a, n: sig2.slice(a, a + n), full_loader=lambda: sig2)
    return align(render(film1, 4), meta, source, frame_rate=0.5)


# ── Moteur ─────────────────────────────────────────────────────────────────


def test_forced_map_equals_the_verified_map_at_the_cuts_without_reading_film2() -> None:
    rng = np.random.default_rng(2)
    content = whole(film(rng))
    film2 = [(Scene(rng, 16, black=True), 0, 16), *content, (Scene(rng, 24, black=True), 0, 24)]
    scenes = film(np.random.default_rng(4))
    missing = whole(scenes[:30] + scenes[31:]), whole(scenes)
    for film1_side, film2_side in ((content, film2), missing):
        forced, verified = _forced(film1_side, film2_side), _verified(film1_side, film2_side)
        assert forced.segments == verified.segments and forced.removed == verified.removed
        assert forced.metadata_only and not verified.metadata_only
        assert forced.stats["shot_concordance"] is None and forced.stats["film2_decoded"] == 0


def test_forced_alignment_without_usable_cuts_is_refused() -> None:
    scenes = whole(film(np.random.default_rng(1), shots=3))
    meta = metadata_for(scenes)
    flat = Film2Metadata(meta.frames, meta.change_points, np.asarray([0]), meta.luminance, meta.kinds, meta.peak)
    with pytest.raises(AlignmentError, match="aucune coupe exploitable"):
        align(render(scenes, 4), flat, Film2Frames(flat.frames, window=_never, full_loader=_never),
              frame_rate=0.5, metadata_only=True)


def test_forced_alignment_logs_an_unverified_warning() -> None:
    logs: list[str] = []
    scenes = whole(film(np.random.default_rng(3)))
    _forced(scenes, scenes, log=logs.append)
    assert any(line.startswith("[WARN]") and "non vérifié sur l'image" in line for line in logs)
    assert not any("Concordance de l'image" in line for line in logs)


# ── Session ────────────────────────────────────────────────────────────────


def _table(frames: int, *, indexable: bool = True) -> FrameTable:
    props = VideoProps("hevc", 144, 96, Fraction(2), "tv", "", "", Fraction(1, 1000))
    return FrameTable(Path("film2.mkv"), props, indexable, "", tuple(500 * k for k in range(frames)))


def _session(monkeypatch, tmp_path: Path, options: AlignmentOptions, *, cancelled=lambda: False,
             table=None, window=None, full=None, logs: list[str] | None = None):
    scenes = whole(film(np.random.default_rng(6)))
    sig1 = render(scenes, 4)
    meta = metadata_for(scenes)
    monkeypatch.setattr(session_mod, "combine", lambda _tracks: meta)
    monkeypatch.setattr(session_mod, "read_frame_table", table or _never)
    monkeypatch.setattr(session_mod, "decode_window", window or _never)
    monkeypatch.setattr(session_mod, "decode_all", full or _never)
    film1_pass = SimpleNamespace(wait=lambda *_a: (_table(len(sig1)), sig1))
    return session_mod.run_alignment(
        film1_pass=cast(Any, film1_pass), film1_frames=len(sig1), film2=tmp_path / "film2.mkv", presence={},
        rpu=None, hdr10plus=None, static1=None, static2=None, options=options, tools=session_mod.AlignmentTools(),
        work_dir=tmp_path / "work", hooks=ProcessHooks(cancelled=cancelled), decoder=cast(Any, None),
        log=(logs if logs is not None else []).append, run_tool=lambda _cmd: "")


def test_unreadable_frame_table_is_refused_at_once_with_its_cause(monkeypatch, tmp_path) -> None:
    def unreadable(*_a, **_k):
        raise FrameTableError("Sortie FFprobe illisible : boum")

    with pytest.raises(Film2UndecodableError, match="boum") as caught:
        _session(monkeypatch, tmp_path, AlignmentOptions(), table=unreadable)
    assert caught.value.metadata_alignment_possible and caught.value.code == "film2_undecodable"


def test_readable_table_then_failed_decoding_is_refused(monkeypatch, tmp_path) -> None:
    def failing(*_a, **_k):
        raise ProbeError("décodage HEVC en échec")

    with pytest.raises(Film2UndecodableError, match="décodage HEVC en échec"):
        _session(monkeypatch, tmp_path, AlignmentOptions(), table=lambda *_a, **_k: _table(len(metadata_for(
            whole(film(np.random.default_rng(6)))).luminance)), window=failing, full=failing)


def test_cancellation_during_decoding_is_never_converted(monkeypatch, tmp_path) -> None:
    flag = {"cancelled": False}

    def killed(*_a, **_k):
        flag["cancelled"] = True  # processus tué par l'annulation
        raise ProbeError("ffmpeg tué")

    frames = len(metadata_for(whole(film(np.random.default_rng(6)))).luminance)
    with pytest.raises(InterruptedError):
        _session(monkeypatch, tmp_path, AlignmentOptions(), cancelled=lambda: flag["cancelled"],
                 table=lambda *_a, **_k: _table(frames), window=killed, full=killed)


def test_forced_session_never_reads_film2_and_never_claims_verification(monkeypatch, tmp_path) -> None:
    logs: list[str] = []
    result = _session(monkeypatch, tmp_path, AlignmentOptions(verify_film2=True, metadata_only=True), logs=logs)
    assert result.map.metadata_only and result.film2_table is None
    assert any("sans objet" in line for line in logs)  # D6 ignoré, signalé
    assert all("vérifié :" not in note and "concordance" not in note.lower() for note in result.notes)
    assert any("non vérifiée" in note for note in result.notes)


# ── Workflow ───────────────────────────────────────────────────────────────


def _align_args(tmp_path: Path, options: AlignmentOptions):
    film1, film2 = tmp_path / "film1.mkv", tmp_path / "film2.mkv"
    film1.write_bytes(b"x")
    film2.write_bytes(b"x")
    paths = _paths(tmp_path, film1)
    paths.film2_rpu.write_bytes(b"rpu")
    return (cast(Any, object()), film2, paths, HDRFlags(has_dovi=True), FrameCountResult(100, 100, 0), options,
            StaticHdrMetadata(), StaticHdrMetadata(), {})


def test_undecodable_refusal_wins_over_the_hdr10_fallback(tmp_path, monkeypatch) -> None:
    """Film 1 SDR (repli HDR10 autorisé) + Film 2 indécodable : refus DC structuré, jamais HDR10 seul."""
    def undecodable(**_kwargs):
        raise Film2UndecodableError("Film 2 non décodable (boum).", metadata_alignment_possible=True)

    monkeypatch.setattr("core.workflows.merge_dovi.run_alignment", undecodable)
    with pytest.raises(WorkflowError, match="boum") as caught:
        MergeDoviWorkflow()._step_align(*_align_args(tmp_path, AlignmentOptions()), allow_hdr10_fallback=True)
    assert caught.value.detail == MergeDoviFailure("film2_undecodable", True)


def test_forced_mode_failure_is_never_an_hdr10_fallback(tmp_path, monkeypatch) -> None:
    def refuse(**_kwargs):
        raise AlignmentError("Alignement forcé impossible : aucune coupe exploitable.")

    monkeypatch.setattr("core.workflows.merge_dovi.run_alignment", refuse)
    with pytest.raises(WorkflowError, match="aucune coupe"):
        MergeDoviWorkflow()._step_align(*_align_args(tmp_path, AlignmentOptions(metadata_only=True)),
                                        allow_hdr10_fallback=True)


def test_forced_summary_never_claims_verification_or_concordance(tmp_path, monkeypatch) -> None:
    table = _table(100)
    forced = AlignmentMap(100, 100, (Segment(0, 100, 0),), (), (), np.asarray([0]),
                          {"metadata_only": True, "shot_concordance": None})
    monkeypatch.setattr("core.workflows.merge_dovi.run_alignment", lambda **_k: AlignmentResult(
        forced, None, None, None, resolve_static_hdr(None, None), None, table, None, ["note"]))
    wf = MergeDoviWorkflow()
    finished: list[Any] = []
    wf.step_finished.connect(lambda _step, result: finished.append(result))
    wf._step_align(*_align_args(tmp_path, AlignmentOptions(metadata_only=True)))
    summary = finished[-1].message
    assert "non vérifiée" in summary and "concordance" not in summary and "Alignement vérifié" not in summary


def test_forced_frame_count_step_never_claims_frame_by_frame(tmp_path) -> None:
    film1, film2 = tmp_path / "a.mkv", tmp_path / "b.mkv"
    wf = MergeDoviWorkflow()
    cast(Any, wf)._get_framecount = lambda path: 100
    lines: list[str] = []
    wf.step_progress.connect(lambda _step, line: lines.append(line))
    wf._step_framecount(film1, film2, aligned=True, metadata_only=True)
    assert lines and not any("image par image" in line for line in lines)
    assert any("non vérifiée" in line for line in lines)


def test_structured_failure_reaches_workflow_failed(tmp_path) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"x")
    wf = MergeDoviWorkflow()
    detail = MergeDoviFailure("film2_undecodable", True)

    def refused(*_args):
        raise WorkflowError(WorkflowStep.ALIGN, "Film 2 non décodable.", detail)

    cast(Any, wf)._step_validate = refused
    failures: list[tuple] = []
    wf.workflow_failed.connect(lambda step, message, received: failures.append((step, message, received)))
    wf._run(film1, tmp_path / "film2.mkv", _paths(tmp_path, film1), cast(Any, None))
    assert failures == [(WorkflowStep.ALIGN, "Film 2 non décodable.", detail)]


# ── Panneau ────────────────────────────────────────────────────────────────


def _panel(tmp_path, monkeypatch, *, answer: bool, global_controls: bool = False):
    import ui.panels.merge_dovi_panel as panel_mod
    from tests.test_merge_dovi_panel import _DummyConfig, _FakeWorkflow

    monkeypatch.setattr(panel_mod, "MergeDoviWorkflow", lambda **kw: _FakeWorkflow(**kw))
    panel = panel_mod.MergeDoviPanel(cast(Any, _DummyConfig(tmp_path)))
    cast(Any, panel._workflow).cancel = lambda: None  # fermeture des fenêtres en fin de test
    asked: list[str] = []
    monkeypatch.setattr(panel, "_ask_forced_alignment", lambda cause: asked.append(cause) or answer)
    runs: list[AlignmentOptions] = []

    def on_run() -> None:  # chemin de « Lancer » : options lues comme au lancement réel
        options = panel._config_section.alignment_options
        if panel._force_metadata_only:
            options = AlignmentOptions(options.mode, options.verify_film2, metadata_only=True)
        runs.append(options)

    monkeypatch.setattr(panel, "_on_run", on_run)
    if global_controls:
        panel.use_global_controls()
    return panel, asked, runs


def _fail(qt_app, panel, detail) -> None:
    panel._on_workflow_failed(WorkflowStep.ALIGN, "Film 2 non décodable (boum).", detail)
    for _ in range(5):
        qt_app.processEvents()


def test_panel_offers_forced_alignment_and_relaunches_with_it(qt_app, tmp_path, monkeypatch) -> None:
    panel, asked, runs = _panel(tmp_path, monkeypatch, answer=True)
    _fail(qt_app, panel, MergeDoviFailure("film2_undecodable", True))
    assert asked and "boum" in asked[0]
    assert runs and runs[0].metadata_only and not panel._force_metadata_only  # jamais mémorisé


def test_panel_cancel_leaves_the_failure(qt_app, tmp_path, monkeypatch) -> None:
    panel, asked, runs = _panel(tmp_path, monkeypatch, answer=False)
    _fail(qt_app, panel, MergeDoviFailure("film2_undecodable", True))
    assert asked and runs == [] and not panel._force_metadata_only


@pytest.mark.parametrize("detail", [None, MergeDoviFailure("film2_undecodable", False),
                                    MergeDoviFailure("autre", True)])
def test_panel_never_offers_without_a_usable_metadata_alignment(qt_app, tmp_path, monkeypatch, detail) -> None:
    panel, asked, runs = _panel(tmp_path, monkeypatch, answer=True)
    _fail(qt_app, panel, detail)
    assert asked == [] and runs == []


def test_global_controls_relaunch_through_the_execution_bar(qt_app, tmp_path, monkeypatch) -> None:
    panel, _asked, runs = _panel(tmp_path, monkeypatch, answer=True, global_controls=True)
    relaunched: list[bool] = []
    panel.forced_rerun_requested.connect(lambda: relaunched.append(panel._force_metadata_only))
    _fail(qt_app, panel, MergeDoviFailure("film2_undecodable", True))
    assert relaunched == [True] and runs == [] and not panel._force_metadata_only


def test_real_run_passes_metadata_only_once(qt_app, tmp_path, monkeypatch) -> None:
    """``_on_run`` réel : option transmise au workflow pour ce lancement seulement."""
    import ui.panels.merge_dovi_panel as panel_mod
    from tests.test_merge_dovi_panel import _DummyConfig, _FakeWorkflow

    starts: list[AlignmentOptions] = []

    class _Recording(_FakeWorkflow):
        output_path_for = staticmethod(MergeDoviWorkflow.output_path_for)

        def set_tool_bins(self, **_kwargs) -> None:
            pass

        def cancel(self) -> None:
            pass

        def start(self, **kwargs) -> None:
            starts.append(kwargs["alignment"])

    monkeypatch.setattr(panel_mod, "MergeDoviWorkflow", _Recording)
    monkeypatch.setattr(panel_mod, "confirm_output", lambda *_a: False)
    panel = panel_mod.MergeDoviPanel(cast(Any, _DummyConfig(tmp_path)))
    film1, film2 = tmp_path / "a.mkv", tmp_path / "b.mkv"
    monkeypatch.setattr(type(panel._file_section), "film1", property(lambda _self: film1))
    monkeypatch.setattr(type(panel._file_section), "film2", property(lambda _self: film2))
    panel._force_metadata_only = True
    panel._on_run()
    panel._running = False
    panel._on_run()
    assert [options.metadata_only for options in starts] == [True, False]


# ── Relecture Codex 20 ─────────────────────────────────────────────────────


def test_projection_copies_the_simple_flags_of_nested_chapters() -> None:
    """``project_chapters`` recopie ChapterFlagHidden / ChapterFlagEnabled, enfants compris (décision DD)."""
    from core.workflows.merge_dovi_align.chapters import project_chapters
    from tests.test_merge_dovi_alignment_workflow import _chapter, _table as _chapter_table

    child = _chapter(1.0, 2.0, "Enfant", 2)
    child.hidden, child.enabled = True, True
    parent = _chapter(0.0, 3.0, "Parent", 1, children=(child,))
    parent.hidden, parent.enabled = False, False
    alignment = AlignmentMap(100, 100, (Segment(0, 100, 0),), (), (), np.asarray([0]))
    projected, dropped = project_chapters((SimpleNamespace(chapters=(parent,)),), alignment,
                                          _chapter_table(100), _chapter_table(100))
    assert projected is not None and dropped == 0
    assert (projected[0].hidden, projected[0].enabled) == (False, False)
    assert (projected[0].children[0].hidden, projected[0].children[0].enabled) == (True, True)


def test_segment_uid_of_a_streamed_origin_never_depends_on_its_header_path(tmp_path) -> None:
    """Origine en flux : en-têtes de producteur à des chemins temporaires différents, même clé → même SegmentUID."""
    from tests.test_matroska_chapter_tracks import _compiled, _source, _streamed

    rewritten = _source(tmp_path / "rewritten.mkv")

    def segment_uid(name: str) -> int:
        origin = _streamed(tmp_path, name, 7)
        return _compiled(tmp_path, rewritten, {(origin, 0): "flux:3"}, origin=(origin, 0)).segment_uid

    assert segment_uid("producteur-1.mkv") == segment_uid("producteur-2.mkv")
