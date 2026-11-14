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
    monkeypatch.setattr(profile_ops, "preview_encode", lambda config, wf: "nvencc …")
    request = profile_ops.ProfilePreviewRequest.from_dict({"profile": "P", "inputs": ["/a.mkv"],
                                                           "encode": {"video": {}}})
    assert not profile_ops.profile_preview.handler(request, _ctx()).valid
    accepted = profile_ops.ProfilePreviewRequest.from_dict({"profile": "P", "inputs": ["/a.mkv"],
                                                            "encode": {"video": {}, "accept_dovi_nvenc_crop": True}})
    assert profile_ops.profile_preview.handler(accepted, _ctx()).command_text == "nvencc …"
