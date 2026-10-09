"""Tests for graceful shutdown, process termination, and temporary cleanup."""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

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
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.poll() is None
    kill_process_tree(proc, timeout=1.0)
    assert proc.poll() is not None
    # The owner can finish reading EOF and closes the streams itself.
    assert proc.stdout is not None and not proc.stdout.closed
    assert proc.stderr is not None and not proc.stderr.closed
    assert proc.communicate(timeout=5) == (b"", b"")


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


@pytest.mark.parametrize("action", ["close", "accept", "reject"])
def test_sync_studio_cancels_audio_and_keeps_resources_until_worker_finishes(
    qt_app, qtbot, tmp_path, monkeypatch, action,
) -> None:
    import numpy as np
    from PySide6.QtWidgets import QDialog
    from core.workflows.audio_sync_scan import AudioSyncScanner
    from core.workflows.remux_models import TrackEntry
    from ui.panels.remux_panel.widgets.sync_studio_dialog import SyncStudioDialog

    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    cancellations = []

    def samples(scanner, *_args, **_kwargs):
        cancellations.append(scanner.cancel_event)
        started.set()
        release.wait(5)
        stopped.set()
        return np.zeros(16)

    monkeypatch.setattr(AudioSyncScanner, "samples", samples)
    dialog = SyncStudioDialog(
        target_entry=TrackEntry(0, "audio", "FLAC", "", "", ""),
        target_source_path=tmp_path / "target.mkv",
        target_stream_index=0,
        reference_entry=None, reference_source_path=None, reference_stream_index=None,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    directory = Path(dialog._temp_dir.name)
    try:
        assert started.wait(5)
        getattr(dialog, action)()
        assert cancellations[0].is_set()
        assert dialog.isVisible()  # fermeture différée, sans bloquer le thread GUI
        assert directory.is_dir()
        release.set()
        qtbot.waitUntil(lambda: stopped.is_set() and not dialog.isVisible(), timeout=5000)
        assert not directory.exists()
        expected = QDialog.DialogCode.Accepted if action == "accept" else QDialog.DialogCode.Rejected
        assert dialog.result() == expected
    finally:
        release.set()
        dialog.close()
        qtbot.waitUntil(lambda: not dialog.isVisible(), timeout=5000)


def test_main_window_close_event_lifecycle(qt_app) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import MagicMock
    from PySide6.QtWidgets import QMainWindow, QWidget
    from PySide6.QtGui import QCloseEvent
    from core.runner import TaskSignals
    from ui.main_window import MainWindow
    from ui.panels.remux_panel.panel import RemuxPanel

    started = threading.Event()
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    def work():
        started.set()
        release.wait(5)
    future = executor.submit(work)
    assert started.wait(5)
    queued = executor.submit(lambda: None)

    class Panel(RemuxPanel):
        def __init__(self):
            QWidget.__init__(self)
            self._scan_cancel = threading.Event()
            self._probe_cancel = threading.Event()
            self._preview_timer = MagicMock()
            self._executor = executor
            self._preview_executor = ThreadPoolExecutor(max_workers=1)
            self._path_executor = ThreadPoolExecutor(max_workers=1)
            self._inspection_executor = ThreadPoolExecutor(max_workers=1)

    class Window(MainWindow):
        def __init__(self):
            QMainWindow.__init__(self)
            self._update_request_id = 0
            self._update_download_cancel = threading.Event()
            self._config = MagicMock()
            self._signals = TaskSignals()
            self._prep_progress_timer = MagicMock()
            self._op_encode_multi_reselect_timer = MagicMock()
            self._remux_panel = Panel()
            self._verbose_file_logger = MagicMock()

    window = Window()
    fake: Any = window  # Attributs remplacés par des MagicMock.
    try:
        event = QCloseEvent()
        start = time.monotonic()
        window.closeEvent(event)
        assert time.monotonic() - start < 0.5
        assert not event.isAccepted()
        assert window._remux_panel._scan_cancel.is_set()
        # Sondes d'inspection interrompues dès la fermeture.
        assert window._remux_panel._probe_cancel.is_set()
        assert queued.cancelled()
        fake._config.save.assert_not_called()
        fake._verbose_file_logger.close.assert_not_called()
        release.set()
        assert window._remux_panel._shutdown.done.wait(5)
        assert future.done()
        # Main workflow is independent of panel executors: also await its end.
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()
        fake._signals.cancelled.emit()
        assert window._shutdown.done.wait(5)
        event = QCloseEvent()
        window.closeEvent(event)
        assert event.isAccepted()
        fake._config.save.assert_called_once()
        fake._verbose_file_logger.close.assert_called_once()
        fake._prep_progress_timer.stop.assert_called_once()
        fake._op_encode_multi_reselect_timer.stop.assert_called_once()
    finally:
        release.set()
        fake._signals.cancelled.emit()
        executor.shutdown()
        window.close()


def test_hybrid_close_keeps_temp_files_until_worker_finishes(qt_app, tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import tempfile
    from PySide6.QtWidgets import QWidget
    from PySide6.QtGui import QCloseEvent
    from ui.panels.hybrid_studio import HybridStudio

    class Panel(HybridStudio):
        def __init__(self):
            QWidget.__init__(self)
            self.executor = ThreadPoolExecutor(max_workers=1)
            self.cancel_event = threading.Event()
            self.signals = None
            self.preview_temp = tempfile.TemporaryDirectory(dir=tmp_path)

    panel = Panel()
    root = Path(panel.preview_temp.name)
    started, release = threading.Event(), threading.Event()
    def work():
        started.set()
        assert release.wait(5)
        (root / 'last-write').write_bytes(b'data')
    future = panel.executor.submit(work)
    assert started.wait(5)
    try:
        event = QCloseEvent()
        panel.closeEvent(event)
        assert not event.isAccepted()
        assert root.exists()
        assert panel.cancel_event.is_set()
        release.set()
        assert panel._shutdown.done.wait(5)
        future.result()
        assert (root / 'last-write').read_bytes() == b'data'
        event = QCloseEvent()
        panel.closeEvent(event)
        assert event.isAccepted()
        assert not root.exists()
    finally:
        release.set()
        panel.executor.shutdown()
        panel.close()


def test_application_exits_after_cancelling_its_worker(tmp_path) -> None:
    """Check actual interpreter exit, not just closeEvent returning."""
    import os
    import textwrap
    script = textwrap.dedent('''
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QWidget
        from PySide6.QtGui import QCloseEvent
        from ui.shutdown import Shutdown, defer_close
        app = QApplication([])
        release = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        executor.submit(release.wait)
        class Window(QWidget):
            def closeEvent(self, event):
                if not hasattr(self, '_shutdown'):
                    release.set()
                    self._shutdown = Shutdown(executors=(executor,))
                if defer_close(self, event, ready=self._shutdown.done.is_set()):
                    return
                super().closeEvent(event)
        window = Window()
        window.show()
        QTimer.singleShot(0, window.close)
        QTimer.singleShot(4000, lambda: app.exit(7))
        raise SystemExit(app.exec())
    ''')
    result = subprocess.run(
        [sys.executable, '-c', script],
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode(errors='replace')


def test_shutdown_waits_for_workflow_finally_after_terminal_signal(qt_app):
    from concurrent.futures import ThreadPoolExecutor
    from core.runner import TaskSignals
    from ui.shutdown import Shutdown

    signals = TaskSignals()
    cleanup_started, cleanup_release = threading.Event(), threading.Event()
    def work():
        try:
            signals.finished.emit('complete')
        finally:
            cleanup_started.set()
            assert cleanup_release.wait(5)
    executor = ThreadPoolExecutor(max_workers=1)
    future = signals.watch_future(executor.submit(work))
    executor.shutdown(wait=False)
    assert cleanup_started.wait(5)
    shutdown = Shutdown(tasks=(signals,))
    try:
        assert not shutdown.done.wait(0.05)
        cleanup_release.set()
        assert shutdown.done.wait(5)
        assert future.done()
    finally:
        cleanup_release.set()
        executor.shutdown()


def test_shutdown_accepts_cancelled_queued_work(qt_app):
    from concurrent.futures import Future
    from core.runner import TaskSignals
    from ui.shutdown import Shutdown

    signals = TaskSignals()
    future = signals.watch_future(Future())
    assert future.cancel()
    shutdown = Shutdown(tasks=(signals,))
    assert shutdown.done.wait(5)


def test_windows_taskkill_path_ignores_environment(monkeypatch) -> None:
    """taskkill est résolu via l'API système, jamais via PATH ni SystemRoot."""
    import core.subprocess_utils as subprocess_utils

    monkeypatch.setenv("SystemRoot", r"D:\Evil")
    monkeypatch.setenv("windir", r"D:\Evil")
    monkeypatch.setattr(subprocess_utils, "_windows_system_directory", lambda: r"C:\Windows\System32")
    path = subprocess_utils._windows_taskkill_path()
    assert str(path).startswith(r"C:\Windows\System32")
    assert "Evil" not in str(path)
    assert path.name == "taskkill.exe"


def test_windows_system_directory_falls_back_without_windll() -> None:
    """Hors Windows (pas de ctypes.windll) : repli sur le dossier système standard."""
    from core.subprocess_utils import _WINDOWS_SYSTEM_DIR_FALLBACK, _windows_system_directory

    if sys.platform == "win32":
        pytest.skip("windll disponible sous Windows")
    assert _windows_system_directory() == _WINDOWS_SYSTEM_DIR_FALLBACK
