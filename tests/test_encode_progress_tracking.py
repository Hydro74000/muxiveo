"""Progression d'encodage partagée GUI / API (``core/workflows/encode/progress_tracking.py``).

Les valeurs attendues des scénarios ont été relevées sur la logique de la GUI avant son déplacement dans le cœur
(``ui/main_window.py``) : le suivi doit les reproduire à l'identique (barre, libellé, fps, ETA).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.workflows.encode.backends.models import ProgressEvent
from core.workflows.encode.progress_tracking import (
    ENCODE_INTERNAL_PROGRESS_PREFIX,
    EncodeProgressTracker,
    ProgressSnapshot,
    progress_target,
    progress_targets,
)


def _fmt_eta(seconds: float) -> str:  # rendu de la GUI (ui/panels/encode_panel/theme.py)
    s = int(seconds)
    m, s = divmod(s, 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _label(snapshot: ProgressSnapshot) -> str:
    fps = f"{snapshot.fps:.1f} fps" if snapshot.fps is not None and snapshot.fps > 0 else ""
    eta = f"ETA {_fmt_eta(snapshot.eta_s)}" if snapshot.eta_s is not None else ""
    return "  ·  ".join(part for part in (snapshot.pct_label, fps, eta) if part)


def _replay(tracker: EncodeProgressTracker, lines: list[tuple[float, str]],
            events: dict[str, ProgressEvent] | None = None) -> list[tuple[int, str]]:
    """Comme la GUI : la barre et le libellé gardent leur dernière valeur quand une ligne n'apporte rien."""
    shown = (0, "")
    out = []
    for now, line in lines:
        if line.startswith("$ "):
            tracker.command_started()
        elif events and line in events:
            snapshot = tracker.on_event(events[line], now)
            shown = (snapshot.pct if snapshot.pct is not None else shown[0], _label(snapshot) or shown[1])
        else:
            snapshot = tracker.on_line(line, now).snapshot
            if snapshot is not None:
                shown = (snapshot.pct or 0, _label(snapshot))
        out.append(shown)
    return out


def test_single_command_from_raw_ffmpeg_lines_matches_the_gui():
    lines = [(float(i), f"frame={i * 48} fps=48.0 q=28.0 size=1kB time=00:00:{i * 2:02d}.00 bitrate=1k speed=2x")
             for i in range(1, 8)]
    assert _replay(EncodeProgressTracker(duration_s=100.0, total_frames=2400), lines) == [
        (2, "2%  ·  48.0 fps"), (4, "4%  ·  48.0 fps  ·  ETA 48s"), (6, "6%  ·  48.0 fps  ·  ETA 47s"),
        (8, "8%  ·  48.0 fps  ·  ETA 46s"), (10, "10%  ·  48.0 fps  ·  ETA 45s"), (12, "12%  ·  48.0 fps  ·  ETA 44s"),
        (14, "14%  ·  48.0 fps  ·  ETA 43s")]


def test_frames_only_use_the_reported_speed_then_the_smoothed_one():
    with_fps = [(float(i), f"frame={i * 240} fps=24.0 q=28.0") for i in range(1, 6)]
    assert _replay(EncodeProgressTracker(total_frames=2400), with_fps) == [
        (10, "10%  ·  24.0 fps  ·  ETA 1m 30s"), (20, "20%  ·  24.0 fps  ·  ETA 1m 20s"),
        (30, "30%  ·  24.0 fps  ·  ETA 1m 10s"), (40, "40%  ·  24.0 fps  ·  ETA 1m 00s"),
        (50, "50%  ·  24.0 fps  ·  ETA 50s")]
    without_fps = [(float(i), f"frame={i * 240} q=28.0") for i in range(1, 6)]
    assert _replay(EncodeProgressTracker(total_frames=2400), without_fps) == [
        (10, "10%"), (20, "20%  ·  ETA 8s"), (30, "30%  ·  ETA 7s"), (40, "40%  ·  ETA 6s"), (50, "50%  ·  ETA 5s")]


