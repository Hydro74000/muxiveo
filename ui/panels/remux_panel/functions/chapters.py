"""Règles chapitres pour RemuxPanel."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.inspector import ChapterEntry
    from ui.panels.remux_panel.panel import RemuxPanel


def on_chapters_changed(panel: "RemuxPanel") -> None:
    panel._rebuild_preview()


def update_chapters_from_sources(panel: "RemuxPanel") -> None:
    # Pousse au panneau la liste des sources porteuses de chapitres pour
    # alimenter le sélecteur (combo box). Le widget gère lui-même :
    #   - le repositionnement automatique sur l'ancienne source si elle existe
    #     toujours après ajout/suppression ;
    #   - la préservation des chapitres édités manuellement (is_modified()) ;
    #   - le grisage du combo quand <=1 source porteuse.
    available: list[tuple[int, str, list]] = []
    for i, sf in enumerate(panel._source_files):
        if sf.info and sf.info.chapters and sf.info.chapters.entries:
            label = f"Source {i + 1} — {sf.path.name}"
            available.append((i, label, panel._chapter_entries_for_source(i)))
    panel._chapter_panel.set_available_sources(available)


def reset_empty_state(panel: "RemuxPanel") -> None:
    panel._color_index = 0
    panel._source_sync_offsets_ms.clear()
    panel._auto_sync_entry_ids.clear()
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
