"""Tests du lecteur Qt Multimedia muet sur la console."""

from __future__ import annotations

from pathlib import Path

import ui.media_player as media_player


class _FakeLib:
    def __init__(self, calls: list[tuple[str, int]], path: str) -> None:
        self._calls, self._path = calls, path

    def av_log_set_level(self, level: int) -> None:
        self._calls.append((self._path, level))


def test_silence_libav_sets_quiet_level_on_first_loadable(monkeypatch) -> None:
    """La première libavutil chargeable reçoit AV_LOG_QUIET, les suivantes sont ignorées."""
    calls: list[tuple[str, int]] = []

    def fake_cdll(path: str) -> _FakeLib:
        if path.endswith("broken.so"):
            raise OSError("introuvable")
        return _FakeLib(calls, path)

    monkeypatch.setattr(
        media_player, "_libavutil_candidates",
        lambda: [Path("/x/broken.so"), Path("/x/libavutil.so.59"), Path("/y/libavutil.so.59")],
    )
    monkeypatch.setattr(media_player.ctypes, "CDLL", fake_cdll)
    media_player._silence_libav()
    assert calls == [(str(Path("/x/libavutil.so.59")), -8)]


def test_silence_libav_without_library_is_noop(monkeypatch) -> None:
    """Sans libavutil trouvée, rien n'échoue."""
    monkeypatch.setattr(media_player, "_libavutil_candidates", lambda: [])
    media_player._silence_libav()