def test_a_new_command_restarts_the_counters():
    lines = [(1.0, "frame=10 fps=5.0 time=00:00:10.00"), (2.0, "frame=20 fps=5.0 time=00:00:20.00"),
             (3.0, "$ ffmpeg -i b.mkv"), (4.0, "frame=30 time=00:00:30.00"), (5.0, "frame=40 time=00:00:40.00")]
    assert _replay(EncodeProgressTracker(duration_s=100.0), lines) == [
        (10, "10%  ·  5.0 fps"), (20, "20%  ·  5.0 fps  ·  ETA 8s"), (20, "20%  ·  5.0 fps  ·  ETA 8s"), (30, "30%"),
        (40, "40%  ·  ETA 6s")]


def test_backend_events_including_a_percent_from_rife():
    events = {f"E{i}": ProgressEvent(raw_line=f"E{i}", elapsed_seconds=float(i * 10), fps=30.0, frame=i * 300,
                                     should_log=False) for i in range(1, 6)}
    events["R1"] = ProgressEvent(raw_line="R1", percent=42.4, fps=25.0, eta_seconds=3600.0, should_log=False)
    lines = [(float(i), f"E{i}") for i in range(1, 6)] + [(6.0, "R1")]
    assert _replay(EncodeProgressTracker(duration_s=100.0, total_frames=1500), lines, events) == [
        (10, "10%  ·  30.0 fps  ·  ETA 40s"), (20, "20%  ·  30.0 fps  ·  ETA 8s"), (30, "30%  ·  30.0 fps  ·  ETA 7s"),
        (40, "40%  ·  30.0 fps  ·  ETA 6s"), (50, "50%  ·  30.0 fps  ·  ETA 5s"), (42, "42.4%  ·  25.0 fps  ·  ETA 60m 00s")]
    frames = {f"F{i}": ProgressEvent(raw_line=f"F{i}", frame=i * 240, should_log=False) for i in range(1, 6)}
    assert _replay(EncodeProgressTracker(total_frames=2400), [(float(i), f"F{i}") for i in range(1, 6)], frames) == [
        (10, "10%"), (20, "20%  ·  ETA 8s"), (30, "30%  ·  ETA 7s"), (40, "40%  ·  ETA 6s"), (50, "50%  ·  ETA 5s")]


def test_two_tracks_two_passes_follow_the_longest_remaining():
    tracker = EncodeProgressTracker(targets={
        1: {"duration_s": 600.0, "total_frames": None, "source_name": "a.mkv"},
        2: {"duration_s": 1200.0, "total_frames": 28800, "source_name": "b.mkv"},
    })
    sequence = [
        (10.0, "ffmpeg-video-1-pass1", "frame=100 fps=50.0 out_time=00:00:30.000000"),
        (12.0, "ffmpeg-video-2", "frame=480 fps=24.0 out_time=00:00:20.000000"),
        (20.0, "ffmpeg-video-1-pass1", "frame=200 fps=50.0 out_time=00:01:00.000000"),
        (22.0, "ffmpeg-video-2", "frame=960 fps=24.0 out_time=00:00:40.000000"),
        (30.0, "ffmpeg-video-1-pass1", None),
        (31.0, "ffmpeg-video-1-pass2", "frame=100 fps=40.0 out_time=00:00:10.000000"),
        (40.0, "ffmpeg-video-2", "frame=1440 fps=24.0"),
        (50.0, "ffmpeg-video-2", None),
        (60.0, "ffmpeg-video-1-pass2", "frame=900 fps=40.0 out_time=00:05:00.000000"),
    ]
    shown: list[tuple[Any, ...]] = []
    for now, label, raw in sequence:
        tracker.state(label, now)
        if raw is None:
            tracker.internal_done(label, now)
        else:
            tracker.internal_line(label, raw, now)
        snap = tracker.multi_snapshot(now)
        assert snap is not None
        shown.append((snap.track, snap.pass_index, snap.pct, _label(snap)))
    # Relevé de la GUI : « Piste vidéo 1/2 · passe 1/2 · 5% · 50.0 fps », etc.
    assert shown == [
        (1, 1, 5, "5%  ·  50.0 fps"), (2, None, 1, "1%  ·  24.0 fps"), (1, 1, 10, "10%  ·  50.0 fps  ·  ETA 2m 24s"),
        (1, 1, 10, "10%  ·  50.0 fps  ·  ETA 2m 24s"), (2, None, 3, "3%  ·  24.0 fps  ·  ETA 1m 56s"),
        (2, None, 3, "3%  ·  24.0 fps  ·  ETA 1m 56s"), (2, None, 3, "3%  ·  24.0 fps  ·  ETA 1m 56s"),
        (1, 2, 1, "1%  ·  40.0 fps"), (1, 2, 50, "50%  ·  40.0 fps  ·  ETA 10s")]
    assert tracker.multi_snapshot(61.0).track_count == 2  # type: ignore[union-attr]


