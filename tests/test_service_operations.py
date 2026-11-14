"""Couche de service : modèles stricts, catalogue, exécution, événements, annulation."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pytest

from core.service import (
    API_VERSION,
    ApiError,
    CancelToken,
    Catalog,
    ErrorCode,
    EventType,
    ExecutionContext,
    JobState,
    ListSink,
    Model,
    call,
    field,
    operation,
)
from core.service.catalog import CATALOG
from core.service.context import Overrides, snapshot_config
from core.update_check import UpdateCheckError, UpdateInfo


@dataclass(frozen=True)
class _Inner(Model):
    level: Literal["low", "high"] = "low"


@dataclass(frozen=True)
class _Request(Model):
    """Requête de test."""

    name: str
    count: int = field(default=1, minimum=1, maximum=10)
    ratio: float = 1.0
    paths: tuple[Path, ...] = ()
    inner: _Inner | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Result(Model):
    echo: str


def _ctx(config: Any = None, **kwargs: Any) -> tuple[ExecutionContext, ListSink]:
    sink = ListSink()
    return ExecutionContext.create(config or SimpleNamespace(), sink, **kwargs), sink


def _details(error: ApiError) -> dict[str, str]:
    return {detail.path: detail.message for detail in error.details}


# --- Modèles ---------------------------------------------------------------

def test_model_parses_valid_payload_and_dumps_json_types():
    request = _Request.from_dict({"name": "a", "count": 3, "ratio": 2, "paths": ["/x/y"], "inner": {"level": "high"}})
    assert request.ratio == 2.0 and request.paths == (Path("/x/y"),) and request.inner == _Inner("high")
    assert request.to_dict()["paths"] == ["/x/y"]
    assert request.to_dict(omit_none=True) == request.to_dict()


def test_model_strict_reports_every_error_with_path():
    with pytest.raises(ApiError) as caught:
        _Request.from_dict({"count": True, "ratio": "x", "paths": ["rel"], "inner": {"level": "mid"}, "zzz": 1})
    error = caught.value
    assert error.code is ErrorCode.INVALID_REQUEST
    assert _details(error) == {
        "name": "champ requis",
        "count": "entier attendu",
        "ratio": "nombre attendu",
        "paths[0]": "chemin absolu attendu",
        "inner.level": "valeur attendue parmi : low, high",
        "zzz": "clé inconnue",
    }


def test_model_absent_differs_from_null():
    assert _Request.from_dict({"name": "a", "inner": None}).inner is None
    with pytest.raises(ApiError) as caught:
        _Request.from_dict({"name": None})
    assert "null refusé" in _details(caught.value)["name"]


def test_model_bounds_are_checked():
    with pytest.raises(ApiError) as caught:
        _Request.from_dict({"name": "a", "count": 11})
    assert _details(caught.value) == {"count": "valeur maximale : 10"}


def test_model_tolerant_mode_turns_unknown_keys_into_warnings():
    request, warnings = _Request.parse({"name": "a", "legacy": True}, strict=False)
    assert request.name == "a"
    assert [(w.path, w.message) for w in warnings] == [("legacy", "clé inconnue")]


def test_model_paths_relative_to_base_dir_or_kept_for_legacy_argv(tmp_path):
    assert _Request.from_dict({"name": "a", "paths": ["f.mkv"]}, base_dir=tmp_path).paths == (tmp_path / "f.mkv",)
    assert _Request.from_dict({"name": "a", "paths": ["f.mkv"]}, allow_relative=True).paths == (Path("f.mkv"),)


def test_model_json_schema_is_closed_and_documented():
    schema = _Request.json_schema()
    assert schema["additionalProperties"] is False and schema["required"] == ["name"]
    assert schema["properties"]["count"] == {"type": "integer", "minimum": 1, "maximum": 10, "default": 1}
    assert schema["properties"]["inner"]["anyOf"][0]["properties"]["level"]["enum"] == ["low", "high"]
    assert schema["description"] == "Requête de test."


# --- Catalogue et exécution --------------------------------------------------

@operation("test.echo", request=_Request, result=_Result, summary="Écho.")
def _echo(request: _Request, ctx: ExecutionContext) -> _Result:
    ctx.events.log("info", f"écho {request.name}", origin="TEST")
    ctx.events.artifact("output", "/tmp/out.mkv")
    return _Result(echo=request.name)


@operation("test.fail", request=_Request, result=_Result, summary="Échec.")
def _fail(request: _Request, ctx: ExecutionContext) -> _Result:
    raise RuntimeError("boum")


def test_catalog_rejects_duplicates_and_unknown_operations():
    with pytest.raises(ValueError):
        Catalog((_echo, _echo))
    with pytest.raises(ApiError) as caught:
        Catalog((_echo,)).get("nope")
    assert caught.value.code is ErrorCode.UNKNOWN_OPERATION


def test_call_emits_sequenced_events_and_returns_result():
    ctx, sink = _ctx()
    result = call(Catalog((_echo,)), "test.echo", {"name": "x"}, ctx)
    assert result == _Result("x")
    events = sink.events
    assert [e.type for e in events] == [EventType.STATE, EventType.LOG, EventType.ARTIFACT, EventType.RESULT,
                                        EventType.STATE]
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]
    assert {e.job for e in events} == {ctx.events.job}
    assert events[0].data["state"] == JobState.RUNNING.value and events[-1].data["state"] == JobState.SUCCEEDED.value
    assert events[1].to_dict()["origin"] == "TEST" and events[1].to_dict()["v"] == 1
    assert events[3].data["result"] == {"echo": "x"}


def test_call_failure_emits_public_error_then_reraises():
    ctx, sink = _ctx()
    with pytest.raises(RuntimeError):
        call(Catalog((_fail,)), "test.fail", {"name": "x"}, ctx)
    error = next(e for e in sink.events if e.type is EventType.ERROR)
    assert error.data["error"] == {"code": "workflow_failed", "message": "boum"}
    assert sink.events[-1].data["state"] == JobState.FAILED.value


def test_call_invalid_request_and_tolerant_warnings():
    ctx, sink = _ctx()
    with pytest.raises(ApiError):
        call(Catalog((_echo,)), "test.echo", {"name": "x", "old": 1}, ctx)
    assert sink.events[-1].data["state"] == JobState.FAILED.value
    ctx, sink = _ctx()
    call(Catalog((_echo,)), "test.echo", {"name": "x", "old": 1}, ctx, strict=False)
    warning = next(e for e in sink.events if e.type is EventType.WARNING)
    assert warning.data == {"message": "clé inconnue : old", "code": "unknown_key", "path": "old"}


def test_cancelled_before_start_reports_cancelled_state():
    token = CancelToken()
    token.cancel()
    ctx, sink = _ctx(cancel=token)
    with pytest.raises(ApiError) as caught:
        call(Catalog((_echo,)), "test.echo", {"name": "x"}, ctx)
    assert caught.value.code is ErrorCode.CANCELLED
    assert sink.events[-1].data["state"] == JobState.CANCELLED.value


def test_cancel_token_callbacks_run_once_even_when_late():
    token, seen = CancelToken(), []
    token.on_cancel(lambda: seen.append("a"))
    token.cancel()
    token.cancel()
    token.on_cancel(lambda: seen.append("b"))
    assert seen == ["a", "b"] and token.wait(0)


def test_successive_calls_in_one_process_have_independent_jobs():
    first, _ = _ctx()
    second, _ = _ctx()
    call(Catalog((_echo,)), "test.echo", {"name": "a"}, first)
    call(Catalog((_echo,)), "test.echo", {"name": "b"}, second)
    assert first.events.job != second.events.job


def test_child_emitter_links_parent():
    ctx, sink = _ctx()
    child = ctx.events.child()
    child.log("info", "épisode")
    assert sink.events[0].parent == ctx.events.job and sink.events[0].job == child.job


# --- Contexte ----------------------------------------------------------------

def test_snapshot_config_isolates_containers_and_drops_qsettings():
    live = SimpleNamespace(_settings=object(), extra={"a": 1}, tool_ffprobe="ffprobe")
    snap = snapshot_config(live)
    live.extra["a"] = 2
    live.tool_ffprobe = "autre"
    assert snap.extra == {"a": 1} and snap.tool_ffprobe == "ffprobe" and snap._settings is None


def test_context_tool_prefers_call_override():
    ctx, _ = _ctx(SimpleNamespace(tool_ffprobe="/opt/ffprobe"), overrides=Overrides(mediainfo="/x/mi"))
    assert ctx.tool("ffprobe") == "/opt/ffprobe" and ctx.tool("mediainfo") == "/x/mi"


# --- Opérations publiées -------------------------------------------------------

def test_published_catalog_describes_every_operation():
    described = CATALOG.describe()
    assert described["api_version"] == API_VERSION and described["event_version"] == 1
    assert {"system.version", "media.inspect"} <= set(described["operations"])
    for name, spec in described["operations"].items():
        assert spec["summary"], name
        assert spec["request"]["additionalProperties"] is False, name
        assert "invalid_request" in spec["errors"], name


def test_system_version_check_via_port():
    ctx, _ = _ctx(SimpleNamespace(update_channel="unstable"),
                  ports={"query_latest_release": lambda channel: UpdateInfo(version="99.0.0", url="u")})
    result = call(CATALOG, "system.version", {"check": True}, ctx)
    assert result.to_dict(omit_none=True) == {"name": result.name, "version": result.version, "channel": "unstable",
                                              "latest": "99.0.0", "update_available": True, "url": "u",
                                              "check_failed": False}


def test_system_version_unreachable_is_a_result_not_an_error():
    def _fail(channel):
        raise UpdateCheckError("hors ligne")

    ctx, sink = _ctx(SimpleNamespace(update_channel="stable"), ports={"query_latest_release": _fail})
    result = call(CATALOG, "system.version", {"check": True}, ctx)
    assert result.check_failed and result.check_error == "hors ligne"
    assert any(e.type is EventType.LOG and "hors ligne" in e.data["message"] for e in sink.events)


def test_media_inspect_uses_tool_overrides(monkeypatch, tmp_path):
    from core.service.ops import media

    seen: dict[str, Any] = {}

    class _Inspector:
        def __init__(self, ffprobe_bin, mediainfo_bin, cancel_event=None):  # signature réelle (jeton)
            seen["bins"] = (ffprobe_bin, mediainfo_bin)

        def inspect(self, path):
            seen.setdefault("paths", []).append(path)
            return SimpleNamespace(path=path)

    monkeypatch.setattr(media, "FileInspector", _Inspector)
    monkeypatch.setattr(media, "serialize_file_info", lambda info: {"path": str(info.path)})
    ctx, _ = _ctx(SimpleNamespace(tool_ffprobe="fp", tool_mediainfo="mi"), overrides=Overrides(ffprobe="/o/fp"))
    result = call(CATALOG, "media.inspect", {"inputs": ["a.mkv"]}, ctx, base_dir=tmp_path)
    assert seen == {"bins": ("/o/fp", "mi"), "paths": [tmp_path / "a.mkv"]}
    assert result.to_dict() == {"files": [{"path": str(tmp_path / "a.mkv")}], "template": None}


# --- Architecture --------------------------------------------------------------

def test_service_layer_imports_neither_cli_nor_argparse_nor_creates_qt_app():
    root = Path(__file__).resolve().parents[1] / "core" / "service"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("cli"), path
                assert node.module != "argparse", path
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith(("cli", "argparse")) for alias in node.names), path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"QCoreApplication", "QApplication"}, path
