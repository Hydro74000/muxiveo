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


# --- Groupe 4 : annulation, transactions d'extraction, --no-sudo ----------------------------------


def test_cancelled_sync_measure_is_reported_as_cancelled():
    import threading

    from core.jobs.source_sync import measure_pair
    from core.service.errors import ApiError, ErrorCode
    from core.workflows.audio_sync import AudioSyncError

    seen: dict[str, Any] = {}
    cancel = threading.Event()

    class _Scanner:
        def __init__(self, *_a, **kwargs):
            seen.update(kwargs)

        def scan(self, *_a, **_k):
            cancel.set()
            raise AudioSyncError("Analyse annulée.")

    sources = [_source(0, "audio"), _source(1, "audio")]
    config = SimpleNamespace(tool_ffmpeg="ffmpeg", tool_ffprobe="ffprobe")
    with pytest.raises(ApiError) as caught:
        measure_pair(sources[0], sources[0].tracks[0], sources[1], sources[1].tracks[0], kind="audio", mode="deep",
                     config=config, options=SimpleNamespace(cadence_auto=False),
                     logger=SimpleNamespace(emit=lambda *a, **k: None), audio_scanner=_Scanner, cancel_event=cancel)
    assert caught.value.code is ErrorCode.CANCELLED and seen["cancel_event"] is cancel


def test_media_run_preparation_gets_the_token_and_exports_nothing_once_cancelled(monkeypatch, tmp_path):
    from core.service.errors import ApiError, ErrorCode
    from core.service.ops import media as media_ops

    ctx = _ctx()

    def build(job, config, options, logger, *, context):
        assert context.cancel_event is ctx.cancel.event
        ctx.cancel.cancel()
        return SimpleNamespace(output=tmp_path / "out.mkv")

    monkeypatch.setattr(media_ops, "build_remux_config", build)
    monkeypatch.setattr(media_ops, "export_exact_job", lambda *a, **k: pytest.fail("aucun export attendu"))
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "media.run", {"job": {"version": 1}, "export_workflow": str(tmp_path / "job.json")}, ctx)
    assert caught.value.code is ErrorCode.CANCELLED


def test_dynamic_hdr_inspection_is_cancellable(monkeypatch, tmp_path):
    from core.service.context import CancelToken
    from core.service.ops import hdr as hdr_ops

    seen: dict[str, Any] = {}

    class _Inspector:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def analyze(self, *_a, **_k):
            return "report"

    monkeypatch.setattr(hdr_ops, "DynamicInspector", _Inspector)
    token = CancelToken()
    config = SimpleNamespace(tool_dovi_tool="dovi_tool", tool_hdr10plus="hdr10plus_tool")
    assert hdr_ops.dynamic_report(config, None, 0, None, False, "ffmpeg", tmp_path,  # type: ignore[arg-type]
                                  SimpleNamespace(emit=lambda *a, **k: None), token=token) == "report"
    assert not seen["cancelled"]()
    token.cancel()
    assert seen["cancelled"]()


def test_extract_releases_the_destination_when_a_candidate_cannot_be_created(monkeypatch, tmp_path):
    from core.jobs import extract as ex
    from core.output_commit import OutputReservation, reserve_candidate

    from tests.test_cli_extract import _info

    info = _info(tmp_path / "f.mkv")
    plan = ex.plan_track(info, 0, layer="bl")
    calls = {"n": 0}

    def flaky(output):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disque plein")
        return reserve_candidate(output)

    monkeypatch.setattr(ex, "reserve_candidate", flaky)
    with pytest.raises(OSError, match="plein"):
        ex.extract(info, plan, tmp_path / "bl.hevc", tools=ex.ExtractTools("ffmpeg"), run=lambda *a: None)
    assert not list(tmp_path.glob("*.partial"))
    OutputReservation.acquire(tmp_path / "bl.hevc").release()  # destination libérée


