"""Pipe lifecycle contract on every OS, plus native Windows integration tests."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import io
import sys
import threading
import time
import uuid

import pytest

from core.windows_named_pipe import WindowsNamedPipe


class PipeApi:
    def __init__(self, blocked=None):
        self.blocked = blocked
        self.entered = threading.Event()
        self.cancelled = threading.Event()
        self.pending = False
        self.flushed = False
        self.closed = []
        self.data = bytearray()
        self.errors = []
        self.pipe_handle = 1 << 40
        self.thread_handle = (1 << 40) + 1

    def CreateNamedPipeW(self, *_args):
        return self.pipe_handle

    def OpenThread(self, *_args):
        return self.thread_handle

    def block(self, operation):
        if self.blocked != operation:
            return True
        self.pending = True
        self.entered.set()
        if not self.cancelled.wait(5):
            self.errors.append('I/O not cancelled')
        self.pending = False
        return False

    def ConnectNamedPipe(self, *_args):
        return self.block('connect')

    def WriteFile(self, handle, data, size, written, _overlapped):
        assert handle == self.pipe_handle
        if not self.block('write'):
            return False
        # Partial writes must not drop the tail.
        count = min(size, 13)
        self.data.extend(data[:count])
        ctypes.cast(written, ctypes.POINTER(wintypes.DWORD)).contents.value = count
        return True

    def FlushFileBuffers(self, *_args):
        self.flushed = self.block('flush')
        return self.flushed

    def DisconnectNamedPipe(self, *_args):
        if self.pending:
            self.errors.append('disconnected during pending I/O')
        if not self.flushed and not self.cancelled.is_set():
            self.errors.append('unread data discarded')
        return True

    def CancelSynchronousIo(self, handle) -> bool:
        assert handle == self.thread_handle
        self.cancelled.set()
        return True

    def CloseHandle(self, handle):
        if self.pending:
            self.errors.append('handle closed during pending I/O')
        if handle in self.closed:
            self.errors.append('handle closed twice')
        self.closed.append(handle)
        return True


def make_pipe(monkeypatch, api):
    monkeypatch.setattr('core.windows_named_pipe._load_api', lambda: api)
    monkeypatch.setattr('core.windows_named_pipe._last_error', lambda: 995)
    return WindowsNamedPipe('test')


def test_normal_eof_flushes_all_data_before_close(monkeypatch):
    api = PipeApi()
    pipe = make_pipe(monkeypatch, api)
    payload = bytes(range(256)) * 300
    source = io.BytesIO(payload)
    pipe.pump(source)
    pipe.cancel()  # Cleanup after successful completion must be harmless.
    assert api.data == payload
    assert api.flushed and source.closed
    assert api.closed == [api.pipe_handle, api.thread_handle]
    assert not api.errors


@pytest.mark.parametrize('blocked', ['connect', 'write', 'flush'])
def test_cancel_completes_pending_io_before_closing_handles(monkeypatch, blocked):
    api = PipeApi(blocked)
    pipe = make_pipe(monkeypatch, api)
    source = io.BytesIO(b'payload')
    thread = threading.Thread(target=pipe.pump, args=(source,))
    thread.start()
    assert api.entered.wait(5)
    pipe.cancel()
    pipe.cancel()
    thread.join(5)
    assert not thread.is_alive()
    assert source.closed
    assert api.closed == [api.pipe_handle, api.thread_handle]
    assert not api.errors


def test_cancel_before_worker_starts(monkeypatch):
    api = PipeApi()
    pipe = make_pipe(monkeypatch, api)
    pipe.cancel()
    source = io.BytesIO(b'payload')
    pipe.pump(source)
    assert source.closed
    assert not api.data
    assert api.closed == [api.pipe_handle]


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows named pipe')
def test_native_pipe_slow_reader_receives_complete_payload():
    pipe = WindowsNamedPipe(rf'\\.\pipe\Muxiveo_test_{uuid.uuid4().hex}')
    payload = bytes(range(256)) * 8192 + b'final-packet'
    source = io.BytesIO(payload)
    writer = threading.Thread(target=pipe.pump, args=(source,), daemon=True)
    received = bytearray()
    errors = []
    def read():
        try:
            with open(pipe.name, 'rb', buffering=0) as stream:
                while chunk := stream.read(8192):
                    received.extend(chunk)
                    time.sleep(0.001)
        except OSError as exc:
            errors.append(exc)
    reader = threading.Thread(target=read, daemon=True)
    writer.start()
    reader.start()
    try:
        reader.join(10)
        writer.join(5)
        assert not reader.is_alive() and not writer.is_alive()
        assert received == payload
        assert not errors
    finally:
        pipe.cancel()
        writer.join(5)
        reader.join(5)


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows named pipe')
@pytest.mark.parametrize('mode', ['connect', 'write', 'flush'])
def test_native_pipe_cancellation_unblocks_writer(mode):
    pipe = WindowsNamedPipe(rf'\\.\pipe\Muxiveo_test_{uuid.uuid4().hex}')
    source = io.BytesIO(b'x' * (4 * 1024 * 1024 if mode == 'write' else 4096))
    writer = threading.Thread(target=pipe.pump, args=(source,), daemon=True)
    writer.start()
    client = None
    try:
        if mode != 'connect':
            client = open(pipe.name, 'rb', buffering=0)
            assert client.read(1) == b'x'
        time.sleep(0.1)
        assert writer.is_alive()
        pipe.cancel()
        writer.join(5)
        assert not writer.is_alive()
        assert source.closed
    finally:
        pipe.cancel()
        if client is not None:
            client.close()
        writer.join(5)


def test_cancellation_between_stop_check_and_io_is_retried(monkeypatch):
    class Api(PipeApi):
        def __init__(self):
            super().__init__('connect')
            self.before_io = threading.Event()
            self.start_io = threading.Event()
            self.missed = threading.Event()

        def ConnectNamedPipe(self, *args):
            self.before_io.set()
            assert self.start_io.wait(5)
            return super().ConnectNamedPipe(*args)

        def CancelSynchronousIo(self, handle):
            if not self.pending:
                self.missed.set()  # ERROR_NOT_FOUND: nothing to cancel yet.
                return False
            return super().CancelSynchronousIo(handle)

    api = Api()
    pipe = make_pipe(monkeypatch, api)
    source = io.BytesIO(b'payload')
    writer = threading.Thread(target=pipe.pump, args=(source,))
    writer.start()
    assert api.before_io.wait(5)
    pipe.cancel()
    assert api.missed.wait(5)
    api.start_io.set()
    writer.join(5)
    assert not writer.is_alive()
    assert not api.errors
