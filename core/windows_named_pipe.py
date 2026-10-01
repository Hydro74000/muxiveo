"""Windows named-pipe writer; imported only by the Windows live-sync backend."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import threading
from typing import Any, BinaryIO

if sys.platform == "win32":
    def _kernel32() -> Any:
        return ctypes.WinDLL("kernel32", use_last_error=True)

    def _last_error() -> int:
        return ctypes.get_last_error()

    def _win_error(code: int) -> OSError:
        return ctypes.WinError(code)
else:
    # Importable hors Windows uniquement pour les tests (API simulée).
    def _kernel32() -> Any:
        raise OSError("kernel32 indisponible hors Windows")

    def _last_error() -> int:
        return 0

    def _win_error(code: int) -> OSError:
        return OSError(code, "Erreur API Windows")


def _load_api() -> Any:
    api = _kernel32()
    # ctypes defaults to int: explicit signatures preserve 64-bit HANDLEs.
    signatures = {
        "CreateNamedPipeW": (wintypes.HANDLE, [wintypes.LPCWSTR, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.LPVOID]),
        "ConnectNamedPipe": (wintypes.BOOL, [wintypes.HANDLE, wintypes.LPVOID]),
        "WriteFile": (wintypes.BOOL, [wintypes.HANDLE, wintypes.LPCVOID,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]),
        "FlushFileBuffers": (wintypes.BOOL, [wintypes.HANDLE]),
        "DisconnectNamedPipe": (wintypes.BOOL, [wintypes.HANDLE]),
        "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        "OpenThread": (wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
        "CancelSynchronousIo": (wintypes.BOOL, [wintypes.HANDLE]),
    }
    for name, (result, args) in signatures.items():
        function = getattr(api, name)
        function.restype, function.argtypes = result, args
    return api


class WindowsNamedPipe:
    """One writer owns all I/O and closes its handles after I/O completes.

    Normal EOF drains the pipe before closing it. Cancellation repeatedly
    interrupts the dedicated writer's synchronous I/O, including connect and
    flush. Repetition covers cancellation between the stop check and an I/O
    call; no other thread closes a handle still used by the writer.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._api = _load_api()
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._done = threading.Event()
        self._thread_handle = None
        self._handle = self._api.CreateNamedPipeW(name, 2, 0, 1, 1 << 20, 1 << 20, 0, None)
        if self._handle == ctypes.c_void_p(-1).value:
            raise _win_error(_last_error())

    def cancel(self) -> None:
        with self._lock:
            if self._done.is_set() or self._cancelled.is_set():
                return
            self._cancelled.set()
            if self._thread_handle is None:
                # The worker has not started (possibly Popen failed).
                self._close_locked()
                return
        threading.Thread(target=self._cancel_io, name="named-pipe-cancellation", daemon=True).start()

    def _cancel_io(self) -> None:
        while not self._done.is_set():
            with self._lock:
                if self._thread_handle is not None:
                    self._api.CancelSynchronousIo(self._thread_handle)
            self._done.wait(0.02)

    def _close_locked(self) -> None:
        if self._handle is not None:
            # Disconnect coupe le client (ERROR_PIPE_NOT_CONNECTED → EINVAL côté
            # lecteur) : réservé à l'annulation. En fin normale, CloseHandle après
            # FlushFileBuffers donne ERROR_BROKEN_PIPE, lu comme un EOF propre.
            if self._cancelled.is_set():
                self._api.DisconnectNamedPipe(self._handle)
            self._api.CloseHandle(self._handle)
            self._handle = None
        if self._thread_handle is not None:
            self._api.CloseHandle(self._thread_handle)
            self._thread_handle = None
        self._done.set()

    def pump(self, stdout: BinaryIO) -> None:
        try:
            with self._lock:
                if self._done.is_set():
                    return
                # THREAD_TERMINATE is required by CancelSynchronousIo.
                self._thread_handle = self._api.OpenThread(1, False, threading.get_native_id())
                if not self._thread_handle:
                    raise _win_error(_last_error())
            if self._cancelled.is_set():
                return
            if not self._api.ConnectNamedPipe(self._handle, None):
                if _last_error() != 535:  # ERROR_PIPE_CONNECTED
                    return
            while not self._cancelled.is_set():
                chunk = stdout.read(64 * 1024)
                if not chunk:
                    if not self._cancelled.is_set():
                        self._api.FlushFileBuffers(self._handle)
                    return
                offset = 0
                while offset < len(chunk) and not self._cancelled.is_set():
                    written = wintypes.DWORD()
                    if not self._api.WriteFile(self._handle, chunk[offset:], len(chunk) - offset,
                                               ctypes.byref(written), None):
                        return
                    if not written.value:
                        return
                    offset += written.value
        except OSError:
            if not self._cancelled.is_set():
                raise
        finally:
            # No more synchronous I/O after closing the native thread handle.
            with self._lock:
                self._close_locked()
            stdout.close()
