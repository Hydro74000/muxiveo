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
    for spec in CATALOG:  # sonde GPU (filtres Vulkan) par la validation de l'encodage
        source = inspect.getsource(spec.handler)
        if "checked_encode(" in source or "prepare_vulkan(" in source:
            assert spec.uses_gpu, f"{spec.name} sonde le GPU : uses_gpu attendu"
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


# --- Contre-vérification : corrections complémentaires ---------------------------------------------


def test_panel_preset_load_keeps_the_tonemap_default_of_the_new_codec(qt_app, monkeypatch, tmp_path):
    from core.config import AppConfig
    from core.inspector import HDRType
    from core.workflows.encode import EncodePreset, ProfileManager
    from ui.panels.encode_panel.panel import EncodePanel

    from tests.test_encode_panel_widgets import _COLOR, _PATH_A, _file_info, _select_codec, _video_entry, _video_track

    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    panel._profiles = ProfileManager(tmp_path)
    panel._profiles.save(EncodePreset(name="SDR", codec="libx264", crf=20, preset="slow"))
    panel._profiles.save(EncodePreset(name="HDR", codec="libx265", crf=20, preset="slow"))
    panel._refresh_profiles()
    panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0, HDRType.HDR10)], hdr_type=HDRType.HDR10),
                             _video_entry(0), _COLOR)])
    try:
        _select_codec(panel, "copy")
        panel._set_combo_data(panel._profile_combo, "SDR")
        panel._load_profile()
        assert panel._codec_combo.currentData() == "libx264" and panel._tonemap_cb.isChecked()
        panel._set_combo_data(panel._profile_combo, "HDR")
        panel._load_profile()  # retour sur un codec HDR : le tone mapping coché d'office est retiré
        assert panel._codec_combo.currentData() == "libx265" and not panel._tonemap_cb.isChecked()
    finally:
        panel.close()


def test_cli_codec_change_applies_the_same_tonemap_default():
    from core.inspector import HDRType

    from tests.test_encode_request import _resolve

    assert _resolve({"video": {"default": {"codec": "libx264"}}}).video[0]["tonemap_to_sdr"] is True
    explicit = _resolve({"video": {"default": {"codec": "libx264", "tonemap": {"enabled": False}}}})
    assert explicit.video[0]["tonemap_to_sdr"] is False
    assert _resolve({"video": {"default": {"codec": "libx264"}}}, HDRType.NONE).video[0]["tonemap_to_sdr"] is False


def test_historic_batch_steps_give_the_batch_token_to_builders_that_accept_it(monkeypatch):
    from cli import batch as cli_batch
    from core.service.context import CancelToken

    cancel = CancelToken()
    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli_batch, "build_remux_config",
                        lambda job, config, options, logger, *, context=None: seen.update(context=context))
    monkeypatch.setattr(cli_batch, "resolve_job_encode_config",
                        lambda *args, cancel_event=None: seen.update(event=cancel_event))
    steps = cli_batch.cli_batch_steps(cancel)
    steps.build({}, None, None, None)
    steps.encode({}, None, None, None, None)
    assert seen["context"].cancel_event is cancel.event and seen["event"] is cancel.event
    # Constructeur de substitution sans ce paramètre (tests oracle) : appelé tel quel.
    monkeypatch.setattr(cli_batch, "build_remux_config", lambda job, config, options, logger: "ok")
    assert steps.build({}, None, None, None) == "ok"


def test_automatic_crop_stops_when_cancelled(tmp_path):
    import threading

    from core.subprocess_utils import ProbeCancelledError
    from core.workflows.encode.runtime.crop_detector import detect_video_crop

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ProbeCancelledError):
        detect_video_crop(tmp_path / "f.mkv", dimensions=(3840, 2160), duration_s=600.0, cancel_event=cancel)


def test_real_exact_job_exports_pass_the_strict_contract(tmp_path):
    import dataclasses

    from core.profiles.selectors import remux_config_to_exact_job
    from core.service.ops.media import MediaJobRequest
    from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry, clone_track_entry

    video = TrackEntry(0, "video", "HEVC", "2160p", "und", "", entry_id="v0", file_id="f0",
                       remove_closed_captions=True)
    audio = TrackEntry(1, "audio", "TRUEHD", "7.1", "eng", "VO", entry_id="a0", file_id="f0", time_shift_ms=40,
                       sync_rewrite_mode="offset")
    variant = dataclasses.replace(clone_track_entry(audio, entry_id="var1"), codec="eac3",
                                  display_info="E-AC-3 640 kbps", time_shift_ms=40)
    donor_sub = TrackEntry(0, "subtitle", "SRT", "", "fre", "", entry_id="s1", file_id="f1",
                           sync_calibration={"segments": [{"start_ms": 0, "shift_ms": 120.0}], "confidence": 1.0})
    sources = [SourceInput(path=tmp_path / "a.mkv", file_index=0, tracks=[video, audio, variant]),
               SourceInput(path=tmp_path / "b.mkv", file_index=1, tracks=[donor_sub])]
    config = RemuxConfig(
        sources=sources, output=tmp_path / "out.mkv",
        track_order=[(0, 0, "v0"), (0, 1, "a0"), (0, 1, "var1"), (1, 0, "s1")],
        extra_attachments=[tmp_path / "cover.jpg"], file_title="Film", tag_overrides={"SEASON": "1", "EPISODE": "2"},
        tmdb_cover=("https://image/x.jpg", "cover.jpg"), sync_mode="physical", sync_subtitles="none",
        sync_calibrations={"1": {"segments": [{"start_ms": 0, "shift_ms": 80.0}]}},
        chapter_overrides=[SimpleNamespace(timecode_s=0.0, name="Début")], chapter_source_index=0,
    )
    for identity in (False, True):
        job = remux_config_to_exact_job(config, name="Mon workflow", fallback_profile="P",
                                        identity_selectors=identity)
        assert "audio_variants" in job
        _request, warnings = MediaJobRequest.parse({"job": job})
        assert warnings == ()
    with pytest.raises(Exception, match="Requête invalide"):  # objet fermé : clé inconnue refusée
        MediaJobRequest.parse({"job": {"version": 1, "sources": [{"path": str(tmp_path / "a.mkv"),
                                                                  "sync": {"reference": 0, "typo": 1}}]}})


def test_preset_files_stay_found_in_requests_and_exports(tmp_path, monkeypatch):
    from core.jobs.options import CommonOptions
    from core.jobs.runtime import export_exact_job
    from core.service.ops.hybrid import HybridPlanRequest
    from core.service.ops.profile import ProfileApplyRequest

    project = tmp_path / "projet"
    request = HybridPlanRequest.parse({"output_dir": "out", "encode": {"preset": "presets/P.json"}}, base_dir=project)[0]
    assert request.encode == {"preset": str(project / "presets/P.json")}
    profile = ProfileApplyRequest.parse({"profile": "P", "inputs": ["a.mkv"], "encode": {"preset": "Nom"}},
                                        base_dir=project)[0]
    assert profile.encode == {"preset": "Nom"}  # preset enregistré : nom inchangé
    # Argument du CLI (relatif au dossier courant) : absolu dans le job exporté ailleurs.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "exports").mkdir()
    monkeypatch.setattr("core.profiles.selectors.remux_config_to_exact_job", lambda config: {"version": 1})
    destination = export_exact_job(SimpleNamespace(output=tmp_path / "o.mkv"),  # type: ignore[arg-type]
                                   CommonOptions(export_workflow=str(tmp_path / "exports" / "job.json")),
                                   encode={"preset": "presets/P.json"})
    assert json.loads(destination.read_text(encoding="utf-8"))["encode"]["preset"] == str(tmp_path / "presets/P.json")


def test_published_plan_follows_the_checked_dovi_nvenc_crop(monkeypatch):
    import dataclasses

    from core.service.ops import media as media_ops
    from core.workflows.encode.models import VideoCropSettings

    from tests.test_encode_request import _resolve

    resolved = _resolve({"video": {"default": {"codec": "hevc_nvenc"}}})
    crop = VideoCropSettings(enabled=True, unit="px", top=8, bottom=8)
    video = dataclasses.replace(resolved.config.video_tracks[0], crop=crop)
    checked = dataclasses.replace(resolved.config, video=video, video_tracks=[video])
    monkeypatch.setattr(media_ops, "_build_cached", lambda request, ctx: (SimpleNamespace(), None, None))
    monkeypatch.setattr(media_ops, "resolve_job_encode", lambda *a: resolved)
    monkeypatch.setattr(media_ops, "encode_workflow", lambda *a: None)
    monkeypatch.setattr(media_ops, "checked_encode", lambda *a, **k: (checked, []))
    monkeypatch.setattr(media_ops, "serialize_remux_config", lambda config: {})
    result = media_ops.media_validate.handler(media_ops.MediaJobRequest.from_dict({"job": {"version": 1}}), _ctx())
    assert result.valid and result.encode["video"][0]["crop"]["top"] == 8


# --- Troisième audit ---------------------------------------------------------------------------------


def test_panel_preset_round_trip_through_sdr_restores_every_hdr_choice(qt_app, monkeypatch, tmp_path):
    from core.config import AppConfig
    from core.inspector import HDRType
    from core.workflows.encode import EncodePreset, ProfileManager
    from ui.panels.encode_panel.panel import EncodePanel

    from tests.test_encode_panel_widgets import _COLOR, _PATH_A, _file_info, _select_codec, _video_entry, _video_track

    hdr = HDRType.DOLBY_VISION_HDR10PLUS
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    panel._profiles = ProfileManager(tmp_path)
    panel._profiles.save(EncodePreset(name="SDR", codec="libx264", crf=20, preset="slow"))
    panel._profiles.save(EncodePreset(name="HDR", codec="libx265", crf=20, preset="slow"))
    panel._refresh_profiles()
    panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0, hdr)], hdr_type=hdr), _video_entry(0), _COLOR)])
    try:
        _select_codec(panel, "libx265")
        before = (panel._inject_hdr_cb.isChecked(), panel._copy_dv_cb.isChecked(), panel._copy_hdr10plus_cb.isChecked())
        assert before == (True, True, True)
        for name in ("SDR", "HDR"):
            panel._set_combo_data(panel._profile_combo, name)
            panel._load_profile()
        after = (panel._inject_hdr_cb.isChecked(), panel._copy_dv_cb.isChecked(), panel._copy_hdr10plus_cb.isChecked())
        assert after == before and not panel._tonemap_cb.isChecked()
        # Même résultat que le changement direct de codec.
        _select_codec(panel, "libx264")
        _select_codec(panel, "libx265")
        assert (panel._inject_hdr_cb.isChecked(), panel._copy_dv_cb.isChecked(),
                panel._copy_hdr10plus_cb.isChecked()) == before
    finally:
        panel.close()


def test_cli_preset_round_trip_through_sdr_restores_hdr_unless_the_job_says_otherwise():
    from core.inspector import HDRType
    from core.workflows.encode.models import EncodePreset

    from tests.test_encode_request import _resolve

    sdr = EncodePreset(name="SDR", codec="libx264", crf=20, preset="slow")
    hdr = HDRType.DOLBY_VISION_HDR10PLUS
    video = _resolve({"preset": "SDR", "video": {"default": {"codec": "libx265"}}}, hdr,
                     load_preset=lambda name: sdr).video[0]
    assert (video["inject_hdr_meta"], video["copy_dv"], video["copy_hdr10plus"], video["tonemap_to_sdr"]) == (
        True, True, True, False)
    explicit = _resolve({"preset": "SDR", "video": {"default": {"codec": "libx265", "static_hdr": {"mode": "off"},
                                                                 "dolby_vision": {"mode": "off"}}}}, hdr,
                        load_preset=lambda name: sdr).video[0]
    assert (explicit["inject_hdr_meta"], explicit["copy_dv"], explicit["copy_hdr10plus"]) == (False, False, True)


def _sleeping_tool(tmp_path: Path) -> Path:
    tool = tmp_path / "outil-lent"
    tool.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX")
