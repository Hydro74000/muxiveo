from __future__ import annotations

from pathlib import Path


def needs_strict_interleave(
    mapped_tracks: list,
    *,
    foreign_track_types: tuple[str, ...] = ("audio", "subtitle"),
) -> bool:
    if len({int(getattr(mt, "source_file_index")) for mt in mapped_tracks}) < 2:
        return False

    has_subtitle_output = any(
        str(getattr(getattr(mt, "track"), "track_type", "")) == "subtitle"
        for mt in mapped_tracks
    )
    if not has_subtitle_output:
        return False

    primary_video = next(
        (mt for mt in mapped_tracks if str(getattr(getattr(mt, "track"), "track_type", "")) == "video"),
        None,
    )
    if primary_video is None:
        return False

    primary_source = int(getattr(primary_video, "source_file_index"))
    return any(
        str(getattr(getattr(mt, "track"), "track_type", "")) in foreign_track_types
        and int(getattr(mt, "source_file_index")) != primary_source
        for mt in mapped_tracks
    )


def append_strict_interleave_mux_flags(cmd: list[str]) -> None:
    cmd.extend(["-max_interleave_delta", "0"])
    cmd.extend(["-max_muxing_queue_size", "9999"])


def append_sync_inputs(
    cmd: list[str],
    sync_inputs: list[Path | str],
    *,
    input_formats: list[str] | None = None,
    default_format: str = "matroska",
) -> None:
    for index, sync_input in enumerate(sync_inputs):
        fmt = default_format
        if input_formats is not None and index < len(input_formats):
            fmt = str(input_formats[index] or default_format)
        cmd.extend(["-f", fmt, "-i", str(sync_input)])


def sync_cleanup_paths(sync_inputs: list[Path | str]) -> list[Path]:
    return [path for path in sync_inputs if isinstance(path, Path)]
