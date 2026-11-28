"""Aperçus : sondes annulables et réglages « auto » résolus sur la configuration d'origine (audit C03)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from core.runner import TaskCancelledError, TaskSignals
from core.workflows.encode import EncodeWorkflow
from core.workflows.encode.models import EncodeConfig, RestoreSettings, VideoEncodeSettings
from tests._slow_tool import slow_tool


def test_preview_probe_is_killed_by_cancellation(qt_app, tmp_path):
    probe, started = slow_tool(tmp_path)
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


def _fake_probe(stream: dict[str, str], times: list[float], keys: set[float], *, start_time: str = "0.000000"):
    """FFprobe factice : flux ``stream`` (cadences), paquets ``times`` en ordre de décodage (base 1/1000). La lecture
    ``début%+#N`` part du premier paquet d'instant ≥ début (conteneur sans index) et rend au plus N paquets."""
    calls: list[str] = []

    def probe(cmd, _signals, timeout=0.0):
        if "-read_intervals" not in cmd:
            return json.dumps({"format": {"start_time": start_time, "duration": "29.000000"}, "streams": [stream]})
        interval = cmd[cmd.index("-read_intervals") + 1]
        calls.append(interval)
        low_s, count_s = interval.split("%+#")  # jamais « début% » : FFprobe y rend parfois zéro paquet (Windows)
        first = next((index for index, time in enumerate(times) if time >= float(low_s) - 1e-9), len(times))
        packets = [{"pts": str(round(time * 1000)), "flags": "K__" if time in keys else "___"}
                   for time in times[first:first + int(count_s)]]
        return json.dumps({"streams": [{"time_base": "1/1000"}], "packets": packets})

    return probe, calls


def _cut_points(tmp_path, monkeypatch, probe, time_s: float):
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg")
    monkeypatch.setattr(wf, "_preview_probe", probe)
    return wf._preview_cut_points(tmp_path / "film.mkv", 0, time_s, None)


SPARSE_VFR = [0, 80, 40, 20, 60, 100, 120, 200, 160, 140, 180, 220]  # images B, une image toutes les 20 s


def test_probe_stops_at_end_of_stream(qt_app, tmp_path, monkeypatch):
    """Moins de paquets rendus que demandés : fin du flux constatée, une seule lecture, pas d'image clé suivante."""
    probe, calls = _fake_probe({"avg_frame_rate": "1/1"}, [float(second) for second in range(12)], {0.0, 6.0})
    assert _cut_points(tmp_path, monkeypatch, probe, 10.1) == (6, 11, None)
    assert calls == ["0.000000%+#41"]  # 1 i/s sur 10,1 s + 30 s


def test_probe_doubles_until_the_next_keyframe(qt_app, tmp_path, monkeypatch):
    """Image clé suivante à 120 s, lue au troisième doublement : image de 20 s (première ≥ 0,1 s)."""
    probe, calls = _fake_probe({"avg_frame_rate": "1/20"}, SPARSE_VFR, {0, 120})
    assert _cut_points(tmp_path, monkeypatch, probe, 0.1) == (0, 20, 120)
    assert calls == ["0.000000%+#2", "0.000000%+#4", "0.000000%+#8"]


def test_unknown_rate_and_wrong_duration_do_not_stop_the_search(qt_app, tmp_path, monkeypatch):
    """Cadence inconnue (0/0) et durée du conteneur sous-estimée (29 s, dernière image à 220 s) : sans effet sur la
    recherche, fondée sur les paquets réellement lus (revue Codex)."""
    probe, calls = _fake_probe({"avg_frame_rate": "0/0", "r_frame_rate": "0/0"}, SPARSE_VFR, {0, 120})
    assert _cut_points(tmp_path, monkeypatch, probe, 0.1) == (0, 20, 120)
    assert calls == ["0.000000%+#31"]


def test_keyframe_search_from_start(qt_app, tmp_path, monkeypatch):
    """Lecture tombée après la dernière image clé ≤ instant (conteneur sans index) : relue depuis le début, avec
    assez de paquets pour couvrir la fenêtre."""
    probe, calls = _fake_probe({"avg_frame_rate": "1/2"}, [float(second) for second in range(0, 80, 2)], {0.0, 70.0})
    assert _cut_points(tmp_path, monkeypatch, probe, 40.5) == (0, 42, 70)
    assert calls == ["10.500000%+#30", "0.000000%+#36"]


def test_start_time_offset(qt_app, tmp_path, monkeypatch):
    """Départ du conteneur à 5 s : lecture demandée en instants du conteneur, résultats relatifs à la source."""
    probe, calls = _fake_probe({"avg_frame_rate": "1/1"}, [5.0 + second for second in range(12)], {5.0, 11.0},
                               start_time="5.000000")
    assert _cut_points(tmp_path, monkeypatch, probe, 2.5) == (0, 3, 6)
    assert calls == ["5.000000%+#33"]
