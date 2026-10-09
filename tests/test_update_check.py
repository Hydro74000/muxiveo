"""Tests de core/update_check.py (réseau mocké)."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

from core import update_check
from core.update_check import (
    LATEST_RELEASE_API_URL,
    UpdateCheckError,
    query_latest_release,
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
    # flux du canal d'abord (réponse inexploitable ici), puis secours sur l'API
    assert seen == [update_check.update_feed_url("stable"), LATEST_RELEASE_API_URL]
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


def test_unstable_query_is_light():
    seen: list[str] = []
    with patch.object(update_check.urllib.request, "urlopen", _urlopen([], seen)), \
         patch.object(update_check, "_query_feed", lambda *a, **k: None):
        fetch_latest_release("unstable")
    assert seen == [f"{update_check.RELEASES_API_URL}?per_page={update_check.UNSTABLE_RELEASES_PAGE_SIZE}"]


def test_total_timeout_aborts_slow_response():
    """Le délai couvre la réponse entière : un serveur lent ne bloque pas le worker indéfiniment."""
    clock = iter([0.0, 0.1, 99.0])
    with patch.object(update_check.urllib.request, "urlopen", _urlopen([_release("v99.0.0")] * 50)), \
         patch.object(update_check, "_READ_CHUNK_BYTES", 8), \
         patch.object(update_check, "_query_feed", lambda *a, **k: None), \
         patch.object(update_check.time, "monotonic", lambda: next(clock)):
        try:
            query_latest_release("unstable", timeout=5.0)
        except UpdateCheckError as exc:
            assert "TimeoutError" in str(exc)
        else:
            raise AssertionError("UpdateCheckError attendue")


def test_oversized_response_is_rejected():
    with patch.object(update_check.urllib.request, "urlopen", _urlopen([_release("v99.0.0")] * 50)), \
         patch.object(update_check, "_MAX_RESPONSE_BYTES", 64):
        assert fetch_latest_release("unstable") is None


def test_query_raises_on_network_error_with_reason():
    with patch.object(update_check.urllib.request, "urlopen", side_effect=urllib.error.URLError("CERTIFICATE_VERIFY_FAILED")):
        try:
            query_latest_release("stable")
        except UpdateCheckError as exc:
            assert "CERTIFICATE_VERIFY_FAILED" in str(exc)
        else:
            raise AssertionError("UpdateCheckError attendue")


def test_get_json_timeout_covers_header_wait():
    """Des en-têtes lents (1 s) ne prolongent pas l'attente au-delà du délai demandé."""
    import time as _time
    from unittest.mock import patch as _patch

    from core import update_check

    def slow_urlopen(*_args, **_kwargs):
        _time.sleep(1.0)
        raise OSError("trop tard")

    started = _time.monotonic()
    with _patch.object(update_check.urllib.request, "urlopen", side_effect=slow_urlopen):
        try:
            update_check._get_json("https://example.invalid/", 0.2)
        except TimeoutError:
            pass
        else:  # pragma: no cover
            raise AssertionError("TimeoutError attendu")
    assert _time.monotonic() - started < 0.6


# ---------------------------------------------------------------------------
# Flux de mise à jour (release update-feed)
# ---------------------------------------------------------------------------

def _feed(channel: str, tag: str, assets: list[dict] | None = None) -> dict:
    return {"schema": 1, "channel": channel, "version": tag.lstrip("v"), "tag": tag,
            "url": f"{APP_REPOSITORY_URL}releases/tag/{tag}", "published": "2026-10-09T08:00:00Z",
            "assets": assets or []}


def _routes(table: dict[str, object], seen: list[str] | None = None):
    """urlopen simulé : réponse par URL, erreur réseau pour les autres."""
    def _open(req, timeout=None):
        if seen is not None:
            seen.append(req.full_url)
        if req.full_url not in table:
            raise urllib.error.URLError("introuvable")
        return _response(table[req.full_url])

    return _open


def test_feed_is_read_before_api():
    seen: list[str] = []
    feed = _feed("stable", "v99.0.0")
    with patch.object(update_check.urllib.request, "urlopen", _routes({update_check.update_feed_url("stable"): feed}, seen)):
        info = fetch_latest_release("stable")
    assert seen == [update_check.update_feed_url("stable")]
    assert info is not None and info.version == "99.0.0" and not info.prerelease
    assert update_check.update_feed_url("unstable").endswith("/releases/download/update-feed/unstable.json")


def test_unstable_feed_keeps_newest_of_unstable_and_stable():
    unstable = _feed("unstable", f"v{_U2}")
    for stable_tag, expected in (("v3.1.2", _U2), ("v99.0.0", "99.0.0")):
        routes = {update_check.update_feed_url("unstable"): unstable,
                  update_check.update_feed_url("stable"): _feed("stable", stable_tag)}
        with patch.object(update_check.urllib.request, "urlopen", _routes(routes)):
            info = fetch_latest_release("unstable")
        assert info is not None and info.version == expected


def test_invalid_feed_falls_back_to_api():
    api = f"{update_check.RELEASES_API_URL}?per_page={update_check.UNSTABLE_RELEASES_PAGE_SIZE}"
    for bad in ({"schema": 2, "channel": "unstable", "tag": f"v{_U1}"}, _feed("stable", f"v{_U1}"),
                _feed("unstable", "muxiveo-rife-v1.6.0"), ["pas", "un", "objet"]):
        seen: list[str] = []
        routes = {update_check.update_feed_url("unstable"): bad, api: [_release(f"v{_U2}", prerelease=True)]}
        with patch.object(update_check.urllib.request, "urlopen", _routes(routes, seen)):
            info = fetch_latest_release("unstable")
        assert seen[-1] == api
        assert info is not None and info.version == _U2


def test_feed_keeps_only_repository_assets():
    prefix = update_check.RELEASE_DOWNLOAD_URL_PREFIX
    feed = _feed("stable", "v99.0.0", [
        {"name": "Muxiveo.AppImage", "url": f"{prefix}v99.0.0/Muxiveo.AppImage", "size": 10},
        {"name": "SHA256SUMS", "url": f"{prefix}v99.0.0/SHA256SUMS", "size": 1},
        {"name": "piege.AppImage", "url": "https://evil.example/piege.AppImage", "size": 1},
    ])
    with patch.object(update_check.urllib.request, "urlopen", _routes({update_check.update_feed_url("stable"): feed})):
        info = fetch_latest_release("stable")
    assert info is not None and [a.name for a in info.assets] == ["Muxiveo.AppImage", "SHA256SUMS"]
