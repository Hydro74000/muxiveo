"""Tests de l'ouverture externe (environnement hôte nettoyé en AppImage)."""

from __future__ import annotations

from unittest.mock import patch

from PySide6.QtCore import QUrl

from core.subprocess_utils import host_environment
from ui import desktop

_ROOT = "/tmp/.mount_muxiveoXYZ"


def test_host_environment_strips_bundle_paths():
    env = {
        "APPDIR": _ROOT,
        "LD_LIBRARY_PATH": f"{_ROOT}/usr/bin/_internal:/opt/lib:",
        "LD_LIBRARY_PATH_ORIG": f"{_ROOT}/usr/bin/_internal",
        "PATH": f"{_ROOT}/usr/bin/tools:/usr/bin",
        "QT_PLUGIN_PATH": f"{_ROOT}/usr/bin/_internal/PySide6/Qt/plugins",
        "XDG_DATA_DIRS": "/usr/share",
        "HOME": "/home/u",
    }
    clean = host_environment(env)
    assert clean["LD_LIBRARY_PATH"] == "/opt/lib"
    assert clean["PATH"] == "/usr/bin"
    assert "QT_PLUGIN_PATH" not in clean and "LD_LIBRARY_PATH_ORIG" not in clean
    assert clean["XDG_DATA_DIRS"] == "/usr/share" and clean["HOME"] == "/home/u"
    assert env["PATH"].startswith(_ROOT)  # entrée non modifiée


def test_host_environment_is_noop_outside_bundle():
    env = {"LD_LIBRARY_PATH": "/opt/lib", "PATH": "/usr/bin"}
    assert host_environment(env, bundle_roots=()) == env


def test_open_external_uses_xdg_open_with_host_env_in_appimage(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setenv("APPDIR", _ROOT)
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{_ROOT}/usr/bin/_internal")
    monkeypatch.setattr(desktop.shutil, "which", lambda name, path=None: "/usr/bin/xdg-open")
    with patch.object(desktop.subprocess, "Popen") as popen, \
         patch.object(desktop.QDesktopServices, "openUrl") as qt_open:
        assert desktop.open_external(QUrl("https://github.com/Hydro74000/muxiveo/releases/tag/v4.0.2"))
    qt_open.assert_not_called()
    args, kwargs = popen.call_args
    assert args[0] == ["/usr/bin/xdg-open", "https://github.com/Hydro74000/muxiveo/releases/tag/v4.0.2"]
    assert "LD_LIBRARY_PATH" not in kwargs["env"]


def test_open_external_passes_local_path(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setenv("APPDIR", _ROOT)
    monkeypatch.setattr(desktop.shutil, "which", lambda name, path=None: "/usr/bin/xdg-open")
    with patch.object(desktop.subprocess, "Popen") as popen:
        desktop.open_external(QUrl.fromLocalFile(str(tmp_path / "a b.mkv")))
    assert popen.call_args.args[0][1] == str(tmp_path / "a b.mkv")


def test_open_external_falls_back_to_qt_outside_appimage(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setattr(desktop.sys, "frozen", False, raising=False)
    with patch.object(desktop.subprocess, "Popen") as popen, \
         patch.object(desktop.QDesktopServices, "openUrl", return_value=True) as qt_open:
        assert desktop.open_external(QUrl("https://example.org"))
    popen.assert_not_called()
    qt_open.assert_called_once()
