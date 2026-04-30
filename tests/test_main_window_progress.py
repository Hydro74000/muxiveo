from __future__ import annotations

import json
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock, patch

from ui.main_window import (
    MainWindow,
    _ENCODE_INTERNAL_PROGRESS_PREFIX,
    _multi_encode_remaining_seconds,
    _select_multi_encode_label,
)


class _FakeProgressBar:
    def __init__(self) -> None:
        self.value = 0

    def setValue(self, value: int) -> None:
        self.value = value


class _FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, value: str) -> None:
        self.text = value


class _FakeTimer:
    def __init__(self) -> None:
        self.active = False

    def isActive(self) -> bool:
        return self.active

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False


def test_select_multi_encode_label_prefers_longest_remaining_and_reswitches() -> None:
    # Stream 1 : 300s média en 100s wall (3.0x), reste 300s média → 100s wall.
    state_1 = {
        "started_at": 0.0,
        "duration_s": 600.0,
        "done": False,
        "last_update": 90.0,
    }
    # Stream 2 : 100s média en 100s wall (1.0x), reste 500s média → 500s wall.
    state_2 = {
        "started_at": 0.0,
        "duration_s": 600.0,
        "done": False,
        "last_update": 95.0,
    }

    # Deux échantillons par tracker pour amorcer l'EWMA (delta requis >= 0.5s).
    state_1["elapsed_video"] = 30.0
    _multi_encode_remaining_seconds(state_1, 10.0)
    state_1["elapsed_video"] = 300.0
    state_2["elapsed_video"] = 10.0
    _multi_encode_remaining_seconds(state_2, 10.0)
    state_2["elapsed_video"] = 100.0

    eta_1 = _multi_encode_remaining_seconds(state_1, 100.0)
    eta_2 = _multi_encode_remaining_seconds(state_2, 100.0)
    assert eta_1 is not None and abs(eta_1 - 100.0) < 1e-6
    assert eta_2 is not None and abs(eta_2 - 500.0) < 1e-6

    states = {"ffmpeg-video-1": state_1, "ffmpeg-video-2": state_2}
    assert _select_multi_encode_label(states, "ffmpeg-video-1", 100.0) == "ffmpeg-video-2"

    state_2["done"] = True
    assert _select_multi_encode_label(states, "ffmpeg-video-2", 100.0) == "ffmpeg-video-1"


