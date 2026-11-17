"""Contrat des événements : avertissements à code, niveaux normalisés, variantes du schéma, étapes stables."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.service import ExecutionContext, ListSink, call
from core.service.events import EVENT_SCHEMA, LOG_LEVELS, Emitter, EventType


def check_event(event: dict[str, Any]) -> None:
    """Vérificateur structurel minimal : variante du type, champs requis, énumérations, bornes."""
    variants = {variant["properties"]["type"]["const"]: variant for variant in EVENT_SCHEMA["oneOf"]}
    variant = variants[event["type"]]
    missing = [key for key in variant["required"] if key not in event]
    assert not missing, (event, missing)
    for key, rule in variant["properties"].items():
        if key in event and "enum" in rule:
            assert event[key] in rule["enum"], (event, key)
        if key in event and "minimum" in rule:
            assert rule["minimum"] <= event[key] <= rule.get("maximum", event[key]), (event, key)


def test_warnings_always_carry_a_code_and_levels_are_normalized():
    sink = ListSink()
    emitter = Emitter(sink)
    emitter.log("WARN", "repli FFmpeg")
    emitter.log("warning", "trop tard", code="cancel_too_late")
    emitter.warning("sans code")
    emitter.log("OK", "fini")
    emitter.log("INFO", "info")
    emitter.log("bizarre", "niveau inconnu")
    data = [(event.type, dict(event.data)) for event in sink.events]
    assert [(kind, payload.get("code")) for kind, payload in data[:3]] == [
        (EventType.WARNING, "unspecified"), (EventType.WARNING, "cancel_too_late"), (EventType.WARNING, "unspecified")]
    assert [payload["level"] for _kind, payload in data[3:]] == ["ok", "info", "info"]
    for event in sink.events:
        check_event(event.to_dict())
    assert set(LOG_LEVELS.values()) == {"debug", "info", "ok", "error"}


def test_events_of_real_operations_respect_their_variant(monkeypatch):
    from core import plugins
    from core.service.catalog import CATALOG

    sink = ListSink()
    call(CATALOG, "system.tools", {}, ExecutionContext.create(SimpleNamespace(tool_commands=lambda: {}), sink))
    call(CATALOG, "schema.get", {"version": "api"}, ExecutionContext.create(SimpleNamespace(), sink))
    monkeypatch.setattr(plugins, "platform_tag", lambda *_a, **_k: "linux-x86_64")
    monkeypatch.setattr("core.service.ops.plugins.plugin_status", lambda spec, config, feed: {
        "name": spec.id, "label": "MVO", "available_version": "9.9.9", "compatible": True, "status": ""})

    def install(*_a: Any, progress=None, **_k: Any) -> None:
        progress(50, 100)
        raise plugins.PluginCancelled("installation annulée")

    monkeypatch.setattr(plugins, "install", install)
    with pytest.raises(Exception):
        call(CATALOG, "plugins.install", {"name": "mvo-rife"}, ExecutionContext.create(SimpleNamespace(), sink))
    types = {event.type for event in sink.events}
    assert {EventType.STATE, EventType.RESULT, EventType.PROGRESS, EventType.ERROR} <= types
    for event in sink.events:
        check_event(event.to_dict())
    progress = next(event for event in sink.events if event.type is EventType.PROGRESS)
    assert progress.data["stage"] == "download"  # identifiant stable


def test_describe_announces_progress_only_where_published():
    from core.service.catalog import CATALOG

    assert "progress" in CATALOG.get("media.run").describe()["events"]
    assert "progress" not in CATALOG.get("media.inspect").describe()["events"]
    assert "progress" not in CATALOG.get("sync.scan").describe()["events"]


def test_hdr_patch_steps_publish_a_stable_stage_before_their_label(tmp_path):
    from core.hdr_inspection.patch import MetadataPatcher

    seen: list[tuple[str, str]] = []
    patcher = MetadataPatcher.__new__(MetadataPatcher)
    patcher._cancelled = lambda: False
    patcher._on_stage = lambda code: seen.append(("stage", code))
    patcher._on_step = lambda label, _start, _span: seen.append(("label", label))
    patcher._step("Écriture du fichier corrigé…", 0.9, 0.1, "write")
    assert seen == [("stage", "write"), ("label", "Écriture du fichier corrigé…")]


def test_hybrid_report_file_keeps_its_historical_entries(tmp_path):
    import json

    from core.service.ops.hybrid import _write_report

    report = tmp_path / "rapport.json"
    _write_report(SimpleNamespace(report_json=report), [{"episode": "E01", "status": "ok", "job": "abc"}])
    assert json.loads(report.read_text(encoding="utf-8"))["episodes"] == [{"episode": "E01", "status": "ok"}]
