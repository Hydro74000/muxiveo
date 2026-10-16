"""Les outils Merge publient leurs messages avant leur fin et restent annulables."""
from __future__ import annotations

import sys
import threading

from PySide6.QtCore import Qt

from core.workflows.merge_dovi import MergeDoviWorkflow, WorkflowStep


def test_merge_streams_stdout_and_stderr_before_completion(qt_app):
    workflow = MergeDoviWorkflow()
    first_line = threading.Event()
    messages = []
    result = []

    def on_line(tool, line):
        messages.append((tool, line))
        if line == "démarrage":
            first_line.set()

    workflow.tool_output.connect(on_line, Qt.ConnectionType.DirectConnection)
    command = [sys.executable, "-u", "-c",
               "import sys,time; print('démarrage'); time.sleep(0.5); "
               "print('avertissement',file=sys.stderr); print('terminé')"]
    thread = threading.Thread(target=lambda: result.append(workflow._run_raw(command, WorkflowStep.EXTRACT_PARALLEL)))
    thread.start()
    try:
        assert first_line.wait(3)
        assert thread.is_alive()
    finally:
        thread.join(5)
        if thread.is_alive():
            workflow.cancel()
            thread.join(3)
    assert not thread.is_alive()
    assert "avertissement" in [line for _, line in messages]
    assert result == ["démarrage\nterminé\n"]


def test_merge_can_cancel_while_draining_both_pipes(qt_app):
    workflow = MergeDoviWorkflow()
    first_line = threading.Event()
    failure = []
    workflow.tool_output.connect(lambda *_: first_line.set(), Qt.ConnectionType.DirectConnection)

    def run():
        try:
            workflow._run_raw([sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(30)"])
        except RuntimeError as exc:
            failure.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert first_line.wait(3)
    finally:
        workflow.cancel()
        thread.join(5)
    assert not thread.is_alive()
    assert failure
    assert not workflow._procs