def test_extract_keeps_the_candidate_when_publication_is_refused(monkeypatch, tmp_path):
    from core.jobs import extract as ex
    from core.output_commit import OutputPublicationError, OutputReservation

    from tests.test_cli_extract import _info

    info = _info(tmp_path / "f.mkv")
    plan = ex.plan_track(info, "a:0")

    def refuse(self, candidate):
        raise OutputPublicationError(self.output, candidate)

    monkeypatch.setattr(OutputReservation, "publish", refuse)

    def run(cmd, _label):
        Path(cmd[-1]).write_bytes(b"data")

    with pytest.raises(OutputPublicationError):
        ex.extract(info, plan, tmp_path / "a.thd", tools=ex.ExtractTools("ffmpeg"), run=run)
    assert [p.read_bytes() for p in tmp_path.glob("*.partial")] == [b"data"]


def test_no_sudo_never_elevates(monkeypatch, tmp_path):
    from core import nvencc_install as nv

    monkeypatch.setattr(nv, "user_root", lambda: tmp_path / "nvencc" / "current")
    monkeypatch.setattr(nv, "privilege_prefix", lambda *a, **k: pytest.fail("aucune élévation attendue"))
    monkeypatch.setattr(nv, "_native_install", lambda *a, **k: pytest.fail("aucun gestionnaire de paquets"))
    monkeypatch.setattr(nv, "_user_install", lambda archive, *a: tmp_path / "NVEncC")
    assert nv._install(nv.METHOD_APT, None, None, lambda *_a: None, None, elevate=False) == tmp_path / "NVEncC"
    from cli.admin import cmd_nvencc
    from cli import admin as cli_admin

    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli_admin, "run_operation", lambda name, payload, **k: (
        seen.update(payload=payload) or (SimpleNamespace(message=""), SimpleNamespace())))
    monkeypatch.setattr(cli_admin, "_render", lambda *a: True)
    args = build_parser().parse_args(["nvencc", "install", "--no-sudo"])
    cmd_nvencc(args, SimpleNamespace(), SimpleNamespace(emit=lambda *a, **k: None))  # type: ignore[arg-type]
    assert seen["payload"]["elevate"] is False


# --- Groupe 5 : validation encode, contrat strict, chemins, Hybrid, catalogue, événements -----------


def test_job_keys_outside_the_contract_are_refused_by_the_api_and_reported_by_the_cli(tmp_path):
    from core.service.errors import ApiError
    from core.service.ops.media import MediaJobRequest

    job = {"version": 1, "sources": [{"path": str(tmp_path / "a.mkv"), "sycn": {}}], "typo": 1,
           "encode": {"video": {"default": {"crff": 20}}}, "_interne": True}
    with pytest.raises(ApiError) as caught:
        MediaJobRequest.parse({"job": job})
    assert {d.path for d in caught.value.details} == {"job.typo", "job.sources[0].sycn", "job.encode.video.default.crff"}
    _request, warnings = MediaJobRequest.parse({"job": job}, strict_documents=False)
    assert {w.path for w in warnings} == {"job.typo", "job.sources[0].sycn", "job.encode.video.default.crff"}
    clean = {"version": 1, "sources": [{"path": str(tmp_path / "a.mkv")}], "tracks": [{"selector": {"type": "audio"},
                                                                                      "language": "fre"}]}
    assert MediaJobRequest.parse({"job": clean})[1] == ()


def test_document_paths_cover_track_calibrations_and_preset_files(tmp_path):
    from core.json_documents import resolve_document_paths

    (tmp_path / "rapide").write_text("{}", encoding="utf-8")
    job = {"calibration": "c.json", "tracks": [{"sync": {"calibration": "t.json"}}, {"language": "fre"}],
           "encode": {"preset": "presets/x.json"}}
    resolved = resolve_document_paths(job, tmp_path)
    assert resolved["calibration"] == str(tmp_path / "c.json")
    assert resolved["tracks"] == [{"sync": {"calibration": str(tmp_path / "t.json")}}, {"language": "fre"}]
    assert resolved["encode"]["preset"] == str(tmp_path / "presets/x.json")
    assert resolve_document_paths({"encode": {"preset": "Mon preset"}}, tmp_path)["encode"]["preset"] == "Mon preset"
    assert resolve_document_paths({"encode": {"preset": "rapide"}}, tmp_path)["encode"]["preset"] == str(
        tmp_path / "rapide")


