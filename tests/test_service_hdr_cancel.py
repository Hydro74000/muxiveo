"""Annulation pendant la sonde initiale de ``hdr.inspect`` et ``hdr.patch`` (audit A04) : sonde tuée, état annulé,
aucun résultat ni fichier."""

from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.service import ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.context import Overrides
from core.service.errors import ErrorCode, error_code


def _slow_probe(tmp_path: Path) -> tuple[Path, Path]:
    """FFprobe qui signale son démarrage puis ne rend jamais la main (tué par le jeton)."""
    started = tmp_path / "probe.started"
    script = tmp_path / "ffprobe"
    script.write_text(f"#!/bin/sh\ntouch '{started}'\nexec sleep 30\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, started


def _ctx(tmp_path: Path, probe: Path) -> ExecutionContext:
    config = SimpleNamespace(app_data_dir=tmp_path / "data", display_peak_nits=1000, tool_ffmpeg="ffmpeg",
                             tool_dovi_tool="dovi_tool", tool_hdr10plus="hdr10plus_tool", ffmpeg_threads=2,
                             ensure_work_dir=lambda: tmp_path / "work")
    return ExecutionContext.create(config, ListSink(), overrides=Overrides(ffprobe=str(probe), mediainfo=str(probe)))


def _cancel_when_started(ctx: ExecutionContext, started: Path) -> list[float]:
    stamps: list[float] = []

    def watch() -> None:
        deadline = time.monotonic() + 20
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        stamps.append(time.monotonic())
        ctx.cancel.cancel()

    threading.Thread(target=watch, daemon=True).start()
    return stamps


@pytest.mark.parametrize("operation, payload", [
    ("hdr.inspect", lambda tmp: {"inputs": [str(tmp / "film.mkv")], "level": "static"}),
    ("hdr.patch", lambda tmp: {"source": str(tmp / "film.mkv"), "output": str(tmp / "fix.mkv"), "dry_run": True,
                               "light_levels": {"max_cll": 1000, "max_fall": 400}}),
    ("hdr.patch", lambda tmp: {"source": str(tmp / "film.mkv"), "output": str(tmp / "fix.mkv"),
                               "light_levels": {"max_cll": 1000, "max_fall": 400}}),
], ids=["inspect", "patch_dry_run", "patch_write"])
def test_cancel_during_initial_probe(tmp_path, operation, payload):
    (tmp_path / "film.mkv").write_bytes(b"\0" * 1024)
    probe, started = _slow_probe(tmp_path)
    ctx = _ctx(tmp_path, probe)
    stamps = _cancel_when_started(ctx, started)
    with pytest.raises(Exception) as caught:
        call(CATALOG, operation, payload(tmp_path), ctx)
    ended = time.monotonic()
    assert error_code(caught.value) == ErrorCode.CANCELLED
    assert stamps and ended - stamps[0] < 2.0  # sonde tuée, pas attendue jusqu'à son terme (30 s)
    assert not (tmp_path / "fix.mkv").exists()
    assert not [name for name in os.listdir(tmp_path) if name.endswith(".partial")]
