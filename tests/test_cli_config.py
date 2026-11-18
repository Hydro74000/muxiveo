"""Administration : catalogue et validation des réglages, opérations NVEncC (installateur simulé)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import nvencc_install as nv
from core.jobs.errors import JobError
from core.service import ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.ops.admin import coerce, config_fields


def test_catalog_covers_settings_panel_and_extensions():
    fields = config_fields()
    assert {"paths.work_dir", "tools.ffmpeg", "ui.theme", "encoding.ram_buffer_threshold_pct",
            "plugins.tensorrt_enabled", "plugins.auto_update"} <= set(fields)
    # Pic de l'écran simulé : réglé dans HDR Studio, pas une option de config.ini.
    assert not any(name.startswith("hdr.") for name in fields)
    assert all(spec["section"] == name.split(".")[0] for name, spec in fields.items())


@pytest.mark.parametrize("name, raw, expected", [
    ("ui.check_updates", "Oui", "true"), ("ui.check_updates", "0", "false"), ("ui.theme", "light", "light"),
    ("ui.ui_scale_percent", "150", "150"), ("ui.language", "fra", "fra"), ("ui.language", "en-US", "eng"),
    ("audio_encoding.aac_bitrate_per_channel_kbps", "96", "96"),
])
def test_values_are_validated(name, raw, expected):
    assert coerce(config_fields()[name], raw) == expected


@pytest.mark.parametrize("name, raw", [
    ("ui.check_updates", "bof"), ("ui.theme", "rose"), ("ui.ui_scale_percent", "20"), ("ui.log_max_lines", "x"),
    ("audio_encoding.aac_bitrate_per_channel_kbps", "97"), ("ui.language", "klingon"), ("ui.language", "xyz"),
])
def test_invalid_values_are_refused(name, raw):
    with pytest.raises(JobError) as caught:
        coerce(config_fields()[name], raw)
    assert caught.value.exit_code == 3


def _ctx(**config: Any) -> ExecutionContext:
    return ExecutionContext.create(SimpleNamespace(tool_nvencc="nvencc", **config), ListSink(), allow_privileged=True)


def _fake_nvencc(monkeypatch, tmp_path: Path, *, version: str, latest: str, user: bool = False) -> list[Any]:
    calls: list[Any] = []
    binary = tmp_path / "nvencc"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(nv, "supported_platform", lambda *a: True)
    monkeypatch.setattr(nv, "nvidia_present", lambda: True)
    monkeypatch.setattr(nv, "plan_method", lambda: nv.METHOD_USER)
    monkeypatch.setattr(nv, "describe", lambda resolved, bundled_dir=None: nv.NvenccTool(
        binary, version, nv.PROVENANCE_USER if user else nv.PROVENANCE_SYSTEM))
    monkeypatch.setattr(nv, "latest_version", lambda timeout=15.0: latest)
    monkeypatch.setattr(nv, "user_binary", lambda *a: binary if user else None)
    monkeypatch.setattr(nv, "read_version", lambda path, timeout=15.0: latest)

    def install(method=None, *, progress=None, cancel=None, log=None, interactive=None, elevate=True):
        calls.append((method, interactive))
        progress(50, 100)
        log("WARN", "repli dossier utilisateur")
        return binary

    monkeypatch.setattr(nv, "install", install)
    monkeypatch.setattr(nv, "remove_user", lambda: calls.append("remove") or True)
    return calls


def test_nvencc_update_only_when_newer(monkeypatch, tmp_path):
    calls = _fake_nvencc(monkeypatch, tmp_path, version="9.36", latest="9.40")
    status: Any = call(CATALOG, "nvencc.status", {"check_latest": True}, _ctx())
    assert status.update_available and status.provenance == "system"
    sink = ListSink()
    result: Any = call(CATALOG, "nvencc.install", {"only_if_newer": True, "interactive": False},
                       ExecutionContext.create(SimpleNamespace(tool_nvencc="nvencc"), sink, allow_privileged=True))
    assert result.installed and calls == [(None, False)]
    assert {event.type.value for event in sink.events} >= {"progress", "warning", "artifact"}
    _fake_nvencc(monkeypatch, tmp_path, version="9.40", latest="9.40")
    unchanged: Any = call(CATALOG, "nvencc.install", {"only_if_newer": True}, _ctx())
    assert not unchanged.installed and "à jour" in unchanged.message
    with pytest.raises(JobError):
        call(CATALOG, "nvencc.install", {"method": "brew"}, _ctx())


def test_nvencc_remove_user_copy_only(monkeypatch, tmp_path):
    calls = _fake_nvencc(monkeypatch, tmp_path, version="9.36", latest="", user=False)
    system: Any = call(CATALOG, "nvencc.remove", {}, _ctx())
    assert not system.removed and "gestionnaire de paquets" in system.message and calls == []
    calls = _fake_nvencc(monkeypatch, tmp_path, version="9.36", latest="", user=True)
    removed: Any = call(CATALOG, "nvencc.remove", {}, _ctx())
    assert removed.removed and calls == ["remove"]
