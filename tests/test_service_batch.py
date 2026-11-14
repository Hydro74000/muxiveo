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


def test_batch_children_have_their_own_jobs_and_states(tmp_path, doubles):
    ctx, sink = _ctx()
    result = call(CATALOG, "media.batch", _template(tmp_path, _items(tmp_path, "a", "b")), ctx)
    assert result.status == "succeeded" and (result.total, result.successes, result.failures) == (2, 2, 0)
    children = {e.job for e in sink.events if e.parent == ctx.events.job}
    assert len(children) == 2
    for child in children:
        states = [e.data["state"] for e in sink.events if e.job == child and e.type is EventType.STATE]
        assert states == [JobState.RUNNING.value, JobState.SUCCEEDED.value]
        assert any(e.type is EventType.ARTIFACT for e in sink.events if e.job == child)


def test_batch_partial_when_some_jobs_fail(tmp_path, doubles):
    doubles.plan["b.mkv"] = "fail"
    ctx, _ = _ctx()
    payload = {**_template(tmp_path, _items(tmp_path, "a", "b", "c")), "continue_on_error": True}
    result = call(CATALOG, "media.batch", payload, ctx)
    assert result.status == "partial" and (result.successes, result.failures) == (2, 1)
    assert [job["status"] for job in result.jobs] == ["success", "failed", "success"]


def test_batch_failed_when_every_job_fails(tmp_path, doubles):
    doubles.plan.update({"a.mkv": "fail"})
    ctx, _ = _ctx()
    assert call(CATALOG, "media.batch", _template(tmp_path, _items(tmp_path, "a")), ctx).status == "failed"


def test_cancel_during_a_job_stops_the_batch(tmp_path, doubles):
    doubles.plan["b.mkv"] = "cancel"
    ctx, sink = _ctx()
    payload = {**_template(tmp_path, _items(tmp_path, "a", "b", "c")), "continue_on_error": True}
    result = call(CATALOG, "media.batch", payload, ctx)
    assert doubles.ran == ["a.mkv", "b.mkv"]
    assert result.status == "partial" and [job["status"] for job in result.jobs] == ["success", "cancelled"]
    cancelled = [e for e in sink.events if e.type is EventType.STATE and e.data["state"] == JobState.CANCELLED.value]
    assert len(cancelled) == 1 and cancelled[0].parent == ctx.events.job


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
