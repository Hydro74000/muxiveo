"""Tests de core/update_install.py (réseau et processus mockés)."""

from __future__ import annotations

import hashlib
import json
import os
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from core import update_apply, update_install
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


def _selected(*args, **kwargs):
    asset = select_asset(*args, **kwargs)
    assert asset is not None
    return asset


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
    assert detect_install_kind({}, "darwin", True) is InstallKind.MACOS_APP
    assert detect_install_kind({}, "darwin", False) is InstallKind.UNSUPPORTED        # Homebrew, sources
    assert detect_install_kind({"FLATPAK_ID": "io.x", "APPIMAGE": "/a"}, "linux", False) is InstallKind.FLATPAK
    cellar = "/opt/homebrew/Cellar/muxiveo/5.0/Muxiveo.app/Contents/MacOS/Muxiveo"
    assert detect_install_kind({}, "darwin", True, executable=cellar) is InstallKind.UNSUPPORTED


def test_detect_source_archive_by_marker_never_git_checkout(tmp_path):
    (tmp_path / update_apply.MARKER_NAME).write_text("{}")
    for platform_name in ("linux", "win32", "darwin"):
        assert detect_install_kind({}, platform_name, False, executable="/usr/bin/python3",
                                   source_root=tmp_path) is InstallKind.SOURCE_ARCHIVE
    assert detect_install_kind({}, "darwin", False, executable="/opt/homebrew/bin/python3",
                               source_root=tmp_path) is InstallKind.UNSUPPORTED
    (tmp_path / ".git").mkdir()
    assert detect_install_kind({}, "linux", False, executable="/usr/bin/python3",
                               source_root=tmp_path) is InstallKind.UNSUPPORTED


def test_manual_update_hint():
    assert manual_update_hint("/opt/homebrew/Cellar/muxiveo/4.0.0/bin/python", True) == "brew upgrade muxiveo"
    assert manual_update_hint("/usr/bin/python3", False) == "git pull"
    assert manual_update_hint("/Applications/Muxiveo.app/Contents/MacOS/Muxiveo", True) == ""


def test_manual_update_hint_git_only_for_checkouts(tmp_path):
    assert manual_update_hint("/usr/bin/python3", False, source_root=tmp_path) == ""
    (tmp_path / ".git").mkdir()
    assert manual_update_hint("/usr/bin/python3", False, source_root=tmp_path) == "git pull"


def test_select_asset_and_can_self_update():
    info = _info(_APPIMAGE, _SETUP, "SHA256SUMS", "Muxiveo-99.0.0.dmg")
    assert _selected(info, InstallKind.APPIMAGE, machine="x86_64").name == _APPIMAGE
    assert select_asset(info, InstallKind.APPIMAGE, machine="aarch64") is None
    assert _selected(info, InstallKind.WINDOWS_INSTALLER).name == _SETUP
    assert select_asset(info, InstallKind.UNSUPPORTED) is None
    assert can_self_update(info, InstallKind.WINDOWS_INSTALLER)
    assert not can_self_update(_info(_SETUP), InstallKind.WINDOWS_INSTALLER)
    for kind in (InstallKind.FLATPAK, InstallKind.UNSUPPORTED):           # gestionnaire externe
        assert not can_self_update(info, kind), kind


def test_swap_assets_selected():
    info = _info("Muxiveo-99.0.0-source.tar.gz", "Muxiveo-99.0.0-source.zip", "Muxiveo-99.0.0.dmg",
                 "Muxiveo-99.0.0-homebrew-macos.tar.gz", "SHA256SUMS")
    assert _selected(info, InstallKind.SOURCE_ARCHIVE).name == "Muxiveo-99.0.0-source.tar.gz"
    assert _selected(info, InstallKind.MACOS_APP).name == "Muxiveo-99.0.0.dmg"


def test_appimage_self_update_needs_writable_folder(tmp_path, monkeypatch):
    appimage = tmp_path / _APPIMAGE
    appimage.write_bytes(b"old")
    monkeypatch.setenv("APPIMAGE", str(appimage))
    info = _info(_APPIMAGE, "SHA256SUMS")
    monkeypatch.setattr(update_install.platform, "machine", lambda: "x86_64")
    assert update_install.self_install_blocker(InstallKind.APPIMAGE) == ""
    assert can_self_update(info, InstallKind.APPIMAGE)
    monkeypatch.setattr(update_install.os, "access", lambda path, mode: False)
    assert update_install.self_install_blocker(InstallKind.APPIMAGE) == update_install.BLOCKER_NOT_WRITABLE
    assert not can_self_update(info, InstallKind.APPIMAGE)


def test_blockers_for_macos_and_unmarked_sources(tmp_path, monkeypatch):
    translocated = Path("/private/var/folders/x/AppTranslocation/ABC/d/Muxiveo.app")
    monkeypatch.setattr(update_install, "installation_root", lambda kind: translocated)
    assert update_install.self_install_blocker(InstallKind.MACOS_APP) == update_install.BLOCKER_TRANSLOCATED
    monkeypatch.setattr(update_install, "installation_root", lambda kind: tmp_path / "Muxiveo.app")
    monkeypatch.setattr(update_install.shutil, "which", lambda name: None)
    assert update_install.self_install_blocker(InstallKind.MACOS_APP) == update_install.BLOCKER_NO_HDIUTIL
    monkeypatch.setattr(update_install, "_SOURCE_ROOT", tmp_path)          # sources sans .git ni marqueur
    monkeypatch.setattr(update_install.sys, "frozen", False, raising=False)
    monkeypatch.setattr(update_install.sys, "executable", "/usr/bin/python3")
    assert update_install.self_install_blocker(InstallKind.UNSUPPORTED) == update_install.BLOCKER_NO_MARKER
    (tmp_path / ".git").mkdir()
    assert update_install.self_install_blocker(InstallKind.UNSUPPORTED) == ""


