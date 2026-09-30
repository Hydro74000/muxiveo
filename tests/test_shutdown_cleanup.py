"""Tests for graceful shutdown, process termination, and temporary cleanup."""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from core.subprocess_utils import kill_process_tree
from core.workflows.remux_timeline_sync import LiveSyncSession


def test_kill_process_tree_none() -> None:
    # Ne doit pas lever d'exception
    kill_process_tree(None)


def test_kill_process_tree_mock_without_methods() -> None:
    # Supporte un objet sans poll/wait/kill/streams
    fake = object()
    kill_process_tree(fake)  # type: ignore[arg-type]


def test_kill_process_tree_real_process() -> None:
    cmd = ["sleep", "10"] if sys.platform != "win32" else ["timeout", "/t", "10"]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.poll() is None
    kill_process_tree(proc, timeout=1.0)
    assert proc.poll() is not None
    assert proc.stdout is not None and proc.stdout.closed
    assert proc.stderr is not None and proc.stderr.closed


def test_live_sync_session_close_idempotent(tmp_path: Path) -> None:
    fifo = tmp_path / "test.fifo"
    fifo.touch()

    callback_count = [0]

    def _cleanup():
        callback_count[0] += 1

    def _worker():
        pass

    t = threading.Thread(target=_worker)
    t.start()

    session = LiveSyncSession(
        inputs=[],
        processes=[],
        fifo_paths=[fifo],
        _cleanup_callbacks=[_cleanup],
        _threads=[t],
    )

    session.close()
    assert callback_count[0] == 1
    assert not fifo.exists()
    assert not t.is_alive()

    # Second appel idempotent : la callback ne doit pas être rappelée
    session.close()
    assert callback_count[0] == 1


def test_main_window_close_event_lifecycle(qt_app) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import MagicMock
    from PySide6.QtWidgets import QMainWindow
    from ui.main_window import MainWindow

    class _TestWindow(MainWindow):
        def __init__(self) -> None:
            QMainWindow.__init__(self)

    dummy = _TestWindow()
    dummy._update_request_id = 0
    dummy._update_download_cancel = threading.Event()
    dummy._config = MagicMock()
    dummy.saveGeometry = MagicMock(return_value=MagicMock(data=lambda: b"geom"))

    dummy._signals = MagicMock()
    dummy._prep_progress_timer = MagicMock(isActive=lambda: True)
    dummy._op_encode_multi_reselect_timer = MagicMock(isActive=lambda: True)

    fake_executor = ThreadPoolExecutor(max_workers=1)
    mock_panel = MagicMock()
    mock_panel._executor = fake_executor
    mock_hybrid = MagicMock()
    mock_hybrid.preview_temp = MagicMock()

    dummy._dashboard = mock_panel
    dummy._encode_panel = mock_panel
    dummy._remux_panel = mock_panel
    dummy._dovi_panel = mock_panel
    dummy._hybrid_panel = mock_hybrid
    dummy._settings_panel = mock_panel
    dummy._verbose_file_logger = MagicMock()

    from PySide6.QtGui import QCloseEvent
    event = QCloseEvent()
    dummy.closeEvent(event)

    dummy._signals.cancel.assert_called_once()
    dummy._prep_progress_timer.stop.assert_called_once()
    dummy._op_encode_multi_reselect_timer.stop.assert_called_once()
    assert mock_panel.close.call_count >= 1
    assert fake_executor._shutdown
    mock_hybrid.preview_temp.cleanup.assert_called_once()
    dummy._verbose_file_logger.close.assert_called_once()