def test_running_crop_and_l5_probes_are_killed_on_cancel(tmp_path):
    import threading
    import time

    from core.dovi_profile_detector import DoviProfileDetector
    from core.subprocess_utils import ProbeCancelledError
    from core.workflows.encode.runtime.crop_detector import detect_black_bars_ffmpeg

    source = tmp_path / "f.mkv"
    source.write_bytes(b"x")
    tool = str(_sleeping_tool(tmp_path))
    for probe in (
        lambda cancel: detect_black_bars_ffmpeg(source, ffmpeg_bin=tool, duration_s=600.0, dimensions=(3840, 2160),
                                                cancel_event=cancel),
        lambda cancel: DoviProfileDetector(dovi_tool_bin=tool, ffmpeg_bin=tool).probe_l5_offsets(
            source, duration_s=600.0, cancel_event=cancel),
    ):
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        start = time.monotonic()
        with pytest.raises(ProbeCancelledError):
            probe(cancel)
        assert time.monotonic() - start < 5  # processus en cours tué, pas d'attente de son délai


def test_hybrid_plan_cancelled_during_encode_validation_exports_nothing(monkeypatch, tmp_path):
    from core.jobs.encode import ResolvedEncode
    from core.service.errors import ApiError, ErrorCode
    from core.service.ops import hybrid as hybrid_ops

    ctx = _ctx()
    item = SimpleNamespace(error=None, remux_config=SimpleNamespace(output=tmp_path / "E01.mkv"),
                           episode=SimpleNamespace(display_name="E01"), describe=lambda: {"episode": "E01"})
    spec = SimpleNamespace(recipe=SimpleNamespace(to_dict=lambda: {}), output_template=None, common=None)
    monkeypatch.setattr(hybrid_ops, "_prepare", lambda request, ctx: (spec, [item]))
    encode_config = SimpleNamespace()
    monkeypatch.setattr("core.jobs.encode.resolve_from_job",
                        lambda *a, **k: ResolvedEncode(config=encode_config, video=()))  # type: ignore[arg-type]
    monkeypatch.setattr("core.jobs.encode_runtime.encode_workflow", lambda *a: None)

    def checked(*_a, cancel_event=None, **_k):
        assert cancel_event is ctx.cancel.event
        ctx.cancel.cancel()  # Ctrl+C pendant la sonde de géométrie
        return encode_config, []

    monkeypatch.setattr("core.jobs.encode_runtime.checked_encode", checked)
    monkeypatch.setattr(hybrid_ops, "export_jobs", lambda *a, **k: pytest.fail("aucun export après l'annulation"))
    request = hybrid_ops.HybridPlanRequest.from_dict({"output_dir": str(tmp_path), "encode": {"video": {}},
                                                      "export_jobs": str(tmp_path / "jobs")})
    with pytest.raises(ApiError) as caught:
        hybrid_ops.hybrid_plan.handler(request, ctx)
    assert caught.value.code is ErrorCode.CANCELLED


def test_registered_preset_names_with_a_slash_stay_names(monkeypatch, tmp_path):
    from core.jobs.encode import preset_loader
    from core.json_documents import portable_encode_block, resolve_document_paths
    from core.service.ops.hybrid import HybridPlanRequest
    from core.workflows.encode import EncodePreset, ProfileManager

    ProfileManager(tmp_path / "presets").save(EncodePreset(name="Film/4K", codec="libx265", crf=18, preset="slow"))
    monkeypatch.chdir(tmp_path)
    assert preset_loader(tmp_path / "presets")("Film/4K").crf == 18
    assert portable_encode_block({"preset": "Film/4K"}) == {"preset": "Film/4K"}
    assert resolve_document_paths({"encode": {"preset": "Film/4K"}}, tmp_path)["encode"]["preset"] == "Film/4K"
    request = HybridPlanRequest.parse({"output_dir": "o", "encode": {"preset": "Film/4K"}}, base_dir=tmp_path)[0]
    assert request.encode == {"preset": "Film/4K"}


def test_contract_accepts_selector_aliases_and_rejects_bad_variant_values(tmp_path):
    from core.jobs.contract import validate_job_contract
    from core.jobs.errors import ContractError
    from core.service.errors import ApiError
    from core.service.ops.media import MediaJobRequest

    alias = {"version": 1, "sources": [str(tmp_path / "a.mkv")],
             "tracks": [{"selector": {"type": "audio", "lang": "fra"}, "enabled": False}]}
    assert MediaJobRequest.parse({"job": alias})[1] == ()
    bad = {"version": 1, "sources": [str(tmp_path / "a.mkv")],
           "audio_variants": [{"selector": {"type": "audio"}, "codec": "eac3", "time_shift_ms": "oops",
                               "sync_calibration": {"typo": 1}, "sync_rewrite_mode": "BOGUS"}]}
    with pytest.raises(ContractError) as contract:  # documents CLI : refus du contrat (validation)
        validate_job_contract(bad)
    assert len(contract.value.errors) == 3
    with pytest.raises(ApiError) as caught:  # requête API : refus dès la lecture, chemin de chaque champ
        MediaJobRequest.parse({"job": bad})
    assert {d.path for d in caught.value.details} == {
        "job.audio_variants[0].time_shift_ms", "job.audio_variants[0].sync_calibration",
        "job.audio_variants[0].sync_rewrite_mode"}
    _request, warnings = MediaJobRequest.parse({"job": bad}, strict_documents=False)  # CLI : refus plus tard
    assert warnings == ()


def test_previews_apply_the_launch_checks_and_the_crop_acceptance(monkeypatch):
    from core.jobs.batch import PlannedBatchJob, execute_job
    from core.service.ops import profile as profile_ops

    seen: list[Any] = []
    steps = SimpleNamespace(run_encode=lambda *a, **k: 0, preview=None, run=None,
                            preview_encode=lambda config, options, logger, encode_config, *, accept_dovi_nvenc_crop=False: seen.append(
                                accept_dovi_nvenc_crop) or 0)
    entry = PlannedBatchJob(0, "a.mkv", "b.mkv")
    entry.encode_config, entry.accept_dovi_nvenc_crop = SimpleNamespace(), True
    assert execute_job(steps, entry, None, None, None, dry_run=True, force=False) == 0  # type: ignore[arg-type]
    assert seen == [True]
    steps.preview_encode = lambda *a: 0  # étape remplacée sans ce paramètre : appelée telle quelle
    assert execute_job(steps, entry, None, None, None, dry_run=True, force=False) == 0  # type: ignore[arg-type]

    from core.jobs.encode import ResolvedEncode
    from core.jobs.options import CommonOptions

    resolved = ResolvedEncode(config=SimpleNamespace(), video=())  # type: ignore[arg-type]
    monkeypatch.setattr(profile_ops, "_build", lambda request, ctx, preview: (SimpleNamespace(sources=[]), {},
                                                                               CommonOptions()))
    monkeypatch.setattr(profile_ops, "_resolve", lambda *a: resolved)
    monkeypatch.setattr(profile_ops, "encode_workflow", lambda *a: None)
    monkeypatch.setattr(profile_ops, "serialize_remux_config", lambda config: {})
    monkeypatch.setattr(profile_ops, "checked_encode", lambda encode_config, wf, *, accept_dovi_nvenc_crop, **k: (
        encode_config, [] if accept_dovi_nvenc_crop else ["Dolby Vision + NVENC : recadrage aligné sur 32 requis"]))
    monkeypatch.setattr(profile_ops, "preview_encode", lambda config, wf, *_a: "nvencc …")
    request = profile_ops.ProfilePreviewRequest.from_dict({"profile": "P", "inputs": ["/a.mkv"],
                                                           "encode": {"video": {}}})
    assert not profile_ops.profile_preview.handler(request, _ctx()).valid
    accepted = profile_ops.ProfilePreviewRequest.from_dict({"profile": "P", "inputs": ["/a.mkv"],
                                                            "encode": {"video": {}, "accept_dovi_nvenc_crop": True}})
    assert profile_ops.profile_preview.handler(accepted, _ctx()).command_text == "nvencc …"


# --- Quatrième audit ---------------------------------------------------------------------------------


def test_propagation_is_stable_and_follows_the_template_hdr_choices():
    from core.inspector import HDRType
    from core.workflows.encode.planning.track_settings import SourceVideoContext, propagate_state

    ctx = SourceVideoContext(hdr_type=HDRType.DOLBY_VISION_HDR10PLUS, transfer="smpte2084")
    own = {"codec": "libx264", "inject_hdr_meta": False, "copy_dv": False, "copy_hdr10plus": False,
           "hdr_disabled_by_codec": True, "dynamic_hdr_disabled_by_codec": (True, True)}  # mémoire active
    hdr = ("inject_hdr_meta", "copy_dv", "copy_hdr10plus")
    off = {"codec": "libx265", "inject_hdr_meta": False, "copy_dv": False, "copy_hdr10plus": False,
           "hdr_common": {"inject_hdr_meta": False, "copy_dv": False, "copy_hdr10plus": False}}
    first = propagate_state(off, own, ctx)
    second = propagate_state(off, first, ctx)
    assert [first[k] for k in hdr] == [second[k] for k in hdr] == [False, False, False]  # idempotent
    on = {**off, "hdr_common": {k: True for k in hdr}}
    assert [propagate_state(on, own, ctx)[k] for k in hdr] == [True, True, True]
    # Intention inconnue : choix propres de la piste (mémoire rétablie par le codec HEVC), jamais ceux du modèle.
    unknown = propagate_state({"codec": "libx265", "inject_hdr_meta": False, "copy_dv": False,
                               "copy_hdr10plus": False}, own, ctx)
    assert [unknown[k] for k in hdr] == [True, True, True]
    assert [propagate_state({"codec": "libx265"}, unknown, ctx)[k] for k in hdr] == [True, True, True]
    # Modèle sans HDR : la mémoire de la piste est gardée pour le retour sur un codec HDR.
    sdr = propagate_state({"codec": "libx264"}, own, ctx)
    assert sdr["hdr_disabled_by_codec"] and sdr["dynamic_hdr_disabled_by_codec"] == (True, True)


def test_panel_two_tracks_apply_all_preset_round_trip_keeps_hdr(qt_app, monkeypatch, tmp_path):
    from core.config import AppConfig
    from core.inspector import HDRType
    from core.workflows.encode import EncodePreset, ProfileManager
    from ui.panels.encode_panel.panel import EncodePanel

    from tests.test_encode_panel_widgets import _COLOR, _PATH_A, _file_info, _select_codec, _video_entry, _video_track

    hdr = HDRType.DOLBY_VISION_HDR10PLUS
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    panel._profiles = ProfileManager(tmp_path)
    panel._profiles.save(EncodePreset(name="SDR", codec="libx264", crf=20, preset="slow"))
    panel._profiles.save(EncodePreset(name="HDR", codec="libx265", crf=20, preset="slow"))
    panel._refresh_profiles()
    first, second = _video_entry(0), _video_entry(1)
    first.entry_id, second.entry_id = "first", "second"
    info = _file_info(_PATH_A, [_video_track(0, hdr), _video_track(1, hdr)], hdr_type=hdr)
    panel.set_video_tracks([(info, first, _COLOR), (info, second, _COLOR)])
    try:
        _select_codec(panel, "libx265")
        panel._on_apply_all_video_toggled(True)
        for name in ("SDR", "HDR"):
            panel._set_combo_data(panel._profile_combo, name)
            panel._load_profile()
        for entry_id in ("first", "second"):
            state = panel._video_settings_by_entry_id[entry_id]
            assert (state["codec"], state["inject_hdr_meta"], state["copy_dv"], state["copy_hdr10plus"]) == (
                "libx265", True, True, True), entry_id
    finally:
        panel.close()


