"""Registre des extensions : flux des versions publiées, choix de la version, extension mvo-rife."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from core import plugins
from core.github_release import ReleaseAsset
from core.plugins import _local_base
from core.plugins import fetch_feed as real_fetch_feed  # neutralisée par tests/conftest.py
from core.version import MVO_RIFE_CONTRACT, MVO_RIFE_VERSION

PLATFORM = "linux-x86_64"


@pytest.fixture(autouse=True)
def _linux_platform(monkeypatch):
    monkeypatch.setattr(plugins, "platform_tag", lambda *_a, **_k: PLATFORM)


def _rife_archive(tmp_path: Path, *, version: str = MVO_RIFE_VERSION, contract: int = MVO_RIFE_CONTRACT) -> ReleaseAsset:
    files = {"muxiveo-rife": b"#!binaire", "rife-models/selector.txt": b"poids", "presets.json": b"{}"}
    manifest = {
        "name": "mvo-rife", "version": version, "contract": contract, "platform": PLATFORM,
        "executable": "muxiveo-rife", "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()},
    }
    top = f"mvo-rife-{version}-{PLATFORM}"
    archive = tmp_path / f"{top}.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        for name, data in [*files.items(), ("manifest.json", json.dumps(manifest).encode())]:
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            info.mode = 0o755
            t.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return ReleaseAsset("Hydro74000/muxiveo-plugins", "tag", archive.name, archive.as_uri(), digest)


def _entry(version: str, *, contract: int = MVO_RIFE_CONTRACT, platforms=(PLATFORM,)) -> dict:
    return {"version": version, "tag": f"mvo-rife-v{version}", "contract": contract,
            "platforms": [{"platform": p, "asset": f"mvo-rife-{version}-{p}.tar.gz", "size": 1} for p in platforms]}


def test_registry_lists_extensions():
    assert list(plugins.EXTENSIONS) == ["mvo-rife", "mvo-rife-trt", "mvo-fel"]
    assert plugins.RIFE.supports("macos-arm64") and not plugins.TRT.supports("macos-arm64")
    assert plugins.RIFE.tag("1.7.0") == "mvo-rife-v1.7.0"


def test_macos_locations_follow_platform_conventions():
    home = Path.home()
    assert _local_base({}, "darwin", False) == home / "Library" / "Application Support" / "Muxiveo"
    assert _local_base({}, "darwin", True) == home / "Library" / "Caches" / "Muxiveo"


def test_version_key_orders_numerically():
    assert plugins.version_key("1.10.0") > plugins.version_key("1.9.3")
    assert plugins.version_key("1.7") == plugins.version_key("1.7.0")


def test_parse_feed_keeps_valid_entries_only():
    feed = {"schema": 1, "name": "mvo-rife", "releases": [
        _entry("1.8.0"), {"version": "1.9.0", "tag": "autre-v1.9.0", "platforms": []}, {"version": "x"}, "texte",
    ]}
    assert [e["version"] for e in plugins.parse_feed(plugins.RIFE, feed) or []] == ["1.8.0"]
    assert plugins.parse_feed(plugins.RIFE, {**feed, "schema": 2}) is None
    assert plugins.parse_feed(plugins.RIFE, {**feed, "name": "mvo-rife-trt"}) is None


def test_target_version_picks_newest_compatible_for_platform():
    feed = [
        _entry("9.0.0", contract=MVO_RIFE_CONTRACT + 1),        # contrat inconnu de cette version de Muxiveo
        _entry("8.0.0", platforms=("windows-x86_64",)),          # absente pour cette plate-forme
        _entry("7.0.0"),
        _entry("1.0.0"),                                         # sous la version épinglée
    ]
    assert plugins.target_version(plugins.RIFE, feed, PLATFORM) == "7.0.0"
    assert plugins.target_version(plugins.RIFE, None, PLATFORM) == MVO_RIFE_VERSION
    assert plugins.target_version(plugins.RIFE, [_entry("1.0.0")], PLATFORM) == MVO_RIFE_VERSION


def test_fetch_feed_reads_published_json(monkeypatch):
    payload = json.dumps({"schema": 1, "name": "mvo-rife", "releases": [_entry("7.0.0")]}).encode()
    seen: list[str] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(req, timeout=0):
        seen.append(req.full_url)
        return _Resp(payload)

    monkeypatch.setattr(plugins.urllib.request, "urlopen", _urlopen)
    entries = real_fetch_feed(plugins.RIFE)
    assert entries and entries[0]["version"] == "7.0.0"
    assert seen == ["https://github.com/Hydro74000/muxiveo-plugins/releases/download/extensions-feed/mvo-rife.json"]
    monkeypatch.setattr(plugins.urllib.request, "urlopen", lambda *_a, **_k: _Resp(b"{pas du json"))
    assert real_fetch_feed(plugins.RIFE) is None


def test_install_rife_extension_and_expose_executable(tmp_path):
    root = tmp_path / "plugins"
    installed = plugins.install(plugins.RIFE, root, asset=_rife_archive(tmp_path))
    assert installed.version == MVO_RIFE_VERSION
    assert plugins.rife_executable(root) == installed.path / "muxiveo-rife"
    assert plugins.main_file(plugins.RIFE, installed, PLATFORM).read_bytes() == b"#!binaire"
    assert not plugins.update_available(plugins.RIFE, root=root)
    assert plugins.update_available(plugins.RIFE, "99.0.0", root=root)
    assert plugins.remove(plugins.RIFE, root) and plugins.rife_executable(root) is None


def test_install_refuses_unknown_contract(tmp_path):
    with pytest.raises(plugins.PluginError, match="contrat"):
        plugins.install(plugins.RIFE, tmp_path / "plugins", asset=_rife_archive(tmp_path, contract=MVO_RIFE_CONTRACT + 1))


def test_install_requested_version_must_match_archive(tmp_path):
    with pytest.raises(plugins.PluginError, match="version"):
        plugins.install(plugins.RIFE, tmp_path / "plugins", version="9.9.9", asset=_rife_archive(tmp_path))


def test_rife_install_keeps_trt_engine_cache(tmp_path):
    cache = plugins.trt_engine_cache_dir()
    cache.mkdir(parents=True)
    (cache / "moteur.bin").write_bytes(b"x")
    plugins.install(plugins.RIFE, tmp_path / "plugins", asset=_rife_archive(tmp_path))
    assert (cache / "moteur.bin").is_file()
