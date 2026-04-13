"""Règles chapitres pour RemuxPanel."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.inspector import ChapterEntry
    from ui.panels.remux_panel.panel import RemuxPanel


def on_chapters_changed(panel: "RemuxPanel") -> None:
    panel._rebuild_preview()


def update_chapters_from_sources(panel: "RemuxPanel") -> None:
    base = panel._resolve_base_chapters()
    panel._chapter_panel.reset_chapters(base)


def reset_empty_state(panel: "RemuxPanel") -> None:
    panel._color_index = 0
    panel._track_table.clear_all()
    panel._attachment_panel.clear_all()
    panel._chapter_panel.clear_all()
    panel._filter_btn.setChecked(False)
    panel._file_title_edit.clear()
    panel._output_edit.clear()
    panel._sync_tmdb_suggested_title()


def resolve_base_chapters(panel: "RemuxPanel") -> list["ChapterEntry"]:
    for sf in panel._source_files:
        if sf.info and sf.info.chapters and sf.info.chapters.entries:
            return list(sf.info.chapters.entries)
    return []


__all__ = [
    "on_chapters_changed",
    "reset_empty_state",
    "resolve_base_chapters",
    "update_chapters_from_sources",
]