def test_progress_targets_use_track_then_file_then_config_duration():
    video_a = SimpleNamespace(index=0, duration_s=120.0)
    info_a = SimpleNamespace(video_tracks=[video_a], primary_video=video_a, duration_s=125.0, frame_count=2880)
    info_b = SimpleNamespace(video_tracks=[], primary_video=None, duration_s=90.0, frame_count=None)
    config = SimpleNamespace(source=Path("/m/a.mkv"), duration_s=60.0, video_tracks=[
        SimpleNamespace(source_path="/m/a.mkv", stream_index=0),
        SimpleNamespace(source_path="/m/b.mkv", stream_index=3),
        SimpleNamespace(source_path="/m/c.mkv", stream_index=0),
    ])
    infos = {Path("/m/a.mkv"): info_a, Path("/m/b.mkv"): info_b}
    assert progress_targets(config, infos.get) == {
        1: {"duration_s": 120.0, "total_frames": 2880, "source_name": "a.mkv"},
        2: {"duration_s": 90.0, "total_frames": None, "source_name": "b.mkv"},
        3: {"duration_s": 60.0, "total_frames": None, "source_name": "c.mkv"},
    }
    assert progress_target(None, None, None, "x.mkv")["duration_s"] is None


def test_run_encode_publishes_progress_in_computation_order_across_threads(qapp, monkeypatch, tmp_path):
    """Deux préparations vidéo publient en même temps : lecture → calcul → publication sérialisés."""
    from PySide6.QtCore import QObject, Signal

    from core.jobs import encode_runtime
    from core.jobs.options import CommonOptions
    from core.runner import TaskSignals
    from core.service.context import CancelToken

    computed: list[int] = []
    real_track = encode_runtime.track_progress_line

    def slow_first(tracker, line, now, parse):
        snapshot = real_track(tracker, line, now, parse)
        if snapshot is not None:
            computed.append(snapshot.pct or 0)
            if line.endswith("time=00:00:10.00"):
                time.sleep(0.05)  # fenêtre où l'autre fil calculerait et publierait avant celui-ci
        return snapshot

    monkeypatch.setattr(encode_runtime, "track_progress_line", slow_first)
    monkeypatch.setattr(encode_runtime, "prepare_vulkan", lambda *_a, **_k: None)
    monkeypatch.setattr(encode_runtime, "align_dovi_nvenc", lambda config, *_a, **_k: config)
    monkeypatch.setattr(encode_runtime, "_progress_tracker",
                        lambda *_a, **_k: encode_runtime.EncodeProgressTracker(duration_s=100.0))

    class _Wf(QObject):
        log_message = Signal(str, str)
        failed_work_dir_retained = Signal(object)

        def validate(self, _config):
            return []

        def parse_progress(self, _config, _line):
            return None

        def run(self, _config, *, validate, retain_failed_work_dir):
            signals = TaskSignals()

            def emit(text: str) -> None:
                signals.progress.emit(text)

            def work() -> None:
                time.sleep(0.05)  # abonnement à la progression après le lancement, comme le workflow
                threads = [threading.Thread(target=emit, args=(f"frame=1 time=00:00:{s:02d}.00",)) for s in (10, 20)]
                for thread in threads:
                    thread.start()
                    time.sleep(0.01)
                for thread in threads:
                    thread.join()
                signals.finished.emit("fini")

            threading.Thread(target=work).start()
            return signals

    published: list[int] = []

    def job() -> None:  # fil de job d'un service : lignes relayées directement depuis les fils émetteurs
        encode_runtime.run_encode(SimpleNamespace(output=tmp_path / "out.mkv"),
                                  config=SimpleNamespace(tool_ffmpeg="ffmpeg"), options=CommonOptions(),
                                  logger=SimpleNamespace(emit=lambda *a, **k: None), cancel=CancelToken(),
                                  workflow_factory=_Wf,  # type: ignore[arg-type]
                                  progress=lambda **fields: published.append(fields["pct"]))

    runner = threading.Thread(target=job)
    runner.start()
    runner.join(10)
    assert sorted(computed) == [10, 20] and published == computed