def test_exports_with_added_chapters_and_strict_types(tmp_path):
    from core.jobs.contract import validate_job_contract
    from core.profiles.selectors import remux_config_to_exact_job
    from core.service.errors import ApiError
    from core.service.ops.media import MediaJobRequest
    from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry

    track = TrackEntry(0, "video", "HEVC", "2160p", "und", "", entry_id="v0", file_id="f0")
    config = RemuxConfig(sources=[SourceInput(path=tmp_path / "a.mkv", file_index=0, tracks=[track])],
                         output=tmp_path / "o.mkv", track_order=[(0, 0, "v0")],
                         chapter_overrides=[SimpleNamespace(timecode_s=0.0, name="Début")])  # sans source
    job = remux_config_to_exact_job(config)
    assert job["chapters"].get("source_index", None) is None
    assert MediaJobRequest.parse({"job": job})[1] == ()
    bad = {"version": 1, "input": [23], "sources": [{"path": str(tmp_path / "a.mkv"), "playlist": "oops"}],
           "calibration": 23, "name": False, "fallback_profile": []}
    with pytest.raises(ApiError) as caught:
        MediaJobRequest.parse({"job": bad})
    assert {d.path for d in caught.value.details} >= {"job.input[0]", "job.sources[0].playlist", "job.calibration",
                                                      "job.name", "job.fallback_profile"}
    validate_job_contract(bad)  # documents CLI : le contrat historique reste celui d'avant (pas de refus)


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX")
def test_duration_vulkan_and_workflow_probes_are_cancellable(tmp_path):
    import threading
    import time

    from core.subprocess_utils import ProbeCancelledError
    from core.video_sampling import probe_video_duration
    from core.workflows.encode.vulkan import detect_vulkan
    from core.workflows.encode.workflow import EncodeWorkflow
    from core.workflows.encode.runtime.crop_detector import detect_black_bars_ffmpeg

    source = tmp_path / "f.mkv"
    source.write_bytes(b"x")
    tool = str(_sleeping_tool(tmp_path))
    probes = [
        lambda cancel: probe_video_duration(source, ffprobe_bin=tool, cancel_event=cancel),
        lambda cancel: detect_vulkan(tool, cancel),
        # Durée inconnue : la recherche de durée elle-même est interrompue.
        lambda cancel: detect_black_bars_ffmpeg(source, ffmpeg_bin=tool, ffprobe_bin=tool, cancel_event=cancel),
    ]
    workflow = EncodeWorkflow(ffmpeg_bin=tool, ffprobe_bin=tool)
    workflow._bins["dovi_tool"] = tool

    def internal_l5(cancel):
        workflow.set_probe_cancel_event(cancel)
        return workflow._probe_dovi_l5_offsets(source)  # appel interne, sans jeton explicite

    probes.append(internal_l5)
    for probe in probes:
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        start = time.monotonic()
        with pytest.raises(ProbeCancelledError):
            probe(cancel)
        assert time.monotonic() - start < 5


# --- Cinquième audit -----------------------------------------------------------------------------------


def test_legitimate_fallback_profile_object_and_strict_variables(tmp_path):
    from core.service.errors import ApiError
    from core.service.ops.media import MediaJobRequest

    base = {"version": 1, "sources": [str(tmp_path / "a.mkv")]}
    for fallback in ("series-fr", {"name": "series-fr"}):
        assert MediaJobRequest.parse({"job": {**base, "fallback_profile": fallback}})[1] == ()
    for bad, path in (({"fallback_profile": 3}, "job.fallback_profile"), ({"variables": []}, "job.variables"),
                      ({"fallback_profile": {"name": 3}}, "job.fallback_profile.name")):
        with pytest.raises(ApiError) as caught:
            MediaJobRequest.parse({"job": {**base, **bad}})
        assert path in {d.path for d in caught.value.details}


def test_operation_token_never_outlives_the_operation_on_a_reused_workflow(qapp, tmp_path):
    import threading

    from core.jobs.encode_runtime import checked_encode, probe_cancel_scope
    from core.jobs.options import CommonOptions
    from core.workflows.encode.workflow import EncodeWorkflow

    from tests.test_cli_encode import _config

    wf = EncodeWorkflow()
    cancelled = threading.Event()
    cancelled.set()
    with probe_cancel_scope(wf, cancelled):
        assert wf._probe_cancel_event is cancelled
    assert wf._probe_cancel_event is None
    # Validation d'une opération annulée, puis réutilisation du même workflow : sonde L5 non contaminée.
    checked_encode(_config(tmp_path), wf, config=SimpleNamespace(tool_ffmpeg="ffmpeg"), options=CommonOptions(),
                   cancel_event=cancelled)
    assert wf._probe_cancel_event is None


def test_encode_block_values_are_refused_when_the_request_is_read(tmp_path):
    from core.service.errors import ApiError
    from core.service.ops.hybrid import HybridPlanRequest
    from core.service.ops.media import MediaJobRequest

    job = {"version": 1, "sources": [str(tmp_path / "a.mkv")], "encode": {"video": {"default": {"crf": 99}}}}
    with pytest.raises(ApiError) as caught:
        MediaJobRequest.parse({"job": job})
    assert "job.encode.video.default.crf" in {d.path for d in caught.value.details}
    with pytest.raises(ApiError) as caught:
        HybridPlanRequest.parse({"output_dir": str(tmp_path), "encode": {"video": {"default": {"crf": "x"}}}})
    assert "encode.video.default.crf" in {d.path for d in caught.value.details}
    # Commande historique (documents tolérants) : refus à la résolution, comme avant.
    assert MediaJobRequest.parse({"job": job}, strict_documents=False)[1] == ()


# --- Sixième audit : annulation des aperçus, portée du jeton, lots, formats HDR10 -----------------------


@pytest.mark.parametrize("scenario", ["probe", "wrapped", "token", "failure", "late"])
def test_real_preview_worker_outcomes_keep_their_public_meaning(qapp, tmp_path, monkeypatch, scenario):
    from core.jobs.errors import JobError
    from core.service.errors import ApiError, ErrorCode
    from core.service.ops import encode as encode_ops
    from core.subprocess_utils import ProbeCancelledError
    from core.workflows.encode.workflow import EncodeWorkflow

    ctx = _ctx(app_data_dir=tmp_path / "data", tool_ffmpeg="ffmpeg")
    wf = EncodeWorkflow()

    def preview_sync(config, request, signals, *, active_inner):
        if scenario == "probe":
            raise ProbeCancelledError("Sonde L5 annulée.")
        if scenario == "wrapped":
            try:
                raise ProbeCancelledError("Sonde annulée.")
            except ProbeCancelledError as exc:
                raise RuntimeError("préparation de l'aperçu interrompue") from exc
        if scenario == "token":
            ctx.cancel.cancel()
            raise RuntimeError("ffmpeg tué")
        if scenario == "late":
            ctx.cancel.cancel()  # annulation après la fin du travail : réussite conservée
            return SimpleNamespace(to_json=lambda: json.dumps({"mode": "image", "captures": []}))
        raise RuntimeError("ffmpeg : codec inconnu")

    monkeypatch.setattr(wf, "_run_preview_sync", preview_sync)  # vrai worker (run_preview), étape interne seule
    resolved = SimpleNamespace(config=SimpleNamespace(output=tmp_path / "film.mkv", video_tracks=[]))
    monkeypatch.setattr(encode_ops, "_build_cached", lambda request, ctx: ("remux", SimpleNamespace(ffmpeg=None), None))
    monkeypatch.setattr(encode_ops, "resolve_job_encode", lambda *a: resolved)
    monkeypatch.setattr(encode_ops, "encode_workflow", lambda config, options: wf)
    monkeypatch.setattr(encode_ops, "prepare_vulkan", lambda *a: None)
    request = {"job": {}, "output_dir": str(tmp_path / "out"), "timecode_s": 1}
    if scenario == "late":
        assert call(CATALOG, "encode.preview_capture", request, ctx).captures == ()
        return
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "encode.preview_capture", request, ctx)
    expected = ErrorCode.WORKFLOW_FAILED if scenario == "failure" else ErrorCode.CANCELLED
    assert caught.value.code is expected and (scenario != "failure" or isinstance(caught.value, JobError))


def test_extraction_tool_failure_after_cancel_is_a_cancellation(qapp, tmp_path):
    import sys
    import threading

    from core.jobs.extract import tool_runner
    from core.service.context import CancelToken
    from core.service.errors import ApiError, ErrorCode

    cancel = CancelToken()
    run = tool_runner(cancel)
    threading.Timer(0.3, cancel.cancel).start()
    with pytest.raises(ApiError) as caught:  # commande réelle tuée par l'annulation
        run([sys.executable, "-c", "import time; time.sleep(30)"], "extract-test")
    assert caught.value.code is ErrorCode.CANCELLED
    with pytest.raises(ApiError) as failed:  # échec ordinaire
        tool_runner(CancelToken())([sys.executable, "-c", "raise SystemExit(3)"], "extract-test")
    assert failed.value.code is ErrorCode.WORKFLOW_FAILED


def _probing_workflow(tmp_path: Path):
    """Vrai workflow dont la construction des commandes relance la sonde L5 interne (outil lent, sans jeton explicite)."""
    from core.workflows.encode.workflow import EncodeWorkflow

    tool = str(_sleeping_tool(tmp_path))
    source = tmp_path / "f.mkv"
    source.write_bytes(b"x")
    wf = EncodeWorkflow(ffmpeg_bin=tool, ffprobe_bin=tool)
    wf._bins["dovi_tool"] = tool
    wf.preview_command = lambda config: str(wf._probe_dovi_l5_offsets(source))  # type: ignore[method-assign]
    return wf


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX")
@pytest.mark.parametrize("path", ["media.preview", "profile.preview", "batch", "cli"])
def test_token_reaches_the_probes_run_while_building_preview_commands(qapp, tmp_path, monkeypatch, path):
    import threading
    import time

    from core.jobs.encode import ResolvedEncode
    from core.jobs.options import CommonOptions
    from core.service.errors import ApiError, ErrorCode
    from core.subprocess_utils import ProbeCancelledError

    ctx = _ctx(tool_ffmpeg="ffmpeg")
    wf = _probing_workflow(tmp_path)
    config = SimpleNamespace(video_tracks=[], video=None)
    resolved = ResolvedEncode(config=config, video=())  # type: ignore[arg-type]
    checked = lambda encode_config, *a, **k: (encode_config, [])  # noqa: E731 - validation réussie
    threading.Timer(0.3, ctx.cancel.cancel).start()
    start = time.monotonic()
    if path == "media.preview":
        from core.service.ops import media as ops

        monkeypatch.setattr(ops, "_build_cached", lambda request, ctx: (SimpleNamespace(), CommonOptions(), None))
        monkeypatch.setattr(ops, "resolve_job_encode", lambda *a: resolved)
        monkeypatch.setattr(ops, "encode_workflow", lambda *a: wf)
        monkeypatch.setattr(ops, "checked_encode", checked)
        monkeypatch.setattr(ops, "serialize_remux_config", lambda c: {})
        with pytest.raises(Exception) as caught:
            call(CATALOG, "media.preview", {"job": {"version": 1}}, ctx)
        assert ApiError.from_exception(caught.value).code is ErrorCode.CANCELLED  # code de l'enveloppe de call
    elif path == "profile.preview":
        from core.service.ops import profile as ops

        monkeypatch.setattr(ops, "_build", lambda request, ctx, preview: (SimpleNamespace(sources=[]), {}, CommonOptions()))
        monkeypatch.setattr(ops, "_resolve", lambda *a: resolved)
        monkeypatch.setattr(ops, "encode_workflow", lambda *a: wf)
        monkeypatch.setattr(ops, "checked_encode", checked)
        monkeypatch.setattr(ops, "serialize_remux_config", lambda c: {})
        with pytest.raises(Exception) as caught:
            call(CATALOG, "profile.preview", {"profile": "P", "inputs": ["/a.mkv"], "encode": {}}, ctx)
        assert ApiError.from_exception(caught.value).code is ErrorCode.CANCELLED
    elif path == "batch":
        from core.service.ops import batch as ops

        monkeypatch.setattr(ops, "encode_workflow", lambda *a: wf)
        monkeypatch.setattr(ops, "checked_encode", checked)
        children = ops._Children(ctx)
        logger = children.logger(0)
        assert children.preview_encode(SimpleNamespace(), CommonOptions(), logger, config) == 6
    else:
        from cli import runtime as ops

        monkeypatch.setattr(ops, "encode_workflow", lambda *a: wf)
        monkeypatch.setattr(ops, "checked_encode", checked)
        with pytest.raises(ProbeCancelledError):
            ops.preview_encode_config(SimpleNamespace(), CommonOptions(), SimpleNamespace(emit=lambda *a, **k: None),
                                      config, cancel_event=ctx.cancel.event)
    assert time.monotonic() - start < 5  # sonde en cours tuée par le jeton
    assert wf._probe_cancel_event is None  # jeton retiré à la fin de l'opération


