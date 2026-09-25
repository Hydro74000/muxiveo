"""Tests de core/update_install.py (réseau et processus mockés)."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from core import update_install
from core.update_check import RELEASE_DOWNLOAD_URL_PREFIX, ReleaseAsset, UpdateInfo
from core.update_install import (
    InstallKind,
    UpdateInstallError,
    apply_update,
    can_self_update,
    detect_install_kind,
    download_asset,
    download_update,
    manual_update_hint,
    parse_checksums,
    select_asset,
)

_APPIMAGE = "Muxiveo-x86_64_allinc-99.0.0.AppImage"
_SETUP = "Muxiveo-Setup-99.0.0.exe"
_PAYLOAD = b"nouvelle version" * 1000


def _asset(name: str, size: int = 0) -> ReleaseAsset:
    return ReleaseAsset(name=name, url=f"{RELEASE_DOWNLOAD_URL_PREFIX}v99.0.0/{name}", size=size)


def _info(*names: str) -> UpdateInfo:
    return UpdateInfo(version="99.0.0", url="https://example/", assets=tuple(_asset(n) for n in names))


class _FakeResponse(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def _fake_urlopen(files: dict[str, bytes]):
    def _open(req, timeout=None):
        name = req.full_url.rsplit("/", 1)[-1]
        return _FakeResponse(files[name])

    return _open


def _sums(payload: bytes, name: str) -> bytes:
    return f"{hashlib.sha256(payload).hexdigest()}  {name}\n".encode()


def test_detect_install_kind():
    assert detect_install_kind({"APPIMAGE": "/a/M.AppImage"}, "linux", True) is InstallKind.APPIMAGE
    assert detect_install_kind({}, "linux", True) is InstallKind.UNSUPPORTED
    assert detect_install_kind({}, "win32", True) is InstallKind.WINDOWS_INSTALLER
    assert detect_install_kind({}, "win32", False) is InstallKind.UNSUPPORTED
    assert detect_install_kind({}, "darwin", True) is InstallKind.UNSUPPORTED


def test_manual_update_hint():
    assert manual_update_hint("/opt/homebrew/Cellar/muxiveo/4.0.0/bin/python", True) == "brew upgrade muxiveo"
    assert manual_update_hint("/usr/bin/python3", False) == "git pull"
    assert manual_update_hint("/Applications/Muxiveo.app/Contents/MacOS/Muxiveo", True) == ""


def test_select_asset_and_can_self_update():
    info = _info(_APPIMAGE, _SETUP, "SHA256SUMS", "Muxiveo-99.0.0.dmg")
    assert select_asset(info, InstallKind.APPIMAGE, machine="x86_64").name == _APPIMAGE
    assert select_asset(info, InstallKind.APPIMAGE, machine="aarch64") is None
    assert select_asset(info, InstallKind.WINDOWS_INSTALLER).name == _SETUP
    assert select_asset(info, InstallKind.UNSUPPORTED) is None
    assert can_self_update(info, InstallKind.WINDOWS_INSTALLER)
    assert not can_self_update(_info(_SETUP), InstallKind.WINDOWS_INSTALLER)


def test_select_asset_matches_unstable_names():
    unstable = "4.0.0-unstable.20260923.46.51df45c"
    info = _info(f"Muxiveo-x86_64_allinc-{unstable}.AppImage", f"Muxiveo-x86_64_allinc-{unstable}.AppImage.zsync", f"Muxiveo-Setup-{unstable}.exe")
    assert select_asset(info, InstallKind.APPIMAGE, machine="x86_64").name.endswith(".AppImage")
    assert select_asset(info, InstallKind.WINDOWS_INSTALLER).name == f"Muxiveo-Setup-{unstable}.exe"


def test_parse_checksums_accepts_binary_marker():
    digest = "a" * 64
    assert parse_checksums(f"{digest}  a.exe\n{digest.upper()} *b c.AppImage\nbruit\n") == {
        "a.exe": digest,
        "b c.AppImage": digest,
    }


def test_download_asset_verifies_sha256(tmp_path: Path):
    dest = tmp_path / "out.bin"
    seen: list[tuple[int, int]] = []
    with patch.object(update_install.urllib.request, "urlopen", _fake_urlopen({"a.bin": _PAYLOAD})):
        download_asset(_asset("a.bin"), dest, hashlib.sha256(_PAYLOAD).hexdigest(), progress=lambda d, t: seen.append((d, t)))
    assert dest.read_bytes() == _PAYLOAD
    assert seen[-1] == (len(_PAYLOAD), len(_PAYLOAD))


def test_download_asset_rejects_bad_checksum_and_cleans_up(tmp_path: Path):
    dest = tmp_path / "out.bin"
    with patch.object(update_install.urllib.request, "urlopen", _fake_urlopen({"a.bin": _PAYLOAD})):
        with pytest.raises(UpdateInstallError, match="SHA-256"):
            download_asset(_asset("a.bin"), dest, "0" * 64)
    assert list(tmp_path.iterdir()) == []


def test_download_asset_can_be_cancelled(tmp_path: Path):
    with patch.object(update_install.urllib.request, "urlopen", _fake_urlopen({"a.bin": _PAYLOAD})):
        with pytest.raises(UpdateInstallError, match="annulé"):
            download_asset(_asset("a.bin"), tmp_path / "out.bin", "0" * 64, cancelled=lambda: True)
    assert list(tmp_path.iterdir()) == []


def test_cancel_after_last_chunk_does_not_publish_download(tmp_path: Path):
    cancelled = False

    def progress(received, total):
        nonlocal cancelled
        cancelled = received == total

    with patch.object(update_install.urllib.request, "urlopen", _fake_urlopen({"a.bin": _PAYLOAD})):
        with pytest.raises(UpdateInstallError, match="annulé"):
            download_asset(
                _asset("a.bin"), tmp_path / "out.bin", hashlib.sha256(_PAYLOAD).hexdigest(),
                progress=progress, cancelled=lambda: cancelled,
            )
    assert list(tmp_path.iterdir()) == []


def test_download_asset_refuses_foreign_url(tmp_path: Path):
    foreign = ReleaseAsset(name="x", url="https://evil.example/x")
    with pytest.raises(UpdateInstallError, match="refusée"):
        download_asset(foreign, tmp_path / "x", "0" * 64)


def test_download_update_requires_checksums(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "Muxiveo.AppImage"))
    with pytest.raises(UpdateInstallError, match="SHA256SUMS"):
        download_update(_info(_APPIMAGE), InstallKind.APPIMAGE)


def test_appimage_update_end_to_end(tmp_path: Path, monkeypatch):
    current = tmp_path / "Muxiveo.AppImage"
    current.write_bytes(b"ancienne version")
    monkeypatch.setenv("APPIMAGE", str(current))
    monkeypatch.setattr(update_install.platform, "machine", lambda: "x86_64")
    files = {_APPIMAGE: _PAYLOAD, "SHA256SUMS": _sums(_PAYLOAD, _APPIMAGE)}
    with patch.object(update_install.urllib.request, "urlopen", _fake_urlopen(files)):
        downloaded = download_update(_info(_APPIMAGE, "SHA256SUMS"), InstallKind.APPIMAGE)
    assert downloaded.parent == tmp_path
    with patch.object(update_install.subprocess, "Popen") as popen:
        apply_update(InstallKind.APPIMAGE, downloaded)
    assert current.read_bytes() == _PAYLOAD
    assert current.stat().st_mode & 0o111
    assert not downloaded.exists()
    assert popen.call_args.args[0] == [str(current)]


def test_windows_update_launches_installer(tmp_path: Path, monkeypatch):
    setup = tmp_path / _SETUP
    setup.write_bytes(b"setup")
    calls: list[str] = []
    monkeypatch.setattr(update_install.os, "startfile", calls.append, raising=False)
    apply_update(InstallKind.WINDOWS_INSTALLER, setup)
    assert calls == [str(setup)]


def test_apply_update_unsupported_raises(tmp_path: Path):
    with pytest.raises(UpdateInstallError):
        apply_update(InstallKind.UNSUPPORTED, tmp_path / "x")
