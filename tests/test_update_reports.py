"""
tests/test_update_reports.py — Mises à jour : transactions par installation et comptes rendus signalés après
écriture confirmée (D3, C5, N4 ; plan de remédiation 5.2.0, lot 7).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import update_apply


def _work(parent: Path, name: str, install: Path | None, result: dict | None = None, *, age: float = 0,
          legacy: bool = False) -> Path:
    work = parent / f"{update_apply.WORKDIR_PREFIX}{name}"
    work.mkdir()
    if legacy:  # format des versions antérieures : propriétaire lu dans prepared.json
        (work / update_apply.WORKDIR_TAG).touch()
        if install is not None:
            (work / update_apply.PREPARED_FILE).write_text(json.dumps({"install": str(install)}))
    else:
        assert install is not None
        update_apply.write_workdir_tag(work, install)
    if result is not None:
        (work / update_apply.RESULT_FILE).write_text(json.dumps(result))
    stamp = time.time() - age
    os.utime(work, (stamp, stamp))
    return work


def test_two_installations_under_one_parent_keep_their_own_backup_and_reports(tmp_path):
    """R4 du contre-audit : la seule sauvegarde de A était supprimable, les comptes rendus mêlés."""
    a, b = tmp_path / "A", tmp_path / "B"
    only_a = _work(tmp_path, "0", a, {"ok": True, "version": "5.2.0"}, age=100)
    _work(tmp_path, "1", b, {"ok": True, "version": "5.2.1"}, age=99)
    _work(tmp_path, "2", b, {"ok": True, "version": "5.2.2"}, age=98)
    discard: list[Path] = []
    reported = update_apply.collect_results(tmp_path, discard=discard, install=a)
    assert only_a not in discard and [entry["version"] for entry in reported] == ["5.2.0"]
    reported_b = update_apply.collect_results(tmp_path, discard=discard, install=b)
    assert sorted(entry["version"] for entry in reported_b) == ["5.2.1", "5.2.2"]
    assert [path.name for path in discard] == [f"{update_apply.WORKDIR_PREFIX}1"]  # remplacée chez B seulement


def test_legacy_and_current_workdirs_share_the_retention_of_their_install(tmp_path):
    install = tmp_path / "Muxiveo"
    old = _work(tmp_path, "old", install, {"ok": True, "version": "5.1.0"}, age=200, legacy=True)
    new = _work(tmp_path, "new", install, {"ok": True, "version": "5.2.0"}, age=100)
    discard: list[Path] = []
    update_apply.collect_results(tmp_path, discard=discard, install=install)
    assert discard == [old] and new not in discard


def test_unknown_owner_is_neither_reported_nor_removed(tmp_path):
    install = tmp_path / "Muxiveo"
    unknown = _work(tmp_path, "unknown", None, {"ok": True, "version": "5.0.0"}, age=9 * 86400, legacy=True)
    discard: list[Path] = []
    assert update_apply.collect_results(tmp_path, discard=discard, install=install) == []
    assert discard == [] and unknown.is_dir()
    abandoned = _work(tmp_path, "abandoned", None, age=7200, legacy=True)  # téléchargement abandonné : retiré
    update_apply.collect_results(tmp_path, install=install)
    assert not abandoned.exists() and unknown.is_dir()


def test_removed_unreported_workdir_keeps_its_report_in_the_journal(tmp_path):
    install = tmp_path / "Muxiveo"
    replaced = _work(tmp_path, "a", install, {"ok": True, "version": "5.1.0"}, age=200)
    _work(tmp_path, "b", install, {"ok": True, "version": "5.2.0"}, age=100)
    update_apply.collect_results(tmp_path, install=install, report=False)  # ménage sans signalement
    assert not replaced.exists()
    pending = update_apply.collect_results(tmp_path, install=install)
    assert sorted(entry["version"] for entry in pending) == ["5.1.0", "5.2.0"]
    update_apply.mark_reported(tmp_path, install, [entry["id"] for entry in pending])
    assert update_apply.collect_results(tmp_path, install=install) == []
    assert update_apply.pending_reports(tmp_path, install) == []


def test_many_unreported_reports_are_never_lost(tmp_path):
    install = tmp_path / "Muxiveo"
    for index in range(25):
        _work(tmp_path, f"{index:02d}", install, {"ok": True, "version": f"5.{index}.0"}, age=1000 - index)
    update_apply.collect_results(tmp_path, install=install, report=False)
    assert len(list(tmp_path.glob(f"{update_apply.WORKDIR_PREFIX}*"))) == 1  # dernière réussite gardée
    assert not update_apply.REPORTS_DIR.startswith(update_apply.WORKDIR_PREFIX)
    assert len(update_apply.collect_results(tmp_path, install=install)) == 25


def test_detached_cleanup_and_marking_never_lose_journal_entries(tmp_path):
    """Deux processus réels : l'auxiliaire copié lancé en ``python -I`` (ménage) et l'application (journal)."""
    install = tmp_path / "Muxiveo"
    works = [_work(tmp_path, f"{index:02d}", install, {"ok": True, "version": f"4.{index}.0"}, age=1000 - index)
             for index in range(30)]
    helper = tmp_path / f"{update_apply.CLEANUP_PREFIX}test"
    helper.mkdir()
    (helper / update_apply.CLEANUP_FILE).write_text(json.dumps({"install": str(install),
                                                                "paths": [str(work) for work in works]}))
    script = helper / "update_apply.py"
    shutil.copyfile(update_apply.__file__, script)
    process = subprocess.Popen([sys.executable, "-I", str(script), "cleanup", str(helper)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    owner = os.path.realpath(install)
    for index in range(40):  # écritures concurrentes du même journal depuis ce processus
        update_apply._keep_report(tmp_path, owner, {"id": f"synthetic-{index}", "ok": True, "version": "x"})
    _out, err = process.communicate(timeout=60)
    assert process.returncode == 0, err.decode()
    ids = {entry["id"] for entry in update_apply.pending_reports(tmp_path, install)}
    assert {f"synthetic-{index}" for index in range(40)} <= ids
    assert {work.name for work in works[:-1]} <= ids  # comptes rendus des 29 dossiers retirés conservés


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