def test_nested_probe_scopes_restore_the_previous_token_after_an_exception():
    import threading

    from core.jobs.encode_runtime import probe_cancel_scope
    from core.workflows.encode.workflow import EncodeWorkflow

    wf = EncodeWorkflow()
    outer, inner = threading.Event(), threading.Event()
    with probe_cancel_scope(wf, outer):
        with pytest.raises(RuntimeError):
            with probe_cancel_scope(wf, inner):
                assert wf._probe_cancel_event is inner
                raise RuntimeError("échec pendant l'opération")
        assert wf._probe_cancel_event is outer
    assert wf._probe_cancel_event is None


def test_run_encode_keeps_the_token_during_the_asynchronous_work(qapp, tmp_path):
    from core.service.context import CancelToken

    from tests.test_cli_encode import _FakeWorkflow, _run

    seen: list[Any] = []
    previous = object()

    class _ProbingWorkflow(_FakeWorkflow):
        _probe_cancel_event: Any = previous

        def set_probe_cancel_event(self, event):
            self._probe_cancel_event = event

    def work(signals):  # fil du worker : jeton de l'opération posé
        seen.append(wf._probe_cancel_event)
        signals.finished.emit("encoded")

    wf = _ProbingWorkflow(work)
    token = CancelToken()
    result, _logger, _events = _run(tmp_path, wf, cancel=token)
    assert result == "encoded" and seen == [token.event] and wf._probe_cancel_event is previous


def test_hdr10_text_formats_are_checked_when_the_request_is_read(tmp_path):
    import re

    from core.service.errors import ApiError
    from core.service.ops.media import MediaJobRequest
    from core.service.schemas.encode import EncodeBlock, StaticHdrSpec
    from core.workflows.encode.planning import validation

    md = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
    base = {"version": 1, "sources": [str(tmp_path / "a.mkv")]}

    def job(static_hdr, where="default"):
        video = {"default": {"static_hdr": static_hdr}} if where == "default" else {
            "tracks": [{"select": {"stream": 0}, "static_hdr": static_hdr}]}
        return {**base, "encode": {"video": video}}

    for valid in ({"master_display": md, "max_cll": "1000,400"}, {"master_display": "", "max_cll": " "},
                  {"master_display": f"  {md} ", "max_cll": " 1000,400"}, {"master_display": None}, {}):
        assert MediaJobRequest.parse({"job": job(valid)})[1] == ()
    for where, prefix in (("default", "job.encode.video.default"), ("track", "job.encode.video.tracks[0]")):
        with pytest.raises(ApiError) as caught:
            MediaJobRequest.parse({"job": job({"master_display": "oops", "max_cll": "1000"}, where)})
        assert {f"{prefix}.static_hdr.master_display", f"{prefix}.static_hdr.max_cll"} <= {
            d.path for d in caught.value.details}
    # Motif publié = motif vérifié, et même règle que la validation de l'encodage pour un texte non vide.
    schema = StaticHdrSpec.json_schema()["properties"]["master_display"]
    published = schema["pattern"]  # JSON Schema : ne s'applique qu'aux textes (null reste valide)
    for text in (md, f" {md}", "", "oops", "G(1,2)"):
        accepted = re.fullmatch(published, text) is not None
        assert accepted == (validation._MASTER_DISPLAY_RE.fullmatch(text) is not None)  # même motif complet
        try:
            EncodeBlock.parse({"video": {"default": {"static_hdr": {"master_display": text}}}})
            parsed = True
        except ApiError:
            parsed = False
        assert parsed == accepted



# --- Septième audit : schéma publié, jeton sur le parcours réel, lots --------------------------------------


def _ecma_anchors(pattern: str) -> str:
    """Motif publié (ECMA-262, sans drapeau ``m``) lu par ``re`` : ``^`` / ``$`` = début / fin de la chaîne seuls.

    En Python, ``$`` accepte aussi un saut de ligne final : ``\\A`` / ``\\Z`` gardent le sens ECMA.
    """
    assert pattern.startswith("^") and pattern.endswith("$") and "\\" not in pattern[-2:], pattern
    return "\\A" + pattern[1:-1] + "\\Z"


def _schema_errors(value: Any, schema: dict, path: str = "") -> list[str]:
    """Validateur JSON Schema indépendant (sous-ensemble publié : type, properties, additionalProperties,
    required, anyOf, enum, items, minimum, maximum, pattern) — sans le lecteur des modèles."""
    import re

    if "anyOf" in schema:
        outer = {key: item for key, item in schema.items() if key != "anyOf"}  # mots-clés communs aux branches
        branches = [_schema_errors(value, {**outer, **branch}, path) for branch in schema["anyOf"]]
        return [] if any(not errors for errors in branches) else [f"{path}: aucune branche"]
    errors: list[str] = []
    kinds = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}
    expected = schema.get("type")
    if expected == "integer":
        if not (isinstance(value, int) and not isinstance(value, bool)):
            return [f"{path}: entier attendu"]
    elif expected == "number":
        if not (isinstance(value, (int, float)) and not isinstance(value, bool)):
            return [f"{path}: nombre attendu"]
    elif expected in kinds and not isinstance(value, kinds[expected]):
        return [f"{path}: {expected} attendu"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: hors énumération")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: sous le minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: au-dessus du maximum")
    if isinstance(value, str) and "pattern" in schema and re.search(_ecma_anchors(schema["pattern"]), value) is None:
        errors.append(f"{path}: motif")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in value:
                errors.append(f"{path}.{key}: requis")
        for key, item in value.items():
            if key in properties:
                errors += _schema_errors(item, properties[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: clé inconnue")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors += _schema_errors(item, schema["items"], f"{path}[{index}]")
    return errors


def test_published_encode_schema_accepts_exactly_what_the_request_reader_accepts():
    from core.service.errors import ApiError
    from core.service.schemas.encode import EncodeBlock

    md = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
    documents = [
        {"video": {"default": {"codec": "libx265", "crf": 20}}},
        {"video": {"default": {"crf": 99}}},
        {"video": {"default": {"crf": True}}},
        {"video": {"default": {"crff": 20}}},
        {"video": {"default": {"static_hdr": {"mode": "manual", "master_display": md, "max_cll": "1000,400"}}}},
        {"video": {"default": {"static_hdr": {"master_display": f" {md}\t", "max_cll": ""}}}},
        {"video": {"default": {"static_hdr": {"master_display": None}}}},
        {"video": {"default": {"static_hdr": {"master_display": "oops"}}}},
        {"video": {"default": {"static_hdr": {"max_cll": "\u0661\u0660\u0660\u0660,\u0664\u0660\u0660"}}}},
        *({"video": {"default": {"static_hdr": {"max_cll": f"1000,400{end}"}}}}
          for end in ("\n", "\r", "\r\n", "\u2028", "\u2029")),  # fins de ligne : refusées partout
        {"video": {"default": {"static_hdr": {"max_cll": " \t1000,400 \t"}}}},
        {"video": {"default": {"static_hdr": {"master_display": f"{md}\n"}}}},
        {"video": {"default": {"static_hdr": {"mode": "bogus"}}}},
        {"video": {"tracks": [{"select": {"stream": 0}, "static_hdr": {"max_cll": "1000"}}]}},
        {"audio": [{"select": {"stream": 1}, "codec": "eac3", "bitrate_kbps": 640}]},
        {"target_size_mb": -1},
    ]
    schema = EncodeBlock.json_schema()
    accepted = []
    for document in documents:
        try:
            EncodeBlock.parse(document)
            parsed = True
        except ApiError:
            parsed = False
        assert parsed == (not _schema_errors(document, schema)), document
        accepted.append(parsed)
    assert accepted.count(True) >= 5 and accepted.count(False) >= 10  # cas valides et refus réellement couverts


# --- Douzième audit : Hybrid (annulation, aperçu), intentions des couches CLI ----------------------------------


def _hybrid_run(monkeypatch, tmp_path, ctx, count: int = 1, **request: Any):
    from core.service.ops import hybrid as hybrid_ops

    items = [SimpleNamespace(error=None, calibration=None, remux_config=SimpleNamespace(output=tmp_path / f"E{i}.mkv"),
                             episode=SimpleNamespace(display_name=f"E{i}", master_file=tmp_path / f"m{i}.mkv",
                                                     donor_files=[]))
             for i in range(count)]
    spec = SimpleNamespace(common=None, output_template=None)
    monkeypatch.setattr(hybrid_ops, "_prepare", lambda request, ctx: (spec, items))
    monkeypatch.setattr(hybrid_ops, "check_outputs", lambda *a, **k: None)
    payload = {"output_dir": str(tmp_path), "encode": {"video": {}}, **request}
    return hybrid_ops.hybrid_run.handler(hybrid_ops.HybridRequest.from_dict(payload), ctx)


@pytest.mark.parametrize("token_raised", [False, True])
def test_hybrid_episode_cancelled_while_resolving_the_encode_is_cancelled(monkeypatch, tmp_path, token_raised):
    from core.service.ops import batch as batch_ops
    from core.subprocess_utils import ProbeCancelledError

    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink)

    def encode(self, *_a, **_k):
        if token_raised:
            ctx.cancel.cancel()
        raise ProbeCancelledError("Sonde annulée.")

    monkeypatch.setattr(batch_ops._Children, "encode", encode)
    result = _hybrid_run(monkeypatch, tmp_path, ctx, count=2, continue_on_error=True)
    assert result.status == "cancelled" and [e["status"] for e in result.episodes] == ["cancelled"]
    assert ctx.cancel.cancelled  # lot arrêté
    finals = _finals(sink, ctx)
    assert list(finals.values()) == [["cancelled"]]  # un seul enfant, un seul état final


def _finals(sink, ctx) -> dict[str, list[str]]:
    from core.service.events import EventType

    finals: dict[str, list[str]] = {}
    for event in sink.events:
        if event.parent == ctx.events.job and event.type is EventType.STATE and event.data["state"] != "running":
            finals.setdefault(event.job, []).append(event.data["state"])
    return finals


@pytest.mark.parametrize("continue_on_error", [False, True])
def test_hybrid_encode_resolution_error_gives_the_child_a_final_state(monkeypatch, tmp_path, continue_on_error):
    from core.service.ops import batch as batch_ops

    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink)
    monkeypatch.setattr(batch_ops._Children, "encode", lambda self, *a, **k: (_ for _ in ()).throw(
        RuntimeError("bloc encode invalide")))
    result = _hybrid_run(monkeypatch, tmp_path, ctx, count=2, continue_on_error=continue_on_error)
    assert [e["status"] for e in result.episodes] == ["failed"] * (2 if continue_on_error else 1)
    assert result.status == "failed" and not ctx.cancel.cancelled
    assert sorted(map(tuple, _finals(sink, ctx).values())) == [("failed",)] * (2 if continue_on_error else 1)


