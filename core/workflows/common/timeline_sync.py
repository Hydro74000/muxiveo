from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from core.workflows.common.track_types import TimelineMappedTrack


def needs_strict_interleave(
    mapped_tracks: Sequence[TimelineMappedTrack],
    *,
    foreign_track_types: tuple[str, ...] = ("audio", "subtitle"),
) -> bool:
    if len({int(mapped_track.source_file_index) for mapped_track in mapped_tracks}) < 2:
        return False

    has_subtitle_output = any(
        str(mapped_track.track.track_type) == "subtitle"
        for mapped_track in mapped_tracks
    )
    if not has_subtitle_output:
        return False

    primary_video = next(
        (mapped_track for mapped_track in mapped_tracks if str(mapped_track.track.track_type) == "video"),
        None,
    )
    if primary_video is None:
        return False

    primary_source = int(primary_video.source_file_index)
    return any(
        str(mapped_track.track.track_type) in foreign_track_types
        and int(mapped_track.source_file_index) != primary_source
        for mapped_track in mapped_tracks
    )


def append_strict_interleave_mux_flags(cmd: list[str]) -> None:
    # Microsecondes. Zéro attend chaque flux sans limite : un sous-titre tardif
    # peut retenir des minutes de vidéo UHD dans la file du muxer.
    cmd.extend(["-max_interleave_delta", "5000000"])
    cmd.extend(["-max_muxing_queue_size", "9999"])


class PreparedSyncInput(Protocol):
    """Entrée de synchro préparée : chemin (fichier, FIFO, pipe nommé) et conteneur écrit."""

    @property
    def path(self) -> Path | str: ...

    @property
    def container_format(self) -> str: ...


#: Entrée de synchro : chemin seul (conteneur ``default_format``) ou entrée préparée
#: portant son conteneur (FIFO live POSIX : NUT ; fichier ou pipe nommé : Matroska).
SyncInput = Path | str | PreparedSyncInput


def _sync_input_parts(sync_input: SyncInput, default_format: str) -> tuple[Path | str, str]:
    if isinstance(sync_input, (Path, str)):
        return sync_input, default_format
    return sync_input.path, str(sync_input.container_format or default_format)


def append_sync_inputs(
    cmd: list[str],
    sync_inputs: Sequence[SyncInput],
    *,
    input_formats: list[str] | None = None,
    default_format: str = "matroska",
) -> None:
    for index, sync_input in enumerate(sync_inputs):
        path, fmt = _sync_input_parts(sync_input, default_format)
        if input_formats is not None and index < len(input_formats):
            fmt = str(input_formats[index] or fmt)
        cmd.extend(["-f", fmt, "-i", str(path)])


def sync_cleanup_paths(sync_inputs: Sequence[SyncInput]) -> list[Path]:
    paths = (_sync_input_parts(item, "")[0] for item in sync_inputs)
    return [path for path in paths if isinstance(path, Path)]
