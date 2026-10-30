"""Contrat Win32 de lancement : COM équilibré, propriétaire et refus UAC."""
import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import windows_shell


@pytest.fixture
def shell(monkeypatch):
    ole = SimpleNamespace(CoInitializeEx=Mock(return_value=0), CoUninitialize=Mock())
    api = SimpleNamespace(ShellExecuteExW=Mock(return_value=1))
    kernel = SimpleNamespace(CloseHandle=Mock(return_value=1))
    libraries = {"ole32": ole, "shell32": api, "kernel32": kernel}
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_kw: libraries[name], raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 1223, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "Élévation annulée"), raising=False)
    return ole, api, kernel


@pytest.mark.parametrize("already_initialized", [False, True])
def test_installer_launch_owns_window_and_finishes_shell_dispatch(tmp_path, shell, already_initialized):
    ole, api, kernel = shell
    ole.CoInitializeEx.return_value = int(already_initialized)
    path = tmp_path / "Mise à jour.exe"
    seen = {}

    def launch(pointer):
        request = ctypes.cast(pointer, ctypes.POINTER(windows_shell._ShellExecuteInfo)).contents
        seen.update(hwnd=request.hwnd, verb=request.lpVerb, path=request.lpFile, flags=request.fMask)
        request.hProcess = 99
        return 1

    api.ShellExecuteExW.side_effect = launch
    windows_shell.launch_installer(path, 1234)
    assert seen["hwnd"] == 1234 and seen["verb"] == "runas" and seen["path"] == str(path.resolve())
    assert seen["flags"] & 0x100  # SEE_MASK_NOASYNC : lancement achevé avant de quitter.
    ole.CoInitializeEx.assert_called_once_with(None, 6)
    ole.CoUninitialize.assert_called_once()
    kernel.CloseHandle.assert_called_once_with(99)


def test_cancelled_uac_reports_error_and_releases_com(tmp_path, shell):
    ole, api, kernel = shell
    api.ShellExecuteExW.return_value = 0
    with pytest.raises(OSError, match="Élévation annulée"):
        windows_shell.launch_installer(tmp_path / "setup.exe", 1234)
    ole.CoUninitialize.assert_called_once()
    kernel.CloseHandle.assert_not_called()


def test_com_failure_prevents_launch_without_uninitializing_someone_elses_com(tmp_path, shell):
    ole, api, _kernel = shell
    ole.CoInitializeEx.return_value = -2147417850  # RPC_E_CHANGED_MODE
    with pytest.raises(OSError, match="COM"):
        windows_shell.launch_installer(tmp_path / "setup.exe", 1234)
    api.ShellExecuteExW.assert_not_called()
    ole.CoUninitialize.assert_not_called()