def test_hybrid_interrupt_while_resolving_the_encode_closes_the_child(monkeypatch, tmp_path):
    from core.service.ops import batch as batch_ops

    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink)
    monkeypatch.setattr(batch_ops._Children, "encode", lambda self, *a, **k: (_ for _ in ()).throw(
        KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        _hybrid_run(monkeypatch, tmp_path, ctx)
    assert list(_finals(sink, ctx).values()) == [["cancelled"]]


@pytest.mark.parametrize("accept", [True, False, None])
def test_hybrid_dry_run_passes_the_dovi_nvenc_crop_acceptance_to_the_preview_checks(monkeypatch, tmp_path, accept):
    from core.service.ops import batch as batch_ops

    seen: list[bool] = []
    monkeypatch.setattr(batch_ops._Children, "encode", lambda self, *a, **k: SimpleNamespace())
    monkeypatch.setattr(batch_ops, "encode_workflow", lambda *a: SimpleNamespace())

    def checked(encode_config, wf, *, config, options, accept_dovi_nvenc_crop, cancel_event):  # contrôle réel appelé
        seen.append(accept_dovi_nvenc_crop)
        return encode_config, ([] if accept_dovi_nvenc_crop else ["recadrage aligné sur 32 requis"])

    monkeypatch.setattr(batch_ops, "checked_encode", checked)
    monkeypatch.setattr(batch_ops, "preview_encode", lambda *a: "ffmpeg …")
    encode: dict[str, Any] = {"video": {}}
    if accept is not None:
        encode["accept_dovi_nvenc_crop"] = accept
    result = _hybrid_run(monkeypatch, tmp_path, _ctx(), dry_run=True, encode=encode)
    assert seen == [bool(accept)]
    assert [e["status"] for e in result.episodes] == ["planned" if accept else "failed"]  # refus sans acceptation


def _hybrid_children_steps(monkeypatch, outcomes: list[Any], ctx):
    """Aperçu et lancement réels de ``_Children`` ; ``outcomes`` : ``None`` (réussite) ou exception, par épisode."""
    from core.service.ops import batch as batch_ops

    queue = list(outcomes)

    def step(*_a, **_k):
        outcome = queue.pop(0)
        if outcome == "token":
            ctx.cancel.cancel()
            outcome = ProbeCancelledError("Sonde annulée.")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    from core.subprocess_utils import ProbeCancelledError

    monkeypatch.setattr(batch_ops._Children, "encode", lambda self, *a, **k: SimpleNamespace(output=Path("/tmp/o.mkv")))
    monkeypatch.setattr(batch_ops, "encode_workflow", lambda *a: SimpleNamespace())
    monkeypatch.setattr(batch_ops, "checked_encode", lambda cfg, *a, **k: (step(), (cfg, []))[1])
    monkeypatch.setattr(batch_ops, "preview_encode", lambda *a: "ffmpeg …")
    monkeypatch.setattr(batch_ops, "run_encode", lambda *a, **k: step())


@pytest.mark.parametrize("continue_on_error", [False, True])
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("second", ["token", "probe"])
def test_hybrid_cancellation_during_the_preview_or_the_run_is_a_cancellation(monkeypatch, tmp_path, dry_run, second,
                                                                             continue_on_error):
    from core.subprocess_utils import ProbeCancelledError

    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink)
    outcome = "token" if second == "token" else ProbeCancelledError("Sonde annulée.")
    _hybrid_children_steps(monkeypatch, [None, outcome], ctx)
    result = _hybrid_run(monkeypatch, tmp_path, ctx, count=3, dry_run=dry_run, continue_on_error=continue_on_error)
    done = "planned" if dry_run else "ok"
    assert [e["status"] for e in result.episodes] == [done, "cancelled"]  # épisodes suivants jamais lancés
    assert result.status == "partial" and ctx.cancel.cancelled  # une réussite conservée
    assert sorted(states[0] for states in _finals(sink, ctx).values()) == ["cancelled", "succeeded"]
    assert all(len(states) == 1 for states in _finals(sink, ctx).values())


def test_hybrid_cancellation_of_the_first_episode_cancels_the_parent(monkeypatch, tmp_path):
    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink)
    _hybrid_children_steps(monkeypatch, ["token"], ctx)
    result = _hybrid_run(monkeypatch, tmp_path, ctx, count=2)
    assert result.status == "cancelled" and [e["status"] for e in result.episodes] == ["cancelled"]


def test_hybrid_interrupt_during_the_run_propagates_with_a_final_child(monkeypatch, tmp_path):
    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink)
    _hybrid_children_steps(monkeypatch, [KeyboardInterrupt()], ctx)
    with pytest.raises(KeyboardInterrupt):
        _hybrid_run(monkeypatch, tmp_path, ctx)
    assert list(_finals(sink, ctx).values()) == [["cancelled"]]


@pytest.mark.parametrize("default, track", [
    ({"codec": "libx265", "tonemap": {"enabled": True}}, {"codec": "copy", "tonemap": {"enabled": False}}),
    ({"codec": "libx265", "dolby_vision": {"mode": "off"}}, {"codec": "copy", "dolby_vision": {"mode": "copy"}}),
    ({"codec": "libx265", "hdr10plus": {"mode": "off"}}, {"codec": "copy", "hdr10plus": {"mode": "copy"}}),
    ({"codec": "libx265", "static_hdr": {"mode": "off"}}, {"codec": "copy", "static_hdr": {"mode": "source"}}),
])
def test_a_later_cli_layer_replaces_the_intents_of_an_earlier_one(default, track):
    from tests.test_encode_request import _resolve

    payload = {"video": {"default": default, "tracks": [{"select": {"stream": 0}, **track}]}}
    from core.inspector import HDRType

    hdr = HDRType.DOLBY_VISION_HDR10PLUS
    layered = _resolve(payload, hdr).config.video_tracks[0]  # accepté : intention de la piste seule retenue
    merged = {**default, **track}
    direct = _resolve({"video": {"default": merged}}, hdr).config.video_tracks[0]
    assert layered == direct
    with pytest.raises(JobError):  # intention finale contradictoire : refus inchangé
        _resolve({"video": {"default": default, "tracks": [{"select": {"stream": 0}, "codec": "copy"}]}}, hdr)


# --- Treizième audit : jeton seul, rapport Hybrid, inspections annulables, fusions et null, avertissements -------


@pytest.mark.parametrize("failure", ["validation", "exception"])
def test_hybrid_failure_after_the_token_alone_is_cancelled_for_the_child_too(monkeypatch, tmp_path, failure):
    from core.service.ops import batch as batch_ops

    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink)
    monkeypatch.setattr(batch_ops._Children, "encode", lambda self, *a, **k: SimpleNamespace(output=Path("/tmp/o.mkv")))
    monkeypatch.setattr(batch_ops, "encode_workflow", lambda *a: SimpleNamespace())

    def checked(cfg, *_a, **_k):
        ctx.cancel.cancel()  # jeton seul : aucune exception typée d'annulation
        if failure == "exception":
            raise RuntimeError("géométrie illisible")
        return cfg, ["géométrie indisponible"]

    monkeypatch.setattr(batch_ops, "checked_encode", checked)
    result = _hybrid_run(monkeypatch, tmp_path, ctx, count=2, dry_run=True, continue_on_error=True)
    assert result.status == "cancelled" and [e["status"] for e in result.episodes] == ["cancelled"]
    assert list(_finals(sink, ctx).values()) == [["cancelled"]]  # enfant cohérent avec l'épisode


def test_hybrid_report_is_written_when_an_interrupt_propagates(monkeypatch, tmp_path):
    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink)
    _hybrid_children_steps(monkeypatch, [None, KeyboardInterrupt()], ctx)
    report = tmp_path / "rapport.json"
    with pytest.raises(KeyboardInterrupt):
        _hybrid_run(monkeypatch, tmp_path, ctx, count=3, report_json=str(report))
    episodes = json.loads(report.read_text(encoding="utf-8"))["episodes"]
    assert [episode["status"] for episode in episodes] == ["ok", "cancelled"]


def _slow_ffprobe(tmp_path: Path) -> Path:
    """FFprobe de test : pose le témoin ``ffprobe-entree`` à son démarrage, puis attend."""
    tool = tmp_path / "ffprobe"
    tool.write_text(f'#!/bin/sh\ntouch "{tmp_path / "ffprobe-entree"}"\nexec sleep 30\n', encoding="utf-8")
    tool.chmod(0o755)
    return tool


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX")
@pytest.mark.parametrize("operation", ["media.inspect", "media.extract", "sync.scan-cadence", "sync.scan-kind",
                                       "sync.scan-kind-cli"])
def test_public_inspections_stop_when_the_operation_is_cancelled(tmp_path, operation):
    import threading
    import time

    from core.service.errors import ApiError, ErrorCode

    source = tmp_path / "src.mkv"
    source.write_bytes(b"x")
    ctx = _ctx(tool_ffprobe=str(_slow_ffprobe(tmp_path)), tool_mediainfo="/bin/false", tool_ffmpeg="ffmpeg",
               sync_cadence_auto_apply=True)
    requests = {
        "media.inspect": {"inputs": [str(source)]},
        "media.extract": {"source": str(source), "all": True},
        "sync.scan-cadence": {"ref": str(source), "target": str(source), "type": "audio", "cadence_auto": True},
        "sync.scan-kind": {"ref": str(source), "target": str(source), "type": "auto", "stream_target": "1",
                           "cadence_auto": False},
    }
    requests["sync.scan-kind-cli"] = requests["sync.scan-kind"]
    if operation.endswith("-cli"):  # port fourni par la commande sync-scan (cli/hybrid.py) : jeton transmis par **k
        from cli.hybrid import detect_stream_kind

        ctx = ExecutionContext.create(ctx.config, ListSink(), ports={
            "detect_stream_kind": lambda *a, **k: detect_stream_kind(*a, **k)})
    entered = tmp_path / "ffprobe-entree"

    def cancel_on_entry() -> None:  # annulation à l'entrée effective de la sonde visée
        deadline = time.monotonic() + 30
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        ctx.cancel.cancel()

    threading.Thread(target=cancel_on_entry, daemon=True).start()
    start = time.monotonic()
    with pytest.raises(Exception) as caught:
        call(CATALOG, operation.split("-")[0], requests[operation], ctx)
    assert entered.exists() and ApiError.from_exception(caught.value).code is ErrorCode.CANCELLED
    assert time.monotonic() - start < 5  # sonde tuée par le jeton, pas attendue


def test_pure_copy_publishes_its_hdr_warnings():
    from tests.test_encode_request import _resolve

    emitted: list[tuple[str, str]] = []
    logger = SimpleNamespace(emit=lambda level, message, **_k: emitted.append((level, message)))
    from core.inspector import HDRType

    resolved = _resolve({"video": {"default": {"codec": "copy", "dolby_vision": {"mode": "copy"}}}},
                        HDRType.NONE, logger=logger)
    assert resolved.config is None and resolved.warnings  # copie pure : le job reste un remux
    assert [message for level, message in emitted if level == "warning"] == list(resolved.warnings)


@pytest.mark.parametrize("base, override, expected", [
    ({"mode": "off"}, {"estimate": None}, ("", False, "")),
    ({"mode": "manual", "max_cll": "2000,500"}, {"estimate": None}, ("", True, "2000,500")),
    ({"estimate": "fast"}, {"mode": None}, ("fast", True, "1000,400")),
    ({"estimate": "fast"}, {"max_cll": None}, ("fast", True, "1000,400")),
])
def test_null_keys_never_remove_a_static_hdr_choice_in_merges(base, override, expected):
    from core.jobs.batch import merge_encode_block
    from core.jobs.json_io import deep_merge
    from tests.test_encode_request import _hdr10_encode

    def block(static_hdr: dict) -> dict:
        return {"video": {"default": {"codec": "libx265", "static_hdr": static_hdr}}}

    for encode in (merge_encode_block({"encode": block(base)}, block(override))["encode"],
                   deep_merge({"encode": block(base)}, {"encode": block(override)})["encode"]):
        settings = _hdr10_encode(encode)
        assert (settings.static_hdr_metadata_analysis_request, settings.inject_hdr_meta, settings.max_cll) == expected


