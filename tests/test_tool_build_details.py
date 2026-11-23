"""Build réelle des outils embarqués : sonde ``-version``, manifeste, lecture à l'exécution."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.github_release import select_asset
from core.tool_manifest import (
    MANIFEST_NAME,
    UNKNOWN_BUILD,
    ToolManifest,
    bundled_tool_entry,
    ffmpeg_build_details,
    is_master_build,
)

_VERSION_OUTPUT = """ffmpeg version N-129166-gae9f4540cb-20261121 Copyright (c) 2000-2026 the FFmpeg developers
built with gcc 15.2.0 (crosstool-NG 1.28.0.21_3c5cc17)
libavutil      60. 26.100 / 60. 26.100
libavcodec     63. 28.100 / 63. 28.100
libavformat    63. 21.100 / 63. 21.100
"""


@pytest.mark.skipif(os.name == "nt", reason="binaire factice en script shell")
def test_build_details_parse_version_and_libraries(tmp_path: Path) -> None:
    binary = tmp_path / "ffmpeg"
    binary.write_text("#!/bin/sh\ncat <<'EOF'\n" + _VERSION_OUTPUT + "EOF\n")
    binary.chmod(0o755)
    details = ffmpeg_build_details(binary)
    assert details["build"] == "N-129166-gae9f4540cb-20261121"
    assert details["version_line"].startswith("ffmpeg version N-129166")
    assert details["libavformat"] == "63.21.100"
    assert details["libavcodec"] == "63.28.100"
    assert is_master_build(details["build"])


@pytest.mark.skipif(os.name == "nt", reason="binaire factice en script shell")
def test_build_details_through_launcher(tmp_path: Path) -> None:
    launcher = tmp_path / "wine"
    launcher.write_text("#!/bin/sh\necho \"ffmpeg version N-1-gabc-20261121 via $1\"\n")
    launcher.chmod(0o755)
    details = ffmpeg_build_details(tmp_path / "ffmpeg.exe", launcher=(str(launcher),))
    assert details["build"] == "N-1-gabc-20261121"


def test_build_details_unknown_when_not_executable(tmp_path: Path) -> None:
    assert ffmpeg_build_details(tmp_path / "absent") == {"build": UNKNOWN_BUILD}
    assert not is_master_build(UNKNOWN_BUILD)
    assert not is_master_build("8.1")


def test_manifest_details_and_bundled_lookup(tmp_path: Path) -> None:
    for name in ("ffmpeg", "ffprobe"):
        (tmp_path / name).write_bytes(name.encode())
    manifest = ToolManifest()
    manifest.add(
        "ffmpeg", version="N-1", source="https://example.invalid/a.tar.xz", verification="github-digest",
        files=[tmp_path / "ffmpeg", tmp_path / "ffprobe"],
        details={"release": "FFmpeg-Builds/FFmpeg-Builds@latest", "asset_updated_at": "2026-11-21"},
    )
    manifest.write(tmp_path)
    payload = json.loads((tmp_path / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert payload["tools"][0]["details"]["asset_updated_at"] == "2026-11-21"

    entry = bundled_tool_entry(tmp_path / "ffprobe")
    assert entry is not None and entry["version"] == "N-1"
    # Outil système (aucun manifeste à côté) ou fichier non listé : pas embarqué.
    other = tmp_path / "system"
    other.mkdir()
    (other / "ffmpeg").write_bytes(b"x")
    assert bundled_tool_entry(other / "ffmpeg") is None
    assert bundled_tool_entry(tmp_path / "mediainfo") is None
    assert bundled_tool_entry(None) is None


def test_bundled_lookup_ignores_corrupt_manifest(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_NAME).write_text("{", encoding="utf-8")
    assert bundled_tool_entry(tmp_path / "ffmpeg") is None


def test_select_asset_keeps_updated_at() -> None:
    url = "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
    release = {"tag_name": "latest", "assets": [{
        "name": "ffmpeg-master-latest-linux64-gpl.tar.xz", "browser_download_url": url,
        "digest": "sha256:" + "a" * 64, "updated_at": "2026-11-21T04:12:00Z",
    }]}
    asset = select_asset(release, "FFmpeg-Builds/FFmpeg-Builds", "linux64-gpl.tar.xz")
    assert asset.updated_at == "2026-11-21T04:12:00Z"


# ---------------------------------------------------------------------------
# Rapport de version (info-bulle, journal, system.tools)
# ---------------------------------------------------------------------------

def _bundle(tmp_path: Path, *, version: str, details: dict | None = None) -> Path:
    for name in ("ffmpeg", "ffprobe"):
        (tmp_path / name).write_bytes(name.encode())
        (tmp_path / name).chmod(0o755)
    manifest = ToolManifest()
    manifest.add("ffmpeg", version=version, source="https://example.invalid/ffmpeg.tar.xz",
                 verification="github-digest", files=[tmp_path / "ffmpeg", tmp_path / "ffprobe"], details=details)
    manifest.write(tmp_path)
    return tmp_path / "ffmpeg"


def test_report_of_bundled_tool_reads_manifest_without_process(tmp_path: Path) -> None:
    from core.tool_versions import tool_version_report

    binary = _bundle(tmp_path, version="N-129166-g1-20261121", details={
        "release": "FFmpeg-Builds/FFmpeg-Builds@latest", "asset_updated_at": "2026-11-21T04:12:00Z",
        "libavformat": "63.21.100", "ffprobe_build": "N-129166-g1-20261121",
    })
    report = tool_version_report("ffmpeg", binary, probe=False)
    assert report.bundled and report.version == "N-129166-g1-20261121"
    assert report.release == "FFmpeg-Builds/FFmpeg-Builds@latest"
    assert report.published == "2026-11-21T04:12:00Z"
    assert report.libraries == {"libavformat": "63.21.100"}
    assert report.label() == (
        "N-129166-g1-20261121 — embarqué (FFmpeg-Builds/FFmpeg-Builds@latest, 2026-11-21T04:12:00Z)"
    )
    assert tool_version_report("ffprobe", tmp_path / "ffprobe", probe=False).version == "N-129166-g1-20261121"


def test_report_of_older_manifest_keeps_release_and_probes_version(tmp_path: Path) -> None:
    from core.tool_versions import tool_version_report

    binary = _bundle(tmp_path, version="FFmpeg-Builds/FFmpeg-Builds@latest")
    report = tool_version_report("ffmpeg", binary, probe=False)
    assert report.bundled and report.version is None
    assert report.release == "FFmpeg-Builds/FFmpeg-Builds@latest"


def test_report_of_missing_tool(tmp_path: Path) -> None:
    from core.tool_versions import tool_version_report

    report = tool_version_report("ffmpeg", tmp_path / "absent", probe=False)
    assert report.path is None and report.label() == "introuvable"


def test_system_tools_versions_on_request(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from core.service.catalog import CATALOG
    from core.service.context import ExecutionContext
    from core.service.events import ListSink
    from core.service.operations import call

    binary = _bundle(tmp_path, version="N-1-g2-20261121", details={"release": "r@latest"})
    config = SimpleNamespace(tool_commands=lambda: {"ffmpeg": str(binary)})
    ctx = ExecutionContext.create(config, ListSink())
    assert call(CATALOG, "system.tools", {}, ctx).versions is None
    ctx = ExecutionContext.create(config, ListSink())
    versions = call(CATALOG, "system.tools", {"versions": True}, ctx).to_dict()["versions"]
    assert versions["ffmpeg"]["version"] == "N-1-g2-20261121"
    assert versions["ffmpeg"]["bundled"] is True
    assert versions["ffmpeg"]["release"] == "r@latest"


@pytest.mark.real_matroska_timestamp_scale_probe
def test_job_log_line_names_bundled_build(tmp_path: Path) -> None:
    from core.workflows.common.ffmpeg_runtime import ffmpeg_version_log_line

    binary = _bundle(tmp_path, version="N-7-g8-20261121", details={"release": "r@latest", "asset_updated_at": "d"})
    assert ffmpeg_version_log_line(binary) == "FFmpeg : N-7-g8-20261121 — embarqué (r@latest, d)"


def test_dashboard_tooltip_and_copied_versions(qt_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtGui import QGuiApplication

    from core.config import AppConfig
    from ui.main_window import DashboardPage

    _ = qt_app
    binary = _bundle(tmp_path, version="N-9-g9-20261121", details={"release": "r@latest", "asset_updated_at": "d"})
    monkeypatch.setattr(AppConfig, "tool_commands", lambda _self: {"ffmpeg": str(binary), "mediainfo": "absent-x"})
    logs: list[str] = []
    page = DashboardPage(AppConfig(), lambda message, *_a, **_kw: logs.append(message))
    page._executor.shutdown(wait=True)
    QCoreApplication.processEvents()
    tooltip = page._tool_badges["ffmpeg"].toolTip()
    assert "N-9-g9-20261121" in tooltip and "r@latest" in tooltip
    from core.i18n import translate_text

    assert page._tool_badges["mediainfo"].toolTip() == translate_text("{name} : introuvable", name="mediainfo")
    page._copy_tool_versions()
    copied = QGuiApplication.clipboard().text()
    assert "ffmpeg : N-9-g9-20261121 — embarqué (r@latest, d)" in copied
    assert logs
    page.deleteLater()
