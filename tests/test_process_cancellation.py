"""Exercise cancellation with real processes, blocked reads and delayed exits."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

import pytest

from core.runner import TaskCancelledError, TaskSignals, ToolRunner
from core.subprocess_utils import kill_process_tree
from core.workflows.common.sync_rewrite import SyncRewriteService
from core.workflows.remux_models import RemuxError
from core.workflows.remux_timeline_sync import FfmpegTimelineSync


def test_cancel_between_registration_and_read(qt_app, monkeypatch):
    signals = TaskSignals()
    registered, resume = threading.Event(), threading.Event()
    outcomes = []
    original = signals._register_proc

    def register(proc):
        original(proc)
        registered.set()
        assert resume.wait(5)

    def worker():
        try:
            ToolRunner()._run_cmd([sys.executable, '-c', 'import time; time.sleep(30)'], signals=signals)
        except BaseException as exc:
            outcomes.append(exc)

    monkeypatch.setattr(signals, '_register_proc', register)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert registered.wait(5)
        signals.cancel()
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], TaskCancelledError)


def test_registration_after_cancel_kills_new_process(qt_app):
    signals = TaskSignals()
    signals.cancel()
    with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']) as proc:
        try:
            signals._register_proc(proc)
            assert proc.wait(timeout=5) != 0
        finally:
            proc.kill()
            signals._unregister_proc(proc)


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX inherited stdout')
def test_kill_does_not_wait_for_reader_or_close_its_stream(tmp_path):
    child_file = tmp_path / 'child.pid'
    script = (
        'import subprocess,sys,time,pathlib; '
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f'pathlib.Path({str(child_file)!r}).write_text(str(child.pid)); '
        "print('ready',flush=True); time.sleep(30)"
    )
    proc = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE)
    stdout = proc.stdout
    assert stdout is not None
    reader = None
    child_pid = None
    try:
        assert stdout.readline() == b'ready\n'
        child_pid = int(child_file.read_text())
        reader = threading.Thread(target=stdout.read)
        reader.start()
        done = threading.Event()
        def kill():
            kill_process_tree(proc, timeout=0.05)
            done.set()
        killer = threading.Thread(target=kill)
        killer.start()
        assert done.wait(1), 'kill must not acquire BufferedReader lock'
        killer.join(5)
        assert not stdout.closed
        assert reader.is_alive()
    finally:
        if child_pid is not None:
            os.kill(child_pid, signal.SIGKILL)
        proc.kill()
        proc.wait(timeout=5)
        if reader is not None:
            reader.join(5)
        stdout.close()


@pytest.mark.parametrize('operation', ['rewrite', 'mmap'])
def test_silent_process_can_be_cancelled(tmp_path, monkeypatch, operation):
    real_popen = subprocess.Popen
    started, cancelled = threading.Event(), threading.Event()
    processes, outcomes = [], []

    def popen(_cmd, **kwargs):
        proc = real_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        processes.append(proc)
        started.set()
        return proc

    monkeypatch.setattr(subprocess, 'Popen', popen)
    destination = tmp_path / 'out.mka'

    def worker():
        try:
            if operation == 'rewrite':
                SyncRewriteService(ffmpeg_bin='ffmpeg')._run_checked(
                    ['ffmpeg'], destination, 'failure', cancel_cb=cancelled.is_set)
            else:
                FfmpegTimelineSync(ffmpeg_bin='ffmpeg')._extract_stream_via_mmap(
                    source=tmp_path / 'in.mkv', stream_index=0,
                    destination=destination, cancel_cb=cancelled.is_set)
        except BaseException as exc:
            outcomes.append(exc)

    # The Windows cancellation utility also uses Popen for taskkill.
    # Keep that call real while substituting only the media command.
    def dispatch(cmd, **kwargs):
        return popen(cmd, **kwargs) if cmd[0] == 'ffmpeg' else real_popen(cmd, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', dispatch)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert started.wait(5)
        cancelled.set()
        thread.join(5)
        assert not thread.is_alive()
        assert len(outcomes) == 1 and isinstance(outcomes[0], TaskCancelledError)
        assert processes[0].poll() is not None
        assert processes[0].stdout.closed
        if destination.exists():
            destination.unlink()  # mmap/file must be released on Windows too.
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
        thread.join(5)


@pytest.mark.parametrize('returncode', [0, 7])
def test_mmap_waits_for_producer_after_eof(tmp_path, monkeypatch, returncode):
    real_popen = subprocess.Popen
    processes = []
    script = ("import os,time; os.write(1,b'complete-data'); os.close(1); "
              f'time.sleep(0.15); raise SystemExit({returncode})')
    def popen(cmd, **kwargs):
        if cmd[0] != 'ffmpeg':
            return real_popen(cmd, **kwargs)
        proc = real_popen([sys.executable, '-c', script], **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(subprocess, 'Popen', popen)
    destination = tmp_path / 'out.mka'
    def extract():
        FfmpegTimelineSync(ffmpeg_bin='ffmpeg')._extract_stream_via_mmap(
            source=tmp_path / 'source.mkv', stream_index=0, destination=destination)
    if returncode:
        with pytest.raises(RemuxError):
            extract()
    else:
        extract()
    assert destination.read_bytes() == b'complete-data'
    assert processes[0].returncode == returncode
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_rewrite_keeps_progress_and_successful_output(tmp_path):
    destination = tmp_path / 'out.mka'
    progress = []
    script = (f'from pathlib import Path; Path({str(destination)!r}).write_bytes(b"data"); '
              'print("frame=1\\rframe=2",flush=True)')
    SyncRewriteService(ffmpeg_bin='ffmpeg', progress_cb=progress.append)._run_checked(
        [sys.executable, '-c', script], destination, 'failure', cancel_cb=lambda: False)
    assert destination.read_bytes() == b'data'
    assert progress[-2:] == ['frame=1', 'frame=2']


def test_rewrite_progress_error_reaps_process(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    processes = []
    def popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(subprocess, 'Popen', popen)
    def progress(line):
        if not line.startswith('$ '):
            raise ValueError('progress callback failed')
    with pytest.raises(ValueError, match='progress callback failed'):
        SyncRewriteService(ffmpeg_bin='ffmpeg', progress_cb=progress)._run_checked(
            [sys.executable, '-c', 'import time; print("line\\n"*128,flush=True); time.sleep(30)'],
            tmp_path / 'out.mka', 'failure')
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed


def test_wait_for_workers_follows_linked_and_late_workers(qt_app):
    from concurrent.futures import Future
    outer, inner = TaskSignals(), TaskSignals()
    first, late, inner_work = Future(), Future(), Future()
    outer.watch_future(first)
    outer.link_workers(inner)
    inner.watch_future(inner_work)
    done = threading.Event()
    waiter = threading.Thread(target=lambda: (outer.wait_for_workers(), done.set()))
    waiter.start()
    try:
        # Worker soumis pendant l'attente (préparation → encodage).
        outer.watch_future(late)
        first.set_result(None)
        assert not done.wait(0.1)
        late.set_result(None)
        assert not done.wait(0.1)
        inner_work.set_result(None)
        assert done.wait(5)
    finally:
        for future in (first, late, inner_work):
            if not future.done():
                future.set_result(None)
        waiter.join(5)


def test_merge_dovi_cancel_kills_running_tool(qt_app):
    from core.workflows.merge_dovi import MergeDoviWorkflow
    workflow = MergeDoviWorkflow()
    outcomes = []

    def worker():
        try:
            workflow._run_raw([sys.executable, '-c', 'import time; time.sleep(30)'])
        except BaseException as exc:
            outcomes.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        for _ in range(100):
            with workflow._procs_lock:
                if workflow._procs:
                    break
            threading.Event().wait(0.05)
        workflow.cancel()
        thread.join(5)
    finally:
        workflow.cancel()
        thread.join(5)
    assert not thread.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], RuntimeError)
    assert not workflow._procs


def test_merge_dovi_unexpected_error_still_emits_terminal_signal(qt_app, tmp_path, monkeypatch):
    from core.workflows.merge_dovi import DoviProfile, MergeDoviWorkflow, _WorkflowPaths
    workflow = MergeDoviWorkflow()
    failures = []
    workflow.workflow_failed.connect(lambda step, message: failures.append(message))

    def boom(*_a, **_k):
        raise OSError('disque plein')

    monkeypatch.setattr(workflow, '_step_validate', boom)
    paths = _WorkflowPaths.from_config(tmp_path / 'w', tmp_path / 'o', tmp_path / 'f.mkv', 'out')
    workflow._run(tmp_path / 'f1.mkv', tmp_path / 'f2.mkv', paths, DoviProfile.P8_1)
    assert failures and 'disque plein' in failures[0]
