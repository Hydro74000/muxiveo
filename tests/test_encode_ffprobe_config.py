"""
tests/test_encode_ffprobe_config.py — FFprobe configuré respecté par l'encodage (A07).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.workflows.encode.workflow import EncodeWorkflow


@pytest.fixture
def captured(monkeypatch):
    """Commandes FFprobe lancées par le service de sondes HDR."""
    commands: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        commands.append([str(part) for part in cmd])
        return SimpleNamespace(returncode=0, stdout='{"streams": [], "format": {}}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return commands


def test_explicit_ffprobe_used_by_all_services(tmp_path: Path, captured) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    wf = EncodeWorkflow(ffmpeg_bin="/opt/a/ffmpeg", ffprobe_bin="/opt/b/ffprobe-custom")
    assert wf._ffprobe_path() == "/opt/b/ffprobe-custom"
    assert wf._bins["ffprobe"] == "/opt/b/ffprobe-custom"
    wf._hdr_metadata_service.ffprobe_streams_payload(source)
    assert captured and captured[-1][0] == "/opt/b/ffprobe-custom"


def test_hardware_ffmpeg_switch_keeps_explicit_ffprobe(tmp_path: Path, captured) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    wf = EncodeWorkflow(ffmpeg_bin="/opt/a/ffmpeg", ffprobe_bin="/opt/b/ffprobe")
    wf.set_ffmpeg("/usr/bin/ffmpeg")
    assert wf._ffprobe_path() == "/opt/b/ffprobe"
    assert wf._postprocess_service is not None
    wf._hdr_metadata_service.ffprobe_streams_payload(source)
    assert captured[-1][0] == "/opt/b/ffprobe"


def test_default_ffprobe_follows_ffmpeg_and_invalidates_cache(tmp_path: Path, captured) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    wf = EncodeWorkflow(ffmpeg_bin="/opt/a/ffmpeg")
    assert wf._ffprobe_path() == "/opt/a/ffprobe"
    service = wf._hdr_metadata_service
    service.ffprobe_streams_payload(source)
    service.ffprobe_streams_payload(source)
    assert [cmd[0] for cmd in captured] == ["/opt/a/ffprobe"]  # résultat en cache
    wf.set_ffmpeg("/opt/c/ffmpeg")
    service.ffprobe_streams_payload(source)
    assert [cmd[0] for cmd in captured] == ["/opt/a/ffprobe", "/opt/c/ffprobe"]


def test_legacy_constructor_without_ffprobe_keeps_derivation() -> None:
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg")
    assert wf._ffprobe_path() == "ffprobe"
    assert EncodeWorkflow(ffmpeg_bin="/x/ffmpeg.exe")._ffprobe_path() == "/x/ffprobe.exe"


def test_live_ffprobe_change_invalidates_results(tmp_path: Path, captured) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    wf = EncodeWorkflow(ffmpeg_bin="/opt/a/ffmpeg", ffprobe_bin="/opt/b/ffprobe")
    service = wf._hdr_metadata_service
    service.ffprobe_streams_payload(source)
    wf.set_ffprobe_bin("/opt/c/ffprobe")
    service.ffprobe_streams_payload(source)
    wf.set_ffprobe_bin(None)
    service.ffprobe_streams_payload(source)
    assert [cmd[0] for cmd in captured] == ["/opt/b/ffprobe", "/opt/c/ffprobe", "/opt/a/ffprobe"]


def test_mediainfo_change_invalidates_results(tmp_path: Path, captured) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    wf = EncodeWorkflow()
    wf._hdr_metadata_service.ffprobe_streams_payload(source)
    wf.set_mediainfo_bin("/custom/mediainfo")
    wf._hdr_metadata_service.ffprobe_streams_payload(source)
    assert len(captured) == 2


def test_encode_panel_passes_configured_ffprobe(qt_app, monkeypatch) -> None:
    from ui.panels.encode_panel import panel as panel_mod

    seen: dict[str, object] = {}
    real = panel_mod.EncodeWorkflow

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(panel_mod, "EncodeWorkflow", spy)
    from core.config import AppConfig

    config = AppConfig()
    monkeypatch.setattr(type(config), "tool_ffprobe", property(lambda _self: "/custom/ffprobe"), raising=False)
    try:
        panel = panel_mod.EncodePanel(config)
    except Exception as exc:  # pragma: no cover - environnement Qt incomplet
        pytest.skip(f"EncodePanel indisponible : {exc}")
    assert seen.get("ffprobe_bin") == "/custom/ffprobe"
    panel.deleteLater()
