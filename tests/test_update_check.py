"""Tests de core/update_check.py (réseau mocké)."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

from core import update_check
from core.update_check import (
    LATEST_RELEASE_API_URL,
    check_for_update,
    display_version,
    fetch_latest_release,
    is_newer,
    normalize_update_channel,
    release_page_url,
    version_key,
)
from core.version import APP_BUILD_VERSION, APP_REPOSITORY_URL

_U1 = "4.0.0-unstable.20260923.46.51df45c"
_U2 = "4.0.0-unstable.20260924.47.0a1b2c3"


def _response(payload: object) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def _release(tag: str, *, prerelease: bool = False, draft: bool = False) -> dict:
    return {"tag_name": tag, "html_url": f"{APP_REPOSITORY_URL}releases/tag/{tag}", "prerelease": prerelease, "draft": draft}


def _urlopen(payload: object, seen: list[str] | None = None):
    def _open(req, timeout=None):
        if seen is not None:
            seen.append(req.full_url)
        return _response(payload)

    return _open


def test_version_key_orders_unstable_before_stable():
    assert version_key("v4.1") == (4, 1, 0, 1, 0, 0)
    assert version_key(_U1) == (4, 0, 0, 0, 20260923, 46)
    assert version_key("") == () and version_key("latest") == ()
    assert version_key("3.1.2") < version_key(_U1) < version_key(_U2) < version_key("4.0.0") < version_key("4.1.0-rc1")


def test_is_newer_handles_channels():
    assert is_newer("v4.10.0", "4.9.9")
    assert not is_newer("v4.0.0", "4.0.0")
    assert not is_newer("garbage", "4.0.0")
    assert is_newer(_U2, _U1)
    assert not is_newer(_U2, "4.0.0")
    assert is_newer("4.0.0", _U2)


def test_display_version_and_channel_helpers():
    assert display_version(_U1) == "4.0.0-unstable.46"
    assert display_version("v4.1.0") == "4.1.0"
    assert normalize_update_channel("UNSTABLE") == "unstable"
    assert normalize_update_channel("beta") in ("stable", "unstable")
    assert release_page_url("v4.1.0") == f"{APP_REPOSITORY_URL}releases/tag/v4.1.0"


def test_stable_channel_uses_latest_endpoint():
    seen: list[str] = []
    with patch.object(update_check.urllib.request, "urlopen", _urlopen(_release("v99.0.0"), seen)):
        info = fetch_latest_release("stable")
    assert seen == [LATEST_RELEASE_API_URL]
    assert info is not None and info.version == "99.0.0" and not info.prerelease


def test_unstable_channel_picks_highest_non_draft_release():
    releases = [
        _release("v3.1.2"),
        _release(f"v{_U1}", prerelease=True),
        _release("v99.0.0-unstable.20990101.1.abc", draft=True),
        _release(f"v{_U2}", prerelease=True),
    ]
    with patch.object(update_check.urllib.request, "urlopen", _urlopen(releases)):
        info = fetch_latest_release("unstable")
    assert info is not None and info.version == _U2 and info.prerelease


def test_check_for_update_returns_none_when_up_to_date():
    payload = _release(f"v{APP_BUILD_VERSION}")
    with patch.object(update_check.urllib.request, "urlopen", _urlopen(payload)):
        info = fetch_latest_release("stable")
    assert info is not None and not info.is_newer
    with patch.object(update_check.urllib.request, "urlopen", _urlopen(payload)):
        assert check_for_update("stable") is None


def test_fetch_keeps_only_repository_assets_case_insensitive():
    lower_repo = APP_REPOSITORY_URL.lower()
    payload = {
        "tag_name": "v99.0.0",
        "html_url": f"{lower_repo}releases/tag/v99.0.0",
        "assets": [
            {"name": "SHA256SUMS", "browser_download_url": f"{lower_repo}releases/download/v99.0.0/SHA256SUMS", "size": 90},
            {"name": "evil.AppImage", "browser_download_url": "https://evil.example/evil.AppImage"},
            "junk",
        ],
    }
    with patch.object(update_check.urllib.request, "urlopen", _urlopen(payload)):
        info = fetch_latest_release("stable")
    assert info is not None
    assert info.url == payload["html_url"]
    assert [a.name for a in info.assets] == ["SHA256SUMS"]
    assert info.asset("SHA256SUMS") is not None and info.asset("evil.AppImage") is None


def test_fetch_rejects_foreign_release_url():
    payload = {"tag_name": "v99.0.0", "html_url": "https://evil.example/download"}
    with patch.object(update_check.urllib.request, "urlopen", _urlopen(payload)):
        info = fetch_latest_release("stable")
    assert info is not None
    assert info.url == release_page_url("99.0.0")


def test_fetch_is_silent_on_network_error():
    with patch.object(update_check.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
        assert fetch_latest_release("stable") is None
        assert fetch_latest_release("unstable") is None


def test_fetch_is_silent_on_invalid_json():
    with patch.object(update_check.urllib.request, "urlopen", return_value=io.BytesIO(b"<html>")):
        assert fetch_latest_release("stable") is None