def test_cli_options_merged_into_a_read_job_then_its_portable_block_keep_the_static_choice(tmp_path):
    """Lecture (load_job_document) → options analysées par le parseur → résolution → bloc portable relu.

    Le bloc est écrit ici ; l'export réel de la commande est prouvé par l'intégration
    ``test_encode_export_then_validate_keeps_the_static_hdr_choice``."""
    from cli.encode_options import apply_encode_options
    from core.json_documents import portable_encode_block
    from core.jobs.json_io import load_job_document
    from tests.test_encode_request import _hdr10_encode

    document = tmp_path / "job.json"
    document.write_text(json.dumps({"version": 1, "sources": ["src.mkv"], "output": "sortie.mkv", "encode": {
        "video": {"default": {"codec": "libx265", "static_hdr": {"estimate": "fast"}},
                  "tracks": [{"select": {"stream": 0}, "static_hdr": {"estimate": "precise"}}]}}}), encoding="utf-8")
    job = load_job_document(document)
    assert job["sources"] == [str(tmp_path / "src.mkv")]  # document réellement relu (chemins résolus)
    cases = (
        (["--video-set", "static_hdr.max_cll=2000,500"], ("", "2000,500")),  # défaut CLI face à la piste du job
        (["--video-set", "static_hdr.mode=null"], ("precise", "1000,400")),  # null d'une autre clé : rien retiré
    )
    for argv, expected in cases:
        args = build_parser().parse_args(["encode", "-o", str(tmp_path / "o.mkv"), *argv])
        merged = apply_encode_options(job, args)
        settings = _hdr10_encode(merged["encode"])
        assert (settings.static_hdr_metadata_analysis_request, settings.max_cll) == expected, argv
        exported = tmp_path / "export.json"
        exported.write_text(json.dumps({**merged, "encode": portable_encode_block(merged["encode"])}), encoding="utf-8")
        reread = _hdr10_encode(load_job_document(exported)["encode"])
        assert (reread.static_hdr_metadata_analysis_request, reread.max_cll) == expected, argv


def test_encoded_path_publishes_each_warning_once():
    from core.inspector import HDRType
    from tests.test_encode_request import _resolve

    emitted: list[str] = []
    logger = SimpleNamespace(emit=lambda level, message, **_k: emitted.append(message) if level == "warning" else None)
    resolved = _resolve({"video": {"default": {"codec": "libx265", "dolby_vision": {"mode": "copy"}}}},
                        HDRType.NONE, logger=logger)
    assert resolved.config is not None and resolved.warnings and emitted == list(resolved.warnings)


@pytest.mark.parametrize("interrupted", [True, False])
def test_hybrid_report_write_failure_never_hides_an_interrupt(monkeypatch, tmp_path, interrupted):
    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink)
    _hybrid_children_steps(monkeypatch, [None, KeyboardInterrupt() if interrupted else None], ctx)
    report = tmp_path / "rapport"
    report.mkdir()  # écriture impossible : un dossier occupe le chemin
    expected = KeyboardInterrupt if interrupted else OSError
    with pytest.raises(expected):
        _hybrid_run(monkeypatch, tmp_path, ctx, count=2, report_json=str(report))
    logged = [e.data.get("message", "") for e in sink.events if e.type.value == "log"]
    assert any("Rapport des épisodes non écrit" in message for message in logged) is interrupted
    assert all(len(states) == 1 for states in _finals(sink, ctx).values())


def _probe_scripts(tmp_path: Path, slow: str) -> tuple[Path, Path, Path]:
    """FFprobe / MediaInfo de test : rapides, sauf l'appel ``slow`` (ffprobe, mediainfo, framecount) qui pose un
    témoin puis attend."""
    mark = tmp_path / "entree"
    quick_probe = 'echo \'{"streams": [], "format": {"duration": "1.0"}}\''
    ffprobe = tmp_path / "ffprobe"
    ffprobe.write_text("#!/bin/sh\n" + (f'touch "{mark}"; exec sleep 30\n' if slow == "ffprobe" else quick_probe + "\n"),
                       encoding="utf-8")
    mediainfo = tmp_path / "mediainfo"
    slow_case = {"mediainfo": "*Output=JSON*", "framecount": "*FrameCount*"}.get(slow, "__jamais__")
    mediainfo.write_text("#!/bin/sh\ncase \"$*\" in " + slow_case + f') touch "{mark}"; exec sleep 30;; esac\n'
                         "case \"$*\" in *Output=JSON*) echo '{}';; esac\n", encoding="utf-8")
    for tool in (ffprobe, mediainfo):
        tool.chmod(0o755)
    return ffprobe, mediainfo, mark


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX")
@pytest.mark.parametrize("slow", ["ffprobe", "mediainfo", "framecount"])
def test_inspection_is_cancelled_in_each_of_its_probes(tmp_path, slow):
    import threading
    import time

    from core.service.errors import ApiError, ErrorCode

    source = tmp_path / "src.mkv"
    source.write_bytes(b"x")
    ffprobe, mediainfo, mark = _probe_scripts(tmp_path, slow)
    ctx = _ctx(tool_ffprobe=str(ffprobe), tool_mediainfo=str(mediainfo))

    def cancel_on_entry() -> None:  # annulation au démarrage effectif de la sonde visée
        deadline = time.monotonic() + 30
        while not mark.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        ctx.cancel.cancel()

    threading.Thread(target=cancel_on_entry, daemon=True).start()
    start = time.monotonic()
    with pytest.raises(Exception) as caught:
        call(CATALOG, "media.inspect", {"inputs": [str(source)]}, ctx)
    assert mark.exists() and ApiError.from_exception(caught.value).code is ErrorCode.CANCELLED
    assert time.monotonic() - start < 10  # sonde terminée par le jeton (30 s sinon)


# --- Quatorzième audit : job unique annulé, chemins relatifs des requêtes de profil, MediaInfo des cadences ------


@pytest.mark.parametrize("token_raised", [True, False])
def test_single_remux_failing_after_the_token_is_cancelled(tmp_path, token_raised):
    from core.jobs.options import CommonOptions
    from core.jobs.runtime import run_remux
    from core.runner import TaskSignals
    from core.service.context import CancelToken
    from core.service.errors import ApiError, ErrorCode

    token = CancelToken()

    class _Wf:
        log_message = backend_event = SimpleNamespace(connect=lambda *_a: None)

        def run(self, _config):
            if token_raised:
                token.cancel()
            signals = TaskSignals()
            signals.failed.emit("ffmpeg interrompu", RuntimeError("ffmpeg interrompu"))  # cause non typée
            return signals

    with pytest.raises(ApiError) as caught:
        run_remux(SimpleNamespace(output=tmp_path / "out.mkv"), config=None, options=CommonOptions(),  # type: ignore[arg-type]
                  logger=SimpleNamespace(emit=lambda *a, **k: None), cancel=token, workflow_factory=_Wf)
    assert caught.value.code is (ErrorCode.CANCELLED if token_raised else ErrorCode.WORKFLOW_FAILED)


@pytest.mark.parametrize("token_raised", [True, False])
def test_single_encode_failing_after_the_token_is_cancelled(qapp, tmp_path, token_raised):
    from core.service.context import CancelToken
    from core.service.errors import ApiError, ErrorCode

    from tests.test_cli_encode import _FakeWorkflow, _run

    token = CancelToken()

    def work(signals):
        if token_raised:
            token.cancel()
        signals.failed.emit("x265 interrompu", RuntimeError("x265 interrompu"))

    result, _logger, _events = _run(tmp_path, _FakeWorkflow(work), cancel=token)
    assert isinstance(result, ApiError)
    assert result.code is (ErrorCode.CANCELLED if token_raised else ErrorCode.WORKFLOW_FAILED)


@pytest.mark.parametrize("ending", ["failed", "finished"])
@pytest.mark.parametrize("kind", ["remux", "encode"])
@pytest.mark.parametrize("operation", ["media.run", "profile.apply"])
def test_single_job_after_the_token_is_cancelled_or_kept_succeeded(qapp, monkeypatch, tmp_path, operation, kind,
                                                                   ending):
    """Audit 14 : job unique (media.run, profile.apply ; remux et encodage). Échec après la levée du jeton →
    ``cancelled`` (un seul état terminal, une seule erreur au journal) ; réussite après le jeton → ``succeeded`` +
    ``cancel_too_late``."""
    from core.jobs.options import CommonOptions
    from core.runner import TaskSignals
    from core.service.errors import ApiError, ErrorCode
    from core.service.events import EventType
    from core.service.ops import media as media_ops

    from tests.test_cli_encode import _config, _FakeWorkflow

    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(tool_ffprobe="ffprobe", tool_ffmpeg="ffmpeg",
                                                  tool_mediainfo="mediainfo"), sink)
    output = tmp_path / "out.mkv"

    def finish(signals) -> None:
        ctx.cancel.cancel()
        if ending == "failed":
            signals.failed.emit("outil interrompu", RuntimeError("outil interrompu"))
        else:
            output.write_bytes(b"mkv")
            signals.finished.emit("publié")

    class _RemuxWf:
        log_message = backend_event = SimpleNamespace(connect=lambda *_a: None)

        def run(self, _config):
            signals = TaskSignals()
            finish(signals)
            return signals

    remux = SimpleNamespace(output=output, allow_missing_output_dir=True)
    encode_config = _config(tmp_path) if kind == "encode" else None
    if encode_config is not None:
        output = encode_config.output
    monkeypatch.setattr("core.jobs.runtime.remux_workflow", lambda *a: _RemuxWf())
    monkeypatch.setattr("core.jobs.encode_runtime.encode_workflow", lambda *a: _FakeWorkflow(finish))
    monkeypatch.setattr("core.jobs.encode_runtime.checked_encode", lambda cfg, *a, **k: (cfg, []))
    if operation == "media.run":
        monkeypatch.setattr(media_ops, "build_remux_config", lambda *a, **k: remux)
        monkeypatch.setattr(media_ops, "resolve_job_encode", lambda *a: SimpleNamespace(config=encode_config))
        request: dict[str, Any] = {"job": {"version": 1}}
    else:
        monkeypatch.setattr(profile_ops, "_build", lambda request, ctx, preview: (remux, {}, CommonOptions()))
        monkeypatch.setattr(profile_ops, "_resolve", lambda *a: SimpleNamespace(config=encode_config))
        request = {"profile": "P", "inputs": [str(tmp_path / "src.mkv")]}
    if ending == "failed":
        with pytest.raises(Exception) as caught:
            call(CATALOG, operation, request, ctx)
        assert ApiError.from_exception(caught.value).code is ErrorCode.CANCELLED
    else:
        call(CATALOG, operation, request, ctx)
    finals = [e.data["state"] for e in sink.events if e.job == ctx.events.job and e.type is EventType.STATE
              and e.data["state"] != "running"]
    assert finals == ["cancelled" if ending == "failed" else "succeeded"]  # un seul état terminal
    logs = [e.data.get("message", "") for e in sink.events if e.type is EventType.LOG and e.data.get("level") == "error"]
    assert logs.count("outil interrompu") == (1 if ending == "failed" else 0)  # pas de doublon au journal
    codes = [e.data.get("code") for e in sink.events if e.type is EventType.LOG and e.data.get("level") == "warning"]
    assert ("cancel_too_late" in codes) is (ending == "finished")


