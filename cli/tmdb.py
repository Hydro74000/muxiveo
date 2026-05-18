"""TMDB option helpers for CLI jobs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_SEASON_EPISODE_RE = (
    re.compile(
        r"(?<!\d)[s](?P<season>\d{1,2})[\s._-]*[e](?P<episode>\d{1,4})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)(?P<season>\d{1,2})\s*[xX]\s*(?P<episode>\d{1,4})(?!\d)"),
)


def extract_season_episode(text: str) -> tuple[int, int] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    for pattern in _SEASON_EPISODE_RE:
        match = pattern.search(raw)
        if not match:
            continue
        season = int(match.group("season"))
        episode = int(match.group("episode"))
        if season > 0 and episode > 0:
            return season, episode
    return None


def inferred_season_episode(source_path: Path, *, source_title: str = "") -> tuple[str, str]:
    for candidate in (source_title, source_path.stem):
        found = extract_season_episode(candidate)
        if found is not None:
            season, episode = found
            return str(season), str(episode)
    return "", ""


def normalized_tmdb_options(
    job: dict[str, Any],
    source_path: Path,
    *,
    source_title: str = "",
) -> dict[str, Any] | None:
    raw = job.get("tmdb")
    if not raw:
        return None
    tmdb: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {"enabled": True}
    if not tmdb.get("enabled", True):
        return None

    season = str(tmdb.get("season") or "").strip()
    episode = str(tmdb.get("episode") or "").strip()
    if not season or not episode:
        inferred_season, inferred_episode = inferred_season_episode(source_path, source_title=source_title)
        season = season or inferred_season
        episode = episode or inferred_episode
        if season:
            tmdb["season"] = season
        if episode:
            tmdb["episode"] = episode

    if season and episode and str(tmdb.get("kind") or "all") == "all":
        tmdb["kind"] = "tv"
    return tmdb