def test_hybrid_plan_resolves_and_exports_the_encode_block(monkeypatch, tmp_path):
    from core.jobs.encode import ResolvedEncode
    from core.jobs.errors import JobError
    from core.service.ops import hybrid as hybrid_ops

    def episode(name):
        config = SimpleNamespace(output=tmp_path / f"{name}.mkv")
        return SimpleNamespace(error=None, remux_config=config, episode=SimpleNamespace(display_name=name),
                               describe=lambda: {"episode": name})

    items = [episode("E01"), episode("E02")]
    spec = SimpleNamespace(recipe=SimpleNamespace(to_dict=lambda: {}), output_template=None, common=None)
    monkeypatch.setattr(hybrid_ops, "_prepare", lambda request, ctx: (spec, items))
    monkeypatch.setattr(hybrid_ops, "check_outputs", lambda *a: None)

    def resolve(block, remux_config, **_kwargs):
        if remux_config.output.stem == "E02":
            raise JobError("encode : preset inconnu", 3)
        return ResolvedEncode(config=SimpleNamespace(), video=({"codec": "libx265"},))

    monkeypatch.setattr("core.jobs.encode.resolve_from_job", resolve)
    monkeypatch.setattr("core.profiles.selectors.remux_config_to_exact_job", lambda config: {"version": 1})
    block = {"video": {"default": {"codec": "libx265"}}}
    request = hybrid_ops.HybridPlanRequest.from_dict({"output_dir": str(tmp_path), "encode": block,
                                                      "export_jobs": str(tmp_path / "jobs")})
    result = hybrid_ops.hybrid_plan.handler(request, _ctx())
    assert result.status == "partial"
    assert result.episodes[0]["encode"]["video"] == [{"codec": "libx265"}] and "preset" in result.episodes[1]["error"]
    assert [Path(p).name for p in result.exported] == ["E01.exact-job.json"]
    assert json.loads(Path(result.exported[0]).read_text(encoding="utf-8"))["encode"] == block


def test_catalog_flags_match_what_operations_do():
    import inspect

    flags = {spec.name: spec for spec in CATALOG}
    for spec in CATALOG:
        if "events.artifact(" in inspect.getsource(spec.handler):
            assert spec.mutating, f"{spec.name} publie des fichiers : mutating attendu"
    for name in ("encode.preview_capture", "hybrid.plan", "hdr.inspect", "sync.scan", "profile.capture"):
        assert flags[name].mutating, name
    for name in ("media.run", "media.batch", "profile.apply", "profile.batch", "hybrid.run"):
        assert flags[name].uses_gpu, name
    assert "artifact" in flags["hybrid.plan"].describe()["events"]


def test_events_of_a_job_are_delivered_in_sequence_order_across_threads():
    import threading

    from core.service.events import Emitter, EventType

    received: list[int] = []
    first_inside = threading.Event()
    release = threading.Event()

    class _Sink:
        def emit(self, event):
            if event.seq == 1:
                first_inside.set()
                release.wait(2)  # livraison lente du premier événement
            received.append(event.seq)

    emitter = Emitter(_Sink())
    slow = threading.Thread(target=emitter.emit, args=(EventType.LOG,), kwargs={"message": "a"})
    slow.start()
    first_inside.wait(2)
    fast = threading.Thread(target=emitter.emit, args=(EventType.LOG,), kwargs={"message": "b"})
    fast.start()
    fast.join(0.2)  # sans verrou de livraison, le second serait livré pendant l'attente du premier
    release.set()
    slow.join(2)
    fast.join(2)
    assert received == [1, 2]


def test_incompatible_preset_is_refused_by_the_cli_like_the_panel():
    from core.jobs.errors import JobError
    from core.workflows.encode.models import EncodePreset
    from core.workflows.encode.planning.track_settings import SourceVideoContext, preset_problem

    from tests.test_encode_request import _resolve

    preset = EncodePreset(name="H10", codec="h264_nvenc", bit_depth="10", preset="p5")
    assert "h264_nvenc" in preset_problem(preset.to_video_settings(), SourceVideoContext())  # sortie 10 bits
    with pytest.raises(JobError, match="H10.*h264_nvenc"):
        _resolve({"preset": "H10"}, load_preset=lambda name: preset)
    ok = EncodePreset(name="X", codec="libx265", crf=22, preset="slow")
    assert _resolve({"preset": "X"}, load_preset=lambda name: ok).video[0]["crf"] == 22
