#!/usr/bin/env python3
"""
Resolve a GitHub release asset URL and optionally compute its SHA256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.request


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Muxiveo-homebrew-release",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _release_api_url(repo: str, tag: str | None) -> str:
    if tag:
        return f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    return f"https://api.github.com/repos/{repo}/releases/latest"


def _load_release(repo: str, tag: str | None) -> dict:
    req = urllib.request.Request(_release_api_url(repo, tag), headers=_headers())
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _select_asset(release: dict, patterns: list[str]) -> dict:
    assets = release.get("assets") or []
    for asset in assets:
        name = str(asset.get("name") or "")
        if all(pattern in name for pattern in patterns):
            return asset
    joined = ", ".join(patterns)
    raise RuntimeError(f"No asset matching all patterns found: {joined}")


def _sha256_of_url(url: str) -> str:
    digest = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "Muxiveo-homebrew-release"})
    with urllib.request.urlopen(req) as resp:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve a GitHub release asset.")
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--tag", default="", help="Specific release tag. Defaults to latest.")
    parser.add_argument(
        "--pattern",
        action="append",
        required=True,
        help="Substring that must appear in the asset filename. Repeat to require multiple substrings.",
    )
    parser.add_argument("--sha256", action="store_true", help="Download the asset and compute its SHA256.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    release = _load_release(args.repo, args.tag.strip() or None)
    asset = _select_asset(release, args.pattern)
    url = str(asset["browser_download_url"])
    payload = {
        "name": asset["name"],
        "url": url,
    }
    if args.sha256:
        payload["sha256"] = _sha256_of_url(url)
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
