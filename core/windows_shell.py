"""Lancement d'un installeur Windows avec COM et fenêtre propriétaire explicites."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


def launch_installer(path: Path, owner_hwnd: int | None = None) -> None:
    """Attend l'acceptation du lancement (UAC compris), puis rend la main sans attendre l'installation."""
    load_dll = getattr(ctypes, "WinDLL", None)
    if load_dll is None:
        raise OSError("Le lancement de l'installeur nécessite Windows.")
    ole32 = load_dll("ole32", use_last_error=True)
    shell32 = load_dll("shell32", use_last_error=True)
    kernel32 = load_dll("kernel32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    # COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE. S_FALSE est aussi un succès :
    # une initialisation préalable sur ce fil doit être équilibrée par notre propre Uninitialize.
    result = ole32.CoInitializeEx(None, 0x2 | 0x4)
    if result & 0x80000000:
        raise OSError(f"Initialisation COM impossible (HRESULT 0x{result & 0xffffffff:08X}).")
    try:
        request = _ShellExecuteInfo()
        request.cbSize = ctypes.sizeof(request)
        # NOCLOSEPROCESS | NOASYNC | FLAG_NO_UI : le lancement est achevé avant la fermeture
        # de l'application ; ses erreurs sont affichées par notre dialogue, l'UAC reste visible.
        request.fMask = 0x40 | 0x100 | 0x400
        request.hwnd = owner_hwnd
        request.lpVerb = "runas"
        request.lpFile = str(path.resolve())
        request.lpDirectory = str(path.resolve().parent)
        request.nShow = 1
        if not shell32.ShellExecuteExW(ctypes.byref(request)):
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
        if request.hProcess:
            kernel32.CloseHandle(request.hProcess)
    finally:
        ole32.CoUninitialize()
