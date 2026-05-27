"""Logique TMDB/suggestions de titre pour RemuxPanel."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from core.media_info_fetcher import (
    MediaDetails,
    clean_filename_for_search,
    extract_year_from_filename,
)
from ui.panels.remux_panel.models import _normalize_tmdb_manual_title_suggestion
from ui.panels.tmdb_search_modal import extract_season_episode

if TYPE_CHECKING:
    from ui.panels.remux_panel.panel import RemuxPanel


def default_tmdb_suggested_title(panel: "RemuxPanel") -> str:
    if not panel._source_files:
        return ""

    first = panel._source_files[0]
    suggested = clean_filename_for_search(first.path)

    year = extract_year_from_filename(first.path)
    if year and suggested and not re.search(r"\b" + re.escape(year) + r"\b", suggested):
        suggested = f"{suggested} {year}"
    return suggested.strip()


def default_tmdb_season_episode(panel: "RemuxPanel") -> tuple[int, int]:
    if not panel._source_files:
        return 0, 0

    first = panel._source_files[0]
    candidates: list[str] = [first.path.stem]
    if first.info and first.info.title:
        candidates.append(first.info.title)

    for text in candidates:
        match = extract_season_episode(text)
        if match is not None:
            return match
    return 0, 0


def sync_tmdb_suggested_title(panel: "RemuxPanel", _text: str = "") -> None:
    manual_title = panel._file_title_edit.text().strip()
    suggested = (
        _normalize_tmdb_manual_title_suggestion(manual_title)
        if manual_title
        else panel._default_tmdb_suggested_title()
    )
    parsed = extract_season_episode(manual_title) if manual_title else None
    season, episode = parsed if parsed is not None else panel._default_tmdb_season_episode()
    panel._attachment_panel.set_suggested_title(suggested, season=season, episode=episode)


def on_tmdb_details_selected(panel: "RemuxPanel", details: object) -> None:
    if not isinstance(details, MediaDetails):
        return
    title = details.formatted_container_title().strip()
    if title:
        panel._file_title_edit.setText(title)


__all__ = [
    "default_tmdb_season_episode",
    "default_tmdb_suggested_title",
    "on_tmdb_details_selected",
    "sync_tmdb_suggested_title",
]