def test_handle_encode_internal_progress_updates_longest_remaining_bar_and_legend() -> None:
    dummy = SimpleNamespace()
    dummy._running = True
    dummy._op_mode = "encode"
    dummy._op_encode_multi_targets = {
        1: {"duration_s": 600.0, "total_frames": None, "source_name": "a.mkv"},
        2: {"duration_s": 1200.0, "total_frames": None, "source_name": "b.mkv"},
    }
    dummy._op_encode_multi_state = {}
    dummy._op_encode_multi_active_label = None
    dummy._op_encode_multi_reselect_timer = _FakeTimer()
    dummy._prog_bar = _FakeProgressBar()
    dummy._prog_lbl = _FakeLabel()
    dummy.log_requested = SimpleNamespace(emit=MagicMock())
    dummy._stop_prep_progress = MagicMock()

    dummy._op_stage_label = ""
    dummy._ensure_multi_encode_state = MethodType(MainWindow._ensure_multi_encode_state, dummy)
    dummy._multi_encode_progress_parts = MethodType(MainWindow._multi_encode_progress_parts, dummy)
    dummy._reevaluate_multi_encode_progress = MethodType(MainWindow._reevaluate_multi_encode_progress, dummy)
    dummy._handle_encode_internal_progress = MethodType(MainWindow._handle_encode_internal_progress, dummy)
    dummy._format_progress_label = MethodType(MainWindow._format_progress_label, dummy)

    line_track_1 = _ENCODE_INTERNAL_PROGRESS_PREFIX + json.dumps(
        {"kind": "encode_ffmpeg", "label": "ffmpeg-video-1", "event": "line", "line": "out_time=00:05:00.000000"},
        ensure_ascii=False,
    )
    line_track_2 = _ENCODE_INTERNAL_PROGRESS_PREFIX + json.dumps(
        {"kind": "encode_ffmpeg", "label": "ffmpeg-video-2", "event": "line", "line": "out_time=00:02:00.000000"},
        ensure_ascii=False,
    )
    done_track_2 = _ENCODE_INTERNAL_PROGRESS_PREFIX + json.dumps(
        {"kind": "encode_ffmpeg", "label": "ffmpeg-video-2", "event": "done", "line": ""},
        ensure_ascii=False,
    )

    with patch("ui.main_window.time.monotonic", return_value=300.0):
        assert dummy._handle_encode_internal_progress(line_track_1) is True
    with patch("ui.main_window.time.monotonic", return_value=600.0):
        assert dummy._handle_encode_internal_progress(line_track_2) is True
    dummy._op_encode_multi_state["ffmpeg-video-2"]["started_at"] = 0.0
    with patch("ui.main_window.time.monotonic", return_value=600.0):
        dummy._reevaluate_multi_encode_progress()

    assert dummy._op_encode_multi_active_label == "ffmpeg-video-2"
    assert dummy._prog_bar.value == 10
    assert "Piste vidéo 2/2" in dummy._prog_lbl.text
    assert "10%" in dummy._prog_lbl.text

    with patch("ui.main_window.time.monotonic", return_value=601.0):
        assert dummy._handle_encode_internal_progress(done_track_2) is True

    assert dummy._op_encode_multi_active_label == "ffmpeg-video-1"
    assert dummy._prog_bar.value == 50
    assert "Piste vidéo 1/2" in dummy._prog_lbl.text


