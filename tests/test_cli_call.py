"""Adaptateur CLI de la couche de service : call, operations, schema api, format events."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cli import commands
from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_UPDATE_AVAILABLE
from cli.logging import Logger
from cli.parser import build_parser
from cli.service_adapter import cmd_call, cmd_operations
from core.service.ops import media
from core.update_check import UpdateInfo


def _fake_inspection(monkeypatch) -> None:
    class _Inspector:
        def __init__(self, ffprobe_bin, mediainfo_bin, cancel_event=None):  # signature réelle (jeton)
            pass

        def inspect(self, path):
            return SimpleNamespace(path=path)

    monkeypatch.setattr(media, "FileInspector", _Inspector)
    monkeypatch.setattr(media, "serialize_file_info", lambda info: {"path": str(info.path)})


def _run(argv: list[str], config: Any = None) -> int:
    args = build_parser().parse_args(argv)
    logger = Logger(fmt=args.log_format, stream=io.StringIO())
    return int(args.func(args, cast(Any, config or SimpleNamespace(tool_ffprobe="ffprobe", tool_mediainfo="mi")),
                         logger))


def test_call_and_legacy_inspect_return_the_same_result(monkeypatch, capsys, tmp_path):
    _fake_inspection(monkeypatch)
    request = tmp_path / "req.json"
    request.write_text(json.dumps({"inputs": ["a.mkv"]}), encoding="utf-8")
    assert _run(["call", "media.inspect", "--request", str(request)]) == EXIT_OK
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "succeeded" and response["operation"] == "media.inspect"
    assert response["result"]["files"] == [{"path": str(tmp_path / "a.mkv")}]

    assert _run(["inspect", str(tmp_path / "a.mkv")]) == EXIT_OK
    assert json.loads(capsys.readouterr().out) == {"files": response["result"]["files"]}


def test_call_reads_stdin_and_reports_field_errors(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"inputs": "a.mkv", "zzz": 1}'))
    assert _run(["call", "media.inspect", "--request", "-"]) == EXIT_ARGS
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "failed" and response["error"]["code"] == "invalid_request"
    assert {d["path"] for d in response["error"]["details"]} == {"inputs", "zzz"}


def test_call_unknown_operation_and_unreadable_request(capsys, tmp_path):
    assert _run(["call", "nope.op"]) == EXIT_ARGS
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "unknown_operation"
    bad = tmp_path / "bad.json"
    bad.write_text('{"inputs": [NaN]}', encoding="utf-8")
    assert _run(["call", "media.inspect", "--request", str(bad)]) == EXIT_ARGS
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_request"


def test_call_streams_events_on_stderr(monkeypatch, capsys, tmp_path):
    _fake_inspection(monkeypatch)
    args = build_parser().parse_args(["call", "media.inspect"])
    stream = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    request = tmp_path / "r.json"
    request.write_text('{"inputs": ["x.mkv"]}', encoding="utf-8")
    args.request = str(request)
    assert cmd_call(args, cast(Any, SimpleNamespace()), Logger(fmt="events", stream=stream)) == EXIT_OK
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [e["type"] for e in events] == ["state", "result", "state"]
    assert events[-1]["state"] == "succeeded" and all(e["v"] == 1 for e in events)
    assert json.loads(capsys.readouterr().out)["job"] == events[0]["job"]


def test_version_events_format_prints_envelope_and_keeps_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(commands, "query_latest_release", lambda channel, timeout=5.0: UpdateInfo("99.0.0", "u"))
    assert _run(["version", "--check", "--log-format", "events"], SimpleNamespace(update_channel="stable")) == \
        EXIT_UPDATE_AVAILABLE
    response = json.loads(capsys.readouterr().out)
    assert response["operation"] == "system.version" and response["result"]["latest"] == "99.0.0"


def test_operations_list_describe_and_schema_api(capsys):
    assert _run(["operations"]) == EXIT_OK
    assert "media.inspect" in capsys.readouterr().out
    assert _run(["operations", "--json"]) == EXIT_OK
    names = [item["name"] for item in json.loads(capsys.readouterr().out)["operations"]]
    assert names == sorted(names) and "system.version" in names
    assert _run(["operations", "--describe", "media.inspect"]) == EXIT_OK
    described = json.loads(capsys.readouterr().out)
    assert described["request"]["required"] == ["inputs"]
    assert _run(["operations", "--describe", "nope"]) == EXIT_ARGS
    capsys.readouterr()
    assert _run(["schema", "--version", "api"]) == EXIT_OK
    catalog = json.loads(capsys.readouterr().out)
    assert catalog["api_version"] == 1 and "media.inspect" in catalog["operations"]


@pytest.mark.parametrize("command", [["inspect", "x.mkv"], ["version"], ["tools"], ["schema"],
                                     ["validate"], ["batch"], ["plugins", "list"]])
def test_every_log_format_option_accepts_events(command):
    args = build_parser().parse_args([*command, "--log-format", "events"])
    assert args.log_format == "events"


def test_legacy_logger_events_format_wraps_messages(capsys):
    stream = io.StringIO()
    Logger(fmt="events", stream=stream).emit("warning", "attention", code="x")
    event = json.loads(stream.getvalue())
    assert event["type"] == "warning" and event["message"] == "attention"  # avertissement : événement warning
    assert event["code"] == "x" and event["seq"] == 1


def test_operations_handler_direct_call(capsys):
    args = build_parser().parse_args(["operations", "--json"])
    assert cmd_operations(args, cast(Any, SimpleNamespace()), Logger()) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["api_version"] == 1


@pytest.mark.parametrize("argv, revealed", [(["call", "system.version"], False),
                                            (["serve"], False),
                                            (["call", "system.version", "--allow-privileged"], True)])
def test_an_unreadable_config_ini_at_startup_is_not_shown_to_api_entries(monkeypatch, capsys, argv, revealed):
    """config.ini illisible au lancement : `call` / `serve` sans autorisation n'en recopient pas les lignes."""
    import configparser

    from cli import main as cli_main

    def broken() -> Any:
        configparser.ConfigParser().read_string("[metadata]\ntmdb_bearer_token = a\nSENTINEL_PRIVATE_TOKEN\n")

    monkeypatch.setattr(cli_main, "AppConfig", broken)
    assert cli_main.main(argv) != 0
    captured = capsys.readouterr()
    assert ("SENTINEL" in captured.out + captured.err) is revealed
    if not revealed:
        assert "ligne 3" in captured.err


def test_api_calls_refuse_an_unusable_reference_folder(tmp_path):
    from pathlib import Path

    from core.service import ApiError, ExecutionContext, ListSink, call
    from core.service.catalog import CATALOG

    for base in (Path("/media\0bad"), Path("relatif")):
        with pytest.raises(ApiError) as caught:
            call(CATALOG, "media.inspect", {"inputs": ["a.mkv"]}, ExecutionContext.create(SimpleNamespace(), ListSink()),
                 base_dir=base)
        assert caught.value.code.value == "invalid_request"