def test_profile_request_paths_are_read_from_the_request_folder(tmp_path):
    from core.service.ops.batch import ProfileBatchRequest
    from core.service.ops.hybrid import HybridRequest
    from core.service.ops.profile import ProfileApplyRequest, ProfileRequest

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "film.json").write_text("{}", encoding="utf-8")
    (jobs / "serie.json").write_text("{}", encoding="utf-8")
    base = {"inputs": [str(tmp_path / "src.mkv")]}

    def apply(**values):
        return ProfileApplyRequest.from_dict({**base, **values}, base_dir=jobs)

    assert apply(profile="film.json", output="sortie.mkv").profile == str(jobs / "film.json")
    assert apply(profile="film.json", output="sortie.mkv").output == str(jobs / "sortie.mkv")
    assert apply(profile="serie", output="sorties/").profile == str(jobs / "serie")  # fichier présent avec .json
    assert apply(profile="serie", output="sorties/").output == str(jobs / "sorties") + "/"  # « dossier/ » gardé
    assert apply(profile="Anime 4K").profile == "Anime 4K"  # nom d'un profil enregistré : inchangé
    assert apply(profile="manquant.json").profile == str(jobs / "manquant.json")  # suffixe : chemin
    absolute = str(tmp_path / "abs.json")
    assert apply(profile=absolute, output=str(tmp_path / "o.mkv")).output == str(tmp_path / "o.mkv")
    assert ProfileRequest.from_dict({"profile": "film.json"}, base_dir=jobs).profile == str(jobs / "film.json")
    batch = ProfileBatchRequest.from_dict({"profile": "film.json", "output_dir": "sorties"}, base_dir=jobs)
    assert batch.profile == str(jobs / "film.json") and batch.output_dir == jobs / "sorties"
    hybrid = HybridRequest.from_dict({"output_dir": "out", "profile": "film.json"}, base_dir=jobs)
    assert hybrid.profile == str(jobs / "film.json")
    historic = ProfileApplyRequest.from_dict({**base, "profile": "film.json", "output": "sortie.mkv"},
                                             base_dir=jobs, allow_relative=True)  # argv : depuis le cwd
    assert (historic.profile, historic.output) == ("film.json", "sortie.mkv")


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX")
def test_cadence_inspection_uses_the_mediainfo_override(tmp_path):
    from core.service.context import Overrides

    calls = tmp_path / "mediainfo-appels"
    mediainfo = tmp_path / "mediainfo-surcharge"
    mediainfo.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\necho "{{}}"\n', encoding="utf-8")
    mediainfo.chmod(0o755)
    ffprobe = tmp_path / "ffprobe"
    ffprobe.write_text('#!/bin/sh\necho \'{"streams": [], "format": {"duration": "1.0"}}\'\n', encoding="utf-8")
    ffprobe.chmod(0o755)
    source = tmp_path / "src.mkv"
    source.write_bytes(b"x")
    scanner = SimpleNamespace(scan=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fin du test")))
    ctx = ExecutionContext.create(SimpleNamespace(tool_ffprobe=str(ffprobe), tool_mediainfo="/bin/false"), ListSink(),
                                  overrides=Overrides(mediainfo=str(mediainfo)), ports={"audio_scanner": lambda: scanner})
    with pytest.raises(Exception):
        call(CATALOG, "sync.scan", {"ref": str(source), "target": str(source), "type": "audio",
                                    "cadence_auto": True}, ctx)
    assert calls.exists()  # binaire de la surcharge appelé, pas celui du PATH


def _slow_ffmpeg_for(tmp_path: Path, slow_case: str) -> tuple[Path, Path]:
    """FFmpeg de test : l'appel dont les arguments correspondent à ``slow_case`` écrit son PID puis attend ; les
    autres répondent aussitôt (``-encoders`` liste libx264)."""
    pid_file = tmp_path / "sonde.pid"
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text(
        "#!/bin/sh\n"
        f'case "$*" in {slow_case}) echo $$ > "{pid_file}"; exec sleep 30;; esac\n'
        'case "$*" in *-encoders*) echo " V....D libx264              libx264 H.264";; esac\n',
        encoding="utf-8")
    ffmpeg.chmod(0o755)
    return ffmpeg, pid_file


def _process_gone(pid: int, within: float = 3.0) -> bool:
    import os
    import time

    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:  # zombie déjà tué : plus en cours d'exécution
            if Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
                return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(not Path("/bin/sh").exists() or not Path("/proc").exists(), reason="Linux")
@pytest.mark.parametrize("section, slow_case", [("vulkan", "*-filters*"), ("encoders", "*-encoders*"),
                                                ("presets", "*-h*encoder=*")])
def test_capabilities_probes_are_killed_when_the_operation_is_cancelled(tmp_path, section, slow_case):
    import threading
    import time

    from core.service.errors import ApiError, ErrorCode

    ffmpeg, pid_file = _slow_ffmpeg_for(tmp_path, slow_case)
    ctx = _ctx(tool_ffmpeg=str(ffmpeg), tool_nvencc="")

    def cancel_on_entry() -> None:
        deadline = time.monotonic() + 30
        while not (pid_file.exists() and pid_file.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.01)
        ctx.cancel.cancel()

    threading.Thread(target=cancel_on_entry, daemon=True).start()
    start = time.monotonic()
    with pytest.raises(Exception) as caught:
        call(CATALOG, "system.capabilities", {"sections": [section]}, ctx)
    assert ApiError.from_exception(caught.value).code is ErrorCode.CANCELLED
    assert time.monotonic() - start < 10
    assert _process_gone(int(pid_file.read_text()))  # sonde tuée, pas abandonnée


def test_capabilities_never_publishes_a_late_result_and_relays_probe_errors(monkeypatch):
    import threading
    import time

    from core.service.errors import ApiError, ErrorCode
    from core.workflows.encode import interpolation

    seen: list[Any] = []
    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink)
    returned = threading.Event()

    def slow_gpu(_rife, **kwargs):  # sonde qui ignore le jeton : l'opération n'attend pas
        seen.append(kwargs.get("cancel_event"))
        time.sleep(5)
        returned.set()
        return interpolation.GpuAcceleration(interpolation.NvofCapability(reason="tard"),
                                             interpolation.TrtCapability(reason="tard"))

    monkeypatch.setattr(interpolation, "detect_gpu_acceleration", slow_gpu)
    threading.Timer(0.2, ctx.cancel.cancel).start()
    start = time.monotonic()
    with pytest.raises(Exception) as caught:
        call(CATALOG, "system.capabilities", {"sections": ["gpu"]}, ctx)
    # Délai de grâce (2 s, temps de tuer une sonde qui reçoit le jeton), jamais toute la sonde (5 s).
    assert ApiError.from_exception(caught.value).code is ErrorCode.CANCELLED and time.monotonic() - start < 3
    assert seen == [ctx.cancel.event]  # jeton transmis à la sonde
    assert returned.wait(10)  # retour tardif effectivement survenu…
    from core.service.events import EventType

    assert not any(e.type is EventType.RESULT for e in sink.events)  # …sans rien publier
    assert [e.data["state"] for e in sink.events if e.type is EventType.STATE][-1] == "cancelled"
    monkeypatch.setattr(interpolation, "detect_gpu_acceleration",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("liste des GPU illisible")))
    with pytest.raises(Exception, match="liste des GPU illisible"):
        call(CATALOG, "system.capabilities", {"sections": ["gpu"]}, _ctx())


