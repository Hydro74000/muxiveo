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


# ── Relecture Codex 21 ─────────────────────────────────────────────────────


def _forced_run(tmp_path: Path, *, presence_fails: bool = False, check_fails: bool = False,
                align_returns_none: bool = False):
    """``_run`` en alignement forcé, Film 1 SDR (repli HDR10 possible hors mode forcé), étapes remplacées."""
    from core.workflows.merge_dovi import DoviProfile, ValidationContext

    film1, film2 = tmp_path / "film1.mkv", tmp_path / "film2.mkv"
    film1.write_bytes(b"x")
    film2.write_bytes(b"x")
    paths = _paths(tmp_path, film1)
    wf = MergeDoviWorkflow()
    any_wf = cast(Any, wf)
    any_wf._step_validate = lambda *_a: ValidationContext(
        flags=HDRFlags(has_dovi=True), static_film1=StaticHdrMetadata(), static_film2=StaticHdrMetadata(),
        film1_needs_sdr_to_hdr10=True)
    any_wf._step_detect_dovi = lambda *_a: None
    any_wf._step_framecount = lambda *_a, **_k: FrameCountResult(100, None, None)
    any_wf._step_check_storage = lambda *_a, **_k: None
    any_wf._start_film1_pass = lambda _film1: SimpleNamespace()
    any_wf._stop_film1_pass = lambda _pass: True
    any_wf._release_film1_pass = lambda *_a: None
    for name in ("_step_extract_hevc", "_step_extract_metadata", "_step_convert_sdr_to_hdr10", "_step_cleanup",
                 "_discard_consumed", "_step_inject_dovi", "_step_inject_hdr10plus"):
        setattr(wf, name, lambda *_a, **_k: None)

    def presence(*_a):
        if presence_fails:
            raise AlignmentError("audit impossible")
        return {}

    def check(_counts, _paths, flags, _adjustment, *, allow_hdr10_fallback=False):
        assert allow_hdr10_fallback is False  # mode forcé : jamais de repli HDR10 seul
        if check_fails:
            raise WorkflowError(WorkflowStep.CHECK_METADATA, "comptages différents")
        return flags

    any_wf._audit_film2_presence = presence
    any_wf._step_check_metadata = check
    seen: dict[str, Any] = {}

    def step_align(*args, allow_hdr10_fallback=False):
        seen["align_fallback"] = allow_hdr10_fallback
        return None if align_returns_none else _forced_result()

    any_wf._step_align = step_align
    any_wf._step_inject_static_hdr = lambda *_a: False
    any_wf._step_verify = lambda *_a, **_k: None
    any_wf._step_remux = lambda *_a, **_k: seen.setdefault("remux", True)
    failures: list[tuple] = []
    wf.workflow_failed.connect(lambda step, message, detail: failures.append((step, message, detail)))
    wf._run(film1, film2, paths, DoviProfile.P8_1, alignment=AlignmentOptions(metadata_only=True))
    return failures, seen


def _forced_result() -> AlignmentResult:
    table = _table(100)
    forced = AlignmentMap(100, 100, (Segment(0, 100, 0),), (), (), np.asarray([0]), {"metadata_only": True})
    return AlignmentResult(forced, None, None, None, resolve_static_hdr(None, None), None, table, None, [])


def test_forced_run_never_falls_back_to_hdr10_after_a_failed_presence_audit(tmp_path) -> None:
    failures, seen = _forced_run(tmp_path, presence_fails=True)
    assert failures and "audit impossible" in failures[0][1] and "remux" not in seen  # Film 2 jamais importé


def test_forced_run_never_falls_back_to_hdr10_at_check_metadata(tmp_path) -> None:
    failures, seen = _forced_run(tmp_path, check_fails=True)
    assert seen["align_fallback"] is False
    assert failures and failures[0][0] == WorkflowStep.CHECK_METADATA and "remux" not in seen


def test_cancellation_during_cleanup_never_offers_a_relaunch(tmp_path) -> None:
    film1 = tmp_path / "film1.mkv"
    film1.write_bytes(b"x")
    wf = MergeDoviWorkflow()

    def refused(*_args):
        raise WorkflowError(WorkflowStep.ALIGN, "Film 2 non décodable.", MergeDoviFailure("film2_undecodable", True))

    cast(Any, wf)._step_validate = refused
    cast(Any, wf)._discard_failed_run = lambda *_a: setattr(wf, "_cancelled", True)  # annulation pendant le nettoyage
    failures: list[tuple] = []
    wf.workflow_failed.connect(lambda step, message, detail: failures.append(detail))
    wf._run(film1, tmp_path / "film2.mkv", _paths(tmp_path, film1), cast(Any, None))
    assert failures == [None]


