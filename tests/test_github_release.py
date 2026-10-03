"""
tests/test_github_release.py — Outils tiers : release dernière/tag, sélection d'asset et SHA-256 obligatoire.
"""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.github_release import release_api_url, requested_tag, select_asset, verify_download

REPO = "quietvoid/dovi_tool"


def _asset(name: str, data: bytes | None = b"x", *, repo: str = REPO, tag: str = "9.9.9") -> dict:
    entry = {"name": name, "browser_download_url": f"https://github.com/{repo}/releases/download/{tag}/{name}"}
    if data is not None:
        entry["digest"] = f"sha256:{hashlib.sha256(data).hexdigest()}"
    return entry


def _release(*assets: dict, tag: str = "9.9.9") -> dict:
    return {"tag_name": tag, "assets": list(assets)}


def test_latest_release_by_default_and_tag_from_env(monkeypatch) -> None:
    monkeypatch.delenv("MUXIVEO_DOVI_TOOL_TAG", raising=False)
    assert requested_tag("dovi_tool") is None
    assert release_api_url(REPO, None).endswith("/releases/latest")
    monkeypatch.setenv("MUXIVEO_DOVI_TOOL_TAG", " 2.1.0 ")
    assert requested_tag("dovi_tool") == "2.1.0"
    assert release_api_url(REPO, "2.1.0").endswith("/releases/tags/2.1.0")


def test_select_asset_excludes_sidecars_and_requires_uniqueness() -> None:
    release = _release(
        _asset("dovi_tool-9.9.9-x86_64-unknown-linux-musl.tar.gz"),
        _asset("dovi_tool-9.9.9-x86_64-unknown-linux-musl.tar.gz.sha256"),
        _asset("dovi_tool-9.9.9-x86_64-pc-windows-msvc.zip"),
        _asset("libdovi-3.4.0-x86_64-pc-windows-msvc.zip"),
    )
    asset = select_asset(release, REPO, "x86_64-unknown-linux-musl")
    assert asset.name.endswith(".tar.gz") and asset.tag == "9.9.9"
    with pytest.raises(RuntimeError, match="2 asset"):
        select_asset(release, REPO, "x86_64-pc-windows-msvc.zip")


def test_select_asset_refuses_missing_digest_or_foreign_url() -> None:
    with pytest.raises(RuntimeError, match="aucun SHA-256"):
        select_asset(_release(_asset("dovi_tool-linux.tar.gz", None)), REPO, "linux")
    with pytest.raises(RuntimeError, match="URL inattendue"):
        select_asset(_release(_asset("dovi_tool-linux.tar.gz", repo="evil/fork")), REPO, "linux")


def test_verify_download_refuses_mismatch(tmp_path: Path) -> None:
    asset = select_asset(_release(_asset("nvencc_9.9_amd64.deb", b"bon")), REPO, "_amd64.deb")
    fake = tmp_path / asset.name
    fake.write_bytes(b"bon")
    verify_download(asset, fake)
    fake.write_bytes(b"altere")
    with pytest.raises(RuntimeError, match="SHA-256 invalide"):
        verify_download(asset, fake)


# ---------------------------------------------------------------------------
# setup.py
# ---------------------------------------------------------------------------

def _fake_tool_archive(path: Path) -> bytes:
    payload_dir = path.parent / "payload"
    payload_dir.mkdir(exist_ok=True)
    (payload_dir / "dovi_tool").write_text("#!/bin/sh\n", encoding="utf-8")
    with tarfile.open(path, "w:gz") as tar:
        tar.add(payload_dir / "dovi_tool", arcname="dovi_tool")
    return path.read_bytes()


@pytest.mark.parametrize("case", ["ok", "tampered", "no_digest"])
def test_setup_installs_third_party_tool_only_with_matching_sha256(
    tmp_path: Path, monkeypatch, case: str,
) -> None:
    import setup as setup_mod

    good_bytes = _fake_tool_archive(tmp_path / "ref.tar.gz")
    asset_name = "dovi_tool-9.9.9-x86_64-unknown-linux-musl.tar.gz"
    release = _release(_asset(asset_name, None if case == "no_digest" else good_bytes))
    requested: list[str] = []
    downloaded: list[str] = []

    def fake_release_json(url: str) -> dict:
        requested.append(url)
        return release

    def fake_download(url: str, dest: Path) -> None:
        downloaded.append(url)
        dest.write_bytes(good_bytes + (b"x" if case == "tampered" else b""))

    monkeypatch.setenv("MUXIVEO_DOVI_TOOL_TAG", "9.9.9")
    prefix = tmp_path / "prefix"
    with patch.object(setup_mod, "OS", "Linux"), \
         patch.object(setup_mod, "_arch_key", return_value="x86_64"), \
         patch.object(setup_mod.shutil, "which", return_value=None), \
         patch.object(setup_mod, "is_root", return_value=True), \
         patch.object(setup_mod, "_github_release_json", side_effect=fake_release_json), \
         patch.object(setup_mod, "_download_file", side_effect=fake_download), \
         patch.object(setup_mod, "_config_ini_path", return_value=tmp_path / "config.ini"), \
         patch.object(setup_mod, "_update_ini_tools_section"):
        setup_mod.install_github_tools(prefix, dry_run=False, force=False, tool_names={"dovi_tool"})

    assert requested == ["https://api.github.com/repos/quietvoid/dovi_tool/releases/tags/9.9.9"]
    assert downloaded == [release["assets"][0]["browser_download_url"]]
    assert (prefix / "bin" / "dovi_tool").exists() is (case == "ok")
