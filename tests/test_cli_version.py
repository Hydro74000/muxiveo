"""Tests de la commande CLI `version` (réseau mocké)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cli import commands
from cli.constants import EXIT_OK, EXIT_UPDATE_AVAILABLE, EXIT_WORKFLOW
from cli.logging import Logger
from cli.parser import build_parser
from core.update_check import UpdateInfo
from core.version import APP_BUILD_VERSION


def _run(argv: list[str], monkeypatch, info: UpdateInfo | None, config=None) -> tuple[int, list[str]]:
    calls: list[str] = []

    def _fetch(channel, timeout=5.0):
        calls.append(channel)
        return info

    monkeypatch.setattr(commands, "fetch_latest_release", _fetch)
    args = build_parser().parse_args(argv)
    rc = commands.cmd_version(args, config or SimpleNamespace(update_channel="stable"), Logger(fmt=args.log_format))
    return rc, calls


def test_version_without_check_does_not_hit_network(monkeypatch, capsys):
    rc, calls = _run(["version"], monkeypatch, None)
    assert rc == EXIT_OK and calls == []
    assert APP_BUILD_VERSION in capsys.readouterr().out


def test_version_check_reports_update_with_exit_code(monkeypatch, capsys):
    info = UpdateInfo(version="99.0.0", url="https://example/r")
    rc, calls = _run(["version", "--check", "--log-format", "jsonl"], monkeypatch, info)
    payload = json.loads(capsys.readouterr().out)
    assert rc == EXIT_UPDATE_AVAILABLE and calls == ["stable"]
    assert payload["update_available"] is True and payload["latest"] == "99.0.0" and payload["channel"] == "stable"


@pytest.mark.parametrize("argv,expected", [(["--channel", "unstable"], "unstable"), ([], "unstable")])
def test_version_check_channel_from_option_or_config(monkeypatch, capsys, argv, expected):
    info = UpdateInfo(version="0.0.1", url="https://example/r")
    rc, calls = _run(["version", "--check", *argv], monkeypatch, info, SimpleNamespace(update_channel="unstable"))
    assert rc == EXIT_OK and calls == [expected]
    assert "À jour" in capsys.readouterr().out


def test_version_check_network_error(monkeypatch, capsys):
    rc, _ = _run(["version", "--check"], monkeypatch, None)
    assert rc == EXIT_WORKFLOW
