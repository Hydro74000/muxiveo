"""TMDB option helpers for CLI jobs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


AUTO_TMDB_EPISODE_KEY = "auto_detect_episode"
AUTO_TMDB_METADATA_KEY = "auto_metadata"

_SEASON_EPISODE_RE = (
    re.compile(
        r"(?<!\d)[s](?P<season>\d{1,2})[\s._-]*[e](?P<episode>\d{1,4})(?!\d)",
        re.IGNORECASE,
    ),
    re.compile(r"(?<!\d)(?P<season>\d{1,2})\s*[xX]\s*(?P<episode>\d{1,4})(?!\d)"),
)
_AUTO_VALUE_NAMES = {"auto", "detect", "from_source", "source", "*"}


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


def inferred_season_episode(
    source_path: Path,
    *,
    source_title: str = "",
    prefer_source_path: bool = False,
) -> tuple[str, str]:
    candidates = (source_path.stem, source_title) if prefer_source_path else (source_title, source_path.stem)
    for candidate in candidates:
        found = extract_season_episode(candidate)
        if found is not None:
            season, episode = found
            return str(season), str(episode)
    return "", ""


def mark_auto_tmdb(tmdb: dict[str, Any]) -> None:
    tmdb[AUTO_TMDB_EPISODE_KEY] = True
    tmdb[AUTO_TMDB_METADATA_KEY] = True


def auto_tmdb_metadata_enabled(job: dict[str, Any]) -> bool:
    tmdb = job.get("tmdb")
    return isinstance(tmdb, dict) and bool(tmdb.get(AUTO_TMDB_METADATA_KEY))


def _is_auto_value(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return not text or text.lower() in _AUTO_VALUE_NAMES


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
    force_auto_episode = bool(tmdb.get(AUTO_TMDB_EPISODE_KEY))
    season_auto = _is_auto_value(tmdb.get("season"))
    episode_auto = _is_auto_value(tmdb.get("episode"))
    if force_auto_episode or season_auto or episode_auto:
        inferred_season, inferred_episode = inferred_season_episode(
            source_path,
            source_title=source_title,
            prefer_source_path=force_auto_episode or season_auto or episode_auto,
        )
        if force_auto_episode or season_auto:
            season = inferred_season
        if force_auto_episode or episode_auto:
            episode = inferred_episode
    if season:
        tmdb["season"] = season
    else:
        tmdb.pop("season", None)
    if episode:
        tmdb["episode"] = episode
    else:
        tmdb.pop("episode", None)

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
