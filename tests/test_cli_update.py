"""Installation des mises à jour en CLI (app.update_install, version --install) : sans relance de l'interface."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import update_apply
from core import update_install as up
from core.jobs.errors import JobError
from core.service import ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.update_check import UpdateInfo


def _ctx(tmp_path: Path, *, kind=up.InstallKind.APPIMAGE, newer: bool = True, blocker: str = "",
         applied: str = up.APPLY_INSTALLED, seen: dict[str, Any] | None = None) -> ExecutionContext:
    seen = seen if seen is not None else {}
    info = UpdateInfo("99.0.0" if newer else "0.0.1", "https://exemple/release")

    def download(info, kind, progress, cancelled, *, relaunch=True):
        seen["download_relaunch"] = relaunch
        progress(5, 10)
        return tmp_path / "Muxiveo.AppImage"

    def apply(kind, downloaded, *, relaunch=True, owner_hwnd=None):
        seen["apply_relaunch"] = relaunch
        return applied

    ports = {"install_kind": lambda: kind, "collect_update_results": lambda kind: [{"ok": True, "version": "5.1"}],
             "query_latest_release": lambda channel: info, "self_install_blocker": lambda kind: blocker,
             "download_update": download, "apply_update": apply}
    return ExecutionContext.create(SimpleNamespace(update_channel="stable"), ListSink(), ports=ports,
                                   allow_privileged=True)


def test_install_requires_confirmation_then_installs_without_relaunch(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "can_self_update", lambda info, kind=None: True)
    with pytest.raises(JobError, match="--yes") as caught:
        call(CATALOG, "app.update_install", {}, _ctx(tmp_path))
    assert caught.value.exit_code == 2
    seen: dict[str, Any] = {}
    result: Any = call(CATALOG, "app.update_install", {"confirm": True}, _ctx(tmp_path, seen=seen))
    assert result.status == "installed" and result.latest == "99.0.0" and result.previous[0]["version"] == "5.1"
    assert result.restart_required
    assert seen == {"download_relaunch": False, "apply_relaunch": False}
    scheduled: Any = call(CATALOG, "app.update_install", {"confirm": True},
                          _ctx(tmp_path, kind=up.InstallKind.SOURCE_ARCHIVE, applied=up.APPLY_SCHEDULED))
    assert scheduled.status == "scheduled" and "prochain démarrage" in scheduled.message
    assert scheduled.restart_required


def test_install_refusals(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "can_self_update", lambda info, kind=None: True)
    up_to_date: Any = call(CATALOG, "app.update_install", {"confirm": True}, _ctx(tmp_path, newer=False))
    assert up_to_date.status == "up_to_date" and not up_to_date.restart_required
    with pytest.raises(JobError, match="modifiable"):
        call(CATALOG, "app.update_install", {"confirm": True}, _ctx(tmp_path, blocker=up.BLOCKER_NOT_WRITABLE))
    with pytest.raises(JobError, match="mode d'installation"):
        call(CATALOG, "app.update_install", {"confirm": True}, _ctx(tmp_path, kind=up.InstallKind.FLATPAK))
    monkeypatch.setattr(up, "can_self_update", lambda info, kind=None: False)
    with pytest.raises(JobError, match="paquet compatible"):
        call(CATALOG, "app.update_install", {"confirm": True}, _ctx(tmp_path))


def test_appimage_replaced_without_relaunch(tmp_path, monkeypatch):
    target, downloaded = tmp_path / "Muxiveo.AppImage", tmp_path / "nouvelle.AppImage"
    target.write_bytes(b"ancienne")
    downloaded.write_bytes(b"nouvelle")
    monkeypatch.setattr(up, "_appimage_path", lambda env=None: target)
    monkeypatch.setattr(up.subprocess, "Popen", lambda *a, **k: pytest.fail("aucune relance attendue"))
    assert up.apply_update(up.InstallKind.APPIMAGE, downloaded, relaunch=False) == up.APPLY_INSTALLED
    assert target.read_bytes() == b"nouvelle" and not downloaded.exists()


def test_swap_preparation_without_relaunch_command(tmp_path, monkeypatch):
    prepared: dict[str, Any] = {}
    root = tmp_path / "Muxiveo"
    root.mkdir()
    monkeypatch.setattr(up, "select_asset", lambda info, kind, machine=None: SimpleNamespace(name="src.tar.gz"))
    monkeypatch.setattr(up, "fetch_expected_sha256", lambda info, asset, timeout=15.0: "0" * 64)
    monkeypatch.setattr(up, "download_asset", lambda asset, dest, expected, progress, cancelled: dest)
    monkeypatch.setattr(up, "installation_root", lambda kind: root)
    monkeypatch.setattr(update_apply, "prepare", lambda *args, **kwargs: prepared.update(relaunch=args[6]))
    work = up.download_update(UpdateInfo("99.0.0", "u"), up.InstallKind.SOURCE_ARCHIVE, relaunch=False)
    assert prepared["relaunch"] == [] and work.parent == tmp_path
    update_apply.launch([], tmp_path)  # commande vide : aucune relance


def test_cli_install_flag_routes_to_the_operation(monkeypatch, capsys):
    from cli import commands
    from cli.logging import Logger
    from cli.parser import build_parser

    seen: dict[str, Any] = {}

    def fake_run(name, payload, **kwargs):
        seen.update(name=name, payload=payload)
        return SimpleNamespace(previous=(), message="À jour."), SimpleNamespace(events=SimpleNamespace(job="j"))

    monkeypatch.setattr(commands, "run_operation", fake_run)
    args = build_parser().parse_args(["version", "--install", "--yes", "--channel", "unstable"])
    assert commands.cmd_version(args, SimpleNamespace(), Logger(fmt="text")) == 0  # type: ignore[arg-type]
    assert seen == {"name": "app.update_install", "payload": {"confirm": True, "channel": "unstable"}}
