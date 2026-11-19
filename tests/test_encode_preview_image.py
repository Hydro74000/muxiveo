"""Aperçus : sondes annulables et réglages « auto » résolus sur la configuration d'origine (audit C03)."""

from __future__ import annotations

import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from core.runner import TaskCancelledError, TaskSignals
from core.workflows.encode import EncodeWorkflow
from core.workflows.encode.models import EncodeConfig, RestoreSettings, VideoEncodeSettings


@pytest.mark.skipif(sys.platform == "win32", reason="sonde factice : script /bin/sh (POSIX)")
def test_preview_probe_is_killed_by_cancellation(qt_app, tmp_path):
    started = tmp_path / "started"
    probe = tmp_path / "ffprobe"
    probe.write_text(f"#!/bin/sh\ntouch '{started}'\nexec sleep 30\n", encoding="utf-8")
    probe.chmod(probe.stat().st_mode | stat.S_IXUSR)
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin=str(probe))
    signals = TaskSignals()

    def cancel_when_started() -> None:
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        signals.cancel()

    threading.Thread(target=cancel_when_started, daemon=True).start()
    begun = time.monotonic()
    with pytest.raises(TaskCancelledError):
        wf._preview_keyframe_at_or_before(tmp_path / "film.mkv", 0, 12.0, signals)
    assert time.monotonic() - begun < 5  # sonde tuée, pas attendue 30 s


def _config(tmp_path: Path, animation: str, tags: dict[str, str] | None) -> EncodeConfig:
    video = VideoEncodeSettings(codec="libx265", restoration=RestoreSettings(enabled=True, animation=animation))
    return EncodeConfig(source=tmp_path / "film.mkv", output=tmp_path / "out.mkv", video=video, video_tracks=[video],
                        audio_tracks=[], copy_subtitles=False, duration_s=60.0, tag_overrides=tags)


@pytest.mark.parametrize("animation, tags, source_genre, expected", [
    ("auto", {"GENRE": "Animation, Familial"}, None, "on"),   # genre de sortie (fiche TMDB du panneau)
    ("auto", None, "Animation", "on"),                         # genre de la source
    ("auto", None, None, "off"),
    ("on", None, None, "on"),                                  # choix explicites inchangés
    ("off", {"GENRE": "Animation"}, None, "off"),
])
def test_preview_resolves_animation_before_cut(qt_app, tmp_path, monkeypatch, animation, tags, source_genre,
                                               expected):
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg")
    payload = {"format": {"tags": {"GENRE": source_genre}}} if source_genre else {"format": {}}
    monkeypatch.setattr(wf, "_ffprobe_streams_payload", lambda _source: payload)
    preview = wf._preview_encode_config(_config(tmp_path, animation, tags), source_segment=tmp_path / "seg.mkv",
                                        output=tmp_path / "clip.mkv", clip_duration_s=2.0)
    assert preview.video is not None and preview.video.restoration.animation == expected
