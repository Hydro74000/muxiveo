"""Tests de scripts/write_update_feed.py (flux de mise à jour écrit par la CI)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from core import update_check

_SPEC = importlib.util.spec_from_file_location(
    "write_update_feed", Path(__file__).resolve().parent.parent / "scripts" / "write_update_feed.py"
)
assert _SPEC and _SPEC.loader
feed_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["write_update_feed"] = feed_mod
_SPEC.loader.exec_module(feed_mod)

_TAG = "v4.3.0-unstable.20261009.160.abc1234"


def _artifacts(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir()
    (d / "Muxiveo-x86_64.AppImage").write_bytes(b"x" * 10)
    (d / "SHA256SUMS").write_text("0 Muxiveo-x86_64.AppImage\n")
    return d


def test_feed_lists_published_files_and_is_read_by_the_app(tmp_path):
    out = tmp_path / "feed" / "unstable.json"
    rc = feed_mod.main(["--channel", "unstable", "--tag", _TAG, "--repo", "Hydro74000/Muxiveo",
                        "--artifacts", str(_artifacts(tmp_path)), "--out", str(out)])
    assert rc == 0
    feed = json.loads(out.read_text())
    assert feed["schema"] == update_check.UPDATE_FEED_SCHEMA and feed["version"] == _TAG[1:]
    assert [a["name"] for a in feed["assets"]] == ["Muxiveo-x86_64.AppImage", "SHA256SUMS"]
    assert feed["assets"][0]["size"] == 10
    assert feed["assets"][0]["url"].endswith(f"/releases/download/{_TAG}/Muxiveo-x86_64.AppImage")
    # format lu tel quel par l'application (même dépôt, sous réserve de la casse du nom)
    info = update_check._parse_feed(feed, "unstable")
    assert info is not None and info.version == _TAG[1:] and info.prerelease
    assert [a.name for a in info.assets] == ["Muxiveo-x86_64.AppImage", "SHA256SUMS"]


@pytest.mark.parametrize("channel, tag", [("stable", _TAG), ("unstable", "v4.3.0"), ("stable", "muxiveo-rife-v1.6.0")])
def test_feed_rejects_inconsistent_channel_or_tag(tmp_path, channel, tag):
    with pytest.raises(ValueError):
        feed_mod.build_feed(channel, tag, "Hydro74000/Muxiveo", _artifacts(tmp_path))


def test_feed_rejects_empty_artifacts(tmp_path):
    empty = tmp_path / "vide"
    empty.mkdir()
    with pytest.raises(ValueError):
        feed_mod.build_feed("stable", "v4.3.0", "Hydro74000/Muxiveo", empty)