def test_capture_verbose_progress_line_records_wrapped_tool_output(tmp_path) -> None:
    dummy = SimpleNamespace()
    dummy._config = SimpleNamespace(
        enable_file_logging=True,
        file_logging_level="verbose",
        app_data_dir=tmp_path,
        verbose_log_dir=tmp_path / "chosen_logs",
    )
    dummy._op_mode = "encode"
    dummy._log_panel = SimpleNamespace(log=MagicMock())
    dummy._verbose_log_file_path = None
    dummy._verbose_log_session_stamp = None
    dummy._verbose_log_file_index = 1
    dummy._verbose_log_file_error_reported = False
    dummy._NOISE_RE = MainWindow._NOISE_RE
    dummy._encode_panel = SimpleNamespace(get_total_frames=lambda: None)

    dummy._verbose_log_part_path = MethodType(MainWindow._verbose_log_part_path, dummy)
    dummy._verbose_log_session_path = MethodType(MainWindow._verbose_log_session_path, dummy)
    dummy._prepare_verbose_log_target = MethodType(MainWindow._prepare_verbose_log_target, dummy)
    dummy._append_verbose_tool_output = MethodType(MainWindow._append_verbose_tool_output, dummy)
    dummy._capture_verbose_progress_line = MethodType(MainWindow._capture_verbose_progress_line, dummy)

    wrapped = _ENCODE_INTERNAL_PROGRESS_PREFIX + json.dumps(
        {"kind": "encode_ffmpeg", "label": "ffmpeg-video-1", "event": "line", "line": "out_time=00:00:05.000000"},
        ensure_ascii=False,
    )

    dummy._capture_verbose_progress_line(wrapped)

    log_files = sorted((tmp_path / "chosen_logs").glob("mediarecode-verbose-*.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8")
    assert "[TOOL] [ffmpeg-video-1] out_time=00:00:05.000000" in content


def test_capture_verbose_progress_line_records_remux_ffmpeg_progress(tmp_path) -> None:
    dummy = SimpleNamespace()
    dummy._config = SimpleNamespace(
        enable_file_logging=True,
        file_logging_level="verbose",
        app_data_dir=tmp_path,
        verbose_log_dir=tmp_path / "chosen_logs",
    )
    dummy._op_mode = "remux"
    dummy._log_panel = SimpleNamespace(log=MagicMock())
    dummy._verbose_log_file_path = None
    dummy._verbose_log_session_stamp = None
    dummy._verbose_log_file_index = 1
    dummy._verbose_log_file_error_reported = False
    dummy._NOISE_RE = MainWindow._NOISE_RE

    dummy._verbose_log_part_path = MethodType(MainWindow._verbose_log_part_path, dummy)
    dummy._verbose_log_session_path = MethodType(MainWindow._verbose_log_session_path, dummy)
    dummy._prepare_verbose_log_target = MethodType(MainWindow._prepare_verbose_log_target, dummy)
    dummy._append_verbose_tool_output = MethodType(MainWindow._append_verbose_tool_output, dummy)
    dummy._capture_verbose_progress_line = MethodType(MainWindow._capture_verbose_progress_line, dummy)

    dummy._capture_verbose_progress_line("out_time=00:00:10.000000")

    log_files = sorted((tmp_path / "chosen_logs").glob("mediarecode-verbose-*.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8")
    assert "[TOOL] out_time=00:00:10.000000" in content


def test_on_tool_output_requested_records_inspector_verbose_lines(tmp_path) -> None:
    dummy = SimpleNamespace()
    dummy._config = SimpleNamespace(
        enable_file_logging=True,
        file_logging_level="verbose",
        app_data_dir=tmp_path,
        verbose_log_dir=tmp_path / "chosen_logs",
    )
    dummy._log_panel = SimpleNamespace(log=MagicMock())
    dummy._verbose_log_file_path = None
    dummy._verbose_log_session_stamp = None
    dummy._verbose_log_file_index = 1
    dummy._verbose_log_file_error_reported = False

    dummy._verbose_log_part_path = MethodType(MainWindow._verbose_log_part_path, dummy)
    dummy._verbose_log_session_path = MethodType(MainWindow._verbose_log_session_path, dummy)
    dummy._prepare_verbose_log_target = MethodType(MainWindow._prepare_verbose_log_target, dummy)
    dummy._append_verbose_tool_output = MethodType(MainWindow._append_verbose_tool_output, dummy)
    dummy._on_tool_output_requested = MethodType(MainWindow._on_tool_output_requested, dummy)

    dummy._on_tool_output_requested("inspector", "Inspection démarrée : /tmp/movie.mkv")

    log_files = sorted((tmp_path / "chosen_logs").glob("mediarecode-verbose-*.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text(encoding="utf-8")
    assert "[TOOL] [inspector] Inspection démarrée : /tmp/movie.mkv" in content


def test_standard_file_logging_skips_verbose_tool_lines(tmp_path) -> None:
    dummy = SimpleNamespace()
    dummy._config = SimpleNamespace(
        enable_file_logging=True,
        file_logging_level="standard",
        app_data_dir=tmp_path,
        verbose_log_dir=tmp_path / "chosen_logs",
    )
    dummy._op_mode = "encode"
    dummy._log_panel = SimpleNamespace(log=MagicMock())
    dummy._NOISE_RE = MainWindow._NOISE_RE
    dummy._encode_panel = SimpleNamespace(get_total_frames=lambda: None)

    dummy._append_verbose_tool_output = MethodType(MainWindow._append_verbose_tool_output, dummy)
    dummy._capture_verbose_progress_line = MethodType(MainWindow._capture_verbose_progress_line, dummy)
    dummy._on_tool_output_requested = MethodType(MainWindow._on_tool_output_requested, dummy)

    wrapped = _ENCODE_INTERNAL_PROGRESS_PREFIX + json.dumps(
        {"kind": "encode_ffmpeg", "label": "ffmpeg-video-1", "event": "line", "line": "out_time=00:00:05.000000"},
        ensure_ascii=False,
    )
    dummy._capture_verbose_progress_line(wrapped)
    dummy._on_tool_output_requested("inspector", "Inspection démarrée : /tmp/movie.mkv")

    log_files = sorted((tmp_path / "chosen_logs").glob("mediarecode-verbose-*.log"))
    assert not log_files
