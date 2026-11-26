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
    """Sonde factice : note son démarrage (une ligne par lancement) puis dort 30 s."""
    tool = tmp_path / "slow-ffprobe"
    marker = tmp_path / "probes.log"
    tool.write_text(f"#!/bin/sh\necho started >> '{marker}'\nsleep 30\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def _started(tmp_path: Path) -> int:
    marker = tmp_path / "probes.log"
    return len(marker.read_text(encoding="utf-8").splitlines()) if marker.is_file() else 0


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
        assert _wait(qt_app, lambda: _started(tmp_path) == 1, 5.0)  # sonde réellement lancée
        assert studio._busy and studio._cancel_btn.isVisibleTo(studio)
        started = time.monotonic()
        studio._cancel_btn.click()
        assert not studio._busy and time.monotonic() - started < 1.0  # libre avant la fin de la sonde
        assert studio._info is None and studio._report is None and studio._compare_info is None
        # Nouvelle source acceptée aussitôt ; sa sonde démarre seulement si la précédente a été arrêtée
        # (fil de travail unique, sonde de 30 s sinon).
        studio.load_source(_media(tmp_path, "c.mkv"))
        assert studio._busy and studio._inspection is not None
        assert _wait(qt_app, lambda: _started(tmp_path) == 2, 5.0)
        studio._cancel_btn.click()
        assert _idle(qt_app, studio, 10.0) and studio._info is None
    finally:
        _close(qt_app, studio)


def test_b_track_change_during_a_new_b_inspection_is_ignored(qt_app, tmp_path):
    from tests.test_hdr_inspection import _info, _video

    studio = _studio(tmp_path)
    try:
        old = _info(tmp_path / "ancien.mkv")
        old.video_tracks = [_video(index=0), _video(index=1)]
        studio._compare_info = old
        studio._compare_tab.set_tracks(old.path, [(0, "#0"), (1, "#1")], 0)
        assert studio._compare_tab.track.isEnabled()
        studio.load_compare(_media(tmp_path, "b.mkv"))
        assert _wait(qt_app, lambda: _started(tmp_path) == 1, 5.0)
        assert not studio._compare_tab.track.isEnabled()  # piste de B figée pendant l'inspection
        studio.compare_track_changed(1)
        assert studio._busy and studio._inspection is not None  # changement ignoré, inspection intacte
        studio._cancel_btn.click()
        assert not studio._busy and _idle(qt_app, studio, 10.0)
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