def test_internal_progress_lines_feed_the_multi_track_state():
    from core.jobs.encode_runtime import progress_fields, track_progress_line

    tracker = EncodeProgressTracker(targets={1: {"duration_s": 100.0, "total_frames": None, "source_name": "a"},
                                             2: {"duration_s": 50.0, "total_frames": None, "source_name": "b"}})

    def internal(label: str, line: str, event: str = "line") -> str:
        return ENCODE_INTERNAL_PROGRESS_PREFIX + json.dumps({"kind": "encode_ffmpeg", "label": label, "event": event,
                                                             "line": line})

    parse = lambda _line: None  # noqa: E731
    assert track_progress_line(tracker, internal("ffmpeg-video-1", "$ ffmpeg …"), 1.0, parse) is None
    snapshot = track_progress_line(tracker, internal("ffmpeg-video-2-pass1", "fps=20.0 out_time=00:00:10.000000"),
                                   2.0, parse)
    assert snapshot is not None
    assert progress_fields(snapshot) == {"pct": 20, "fps": 20.0, "track": 2, "track_count": 2, "pass_index": 1,
                                         "pass_count": 2}
    assert track_progress_line(tracker, internal("autre", "x"), 3.0, parse) is None
    assert track_progress_line(tracker, "$ ffmpeg -i a.mkv", 4.0, parse) is None


def test_run_encode_publishes_only_informative_changes(qapp, monkeypatch, tmp_path):
    """Une clé FFmpeg par ligne (``bitrate=``, ``out_time_ms=``…) : seuls les changements utiles sont publiés."""
    from PySide6.QtCore import QObject, Signal

    from core.jobs import encode_runtime
    from core.jobs.options import CommonOptions
    from core.runner import TaskSignals
    from core.service.context import CancelToken

    monkeypatch.setattr(encode_runtime, "prepare_vulkan", lambda *_a, **_k: None)
    monkeypatch.setattr(encode_runtime, "align_dovi_nvenc", lambda config, *_a, **_k: config)
    monkeypatch.setattr(encode_runtime, "_progress_tracker",
                        lambda *_a, **_k: encode_runtime.EncodeProgressTracker(duration_s=100.0))
    lines = ["bitrate=1000k", "fps=0.0", "out_time=00:00:10.000000", "out_time_us=10000000", "speed=1.0x",
             "fps=24.0", "out_time=00:00:20.000000"]

    class _Wf(QObject):
        log_message = Signal(str, str)
        failed_work_dir_retained = Signal(object)

        def validate(self, _config):
            return []

        def parse_progress(self, _config, _line):
            return None

        def run(self, _config, *, validate, retain_failed_work_dir):
            signals = TaskSignals()

            def work() -> None:
                time.sleep(0.05)  # abonnement à la progression après le lancement, comme le workflow
                for line in lines:
                    signals.progress.emit(line)
                signals.finished.emit("fini")

            threading.Thread(target=work).start()
            return signals

    published: list[dict[str, Any]] = []
    encode_runtime.run_encode(SimpleNamespace(output=tmp_path / "out.mkv"), config=SimpleNamespace(tool_ffmpeg="ffmpeg"),
                              options=CommonOptions(), logger=SimpleNamespace(emit=lambda *a, **k: None),
                              cancel=CancelToken(), workflow_factory=_Wf,  # type: ignore[arg-type]
                              progress=lambda **fields: published.append(fields))
    # fps=0 : inconnue ; out_time_us : même pourcentage ; bitrate / speed : rien ; ETA : pas encore mesurable.
    assert published == [{"pct": 10}, {"pct": 20, "fps": 24.0}]