def test_forced_frame_count_never_reads_film2(tmp_path) -> None:
    film1, film2 = tmp_path / "a.mkv", tmp_path / "b.mkv"
    wf = MergeDoviWorkflow()
    counted: list[Path] = []
    cast(Any, wf)._get_framecount = lambda path: counted.append(path) or 100
    lines: list[str] = []
    wf.step_progress.connect(lambda _step, line: lines.append(line))
    result = wf._step_framecount(film1, film2, aligned=True, metadata_only=True)
    assert counted == [film1] and result.fc2 is None
    assert any("sera établie" in line for line in lines)
    assert not any("correspondance établie" in line or "image par image" in line for line in lines)


def test_forced_remux_never_imports_film2_chapters(tmp_path) -> None:
    from unittest.mock import patch

    film1, film2 = tmp_path / "film1.mkv", tmp_path / "film2.mkv"
    film1.write_bytes(b"film1")
    film2.write_bytes(b"film2")
    paths = _paths(tmp_path, film1)
    flags = HDRFlags(has_dovi=False, has_hdr10plus=False)
    paths.film1_hevc_input.write_bytes(b"hevc")
    wf = MergeDoviWorkflow()
    cast(Any, wf)._mux_native_video = lambda _fh, _f1, p, _vp, _d, _c, **_kw: p.film1_wrapped_video.write_bytes(b"w")
    cast(Any, wf)._read_video_track_props = lambda _f: {"pixel_width": 3840, "pixel_height": 2160}
    plans: list[Any] = []
    cast(Any, wf)._assemble_final_mkv = lambda plan, _d: plans.append(plan) or plan.output.write_bytes(b"out")
    lines: list[str] = []
    wf.step_progress.connect(lambda _step, line: lines.append(line))

    class _Reader:
        def __init__(self, path): self.path = path
        def tracks(self): return []
        def raw_top_level(self, _id): return [b"chapters"] if self.path == film2 else []
        def attachment_headers(self): return []
        def segment_title(self): return ""
        def chapter_editions(self): pytest.fail("chapitres de Film 2 lus en alignement forcé")

    with patch("core.workflows.merge_dovi.MatroskaReader", _Reader):
        wf._step_remux(film1, paths, flags, film2=film2, aligned=_forced_result())
    assert plans and plans[0].chapter_source is None and not plans[0].chapter_entries
    assert any(line.startswith("[WARN]") and "alignement forcé" in line for line in lines)



# ── Relecture Codex 22 ─────────────────────────────────────────────────────


def test_forced_run_without_dynamic_metadata_is_refused(tmp_path, monkeypatch) -> None:
    """Relance forcée dont la validation ne retrouve que du HDR10 statique (Film 1 SDR) : refus dans la validation,
    avant tout bilan de réussite, jamais HDR10 seul (relectures Codex 22 et 23)."""
    from core.workflows.merge_dovi import DoviProfile

    film1, film2 = tmp_path / "film1_sdr.mkv", tmp_path / "film2_hdr10.mkv"
    film1.write_bytes(b"1")
    film2.write_bytes(b"2")
    monkeypatch.setattr("core.workflows.merge_dovi.shutil.which", lambda _cmd: "/bin/tool")
    wf = MergeDoviWorkflow()

    def mediainfo(path: Path, query: str) -> str:
        return {"Video;%Format%": "HEVC", "Video;%transfer_characteristics%": "BT.709" if path == film1 else "PQ"
                }.get(query, "")

    cast(Any, wf)._mediainfo = mediainfo
    cast(Any, wf)._read_static_hdr_metadata = lambda path: StaticHdrMetadata() if path == film1 else StaticHdrMetadata(
        master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,50)", max_cll="1000,400")
    finished: list[Any] = []
    wf.step_finished.connect(lambda step, result: finished.append(step))
    assert wf._step_validate(film1, film2, DoviProfile.DISABLED).flags.has_dovi is False  # hors mode forcé : HDR10 seul
    finished.clear()
    with pytest.raises(WorkflowError, match="aucune métadonnée dynamique"):
        wf._step_validate(film1, film2, DoviProfile.DISABLED, True)
    assert WorkflowStep.VALIDATION not in finished


def test_forced_run_never_remuxes_without_an_alignment_map(tmp_path) -> None:
    failures, seen = _forced_run(tmp_path, align_returns_none=True)
    assert failures and failures[0][0] == WorkflowStep.ALIGN and "remux" not in seen


def test_forced_frame_count_status_never_mentions_a_raw_stream() -> None:
    for fc1 in (100, None):
        status = FrameCountResult(fc1, None, None, fc2_deferred=True, fc2_forced=True).status_text
        assert "flux brut" not in status and "alignement forcé" in status and "seront contrôlées" in status
        assert "illisible" not in status


def test_store_package_writes_the_python_inventory() -> None:
    """Paquet Windows Store (MSIX) sans outils : inventaire écrit comme pour l'installeur standard."""
    source = (Path(__file__).resolve().parents[1] / "package.py").read_text(encoding="utf-8")
    branch = source.split("elif args.msix or args.msixupload:", 1)[1].split("_build_msix_package(", 1)[0]
    assert "TOOL_MANIFEST.write(bundle_dir)" in branch
