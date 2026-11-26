"""
tests/test_hdr_studio_inspect_cancel.py — HDR Studio : annulation de l'inspection (D5 ; plan de remédiation 5.2.0,
lot 5).

Sonde FFprobe lente : « Annuler » libère le panneau aussitôt (A comme B), la sonde est arrêtée, son retour n'est
jamais affiché et une nouvelle source est acceptée.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from core.config import AppConfig
from tests.test_hdr_studio import _wait
from ui.panels.hdr_studio import HdrStudio

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="sonde factice en script POSIX")


def _slow_tool(tmp_path: Path) -> Path:
    tool = tmp_path / "slow-ffprobe"
    tool.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def _studio(tmp_path: Path) -> HdrStudio:
    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    config.tool_ffprobe = _slow_tool(tmp_path)
    config.tool_mediainfo = _slow_tool(tmp_path)
    return HdrStudio(config)


def _media(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x1a\x45\xdf\xa3")
    return path


def _idle(qt_app, studio: HdrStudio, timeout: float) -> bool:
    return _wait(qt_app, lambda: getattr(studio, "_inflight", 0) == 0, timeout)


def _close(qt_app, studio: HdrStudio) -> None:
    studio.close()
    _wait(qt_app, lambda: getattr(studio, "_shutdown", None) is not None and studio._shutdown.done.is_set(), 2.0)


@pytest.mark.parametrize("side", ["main", "compare"])
def test_cancel_frees_the_panel_and_kills_the_probe(qt_app, tmp_path, side):
    studio = _studio(tmp_path)
    try:
        if side == "main":
            studio.load_source(_media(tmp_path, "a.mkv"))
        else:
            studio.load_compare(_media(tmp_path, "b.mkv"))
        assert _wait(qt_app, lambda: studio._busy and studio._cancel_btn.isVisibleTo(studio), 2.0)
        started = time.monotonic()
        studio.cancel()
        assert not studio._busy and time.monotonic() - started < 1.0  # libre avant la fin de la sonde
        assert _idle(qt_app, studio, 10.0)  # sonde arrêtée (30 s sinon)
        assert studio._info is None and studio._report is None and studio._compare_info is None
        # Nouvelle source acceptée aussitôt.
        studio.load_source(_media(tmp_path, "c.mkv"))
        assert studio._busy and studio._inspection is not None
        studio.cancel()
        assert _idle(qt_app, studio, 10.0)
    finally:
        _close(qt_app, studio)


def test_cancelled_patch_still_publishes_its_terminal_state(qt_app, tmp_path):
    """Non-régression : jeton global intact, une correction annulée publie l'état « cancelled »."""
    studio = _studio(tmp_path)
    states: list[str] = []
    studio.op_state_changed.connect(lambda state, _message: states.append(state))
    try:
        studio._token = 7
        studio._patch_token = 7
        studio._on_patched(7, None, "")
        assert states == ["cancelled"]
    finally:
        _close(qt_app, studio)
