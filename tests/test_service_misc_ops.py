"""Opérations système, extensions, synchro, HDR et Hybrid de la couche de service (contrat et erreurs)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import plugins
from core.jobs.errors import JobError
from core.service import ApiError, EventType, ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.ops import hybrid as hybrid_ops
from core.workflows.sync_calibration import SyncCalibration


def _ctx(config: Any = None, **kwargs: Any) -> tuple[ExecutionContext, ListSink]:
    sink = ListSink()
    return ExecutionContext.create(config or SimpleNamespace(), sink, **kwargs), sink


def test_every_published_operation_has_a_closed_request_schema():
    assert len(CATALOG.names()) >= 20
    for spec in CATALOG:
        assert spec.request.json_schema()["additionalProperties"] is False, spec.name


def test_system_tools_and_schema_get():
    ctx, _ = _ctx(SimpleNamespace(tool_commands=lambda: {"ffmpeg": "/x/ffmpeg"}))
    assert call(CATALOG, "system.tools", {}, ctx).to_dict() == {"version": 1, "tools": {"ffmpeg": "/x/ffmpeg"}}
    ctx, _ = _ctx()
    assert call(CATALOG, "schema.get", {"version": "api"}, ctx).schema["api_version"] == 1
    ctx, _ = _ctx()
    assert "properties" in call(CATALOG, "schema.get", {}, ctx).schema
    with pytest.raises(ApiError):
        call(CATALOG, "schema.get", {"version": "2"}, _ctx()[0])


# --- Extensions --------------------------------------------------------------------------

def test_plugins_unsupported_platform_is_logged_then_raised(monkeypatch):
    monkeypatch.setattr(plugins, "platform_tag", lambda *_a, **_k: None)
    ctx, sink = _ctx()
    with pytest.raises(JobError) as caught:
        call(CATALOG, "plugins.list", {}, ctx)
    assert caught.value.logged and caught.value.exit_code == 4
    assert any(e.type is EventType.LOG and "plate-forme" in e.data["message"] for e in sink.events)


def test_trt_install_requires_license_before_any_download(monkeypatch):
    monkeypatch.setattr(plugins, "platform_tag", lambda *_a, **_k: "linux-x86_64")
    monkeypatch.setattr("core.service.ops.plugins.plugin_status", lambda spec, config, feed: {
        "name": spec.id, "label": "TRT", "available_version": "9.9.9", "compatible": True, "status": ""})
    monkeypatch.setattr(plugins, "install", lambda *_a, **_k: pytest.fail("téléchargement interdit"))
    with pytest.raises(JobError) as caught:
        call(CATALOG, "plugins.install", {"name": "mvo-rife-trt"}, _ctx()[0])
    assert caught.value.exit_code == 2 and "--accept-license" in caught.value.message


def test_plugin_install_passes_the_token_and_reports_cancellation(monkeypatch):
    from core.service.errors import ErrorCode

    monkeypatch.setattr(plugins, "platform_tag", lambda *_a, **_k: "linux-x86_64")
    monkeypatch.setattr("core.service.ops.plugins.plugin_status", lambda spec, config, feed: {
        "name": spec.id, "label": "MVO", "available_version": "9.9.9", "compatible": True, "status": ""})
    ctx, sink = _ctx()
    seen: list[Any] = []

    def cancelled_install(*_a: Any, cancel: Any = None, **_k: Any) -> None:
        seen.append(cancel)
        raise plugins.PluginCancelled("installation annulée")

    monkeypatch.setattr(plugins, "install", cancelled_install)
    with pytest.raises(Exception) as caught:
        call(CATALOG, "plugins.install", {"name": "mvo-rife"}, ctx)
    assert seen == [ctx.cancel.event]
    assert ApiError.from_exception(caught.value).code is ErrorCode.CANCELLED
    assert [e.data["state"] for e in sink.events if e.type is EventType.STATE][-1] == "cancelled"


def test_plugins_update_reports_each_extension(monkeypatch):
    monkeypatch.setattr(plugins, "platform_tag", lambda *_a, **_k: "linux-x86_64")
    statuses = {
        "mvo-rife": {"name": "mvo-rife", "label": "MVO", "installed_version": "1.0", "update_available": False},
        "mvo-rife-trt": {"name": "mvo-rife-trt", "label": "TRT", "installed_version": None},
        "mvo-fel": {"name": "mvo-fel", "label": "FEL", "installed_version": "0.1", "update_available": True,
                    "available_version": None, "compatible": True, "status": ""},
    }
    monkeypatch.setattr("core.service.ops.plugins.plugin_status", lambda spec, config, feed: statuses[spec.id])
    result = call(CATALOG, "plugins.update", {}, _ctx()[0])
    by_name = {item["name"]: item for item in result.extensions}
    assert by_name["mvo-rife"]["status"] == "up_to_date"
    assert by_name["mvo-fel"]["status"] == "failed" and by_name["mvo-fel"]["exit_code"] == 6
    assert "mvo-rife-trt" not in by_name


# --- Synchro -----------------------------------------------------------------------------

def test_shift_subtitles_requires_exactly_one_method_and_respects_force(tmp_path):
    src, out = tmp_path / "a.srt", tmp_path / "b.srt"
    src.write_text("1\n00:00:01,000 --> 00:00:02,000\nBonjour\n", encoding="utf-8")
    with pytest.raises(JobError):
        call(CATALOG, "sync.shift_subtitles", {"input": str(src), "output": str(out)}, _ctx()[0])
    result = call(CATALOG, "sync.shift_subtitles", {"input": str(src), "output": str(out), "offset_ms": 1000.0},
                  _ctx()[0])
    assert result.output == str(out) and "00:00:02,000" in out.read_text(encoding="utf-8")
    with pytest.raises(JobError) as caught:
        call(CATALOG, "sync.shift_subtitles", {"input": str(src), "output": str(out), "offset_ms": 0.0}, _ctx()[0])
    assert caught.value.exit_code == 5


def test_sync_scan_uses_ports_and_writes_calibration(tmp_path):
    calibration = SyncCalibration.linear(-120.0)
    seen: dict[str, Any] = {}

    class _Scanner:
        def scan(self, ref, target, **kwargs):
            seen.update(ref=ref, target=target, kwargs=kwargs)
            return calibration

    out = tmp_path / "calib.json"
    ctx, sink = _ctx(SimpleNamespace(sync_cadence_auto_apply=False),
                     ports={"audio_scanner": lambda: _Scanner(),
                            "detect_stream_kind": lambda *_a, **_k: "audio"})
    result = call(CATALOG, "sync.scan", {"ref": str(tmp_path / "r.mkv"), "target": str(tmp_path / "t.mkv"),
                                         "output_json": str(out)}, ctx)
    assert result.calibration == calibration.to_dict()
    assert json.loads(out.read_text(encoding="utf-8")) == calibration.to_dict()
    assert seen["kwargs"]["drift_threshold_ms"] == 25
    assert any(e.type is EventType.ARTIFACT for e in sink.events)


# --- HDR ---------------------------------------------------------------------------------

def test_hdr_inspect_validates_compare_arity(tmp_path):
    ctx, _ = _ctx(SimpleNamespace(app_data_dir=tmp_path, ensure_work_dir=lambda: tmp_path / "work"),
                  ports={"file_inspector": lambda **_k: pytest.fail("aucune inspection attendue")})
    with pytest.raises(JobError) as caught:
        call(CATALOG, "hdr.inspect", {"inputs": [str(tmp_path / "a.mkv")], "compare": True}, ctx)
    assert caught.value.exit_code == 2


def test_hdr_inspect_fel_survival_is_structured():
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "hdr.inspect", {"inputs": ["/a.mkv"], "fel_survival": {"encoder": "x264",
                                                                           "bitrate_kbps": 0}}, _ctx()[0])
    assert {d.path for d in caught.value.details} == {"fel_survival.encoder", "fel_survival.bitrate_kbps"}


# --- Hybrid ------------------------------------------------------------------------------

def test_hybrid_run_applies_hybrid_defaults(monkeypatch, tmp_path):
    from core.jobs.hybrid_studio import PreparedEpisode
    from core.workflows.hybrid_matrix import MatrixEpisode, SourceRole

    seen: dict[str, Any] = {}
    episode = MatrixEpisode(season=1, episode=2, master_file=Path("/r.mkv"))

    def prepare(spec, episodes, config, logger_for, cancel):
        seen.update(spec=spec, cancel=cancel)
        return [PreparedEpisode(episode, SimpleNamespace(output=tmp_path / "o.mkv", allow_missing_output_dir=True))]

    class Children:
        def __init__(self, ctx):
            pass

        def logger(self, index):
            return SimpleNamespace(emit=lambda *a, **k: None)

        def run(self, config, common, logger, remux_config, *, force):
            seen["ran"] = remux_config
            return 0

    monkeypatch.setattr(hybrid_ops, "plan_episodes", lambda spec, logger: [episode])
    monkeypatch.setattr(hybrid_ops, "prepare_episodes", prepare)
    monkeypatch.setattr(hybrid_ops, "check_outputs", lambda prepared, template="": None)
    monkeypatch.setattr(hybrid_ops, "_Children", Children)
    ctx, _ = _ctx()
    result = call(CATALOG, "hybrid.run", {"output_dir": str(tmp_path), "ref": "/r.mkv", "donor": "/d.mkv"}, ctx)
    spec = seen["spec"]
    common = spec.common
    assert (common.sync_mode, common.sync_subtitles, common.crossfade_ms, common.clean_nfo) == \
        ("physical", "mirror", 80, True)
    assert spec.sources == ((SourceRole.MASTER, Path("/r.mkv")), (SourceRole.DONOR, Path("/d.mkv")))
    assert (spec.recipe.sync_mode, spec.recipe.sync_subtitles) == ("physical", "mirror")
    assert result.status == "succeeded" and seen["cancel"] is ctx.cancel and "ran" in seen


def test_hybrid_run_refuses_ambiguous_sources(tmp_path):
    with pytest.raises(JobError) as caught:
        call(CATALOG, "hybrid.run", {"output_dir": str(tmp_path), "ref": str(tmp_path / "r.mkv")}, _ctx()[0])
    assert caught.value.exit_code == 2
