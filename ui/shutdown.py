"""Wait for cooperative shutdown while keeping the Qt event loop running."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
from collections.abc import Sequence

from PySide6.QtCore import QTimer
from core.runner import TaskSignals


class Shutdown:
    """Cancel tasks, then join executors outside the GUI thread.

    Widgets keep their resources alive until done; cancel_futures alone cannot
    stop work that is already running. Construct only once per widget closure.
    """

    def __init__(self, *, executors: Sequence[ThreadPoolExecutor] = (),
                 tasks: Sequence[TaskSignals | None] = ()) -> None:
        self.done = threading.Event()
        active_tasks = [task for task in tasks if task is not None]
        terminal_events = []
        for task in active_tasks:
            terminal = threading.Event()
            def mark_done(*_args, done=terminal):
                done.set()
            task.connect_terminal(finished=mark_done, failed=mark_done,
                                  cancelled=mark_done, direct=True)
            terminal_events.append(terminal)
        # Prevent new submissions immediately, before starting the join thread.
        for executor in executors:
            executor.shutdown(wait=False, cancel_futures=True)

        def finish() -> None:
            for task in active_tasks:
                task.cancel()
            for executor in executors:
                executor.shutdown(wait=True)
            for terminal in terminal_events:
                terminal.wait()
            for task in active_tasks:
                task.wait_for_workers()
            self.done.set()

        threading.Thread(target=finish, name="widget-shutdown", daemon=True).start()


def defer_close(widget, event, *, ready: bool) -> bool:
    """Return True when closure must be retried after workers have finished."""
    if ready:
        timer = getattr(widget, "_shutdown_timer", None)
        if timer is not None:
            timer.stop()
        return False
    event.ignore()
    timer = getattr(widget, "_shutdown_timer", None)
    if timer is None:
        timer = QTimer(widget)
        timer.setSingleShot(True)
        timer.timeout.connect(widget.close)
        widget._shutdown_timer = timer
    if not timer.isActive():
        timer.start(50)
    return True
