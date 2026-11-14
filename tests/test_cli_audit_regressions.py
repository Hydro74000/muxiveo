"""Correctifs de l'audit du chantier CLI ↔ API (export / aperçu avec profil, priorités, états, annulation…)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.errors import CliError
from core.jobs.errors import JobError
from cli.parser import build_parser
from core.service import ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.ops import profile as profile_ops


def _ctx(**config: Any) -> ExecutionContext:
    return ExecutionContext.create(SimpleNamespace(**config), ListSink())


# --- Groupe 1 : export et aperçu avec profil -----------------------------------------------------


def test_profile_apply_exports_instead_of_running(monkeypatch, tmp_path):
    remux = SimpleNamespace(output=tmp_path / "film.mkv")
    from core.jobs.options import CommonOptions

    monkeypatch.setattr(profile_ops, "_build", lambda request, ctx, preview: (remux, {}, CommonOptions()))
    monkeypatch.setattr(profile_ops, "run_remux", lambda *a, **k: pytest.fail("aucun remux attendu"))
    monkeypatch.setattr("core.profiles.selectors.remux_config_to_exact_job", lambda config: {"version": 1})
    destination = tmp_path / "job.json"
    result: Any = call(CATALOG, "profile.apply", {"profile": "P", "inputs": [str(tmp_path / "a.mkv")],
                                                  "export_workflow": str(destination),
                                                  "encode": {"video": {"default": {"codec": "libx265"}}}}, _ctx())
    assert result.status == "exported" and result.output == str(destination)
    assert json.loads(destination.read_text(encoding="utf-8"))["encode"]["video"]["default"]["codec"] == "libx265"


def test_cli_profile_apply_forwards_export_workflow(monkeypatch, tmp_path):
    from cli import profile as cli_profile
    from cli.options import common_options

    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli_profile, "run_operation", lambda name, payload, **kwargs: (
        seen.update(name=name, payload=payload) or (SimpleNamespace(), SimpleNamespace(events=SimpleNamespace(
            job="j")))))
    args = build_parser().parse_args(["run", "--profile", "P", "-i", "a.mkv", "-o", "b.mkv",
                                      "--export-workflow", str(tmp_path / "job.json")])
    cli_profile.profile_apply("P", inputs=["a.mkv"], output="b.mkv", force=False, dry_run=False,
                              config=SimpleNamespace(), options=common_options(args),  # type: ignore[arg-type]
                              logger=SimpleNamespace(fmt="text"))  # type: ignore[arg-type]
    assert seen["name"] == "profile.apply" and seen["payload"]["export_workflow"] == str(tmp_path / "job.json")


def test_previews_with_profile_are_refused():
    from cli.commands import cmd_run

    args = build_parser().parse_args(["encode", "--profile", "P", "-i", "a.mkv", "-o", "b.mkv",
                                      "--preview-image", "10"])
    with pytest.raises(CliError, match="aperçus") as caught:
        cmd_run(args, SimpleNamespace(), SimpleNamespace(fmt="text"))  # type: ignore[arg-type]
    assert caught.value.exit_code == 2


# --- Groupe 2 : priorité des options, ordre des synchros ------------------------------------------


def test_cli_encode_options_override_per_track_settings():
    from core.jobs.batch import merge_encode_block

    job = {"encode": {"video": {"tracks": [{"select": {"source": 0}, "crf": 18, "codec": "libx265",
                                            "restoration": {"enabled": True, "mode": "fast"}}]}}}
    merged = merge_encode_block(job, {"video": {"default": {"crf": 28, "restoration": {"mode": "normal"}}}})
    track = merged["encode"]["video"]["tracks"][0]
    assert track == {"select": {"source": 0}, "codec": "libx265", "restoration": {"enabled": True}}
    assert merged["encode"]["video"]["default"]["crf"] == 28


def _source(index: int, *types: str) -> Any:
    from core.workflows.remux_models import SourceInput, TrackEntry

    tracks = [TrackEntry(mkv_tid=tid, track_type=kind, codec="x", display_info="", language="fre", title="")
              for tid, kind in enumerate(types)]
    return SourceInput(path=Path(f"/s{index}.mkv"), file_index=index, tracks=tracks)


def test_source_linked_to_a_track_with_its_own_sync_follows_it():
    from core.jobs.remux_config import apply_donor_calibration
    from core.jobs.source_sync import resolve_syncs

    sources = [_source(0, "video", "audio"), _source(1, "audio", "subtitle"), _source(2, "audio")]
    job: dict[str, Any] = {"sync_mode": "container", "sync_subtitles": "none"}

    def apply(calibration, index):
        job["sync_mode"], job["sync_calibrations"] = apply_donor_calibration(
            calibration, sync_mode=job["sync_mode"], sources=sources, sync_subtitles="none",
            sync_calibrations=job.get("sync_calibrations"), source_index=index)

    track_pairs = [({"sync": {"reference": 0, "offset_ms": 1000}}, sources[1].tracks[0])]
    specs = {2: {"reference": 1, "reference_track": "a:0", "offset_ms": 200}}
    logger = SimpleNamespace(emit=lambda *a, **k: None)
    resolve_syncs(job, sources, specs, track_pairs, config=None, options=None, logger=logger, apply=apply)
    assert sources[1].tracks[0].time_shift_ms == 1000 and sources[2].tracks[0].time_shift_ms == 1200
    # Boucle entre une piste et une source : refusée.
    loop_pairs = [({"sync": {"reference": 2}}, sources[1].tracks[0])]
    with pytest.raises(JobError, match="boucle"):
        resolve_syncs(job, sources, specs, loop_pairs, config=None, options=None, logger=logger, apply=apply)


# --- Groupe 3 : états terminaux, codes d'erreur ---------------------------------------------------


def _parent_op(status: str):
    from core.service.models import Model
    from core.service.operations import operation
    from core.service.ops.batch import BatchResult

    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Empty(Model):
        pass

    @operation("test.lot", request=_Empty, result=BatchResult, summary="Lot.")
    def _lot(request, ctx):
        return BatchResult(status=status, total=2, successes=1 if status == "partial" else 0,  # type: ignore[arg-type]
                           failures=1 if status == "partial" else 2)

    return _lot


@pytest.mark.parametrize("status", ["succeeded", "partial", "failed", "cancelled"])
def test_parent_result_status_sets_terminal_state_and_call_exit_code(status):
    from cli.service_adapter import envelope, exit_code_for_result
    from core.service.operations import Catalog

    ctx = _ctx()
    result = call(Catalog((_parent_op(status),)), "test.lot", {}, ctx)
    states = [event.data.get("state") for event in ctx.events.sink.events if event.type.value == "state"]
    assert states == ["running", status]
    assert envelope("test.lot", ctx.events.job, result=result)["status"] == status
    assert exit_code_for_result(result) == (0 if status == "succeeded" else 7)


def test_wrapped_destination_conflict_and_fel_cancellation_keep_their_codes():
    from core.fel.engine import FelCancelled
    from core.output_commit import OutputBusyError
    from core.service.errors import ApiError, ErrorCode
    from core.workflows.remux_models import RemuxError

    try:
        try:
            raise OutputBusyError(Path("/out.mkv"))
        except OutputBusyError as exc:
            raise RemuxError(str(exc)) from exc
    except RemuxError as wrapped:
        assert ApiError.from_exception(wrapped).code is ErrorCode.BUSY
    assert ApiError.from_exception(FelCancelled("stop")).code is ErrorCode.CANCELLED
    assert ApiError.from_exception(RuntimeError("x")).code is ErrorCode.WORKFLOW_FAILED


def test_remux_failure_keeps_the_public_code_of_its_cause(tmp_path):
    from core.jobs.options import CommonOptions
    from core.jobs.runtime import run_remux
    from core.runner import TaskSignals
    from core.service.context import CancelToken
    from core.service.errors import ApiError, ErrorCode

    class _Wf:
        log_message = backend_event = SimpleNamespace(connect=lambda *_a: None)

        def run(self, _config):
            signals = TaskSignals()
            signals.failed.emit("stop", FelCancelled_())
            return signals

    from core.fel.engine import FelCancelled as FelCancelled_

    remux = SimpleNamespace(output=tmp_path / "out.mkv")
    logger = SimpleNamespace(emit=lambda *a, **k: None)
    with pytest.raises(ApiError) as caught:
        run_remux(remux, config=None, options=CommonOptions(), logger=logger,  # type: ignore[arg-type]
                  cancel=CancelToken(), workflow_factory=_Wf)  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.CANCELLED


def test_hybrid_plan_reports_partial_when_an_episode_fails(monkeypatch):
    from core.service.ops import hybrid as hybrid_ops

    items = [SimpleNamespace(error=None, describe=lambda: {"episode": "E01"}),
             SimpleNamespace(error=RuntimeError("x"), describe=lambda: {"episode": "E02", "error": "x"})]
    spec = SimpleNamespace(recipe=SimpleNamespace(to_dict=lambda: {}), output_template=None)
    monkeypatch.setattr(hybrid_ops, "_prepare", lambda request, ctx: (spec, items))
    request = hybrid_ops.HybridPlanRequest.from_dict({"output_dir": "/out"})
    assert hybrid_ops.hybrid_plan.handler(request, _ctx()).status == "partial"
    monkeypatch.setattr(hybrid_ops, "_prepare", lambda request, ctx: (spec, items[1:]))
    assert hybrid_ops.hybrid_plan.handler(request, _ctx()).status == "failed"
