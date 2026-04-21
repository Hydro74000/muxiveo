"""Construction/émission des signaux Encode depuis RemuxPanel."""

from __future__ import annotations

from dataclasses import replace as dc_replace
from typing import TYPE_CHECKING

from core.inspector import AudioTrack
from ui.panels.remux_panel.theme import _C

if TYPE_CHECKING:
    from pathlib import Path
    from ui.panels.remux_panel.panel import RemuxPanel


def emit_signals(panel: "RemuxPanel") -> None:
    emit_video_tracks(panel)
    emit_audio_tracks(panel)


def emit_video_tracks(panel: "RemuxPanel") -> None:
    track_entries = panel._track_table.current_tracks()
    file_info_by_id = {
        sf.id: sf.info
        for sf in panel._source_files
        if sf.info is not None
    }

    video_tuples: list[tuple] = []
    for entry in track_entries:
        if entry.track_type != "video" or not entry.enabled:
            continue
        file_info = file_info_by_id.get(entry.file_id)
        if file_info is None:
            continue
        color = panel._source_colors.get(entry.file_id, _C.BORDER)
        video_tuples.append((file_info, entry, color))

    panel.video_tracks_changed.emit(video_tuples)


def emit_audio_tracks(panel: "RemuxPanel") -> None:
    track_entries = panel._track_table.current_tracks()
    audio_lookup: dict[tuple[str, int], tuple[AudioTrack, str, "Path"]] = {}
    for sf in panel._source_files:
        if sf.info is None:
            continue
        color = panel._source_colors.get(sf.id, _C.BORDER)
        for audio_track in sf.info.audio_tracks:
            audio_lookup[(sf.id, audio_track.index)] = (audio_track, color, sf.info.path)

    audio_tuples: list[tuple] = []
    for entry in track_entries:
        if entry.track_type != "audio" or not entry.enabled:
            continue
        audio_data = audio_lookup.get((entry.file_id, entry.mkv_tid))
        if audio_data is None:
            continue
        audio_track, color, source_path = audio_data
        audio_tuples.append(
            (
                dc_replace(audio_track, language=entry.language, title=entry.title),
                color,
                source_path,
                entry,
            )
        )

    panel.audio_tracks_changed.emit(audio_tuples)


__all__ = ["emit_audio_tracks", "emit_signals", "emit_video_tracks"]