def test_download_update_prepares_swap_workdir_and_cleans_on_failure(tmp_path, monkeypatch):
    install = tmp_path / "Muxiveo"
    install.mkdir()
    monkeypatch.setattr(update_install, "installation_root", lambda kind: install)
    monkeypatch.setattr(update_install, "fetch_expected_sha256", lambda info, asset: "0" * 64)
    monkeypatch.setattr(update_install, "download_asset",
                        lambda asset, dest, expected, progress, cancelled: dest.write_bytes(b"x") or dest)
    calls = []
    monkeypatch.setattr(update_apply, "prepare", lambda *args: calls.append(args))
    info = _info("Muxiveo-99.0.0-source.tar.gz", "SHA256SUMS")
    work = download_update(info, InstallKind.SOURCE_ARCHIVE)
    assert work.parent == tmp_path and work.name.startswith(update_apply.WORKDIR_PREFIX)
    assert (work / update_apply.WORKDIR_TAG).is_file()
    kind, archive, prepared_work, root, version, probe, relaunch, _bundle = calls[0]
    assert (kind, prepared_work, root, version) == (update_apply.KIND_SOURCE, work, install, "99.0.0")
    assert probe[-1].endswith("main.py") and update_apply.ROOT_TOKEN in probe[-1]

    def refuse(*args):
        raise update_apply.UpdateApplyError("archive inattendue")

    monkeypatch.setattr(update_apply, "prepare", refuse)
    with pytest.raises(UpdateInstallError, match="archive inattendue"):
        download_update(info, InstallKind.SOURCE_ARCHIVE)
    assert [p.name for p in tmp_path.iterdir() if p != work] == ["Muxiveo"]   # dossier de travail supprimé


def test_apply_update_starts_isolated_helper_and_waits_until_ready(tmp_path, monkeypatch):
    work = tmp_path / f"{update_apply.WORKDIR_PREFIX}x"
    work.mkdir()
    launched = {}

    class FakeHelper:
        def __init__(self, command, **kwargs):
            launched.update(command=command, **kwargs)
            (work / update_apply.READY_FILE).touch()

        def poll(self):
            return None

    monkeypatch.setattr(update_install.subprocess, "Popen", FakeHelper)
    apply_update(InstallKind.SOURCE_ARCHIVE, work)
    command = launched["command"]
    assert command[1:3] == ["-I", str(work / "update_apply.py")] and (work / "update_apply.py").is_file()
    assert command[3:] == ["commit", str(work), "--pid", str(os.getpid())] and launched["cwd"] == work


def test_apply_update_reports_helper_failure(tmp_path, monkeypatch):
    work = tmp_path / f"{update_apply.WORKDIR_PREFIX}x"
    work.mkdir()

    class DeadHelper:
        def __init__(self, command, **kwargs):
            (work / update_apply.RESULT_FILE).write_text(json.dumps({"ok": False, "error": "préparation absente"}))

        def poll(self):
            return 1

    monkeypatch.setattr(update_install.subprocess, "Popen", DeadHelper)
    with pytest.raises(UpdateInstallError, match="préparation absente"):
        apply_update(InstallKind.SOURCE_ARCHIVE, work)
    assert not work.exists()


def test_select_asset_matches_unstable_names():
    unstable = "4.0.0-unstable.20260923.46.51df45c"
    info = _info(f"Muxiveo-x86_64_allinc-{unstable}.AppImage", f"Muxiveo-x86_64_allinc-{unstable}.AppImage.zsync", f"Muxiveo-Setup-{unstable}.exe")
    assert _selected(info, InstallKind.APPIMAGE, machine="x86_64").name.endswith(".AppImage")
    assert _selected(info, InstallKind.WINDOWS_INSTALLER).name == f"Muxiveo-Setup-{unstable}.exe"


def test_select_asset_distinguishes_standard_and_allinc_windows(tmp_path, monkeypatch):
    import sys
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    exe = exe_dir / "Muxiveo.exe"
    exe.write_bytes(b"MZ")

    info = _info(
        "Muxiveo-Setup-99.0.0.exe",
        "Muxiveo-Setup-AllInc-99.0.0.exe",
        "Muxiveo-Windows-x64-allinc-99.0.0.zip",
        "SHA256SUMS",
    )

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    # Sans marqueur _ALLINC -> sélectionne l'installeur standard
    assert _selected(info, InstallKind.WINDOWS_INSTALLER).name == "Muxiveo-Setup-99.0.0.exe"

    # Avec marqueur _ALLINC -> sélectionne l'installeur AllInc
    (exe_dir / "_ALLINC").touch()
    assert _selected(info, InstallKind.WINDOWS_INSTALLER).name == "Muxiveo-Setup-AllInc-99.0.0.exe"


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
    monkeypatch.setattr(update_install.platform, "machine", lambda: "x86_64")
    with pytest.raises(UpdateInstallError, match="SHA256SUMS"):
        download_update(_info(_APPIMAGE), InstallKind.APPIMAGE)


@pytest.mark.skipif(os.name == "nt", reason="bit exécutable POSIX (AppImage Linux uniquement)")
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


def test_apply_update_wraps_helper_launch_errors(tmp_path, monkeypatch):
    """Interpréteur absent (ou copie impossible) : erreur signalée, dossier de travail retiré."""
    work = tmp_path / f"{update_apply.WORKDIR_PREFIX}x"
    work.mkdir()

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("python introuvable")

    monkeypatch.setattr(update_install.subprocess, "Popen", missing)
    with pytest.raises(UpdateInstallError, match="python introuvable"):
        apply_update(InstallKind.SOURCE_ARCHIVE, work)
    assert not work.exists()