def test_profile_reference_edge_cases_and_operations(tmp_path, monkeypatch):
    from core.json_documents import resolve_output_reference, resolve_profile_reference
    from core.service.ops.hybrid import HybridPlanRequest
    from core.service.ops.profile import ProfilePreviewRequest

    jobs, cwd = tmp_path / "jobs", tmp_path / "cwd"
    jobs.mkdir()
    cwd.mkdir()
    (cwd / "film.json").write_text("{}", encoding="utf-8")  # homonyme dans le cwd
    (jobs / "film.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(cwd)
    assert resolve_profile_reference("film.json", jobs) == str(jobs / "film.json")  # dossier de la requête
    assert resolve_profile_reference("Film/4K", jobs) == "Film/4K"  # nom enregistré avec « / »
    assert resolve_profile_reference("~/profils/film.json", jobs) == "~/profils/film.json"  # chemin personnel
    assert resolve_profile_reference(str(tmp_path / "abs.json"), jobs) == str(tmp_path / "abs.json")
    assert resolve_output_reference("~/sorties/", jobs) == "~/sorties/"
    assert resolve_output_reference(None, jobs) is None and resolve_output_reference("", jobs) == ""
    base = {"inputs": [str(tmp_path / "src.mkv")]}
    assert ProfilePreviewRequest.from_dict({**base, "profile": "film.json"}, base_dir=jobs).profile == \
        str(jobs / "film.json")
    assert HybridPlanRequest.from_dict({"output_dir": "o", "profile": "film.json"}, base_dir=jobs).profile == \
        str(jobs / "film.json")
    # Requête sur stdin : dossier de base = cwd (``call --request -``).
    assert ProfilePreviewRequest.from_dict({**base, "profile": "film.json"}, base_dir=Path.cwd()).profile == \
        str(cwd / "film.json")


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX")
def test_cadence_inspection_uses_the_configured_mediainfo_without_override(tmp_path):
    calls = tmp_path / "mediainfo-appels"
    mediainfo = tmp_path / "mediainfo-config"
    mediainfo.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\necho "{{}}"\n', encoding="utf-8")
    mediainfo.chmod(0o755)
    ffprobe = tmp_path / "ffprobe"
    ffprobe.write_text('#!/bin/sh\necho \'{"streams": [], "format": {"duration": "1.0"}}\'\n', encoding="utf-8")
    ffprobe.chmod(0o755)
    source = tmp_path / "src.mkv"
    source.write_bytes(b"x")
    scanner = SimpleNamespace(scan=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fin du test")))
    ctx = ExecutionContext.create(SimpleNamespace(tool_ffprobe=str(ffprobe), tool_mediainfo=str(mediainfo)),
                                  ListSink(), ports={"audio_scanner": lambda: scanner})
    with pytest.raises(Exception):
        call(CATALOG, "sync.scan", {"ref": str(source), "target": str(source), "type": "audio",
                                    "cadence_auto": True}, ctx)
    assert calls.exists()  # binaire de la configuration, pas celui du PATH


# --- Quinzième audit : sondes RIFE, adaptateurs Windows, sondes parallèles, NVEncC, --list-gpus ------------------


def _pid_script(path: Path, slow: str, quick: str = "") -> Path:
    """Script de test : arguments ``slow`` → PID écrit dans ``<path>.pid`` puis attente ; sinon ``quick``."""
    pid = path.with_suffix(".pid")
    path.write_text(f'#!/bin/sh\ncase "$*" in {slow}) echo $$ > "{pid}"; exec sleep 30;; esac\n{quick}\n',
                    encoding="utf-8")
    path.chmod(0o755)
    return path


def _cancel_when(paths: list[Path], event) -> None:
    import threading
    import time

    def wait() -> None:
        deadline = time.monotonic() + 30
        while not all(p.exists() and p.read_text().strip() for p in paths) and time.monotonic() < deadline:
            time.sleep(0.01)
        event.set()

    threading.Thread(target=wait, daemon=True).start()


_LINUX = pytest.mark.skipif(not Path("/bin/sh").exists() or not Path("/proc").exists(), reason="Linux")


@_LINUX
@pytest.mark.parametrize("slow", ["--version", "--capabilities"])
def test_rife_version_and_capabilities_probes_are_killed_with_a_token(tmp_path, slow):
    import threading

    from core.subprocess_utils import ProbeCancelledError
    from core.workflows.encode.interpolation import rife_capabilities

    from core.workflows.encode.interpolation import clear_rife_version_cache

    clear_rife_version_cache()  # caches froids
    rife = _pid_script(tmp_path / f"rife{slow}", f"*{slow}*", 'echo "muxiveo-rife 9.9.9"')
    event = threading.Event()
    _cancel_when([rife.with_suffix(".pid")], event)
    with pytest.raises(ProbeCancelledError):
        rife_capabilities(str(rife), event)
    assert _process_gone(int(rife.with_suffix(".pid").read_text()))
    # Nouvel appel (interface, sans jeton) après l'annulation : rien de mémorisé, sonde relancée puis cache.
    rife.write_text('#!/bin/sh\ncase "$*" in *--version*) echo "muxiveo-rife 1.6.0";; esac\n', encoding="utf-8")
    first = rife_capabilities(str(rife))
    assert first is not None and first.label == "1.6.0"
    assert rife_capabilities(str(rife)) is first  # réussite mémorisée sans jeton
    rife.write_text('#!/bin/sh\ncase "$*" in *--version*) echo "muxiveo-rife 1.6.1";; esac\n# remplacé\n',
                    encoding="utf-8")
    replaced = rife_capabilities(str(rife))  # binaire remplacé : identité du fichier changée, relu
    assert replaced is not None and replaced.label == "1.6.1"
    clear_rife_version_cache()


@_LINUX
@pytest.mark.parametrize("codec", ["hevc_qsv", "h264_amf", "hevc_nvenc"])
def test_windows_adapter_selection_is_killed_with_a_token(tmp_path, monkeypatch, codec):
    import threading

    from core.subprocess_utils import ProbeCancelledError
    from core.workflows.encode import hw_devices

    probe = _pid_script(tmp_path / "ffmpeg.exe", "*")  # véritable processus (simulation .exe sous Linux)
    started: list[int] = []
    monkeypatch.setattr(hw_devices, "_windows_probe_command",
                        lambda ffmpeg, codec_id, index: started.append(index) or [str(probe), str(index)])
    event = threading.Event()
    _cancel_when([probe.with_suffix(".pid")], event)
    with pytest.raises(ProbeCancelledError):
        hw_devices.probe_windows_hwaccel_device(codec, ffmpeg_bin=str(probe), cancel_event=event)
    assert _process_gone(int(probe.with_suffix(".pid").read_text()))
    assert started == [0]  # arrêt avant l'adaptateur suivant


@_LINUX
def test_parallel_hardware_probes_are_all_killed_with_a_token(tmp_path, monkeypatch):
    import threading

    from core.subprocess_utils import ProbeCancelledError
    from core.workflows.encode.hardware import HardwareEncoderDetector

    scripts = {codec: _pid_script(tmp_path / f"{codec}.sh", "*") for codec in ("hevc_vaapi", "h264_vaapi")}
    event = threading.Event()
    detector = HardwareEncoderDetector(cancel_event=event)
    monkeypatch.setattr(detector, "_probe_command", lambda ffmpeg, codec: [str(scripts[codec])])
    _cancel_when([script.with_suffix(".pid") for script in scripts.values()], event)
    with pytest.raises(ProbeCancelledError):
        detector._probe_codecs("ffmpeg", set(scripts))
    assert all(_process_gone(int(script.with_suffix(".pid").read_text())) for script in scripts.values())


@_LINUX
def test_nvencc_and_gpu_list_probes_are_killed_with_a_token(tmp_path, monkeypatch):
    import threading

    from core.subprocess_utils import ProbeCancelledError
    from core.workflows.encode import interpolation
    from core.workflows.encode.runtime.nvencc import detect_nvencc_available

    nvencc = _pid_script(tmp_path / "nvencc", "*--check-hw*")
    event = threading.Event()
    _cancel_when([nvencc.with_suffix(".pid")], event)
    with pytest.raises(ProbeCancelledError):
        detect_nvencc_available(str(nvencc), cancel_event=event)
    assert _process_gone(int(nvencc.with_suffix(".pid").read_text()))
    rife = _pid_script(tmp_path / "rife", "*--list-gpus*")
    monkeypatch.setattr(interpolation, "_resolve_rife", lambda _bin: str(rife))
    monkeypatch.setattr(interpolation, "rife_capabilities",
                        lambda *_a, **_k: SimpleNamespace(label="9.9.9", supports=lambda option: option == "nvof"))
    event = threading.Event()
    _cancel_when([rife.with_suffix(".pid")], event)
    with pytest.raises(ProbeCancelledError):
        interpolation._probe_acceleration(str(rife), "", -1, 60.0, event)
    assert _process_gone(int(rife.with_suffix(".pid").read_text()))


@_LINUX
@pytest.mark.parametrize("section", ["rife", "encoders"])
def test_no_capabilities_probe_survives_the_host_process(tmp_path, section):
    """Hôte Python terminé juste après l'annulation : sonde RIFE, ou sondes matérielles parallèles, jamais laissées
    orphelines (processus et descendants)."""
    import subprocess
    import sys

    scripts = ([_pid_script(tmp_path / "rife", "*--version*")] if section == "rife" else
               [_pid_script(tmp_path / f"{codec}.sh", "*", "") for codec in ("hevc_vaapi", "h264_vaapi")])
    pids = [script.with_suffix(".pid") for script in scripts]
    patch = "" if section == "rife" else f"""
from core.workflows.encode.hardware import HardwareEncoderDetector
scripts = {{"hevc_vaapi": {str(scripts[0])!r}, "h264_vaapi": {str(scripts[1])!r}}}
HardwareEncoderDetector._compiled_hw = lambda self, ffmpeg: (ffmpeg, set(scripts))
HardwareEncoderDetector._probe_command = lambda self, ffmpeg, codec: [scripts[codec]]
HardwareEncoderDetector._probe_vaapi_rate_controls = lambda self, ffmpeg, codec: frozenset()
HardwareEncoderDetector._get_encoders_output = lambda self, ffmpeg: ""
"""
    host = tmp_path / "hote.py"
    host.write_text(f"""
import threading, time
from pathlib import Path
from types import SimpleNamespace
from core.service import ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
{patch}
ctx = ExecutionContext.create(SimpleNamespace(tool_muxiveo_rife={str(scripts[0])!r}, tool_ffmpeg="ffmpeg",
                                              tool_nvencc=""), ListSink())
def cancel():
    while not all(Path(p).exists() and Path(p).read_text().strip() for p in {[str(p) for p in pids]!r}):
        time.sleep(0.01)
    ctx.cancel.cancel()
threading.Thread(target=cancel, daemon=True).start()
try:
    call(CATALOG, "system.capabilities", {{"sections": [{section!r}]}}, ctx)
except Exception:
    pass
""", encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, str(host)], cwd=root, check=True, timeout=60,
                   env={**__import__("os").environ, "PYTHONPATH": str(root)})
    assert all(_process_gone(int(pid.read_text())) for pid in pids)


def test_windows_path_conventions_stay_untouched_in_requests(tmp_path):
    from core.json_documents import resolve_output_reference, resolve_profile_reference

    for value in ("C:\\\\jobs\\\\film.json", "\\\\jobs\\\\film.json", "\\\\\\\\srv\\\\partage\\\\film.json", "/jobs/film.json"):
        assert resolve_profile_reference(value, tmp_path) == value
    for value in ("C:\\\\sorties\\\\", "\\\\sorties\\\\", "\\\\\\\\srv\\\\partage\\\\o.mkv"):
        assert resolve_output_reference(value, tmp_path) == value


@pytest.mark.parametrize("analyser", ["late", "raises"])
def test_calibration_cadence_resolution_is_cancellable_and_writes_nothing(monkeypatch, tmp_path, analyser):
    from core.service.errors import ApiError, ErrorCode
    from core.service.events import EventType
    from core.workflows import audio_sync_scan
    from core.workflows.sync_calibration import SyncCalibration

    sink = ListSink()
    ctx = ExecutionContext.create(SimpleNamespace(), sink)
    seen: list[Any] = []

    class _Scanner:
        def __init__(self, *_a, cancel_event=None, **_k):
            seen.append(cancel_event)

        def resolve_cadence_method(self, _ref, _tgt, calibration, log=None):
            ctx.cancel.cancel()  # annulation pendant l'analyse
            if analyser == "raises":  # extraction interrompue par le jeton (erreur non typée d'annulation)
                raise audio_sync_scan.AudioSyncError("Analyse annulée.")
            return calibration  # analyseur qui rend quand même un résultat

    monkeypatch.setattr(audio_sync_scan, "AudioSyncScanner", _Scanner)
    output = tmp_path / "calibration.json"
    output.write_text('{"existant": true}', encoding="utf-8")
    with pytest.raises(Exception) as caught:
        call(CATALOG, "calibration.edit", {
            "calibration": {"value": SyncCalibration.linear(0).to_dict()}, "output": str(output), "force": True,
            "resolve_cadence": [{"path": str(tmp_path / "ref.mkv"), "stream": 1},
                                {"path": str(tmp_path / "tgt.mkv"), "stream": 1}]}, ctx)
    assert ApiError.from_exception(caught.value).code is ErrorCode.CANCELLED
    assert seen == [ctx.cancel.event]
    assert output.read_text(encoding="utf-8") == '{"existant": true}'  # jamais écrasé
    assert not any(e.type in (EventType.ARTIFACT, EventType.RESULT) for e in sink.events)
    assert "succeeded" not in [e.data.get("state") for e in sink.events if e.type is EventType.STATE]


@pytest.mark.parametrize("fallback", ["/usr/bin/ffmpeg-systeme", None])
def test_capabilities_presets_use_the_ffmpeg_that_detected_the_hardware_encoders(monkeypatch, fallback):
    from core.workflows.encode.hardware import HardwareEncoderDetector

    configured = "/opt/app/ffmpeg"
    calls: list[tuple[str, frozenset[str]]] = []
    presets_by_binary = {configured: {"libx264": frozenset({"slow"}), "hevc_nvenc": frozenset({"faux"})},
                         "/usr/bin/ffmpeg-systeme": {"hevc_nvenc": frozenset({"p4"})}}

    def detect_presets(self, ffmpeg, codecs):  # accumule comme le vrai détecteur (``self.presets``)
        calls.append((ffmpeg, frozenset(codecs)))
        self.presets.update({codec: presets_by_binary[ffmpeg][codec] for codec in codecs})
        return dict(self.presets)

    monkeypatch.setattr(HardwareEncoderDetector, "detect_software", lambda self, ffmpeg: {"libx264"})
    monkeypatch.setattr(HardwareEncoderDetector, "detect",
                        lambda self, ffmpeg, nvencc_bin=None: ({"hevc_nvenc"}, fallback or ffmpeg))
    monkeypatch.setattr(HardwareEncoderDetector, "detect_presets", detect_presets)
    result = call(CATALOG, "system.capabilities", {"sections": ["presets"]}, _ctx(tool_ffmpeg=configured))
    hw_binary = fallback or configured
    assert calls == [(configured, frozenset({"libx264"})), (hw_binary, frozenset({"hevc_nvenc"}))]
    expected_nvenc = ["p4"] if fallback else ["faux"]
    assert result.presets == {"hevc_nvenc": expected_nvenc, "libx264": ["slow"]}  # logiciels conservés


def test_gui_encoder_listing_keeps_no_timeout(monkeypatch):
    import subprocess

    from core.workflows.encode.hardware import HardwareEncoderDetector

    seen: list[Any] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: seen.append(kwargs.get("timeout", "absent")) or
                        subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    HardwareEncoderDetector()._get_encoders_output("ffmpeg")  # interface : sans jeton
    assert seen == ["absent"]  # appel historique exact : aucune clé ``timeout``


def test_headless_encoder_listing_is_bounded_and_a_cancelled_listing_is_never_cached(monkeypatch):
    import threading

    from core import subprocess_utils
    from core.subprocess_utils import ProbeCancelledError
    from core.workflows.encode.hardware import HardwareEncoderDetector

    seen: list[float] = []
    event = threading.Event()

    def probe(cmd, *, timeout, cancel_event, **_k):
        seen.append(timeout)
        if cancel_event.is_set():
            raise ProbeCancelledError("Sonde annulée.")
        return subprocess_utils.subprocess.CompletedProcess(cmd, 0, stdout=" V..... libx264", stderr="")

    monkeypatch.setattr(subprocess_utils, "run_probe", probe)
    detector = HardwareEncoderDetector(cancel_event=event)
    event.set()
    with pytest.raises(ProbeCancelledError):
        detector._get_encoders_output("ffmpeg")
    event.clear()
    assert "libx264" in detector._get_encoders_output("ffmpeg")  # rien de mémorisé après l'annulation
    assert seen == [120.0, 120.0]  # opération headless : bornée
