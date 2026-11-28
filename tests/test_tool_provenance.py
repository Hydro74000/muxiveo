"""Provenance et intégrité des outils embarqués par le packaging (A25)."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.error
import zipfile
from pathlib import Path

import pytest

import core.github_release as gh
import package as package_mod
import package_appimage as appimage_mod
from core.github_release import ReleaseAsset
from core.tool_manifest import MANIFEST_NAME, ToolManifest
from core.version import MUXIVEO_PLUGINS_REPOSITORY, MVO_RIFE_RELEASE_TAG, MVO_RIFE_VERSION


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tar(files: dict[str, bytes], *, mode: str = "w:gz") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def _serve(monkeypatch, module, function: str, payloads: dict[str, bytes]):
    """Téléchargements simulés : URL → contenu écrit à destination."""
    def fake(url, dest, timeout=30):
        Path(dest).write_bytes(payloads[url])
    monkeypatch.setattr(module, function, fake)


# ---------------------------------------------------------------------------
# Helpers communs
# ---------------------------------------------------------------------------

def test_github_release_asset_requires_published_digest(monkeypatch):
    release = {"tag_name": "latest", "assets": [{
        "name": "ffmpeg-master-latest-linux64-gpl.tar.xz",
        "browser_download_url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
    }]}
    monkeypatch.setattr(gh, "fetch_release_by_repo", lambda repo, tag: release)
    with pytest.raises(RuntimeError, match="aucun SHA-256"):
        gh.github_release_asset("BtbN/FFmpeg-Builds", "latest", "linux64-gpl.tar.xz")
    release["assets"][0]["digest"] = "sha256:" + "a" * 64
    asset = gh.github_release_asset("BtbN/FFmpeg-Builds", "latest", "linux64-gpl.tar.xz")
    assert asset.sha256 == "a" * 64 and asset.tag == "latest"


def test_published_checksum_parsing(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    digest = "b" * 64
    monkeypatch.setattr(gh.urllib.request, "urlopen", lambda *_a, **_k: Response(f"{digest}  file.zip\n".encode()))
    assert gh.published_checksum("https://example.invalid/file.zip.sha256") == digest
    monkeypatch.setattr(gh.urllib.request, "urlopen", lambda *_a, **_k: Response(b"<html>404</html>"))
    with pytest.raises(RuntimeError):
        gh.published_checksum("https://example.invalid/file.zip.sha256")


def test_manifest_lists_tools_with_hashes(tmp_path):
    binary = tmp_path / "ffmpeg"
    binary.write_bytes(b"bin")
    manifest = ToolManifest()
    manifest.add("ffmpeg", version="BtbN/FFmpeg-Builds@latest", source="https://x/ffmpeg.tar.xz",
                 verification="github-digest", archive=binary, files=[binary])
    path = manifest.write(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == MANIFEST_NAME and data["version"] == 1
    assert data["tools"][0]["files"] == {"ffmpeg": _sha(b"bin")}


# ---------------------------------------------------------------------------
# AppImage
# ---------------------------------------------------------------------------

def _rife_asset(data: bytes, suffix: str, *, sha: str | None = None) -> ReleaseAsset:
    name = f"mvo-rife-{MVO_RIFE_VERSION}-{suffix}"
    url = f"https://github.com/{MUXIVEO_PLUGINS_REPOSITORY}/releases/download/{MVO_RIFE_RELEASE_TAG}/{name}"
    return ReleaseAsset(MUXIVEO_PLUGINS_REPOSITORY, MVO_RIFE_RELEASE_TAG, name, url, sha or _sha(data))


def test_appimage_rife_verified_and_recorded(tmp_path, monkeypatch):
    archive = _tar({"muxiveo-rife-x/muxiveo-rife": b"rife", "muxiveo-rife-x/rife-models/m.bin": b"m"})
    asset = _rife_asset(archive, "linux-x86_64.tar.gz")
    monkeypatch.delenv("MUXIVEO_RIFE_ARCHIVE", raising=False)
    monkeypatch.setattr(appimage_mod, "github_release_asset", lambda *_a: asset)
    _serve(monkeypatch, appimage_mod, "_download", {asset.url: archive})
    appimage_mod.TOOL_MANIFEST.entries.clear()
    appimage_mod._dl_muxiveo_rife(tmp_path, "x86_64")
    assert (tmp_path / "muxiveo-rife").read_bytes() == b"rife"
    entry = appimage_mod.TOOL_MANIFEST.entries[-1]
    assert entry["tool"] == "muxiveo-rife" and entry["verification"] == "github-digest"
    assert entry["archive_sha256"] == asset.sha256


def test_appimage_rife_digest_mismatch_refused(tmp_path, monkeypatch):
    archive = _tar({"x/muxiveo-rife": b"rife"})
    asset = _rife_asset(archive, "linux-x86_64.tar.gz", sha="0" * 64)
    monkeypatch.delenv("MUXIVEO_RIFE_ARCHIVE", raising=False)
    monkeypatch.setattr(appimage_mod, "github_release_asset", lambda *_a: asset)
    _serve(monkeypatch, appimage_mod, "_download", {asset.url: archive})
    with pytest.raises(RuntimeError, match="SHA-256 invalide"):
        appimage_mod._dl_muxiveo_rife(tmp_path, "x86_64")
    assert not (tmp_path / "muxiveo-rife").exists()


def test_appimage_rife_network_error_keeps_optional_behaviour(tmp_path, monkeypatch):
    monkeypatch.delenv("MUXIVEO_RIFE_ARCHIVE", raising=False)

    def offline(*_args):
        raise urllib.error.URLError("hors ligne")

    monkeypatch.setattr(appimage_mod, "github_release_asset", offline)
    appimage_mod.TOOL_MANIFEST.entries.clear()
    appimage_mod._dl_muxiveo_rife(tmp_path, "x86_64")
    assert not (tmp_path / "muxiveo-rife").exists()
    assert appimage_mod.TOOL_MANIFEST.entries == []


_MASTER = {"build": "N-129166-gae9f4540cb-20261121", "version_line": "ffmpeg version N-129166-gae9f4540cb-20261121",
           "libavformat": "63.21.100", "libavcodec": "63.28.100"}


def _fake_details(calls: list | None = None, build: str = _MASTER["build"]):
    def details(binary, *, launcher=(), env=None, timeout=60):
        if calls is not None:
            calls.append((Path(binary).name, tuple(launcher)))
        return {**_MASTER, "build": build}
    return details


def test_appimage_ffmpeg_verified_against_github_digest(tmp_path, monkeypatch):
    archive = _tar({"ffmpeg-master/bin/ffmpeg": b"ff", "ffmpeg-master/bin/ffprobe": b"fp"}, mode="w:xz")
    url = "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
    asset = ReleaseAsset("FFmpeg-Builds/FFmpeg-Builds", "latest", Path(url).name, url, _sha(archive),
                         "2026-11-21T04:12:00Z")
    monkeypatch.setattr(appimage_mod, "github_release_asset", lambda *_a: asset)
    monkeypatch.setattr(appimage_mod, "ffmpeg_build_details", _fake_details())
    _serve(monkeypatch, appimage_mod, "_download", {url: archive})
    appimage_mod.TOOL_MANIFEST.entries.clear()
    appimage_mod._dl_ffmpeg(tmp_path, "x86_64")
    entry = appimage_mod.TOOL_MANIFEST.entries[-1]
    assert entry["verification"] == "github-digest"
    assert entry["files"] == {"ffmpeg": _sha(b"ff"), "ffprobe": _sha(b"fp")}
    # Build réelle consignée, pas seulement « latest ».
    assert entry["version"] == _MASTER["build"]
    assert entry["details"]["release"] == "FFmpeg-Builds/FFmpeg-Builds@latest"
    assert entry["details"]["asset_updated_at"] == "2026-11-21T04:12:00Z"
    assert entry["details"]["libavformat"] == "63.21.100"
    assert entry["details"]["ffprobe_build"] == _MASTER["build"]


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def test_windows_ffmpeg_master_only_without_fallback(tmp_path, monkeypatch):
    def offline(*_args):
        raise urllib.error.URLError("FFmpeg-Builds indisponible")

    monkeypatch.setattr(package_mod, "github_release_asset", offline)
    package_mod.TOOL_MANIFEST.entries.clear()
    with pytest.raises(RuntimeError, match="FFmpeg master .* indisponible"):
        package_mod._dl_windows_ffmpeg(tmp_path)
    assert not (tmp_path / "ffmpeg.exe").exists()
    assert not hasattr(package_mod, "_GYAN_FFMPEG_URL")


def test_windows_ffmpeg_build_probed_through_wine_when_cross_building(tmp_path, monkeypatch):
    archive = _zip({"ffmpeg/bin/ffmpeg.exe": b"ff", "ffmpeg/bin/ffprobe.exe": b"fp"})
    url = "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    asset = ReleaseAsset("FFmpeg-Builds/FFmpeg-Builds", "latest", Path(url).name, url, _sha(archive), "2026-11-21")
    calls: list = []
    monkeypatch.setattr(package_mod, "github_release_asset", lambda *_a: asset)
    monkeypatch.setattr(package_mod, "ffmpeg_build_details", _fake_details(calls))
    monkeypatch.setattr(package_mod.os, "name", "posix")
    monkeypatch.setattr(package_mod.shutil, "which", lambda name: "/usr/bin/wine" if name == "wine" else None)
    _serve(monkeypatch, package_mod, "_download_file", {url: archive})
    package_mod.TOOL_MANIFEST.entries.clear()
    package_mod._dl_windows_ffmpeg(tmp_path)
    assert calls == [("ffmpeg.exe", ("wine",)), ("ffprobe.exe", ("wine",))]
    entry = package_mod.TOOL_MANIFEST.entries[-1]
    assert entry["version"] == _MASTER["build"]
    assert entry["details"]["asset_updated_at"] == "2026-11-21"


def test_windows_preexisting_ffmpeg_must_be_master(tmp_path, monkeypatch):
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (tmp_path / name).write_bytes(b"old")
    monkeypatch.setattr(package_mod, "ffmpeg_build_details", _fake_details(build="8.1"))
    package_mod.TOOL_MANIFEST.entries.clear()
    with pytest.raises(RuntimeError, match="pas en build master"):
        package_mod._dl_windows_ffmpeg(tmp_path)
    monkeypatch.setattr(package_mod, "ffmpeg_build_details", _fake_details())
    package_mod._dl_windows_ffmpeg(tmp_path)
    entry = package_mod.TOOL_MANIFEST.entries[-1]
    assert entry["verification"] == "preexisting"
    assert entry["version"] == _MASTER["build"]


def test_windows_rife_verified_and_manifest_written(tmp_path, monkeypatch):
    archive = _zip({"muxiveo-rife/muxiveo-rife.exe": b"rife", "muxiveo-rife/rife-models/m.bin": b"m"})
    asset = _rife_asset(archive, "windows-x86_64.zip")
    monkeypatch.delenv("MUXIVEO_RIFE_ARCHIVE", raising=False)
    monkeypatch.setattr(package_mod, "github_release_asset", lambda *_a: asset)
    _serve(monkeypatch, package_mod, "_download_file", {asset.url: archive})
    package_mod.TOOL_MANIFEST.entries.clear()
    package_mod._dl_windows_muxiveo_rife(tmp_path)
    assert (tmp_path / "muxiveo-rife.exe").read_bytes() == b"rife"
    path = package_mod.TOOL_MANIFEST.write(tmp_path)
    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    assert [t["tool"] for t in tools] == ["muxiveo-rife"]
    assert tools[0]["files"] == {"muxiveo-rife.exe": _sha(b"rife")}
