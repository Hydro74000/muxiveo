"""Aperçus : sondes annulables et réglages « auto » résolus sur la configuration d'origine (audit C03)."""

from __future__ import annotations

import json
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


def _fake_probe(format_csv: str, times: list[float], keys: set[float]):
    """Sonde factice : ``format`` en CSV, paquets (base 1/1000) compris dans la fenêtre demandée."""
    calls: list[str] = []

    def probe(cmd, _signals, timeout=0.0):
        if "-read_intervals" not in cmd:
            return format_csv
        interval = cmd[cmd.index("-read_intervals") + 1]
        calls.append(interval)
        low_s, high_s = interval.split("%")
        low, high = float(low_s), float(high_s or "inf")  # borne de fin absente : jusqu'à la fin
        packets = [{"pts": str(round(time * 1000)), "flags": "K__" if time in keys else "___"}
                   for time in times if low - 1e-9 <= time <= high]
        return json.dumps({"streams": [{"time_base": "1/1000"}], "packets": packets})

    return probe, calls


@pytest.mark.parametrize("format_csv, expected_reads", [
    ("0.000000,12.000000", 1),   # fin de la source dans la première fenêtre : une seule lecture
    ("0.000000,N/A", 5),         # durée inconnue : élargissement jusqu'à la dernière fenêtre
])
def test_probe_stops_widening_at_end_of_source(qt_app, tmp_path, monkeypatch, format_csv, expected_reads):
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg")
    probe, calls = _fake_probe(format_csv, [float(second) for second in range(12)], {0.0, 6.0})
    monkeypatch.setattr(wf, "_preview_probe", probe)
    keyframe, target, following = wf._preview_cut_points(tmp_path / "film.mkv", 0, 10.1, None)
    assert (keyframe, target, following) == (6, 11, None)
    assert len(calls) == expected_reads


def test_probe_widens_until_the_next_keyframe(qt_app, tmp_path, monkeypatch):
    """Images espacées de 20 s, image clé suivante à 120 s : seconde fenêtre (120 s), pas au-delà."""
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg")
    probe, calls = _fake_probe("0.000000,240.000000", [0, 80, 40, 20, 60, 100, 120, 200, 160, 140, 180, 220],
                               {0, 120})
    monkeypatch.setattr(wf, "_preview_probe", probe)
    assert wf._preview_cut_points(tmp_path / "film.mkv", 0, 0.1, None) == (0, 20, 120)
    assert len(calls) == 2


def test_underestimated_duration_reads_to_the_real_end(qt_app, tmp_path, monkeypatch):
    """Durée du conteneur sous-estimée (29 s, dernière image à 220 s) : lecture finale sans borne de fin, image de
    20 s atteinte (revue Codex)."""
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg")
    probe, calls = _fake_probe("0.000000,29.000000", [0, 80, 40, 20, 60, 100, 120, 200, 160, 140, 180, 220],
                               {0, 120})
    monkeypatch.setattr(wf, "_preview_probe", probe)
    assert wf._preview_cut_points(tmp_path / "film.mkv", 0, 0.1, None) == (0, 20, 120)
    assert calls == ["0.000000%"]


def test_keyframe_search_from_start_keeps_the_window(qt_app, tmp_path, monkeypatch):
    """Aucune image clé dans les 30 s précédant la cible : même fenêtre relue depuis le début, sans en consommer
    une plus large."""
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg")
    probe, calls = _fake_probe("0.000000,N/A", [float(second) for second in range(0, 80, 2)], {0.0, 70.0})
    monkeypatch.setattr(wf, "_preview_probe", probe)
    assert wf._preview_cut_points(tmp_path / "film.mkv", 0, 40.5, None) == (0, 42, 70)
    assert calls == ["10.500000%70.500000", "0.000000%70.500000"]
