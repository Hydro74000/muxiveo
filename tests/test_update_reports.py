"""
tests/test_update_reports.py — Mises à jour : transactions par installation et comptes rendus signalés après
écriture confirmée (D3, C5, N4 ; plan de remédiation 5.2.0, lot 7).
"""

from __future__ import annotations

import io
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest



# ── Signalement après écriture confirmée (N4) ───────────────────────────────


def _service_call(previous: list[dict], marked: list[Any]):
    from core.service import ExecutionContext, ListSink, call
    from core.service.catalog import CATALOG

    from core.update_install import InstallKind

    info = SimpleNamespace(is_newer=False, version="5.2.0")
    ports = {"install_kind": lambda: InstallKind.SOURCE_ARCHIVE, "collect_update_results": lambda kind: previous,
             "mark_update_results_reported": lambda results, kind: marked.append([r["id"] for r in results]),
             "query_latest_release": lambda channel: info}
    ctx = ExecutionContext.create(SimpleNamespace(update_channel="stable"), ListSink(), ports=ports,
                                  allow_privileged=True)
    result = call(CATALOG, "app.update_install", {}, ctx)
    return result, ctx


def test_reports_are_marked_only_once_the_result_is_written():
    marked: list[Any] = []
    result, ctx = _service_call([{"id": "w1", "ok": True, "version": "5.1.0"}], marked)
    assert result.previous and marked == []  # résultat produit mais pas encore écrit
    ctx.confirm_result_written()
    ctx.confirm_result_written()
    assert marked == [["w1"]]  # une seule fois


class _BrokenStream(io.StringIO):
    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


@pytest.mark.parametrize("fmt", ["text", "jsonl", "events"])
def test_cli_install_marks_reports_only_after_a_successful_flush(monkeypatch, fmt):
    import cli.commands as commands
    from cli.logging import Logger
    from cli.parser import build_parser

    for broken in (True, False):
        marked: list[Any] = []

        def run_operation(name, payload, **_kwargs):
            return _service_call([{"id": "w1", "ok": True, "version": "5.1.0"}], marked)

        monkeypatch.setattr(commands, "run_operation", run_operation)
        stream = _BrokenStream() if broken else io.StringIO()
        logger = Logger(fmt=fmt if fmt != "events" else "text", stream=stream)
        if fmt == "events":
            monkeypatch.setattr(sys, "stdout", stream)
        args = build_parser().parse_args(["version", "--install", "--yes", "--log-format", fmt])
        commands._install_update(args, SimpleNamespace(), logger)
        assert marked == ([] if broken else [["w1"]]), (fmt, broken)


def test_call_marks_reports_only_after_stdout_is_flushed(monkeypatch):
    from cli import service_adapter
    from core.service.ops.update import UpdateInstallResult

    for broken in (True, False):
        marked: list[int] = []

        def fake_call(_catalog, _name, _payload, ctx, **_kwargs):
            ctx.on_result_written(lambda: marked.append(1))  # comme app.update_install
            return UpdateInstallResult(status="up_to_date", current="5.2.0")

        monkeypatch.setattr(service_adapter, "call", fake_call)
        monkeypatch.setattr(sys, "stdout", _BrokenStream() if broken else io.StringIO())
        args = SimpleNamespace(operation="app.update_install", request=None, allow_privileged=True,
                               ffmpeg=None, ffprobe=None, mediainfo=None, threads=None, work_dir=None)
        service_adapter.cmd_call(args, SimpleNamespace(), SimpleNamespace(emit=lambda *a, **k: None))
        assert marked == ([] if broken else [1]), broken


# ── serve ───────────────────────────────────────────────────────────────────


class _Stream(io.BytesIO):
    def __init__(self, fail_flush: bool = False) -> None:
        super().__init__()
        self.fail_flush = fail_flush

    def flush(self) -> None:
        if self.fail_flush:
            raise BrokenPipeError(32, "Broken pipe")


def _wait(predicate, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_serve_runs_on_written_after_the_frame_flush():
    from cli.serve import _Output

    written = threading.Event()
    output = _Output(_Stream(), lambda: None)
    output.send(b"{}\n", written.set)
    assert written.wait(2)


def test_serve_never_marks_on_a_broken_flush_or_a_gone_client():
    from cli.serve import _Output

    called: list[int] = []
    gone = threading.Event()
    broken = _Output(_Stream(fail_flush=True), gone.set)
    broken.send(b"{}\n", lambda: called.append(1))
    assert gone.wait(2) and called == []
    full = _Output(_Stream(), lambda: None, limit=4)
    full.send(b"0123456789\n", lambda: called.append(2))  # file pleine : client parti, trame abandonnée
    time.sleep(0.05)
    assert called == []


def test_serve_event_too_large_drops_the_callback(monkeypatch):
    import cli.serve as serve
    from core.service.events import Event, EventType

    sent: list[Any] = []
    server = serve.Server.__new__(serve.Server)
    server._out = SimpleNamespace(send=lambda frame, on_written=None: sent.append(on_written))
    monkeypatch.setattr(serve, "MAX_FRAME", 400)
    event = Event(EventType.RESULT, "job", 1, "ts", {"result": {"previous": ["x" * 2000]}}, None,
                  on_written=lambda: None)
    server.relay(event)
    assert sent == [None]  # remplacée par event_too_large : jamais marqué
    small = Event(EventType.RESULT, "job", 2, "ts", {"result": {}}, None, on_written=lambda: None)
    server.relay(small)
    assert sent[-1] is small.on_written
