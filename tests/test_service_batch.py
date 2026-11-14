"""Lots (media.batch, profile.batch) : jobs enfants, résultats partiels, annulation ; opérations profile.*."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.interrupts import interrupt_guard
from core.service import ApiError, CancelToken, ErrorCode, EventType, ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.events import JobState
from core.service.ops import batch as batch_ops
from core.service.ops import profile as profile_ops


def _ctx(cancel: CancelToken | None = None, **config: Any) -> tuple[ExecutionContext, ListSink]:
    sink = ListSink()
    return ExecutionContext.create(SimpleNamespace(**config), sink, cancel=cancel), sink


def _template(tmp_path: Path, items: list[dict]) -> dict[str, Any]:
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"version": 1, "kind": "exact-job"}), encoding="utf-8")
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps({"jobs": items}), encoding="utf-8")
    return {"template": str(template), "batch": str(batch)}


@pytest.fixture
def doubles(monkeypatch):
    """Construction et exécution remplacées ; ``plan`` : comportement de chaque sortie."""
    plan: dict[str, str] = {}
    ran: list[str] = []

    def build(job, _config, _options, _logger, **_kwargs):  # context : préparation annulable
        return SimpleNamespace(output=Path(str(job["output"])).absolute(), allow_missing_output_dir=False)

    def run_remux(remux_config, *, config, options, logger, cancel, force):
        name = remux_config.output.name
        ran.append(name)
        behaviour = plan.get(name, "ok")
        if behaviour == "fail":
            logger.emit("error", "échec")
            raise ApiError(ErrorCode.WORKFLOW_FAILED, "échec", logged=True)
        if behaviour == "cancel":
            cancel.cancel()
            raise ApiError(ErrorCode.CANCELLED, "Opération annulée.")
        if behaviour == "ok_then_cancel":
            cancel.cancel()
        return "fini"

    monkeypatch.setattr(batch_ops, "build_remux_config", build)
    monkeypatch.setattr(batch_ops, "run_remux", run_remux)
    return SimpleNamespace(plan=plan, ran=ran)


def _items(tmp_path: Path, *names: str) -> list[dict]:
    return [{"sources": [str(tmp_path / f"{n}.mkv")], "output": str(tmp_path / "out" / f"{n}.mkv")} for n in names]


def _parent_states(sink, ctx) -> list[str]:
    return [e.data["state"] for e in sink.events if e.job == ctx.events.job and e.type is EventType.STATE]


def test_batch_children_have_their_own_jobs_and_states(tmp_path, doubles):
    ctx, sink = _ctx()
    result = call(CATALOG, "media.batch", _template(tmp_path, _items(tmp_path, "a", "b")), ctx)
    assert result.status == "succeeded" and (result.total, result.successes, result.failures) == (2, 2, 0)
    assert _parent_states(sink, ctx) == [JobState.RUNNING.value, JobState.SUCCEEDED.value]
    children = {e.job for e in sink.events if e.parent == ctx.events.job}
    assert len(children) == 2
    for child in children:
        states = [e.data["state"] for e in sink.events if e.job == child and e.type is EventType.STATE]
        assert states == [JobState.RUNNING.value, JobState.SUCCEEDED.value]
        assert any(e.type is EventType.ARTIFACT for e in sink.events if e.job == child)


def test_batch_partial_when_some_jobs_fail(tmp_path, doubles):
    doubles.plan["b.mkv"] = "fail"
    ctx, sink = _ctx()
    payload = {**_template(tmp_path, _items(tmp_path, "a", "b", "c")), "continue_on_error": True}
    result = call(CATALOG, "media.batch", payload, ctx)
    assert result.status == "partial" and (result.successes, result.failures) == (2, 1)
    assert _parent_states(sink, ctx)[-1] == JobState.PARTIAL.value
    assert [job["status"] for job in result.jobs] == ["success", "failed", "success"]


def test_batch_failed_when_every_job_fails(tmp_path, doubles):
    doubles.plan.update({"a.mkv": "fail"})
    ctx, sink = _ctx()
    assert call(CATALOG, "media.batch", _template(tmp_path, _items(tmp_path, "a")), ctx).status == "failed"
    assert _parent_states(sink, ctx)[-1] == JobState.FAILED.value


def test_cancel_during_a_job_stops_the_batch(tmp_path, doubles):
    doubles.plan["b.mkv"] = "cancel"
    ctx, sink = _ctx()
    payload = {**_template(tmp_path, _items(tmp_path, "a", "b", "c")), "continue_on_error": True}
    result = call(CATALOG, "media.batch", payload, ctx)
    assert doubles.ran == ["a.mkv", "b.mkv"]
    assert result.status == "partial" and [job["status"] for job in result.jobs] == ["success", "cancelled"]
    cancelled = [e for e in sink.events if e.type is EventType.STATE and e.data["state"] == JobState.CANCELLED.value]
    assert len(cancelled) == 1 and cancelled[0].parent == ctx.events.job
    assert _parent_states(sink, ctx)[-1] == JobState.PARTIAL.value  # un job publié avant l'annulation


def test_cancel_after_a_published_job_keeps_it_and_skips_the_rest(tmp_path, doubles):
    doubles.plan["a.mkv"] = "ok_then_cancel"
    ctx, _ = _ctx()
    result = call(CATALOG, "media.batch", _template(tmp_path, _items(tmp_path, "a", "b")), ctx)
    assert doubles.ran == ["a.mkv"] and result.status == "partial" and result.successes == 1


def test_batch_cancelled_before_any_success(tmp_path, doubles):
    doubles.plan["a.mkv"] = "cancel"
    ctx, _ = _ctx()
    assert call(CATALOG, "media.batch", _template(tmp_path, _items(tmp_path, "a", "b")), ctx).status == "cancelled"


def test_batch_collisions_are_refused_before_any_job(tmp_path, doubles):
    items = _items(tmp_path, "a") * 2
    ctx, _ = _ctx()
    with pytest.raises(ApiError):
        call(CATALOG, "media.batch", _template(tmp_path, items), ctx)
    assert doubles.ran == []


def test_batch_request_is_strict(tmp_path):
    ctx, _ = _ctx()
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "media.batch", {"template": str(tmp_path / "t.json"), "sources": {"input": []}}, ctx)
    assert {d.path for d in caught.value.details} == {"sources.input"}


# --- profile.* ---------------------------------------------------------------------------

def _profile(tmp_path: Path) -> Path:
    path = tmp_path / "profil.json"
    path.write_text(json.dumps({"kind": "decision-profile", "version": 1, "name": "Test", "rules": [
        {"id": "keep", "scope": "all", "match": {"all": []}, "actions": [{"type": "set_enabled", "value": True}]},
    ]}), encoding="utf-8")
    return path


def test_profile_validate_operation(tmp_path):
    ctx, _ = _ctx(profiles_dir=tmp_path / "profiles")
    result = call(CATALOG, "profile.validate", {"profile": str(_profile(tmp_path))}, ctx)
    assert result.to_dict() == {"valid": True, "kind": "decision-profile", "version": 1, "name": "Test", "rules": 1}


def test_profile_preview_operation_returns_tracks_and_commands(tmp_path, monkeypatch):
    track = SimpleNamespace()
    remux_config = SimpleNamespace(sources=[SimpleNamespace(tracks=[track])], output=tmp_path / "o.mkv")
    seen: dict[str, Any] = {}

    def build(profile, **kwargs):
        seen.update(kwargs)
        return remux_config, {"rules": ["keep"]}

    class _Wf:
        def validate(self, _c):
            return []

        def build_command(self, _c):
            return ["ffmpeg"]

        def preview_command(self, _c):
            return "ffmpeg"

        def execution_preview(self, _c):
            return {"selected_backend": "native"}

    monkeypatch.setattr(profile_ops, "build_profile_remux_config", build)
    monkeypatch.setattr(profile_ops, "remux_workflow", lambda config, options: _Wf())
    monkeypatch.setattr(profile_ops, "serialize_track_preview", lambda t: {"id": "t"})
    monkeypatch.setattr(profile_ops, "serialize_remux_config", lambda c: {"output": str(c.output)})
    ctx, _ = _ctx(profiles_dir=tmp_path / "profiles")
    result = call(CATALOG, "profile.preview", {"profile": str(_profile(tmp_path)), "inputs": ["a.mkv"],
                                               "metadata": {"output_template": "{title}"}}, ctx, base_dir=tmp_path)
    assert result.valid and result.command_text == "ffmpeg" and result.tracks == ({"id": "t"},)
    assert result.profile_report == {"rules": ["keep"]}
    assert seen["cli_inputs"] == [str(tmp_path / "a.mkv")] and seen["preview"] is True
    assert seen["metadata_job"] == {"output_template": "{title}"}


# --- Ctrl+C : gardes imbriquées --------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="signal POSIX envoyé au processus")
def test_nested_interrupt_guards_cancel_job_and_batch():
    with interrupt_guard(CancelToken()) as batch:
        with interrupt_guard(CancelToken()) as job:
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
        assert job.cancelled and batch.cancelled


def test_cancel_during_preparation_cancels_the_batch(tmp_path, doubles, monkeypatch):
    from cli.service_adapter import envelope, exit_code_for_result

    ctx, sink = _ctx()

    def build(job, _config, _options, _logger, *, context=None):
        assert context is not None and context.cancel_event is ctx.cancel.event  # préparation annulable
        ctx.cancel.cancel()  # Ctrl+C pendant l'inspection du premier job
        raise ApiError(ErrorCode.CANCELLED, "inspection annulée")

    monkeypatch.setattr(batch_ops, "build_remux_config", build)
    payload = {**_template(tmp_path, _items(tmp_path, "a", "b")), "continue_on_error": True}
    result = call(CATALOG, "media.batch", payload, ctx)
    assert doubles.ran == [] and result.status == "cancelled" and result.total == 0
    assert _parent_states(sink, ctx)[-1] == JobState.CANCELLED.value
    assert envelope("media.batch", ctx.events.job, result=result)["status"] == "cancelled"
    assert exit_code_for_result(result) == 7


def test_historic_batch_cancelled_before_any_job_exits_7(tmp_path, doubles, monkeypatch):
    from cli import batch as cli_batch
    from core.jobs.options import CommonOptions

    cancel = CancelToken()
    cancel.cancel()  # Ctrl+C reçu pendant la découverte
    template = _template(tmp_path, _items(tmp_path, "a"))
    logger = SimpleNamespace(emit=lambda *a, **k: None)
    outcome = cli_batch._run_batch(template_path=template["template"], batch_path=template["batch"],
                                   cli_inputs=[], input_dirs=[], recursive=False, include_patterns=[],
                                   exclude_patterns=[], output_dir=None, dry_run=False, force=False,
                                   continue_on_error=False, summary_path=None,
                                   config=SimpleNamespace(), options=CommonOptions(), logger=logger,
                                   steps=cli_batch.cli_batch_steps(), cancel=cancel)
    assert outcome.cancelled and outcome.exit_code == 7 and doubles.ran == []


def test_cancel_during_an_encode_preview_cancels_child_and_batch(tmp_path, doubles, monkeypatch):
    from core.subprocess_utils import ProbeCancelledError

    ctx, sink = _ctx()
    monkeypatch.setattr(batch_ops, "resolve_from_job", lambda *a, **k: SimpleNamespace(config=SimpleNamespace()))
    monkeypatch.setattr(batch_ops, "encode_workflow", lambda *a: None)

    def checked(*_a, **_k):
        ctx.cancel.cancel()  # Ctrl+C pendant la sonde de géométrie
        raise ProbeCancelledError("Sonde L5 annulée.")

    monkeypatch.setattr(batch_ops, "checked_encode", checked)
    items = [{**item, "encode": {"video": {"default": {"codec": "libx265"}}}} for item in _items(tmp_path, "a", "b")]
    payload = {**_template(tmp_path, items), "dry_run": True, "continue_on_error": True}
    result = call(CATALOG, "media.batch", payload, ctx)
    assert result.status == "cancelled" and [job["status"] for job in result.jobs] == ["cancelled"]
    assert _parent_states(sink, ctx)[-1] == JobState.CANCELLED.value
    child = next(e.job for e in sink.events if e.parent == ctx.events.job)
    states = [e.data["state"] for e in sink.events if e.job == child and e.type is EventType.STATE]
    assert states == [JobState.RUNNING.value, JobState.CANCELLED.value]


def test_cancellation_raised_by_a_step_stops_the_batch_even_without_the_token(tmp_path, doubles, monkeypatch):
    from core.subprocess_utils import ProbeCancelledError

    ctx, sink = _ctx()
    monkeypatch.setattr(batch_ops, "resolve_from_job", lambda *a, **k: SimpleNamespace(config=SimpleNamespace()))
    monkeypatch.setattr(batch_ops, "encode_workflow", lambda *a: None)
    calls: list[int] = []

    def checked(*_a, **_k):  # annulation levée par une sonde, jeton du lot non levé
        calls.append(1)
        raise ProbeCancelledError("Sonde annulée.")

    monkeypatch.setattr(batch_ops, "checked_encode", checked)
    items = [{**item, "encode": {"video": {"default": {"codec": "libx265"}}}} for item in _items(tmp_path, "a", "b")]
    result = call(CATALOG, "media.batch", {**_template(tmp_path, items), "dry_run": True, "continue_on_error": True},
                  ctx)
    assert calls == [1] and result.status == "cancelled" and [job["status"] for job in result.jobs] == ["cancelled"]
    assert _parent_states(sink, ctx)[-1] == JobState.CANCELLED.value


def test_every_child_gets_a_final_state_after_a_preparation_error(tmp_path, doubles, monkeypatch):
    ctx, sink = _ctx()

    def build(job, _config, _options, _logger, **_kwargs):
        if str(job["output"]).endswith("a.mkv"):
            raise ApiError(ErrorCode.VALIDATION, "source illisible")
        return SimpleNamespace(output=Path(str(job["output"])).absolute(), allow_missing_output_dir=False)

    monkeypatch.setattr(batch_ops, "build_remux_config", build)
    result = call(CATALOG, "media.batch", {**_template(tmp_path, _items(tmp_path, "a", "b")), "continue_on_error": True},
                  ctx)
    assert result.status == "partial"
    children = [e.job for e in sink.events if e.parent == ctx.events.job and e.type is EventType.STATE
                and e.data["state"] == JobState.RUNNING.value]
    finals = {e.job: e.data["state"] for e in sink.events if e.job in children and e.type is EventType.STATE
              and e.data["state"] != JobState.RUNNING.value}
    assert sorted(finals.values()) == [JobState.FAILED.value, JobState.SUCCEEDED.value]


def test_keyboard_interrupt_in_a_step_cancels_the_child():
    from core.service.ops.batch import _Children

    ctx, sink = _ctx()
    children = _Children(ctx)
    logger = children.logger(0)

    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        children._guarded(logger, interrupted)
    states = [e.data["state"] for e in sink.events if e.job == logger.events.job and e.type is EventType.STATE]
    assert states == [JobState.RUNNING.value, JobState.CANCELLED.value] and ctx.cancel.cancelled


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("continue_on_error", [False, True])
def test_a_failed_job_stops_the_batch_unless_continue_on_error(tmp_path, doubles, monkeypatch, dry_run,
                                                                continue_on_error):
    ctx, sink = _ctx()
    if dry_run:  # aperçu invalide (code 3) pour b
        monkeypatch.setattr(batch_ops, "remux_workflow", lambda config, options: SimpleNamespace(
            validate=lambda remux: ["refus"] if remux.output.name == "b.mkv" else [],
            preview_command=lambda remux: "ffmpeg", execution_preview=lambda remux: {}))
    else:
        doubles.plan["b.mkv"] = "fail"
    payload = {**_template(tmp_path, _items(tmp_path, "a", "b", "c")), "dry_run": dry_run,
               "continue_on_error": continue_on_error}
    result = call(CATALOG, "media.batch", payload, ctx)
    statuses = [job["status"] for job in result.jobs]
    if continue_on_error:
        assert statuses == ["success", "failed", "success"] and result.status == "partial"
    else:
        assert statuses == ["success", "failed"] and result.status == "partial"  # c jamais lancé
        if not dry_run:
            assert doubles.ran == ["a.mkv", "b.mkv"]
    assert (result.successes, result.failures) == (statuses.count("success"), 1)
    children = {e.job for e in sink.events if e.parent == ctx.events.job}
    assert len(children) == len(statuses)  # aucun job enfant ouvert pour c sans continue_on_error


@pytest.mark.parametrize("continue_on_error", [False, True])
def test_profile_batch_and_historic_batch_stop_after_a_failed_job(tmp_path, monkeypatch, continue_on_error):
    from cli import batch as cli_batch
    from core.jobs.batch import BatchDiscovery
    from core.jobs.options import CommonOptions
    from core.jobs.profile import ProfileBatchSteps, run_profile_batch

    ran: list[str] = []

    def run(_config, _options, _logger, remux_config, *, force=False):
        ran.append(remux_config.output.name)
        return 6 if remux_config.output.name == "b.mkv" else 0  # échec par code retour, sans exception

    jobs = [{"sources": [str(tmp_path / f"{n}.mkv")], "output": str(tmp_path / f"{n}.mkv")} for n in "abc"]
    steps = ProfileBatchSteps(
        load=lambda path, config: {}, discover=lambda **k: BatchDiscovery(jobs=jobs, scanned=3, selected=3, explicit_inputs=0, roots=[]),
        build=lambda profile, **k: (SimpleNamespace(output=Path(k["cli_output"])), {}),
        preview=lambda *a: 0, run=run)
    outcome = run_profile_batch("P", cli_inputs=[], input_dirs=None, recursive=False, include_patterns=None,
                                exclude_patterns=None, output_dir=str(tmp_path), dry_run=False, force=False,
                                continue_on_error=continue_on_error, summary_path=None, config=SimpleNamespace(),
                                options=CommonOptions(), logger=SimpleNamespace(emit=lambda *a, **k: None),
                                steps=steps)
    assert ran == (["a.mkv", "b.mkv", "c.mkv"] if continue_on_error else ["a.mkv", "b.mkv"])
    assert outcome.exit_code == 7 and outcome.summary["failures"] == 1
    # Commande `batch` historique : même règle.
    ran.clear()
    monkeypatch.setattr(cli_batch, "build_remux_config",
                        lambda job, config, options, logger: SimpleNamespace(output=Path(job["output"]),
                                                                            allow_missing_output_dir=False))
    monkeypatch.setattr(cli_batch, "run_remux_config", run)
    template = tmp_path / "t.json"
    template.write_text(json.dumps({"version": 1}), encoding="utf-8")
    batch = tmp_path / "b.json"
    batch.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    rc = cli_batch.run_batch(template_path=str(template), batch_path=str(batch), cli_inputs=None, input_dirs=None,
                             recursive=False, include_patterns=None, exclude_patterns=None, output_dir=None,
                             dry_run=False, force=False, continue_on_error=continue_on_error, summary_path=None,
                             config=None, options=None, logger=SimpleNamespace(emit=lambda *a, **k: None))
    assert rc == 7 and ran == (["a.mkv", "b.mkv", "c.mkv"] if continue_on_error else ["a.mkv", "b.mkv"])


@pytest.mark.parametrize("failing", ["a.mkv", "b.mkv"])
@pytest.mark.parametrize("continue_on_error", [False, True])
def test_encode_jobs_failing_in_the_runtime_follow_the_stop_rule(tmp_path, doubles, monkeypatch, failing,
                                                                  continue_on_error):
    ctx, sink = _ctx()
    ran: list[str] = []
    monkeypatch.setattr(batch_ops, "resolve_from_job",
                        lambda block, remux, **k: SimpleNamespace(config=SimpleNamespace(output=remux.output)))

    def run_encode(encode_config, **_kwargs):  # runtime : lève une erreur (jamais un code)
        ran.append(encode_config.output.name)
        if encode_config.output.name == failing:
            raise ApiError(ErrorCode.WORKFLOW_FAILED, "x265 KO", logged=True)
        return "fini"

    monkeypatch.setattr(batch_ops, "run_encode", run_encode)
    items = [{**item, "encode": {"video": {"default": {"codec": "libx265"}}}} for item in _items(tmp_path, "a", "b", "c")]
    result = call(CATALOG, "media.batch", {**_template(tmp_path, items), "continue_on_error": continue_on_error}, ctx)
    names = ["a.mkv", "b.mkv", "c.mkv"]
    expected = names if continue_on_error else names[:names.index(failing) + 1]
    assert ran == expected and result.failures == 1
    assert result.status == ("failed" if expected == ["a.mkv"] else "partial")
    assert [job["status"] for job in result.jobs] == ["failed" if name == failing else "success" for name in expected]
    finals: dict[str, list[str]] = {}
    for event in sink.events:
        if event.parent == ctx.events.job and event.type is EventType.STATE and event.data["state"] != "running":
            finals.setdefault(event.job, []).append(event.data["state"])
    assert all(len(states) == 1 for states in finals.values())  # un seul état final par enfant
    assert len(finals) == len(expected)


@pytest.mark.parametrize("continue_on_error", [False, True])
def test_historic_encode_steps_returning_a_failure_code_and_profile_previews_stop(tmp_path, monkeypatch,
                                                                                   continue_on_error):
    from cli import batch as cli_batch
    from core.jobs.batch import BatchDiscovery
    from core.jobs.options import CommonOptions
    from core.jobs.profile import ProfileBatchSteps, run_profile_batch

    jobs = [{"sources": [str(tmp_path / f"{n}.mkv")], "output": str(tmp_path / f"{n}.mkv"),
             "encode": {"video": {"default": {"codec": "libx265"}}}} for n in "abc"]
    ran: list[str] = []
    monkeypatch.setattr(cli_batch, "build_remux_config", lambda job, config, options, logger: SimpleNamespace(
        output=Path(job["output"]), allow_missing_output_dir=False))
    monkeypatch.setattr(cli_batch, "resolve_job_encode_config", lambda job, remux, *a, **k: SimpleNamespace(
        output=remux.output))

    def run_encode_config(_config, _options, _logger, encode_config, **_kwargs):  # callback : rend un code
        ran.append(encode_config.output.name)
        return 6 if encode_config.output.name == "a.mkv" else 0

    monkeypatch.setattr(cli_batch, "run_encode_config", run_encode_config)
    template = tmp_path / "t.json"
    template.write_text(json.dumps({"version": 1}), encoding="utf-8")
    batch = tmp_path / "b.json"
    batch.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    rc = cli_batch.run_batch(template_path=str(template), batch_path=str(batch), cli_inputs=None, input_dirs=None,
                             recursive=False, include_patterns=None, exclude_patterns=None, output_dir=None,
                             dry_run=False, force=False, continue_on_error=continue_on_error, summary_path=None,
                             config=None, options=None, logger=SimpleNamespace(emit=lambda *a, **k: None))
    assert rc == 7 and ran == (["a.mkv", "b.mkv", "c.mkv"] if continue_on_error else ["a.mkv"])
    # Aperçu d'un lot de profils : premier aperçu refusé (code 3).
    previews: list[str] = []
    steps = ProfileBatchSteps(
        load=lambda path, config: {}, discover=lambda **k: BatchDiscovery(jobs=jobs, scanned=3, selected=3,
                                                                          explicit_inputs=0, roots=[]),
        build=lambda profile, **k: (SimpleNamespace(output=Path(k["cli_output"])), {}),
        preview=lambda _c, _o, _l, remux: previews.append(remux.output.name) or (3 if remux.output.name == "a.mkv" else 0),
        run=lambda *a, **k: 0)
    outcome = run_profile_batch("P", cli_inputs=[], input_dirs=None, recursive=False, include_patterns=None,
                                exclude_patterns=None, output_dir=str(tmp_path), dry_run=True, force=False,
                                continue_on_error=continue_on_error, summary_path=None, config=SimpleNamespace(),
                                options=CommonOptions(), logger=SimpleNamespace(emit=lambda *a, **k: None),
                                steps=steps)
    assert previews == (["a.mkv", "b.mkv", "c.mkv"] if continue_on_error else ["a.mkv"])
    assert outcome.exit_code == 7 and outcome.summary["failures"] == 1


@pytest.mark.parametrize("failure", ["validation", "exception"])
def test_batch_job_failing_after_the_token_is_raised_is_cancelled_everywhere(tmp_path, doubles, monkeypatch, failure):
    """Audit 13 : issue non réussie avec jeton levé → job, enfant et lot annulés (plus d'enfant « failed »)."""
    ctx, sink = _ctx()
    monkeypatch.setattr(batch_ops, "resolve_from_job",
                        lambda block, remux, **k: SimpleNamespace(config=SimpleNamespace(output=remux.output)))
    monkeypatch.setattr(batch_ops, "encode_workflow", lambda *a: SimpleNamespace())

    def checked(encode_config, *_a, **_k):
        ctx.cancel.cancel()  # jeton levé seul, sans exception typée d'annulation
        if failure == "exception":
            raise RuntimeError("sonde interrompue")
        return encode_config, ["géométrie indisponible"]

    monkeypatch.setattr(batch_ops, "checked_encode", checked)
    items = [{**item, "encode": {"video": {"default": {"codec": "libx265"}}}} for item in _items(tmp_path, "a", "b")]
    try:
        result = call(CATALOG, "media.batch", {**_template(tmp_path, items), "dry_run": True}, ctx)
        status = result.status
    except ApiError as error:
        status = error.code.value
    assert status in ("cancelled", ErrorCode.CANCELLED.value)
    finals = [e.data["state"] for e in sink.events if e.parent == ctx.events.job and e.type is EventType.STATE
              and e.data["state"] != JobState.RUNNING.value]
    assert finals == [JobState.CANCELLED.value]  # un seul enfant, annulé ; le suivant jamais lancé


_CASES = [(kind, failure) for kind in ("remux-run", "remux-preview", "encode-run", "encode-preview")
          for failure in ("api", "exception", "validation") if failure != "validation" or kind.endswith("preview")]


@pytest.mark.parametrize("kind, failure", _CASES)
def test_batch_success_then_failure_after_the_token_is_partial_and_stops(tmp_path, doubles, monkeypatch, kind,
                                                                           failure):
    """Audit 13 : 1er job réussi, 2e en échec après la levée du jeton → parent partiel, 2e annulé partout, 3e
    jamais lancé, même avec --continue-on-error ; une réussite déjà publiée reste réussie."""
    ctx, sink = _ctx()
    calls: list[str] = []

    def outcome(value: Any = None) -> Any:
        calls.append("x")
        if len(calls) < 2:
            return value
        ctx.cancel.cancel()
        if failure == "api":
            raise ApiError(ErrorCode.WORKFLOW_FAILED, "échec après annulation")
        if failure == "exception":
            raise RuntimeError("échec après annulation")
        return ["géométrie indisponible"]  # validation en erreur (aperçu)

    if kind.startswith("remux"):
        if kind == "remux-run":
            monkeypatch.setattr(batch_ops, "run_remux", lambda *a, **k: outcome("fini"))
        else:
            monkeypatch.setattr(batch_ops, "remux_workflow", lambda *a: SimpleNamespace(
                validate=lambda config: outcome([]), preview_command=lambda config: "ffmpeg …"))
        items = _items(tmp_path, "a", "b", "c")
    else:
        monkeypatch.setattr(batch_ops, "resolve_from_job",
                            lambda block, remux, **k: SimpleNamespace(config=SimpleNamespace(output=remux.output)))
        monkeypatch.setattr(batch_ops, "encode_workflow", lambda *a: SimpleNamespace())
        if kind == "encode-run":
            monkeypatch.setattr(batch_ops, "run_encode", lambda *a, **k: outcome("fini"))
        else:
            monkeypatch.setattr(batch_ops, "checked_encode", lambda cfg, *a, **k: (cfg, outcome([])))
            monkeypatch.setattr(batch_ops, "preview_encode", lambda *a: "ffmpeg …")
        items = [{**item, "encode": {"video": {"default": {"codec": "libx265"}}}}
                 for item in _items(tmp_path, "a", "b", "c")]
    request = {**_template(tmp_path, items), "continue_on_error": True, "dry_run": kind.endswith("preview")}
    result = call(CATALOG, "media.batch", request, ctx)
    assert result.status == "partial" and len(calls) == 2
    assert [job["status"] for job in result.jobs] == ["success", "cancelled"]
    finals: dict[str, list[str]] = {}
    for event in sink.events:
        if event.parent == ctx.events.job and event.type is EventType.STATE and event.data["state"] != "running":
            finals.setdefault(event.job, []).append(event.data["state"])
    assert sorted(finals.values()) == [[JobState.CANCELLED.value], [JobState.SUCCEEDED.value]]


def test_profile_batch_failure_after_the_token_is_cancelled_for_the_child(tmp_path, doubles, monkeypatch):
    from core.jobs.batch import BatchDiscovery

    ctx, sink = _ctx()
    jobs = [{"sources": [str(tmp_path / f"{n}.mkv")], "output": str(tmp_path / f"{n}.mkv")} for n in "ab"]
    monkeypatch.setattr(batch_ops, "load_decision_profile", lambda path, config: {})
    monkeypatch.setattr(batch_ops, "discover_direct_batch_jobs", lambda **k: BatchDiscovery(
        jobs=jobs, scanned=2, selected=2, explicit_inputs=0, roots=[]))
    monkeypatch.setattr(batch_ops, "build_profile_remux_config", lambda profile, **k: (SimpleNamespace(
        output=Path(k["cli_output"]), allow_missing_output_dir=False), {}))
    done: list[str] = []

    def run_remux(remux_config, *, cancel, **_k):
        done.append(remux_config.output.name)
        if len(done) == 2:
            cancel.cancel()
            raise RuntimeError("échec après annulation")
        return "fini"

    monkeypatch.setattr(batch_ops, "run_remux", run_remux)
    result = call(CATALOG, "profile.batch", {"profile": "P", "sources": {"inputs": [j["sources"][0] for j in jobs]},
                                             "output_dir": str(tmp_path), "continue_on_error": True}, ctx)
    assert result.status == "partial" and [job["status"] for job in result.jobs] == ["success", "cancelled"]
    finals = [e.data["state"] for e in sink.events if e.parent == ctx.events.job and e.type is EventType.STATE
              and e.data["state"] != JobState.RUNNING.value]
    assert sorted(finals) == [JobState.CANCELLED.value, JobState.SUCCEEDED.value]
