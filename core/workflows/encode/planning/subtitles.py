from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.workflows.encode.models import EncodeConfig

from .plan_models import ResolvedSubtitleTracks


def probe_stream_indices(
    source: Path,
    codec_type: str,
    *,
    ffprobe_streams_payload: Callable[[Path], dict[str, object] | None],
    ffprobe_stream_dicts: Callable[[dict[str, object]], list[dict[str, object]]],
) -> list[int] | None:
    payload = ffprobe_streams_payload(source)
    if payload is None:
        return None
    indices: list[int] = []
    for stream in ffprobe_stream_dicts(payload):
        if stream.get("codec_type") != codec_type:
            continue
        raw_index = stream.get("index")
        if not isinstance(raw_index, (int, str)):
            continue
        try:
            indices.append(int(raw_index))
        except (TypeError, ValueError):
            continue
    return sorted(set(indices))


def resolve_subtitle_tracks_for_encode(
    config: EncodeConfig,
    all_sources: list[Path],
    *,
    probe_indices: Callable[[Path, str], list[int] | None],
) -> ResolvedSubtitleTracks:
    if config.subtitle_tracks:
        deduped: list[tuple[Path, int]] = []
        seen: set[tuple[Path, int]] = set()
        for source_path, stream_index in config.subtitle_tracks:
            key = (Path(source_path), int(stream_index))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return ResolvedSubtitleTracks(tuple(deduped), True)

    if not config.copy_subtitles:
        return ResolvedSubtitleTracks((), True)

    resolved: list[tuple[Path, int]] = []
    seen: set[tuple[Path, int]] = set()
    for source_path in all_sources:
        subtitle_indices = probe_indices(source_path, "subtitle")
        if subtitle_indices is None:
            return ResolvedSubtitleTracks((), False)
        for stream_index in subtitle_indices:
            key = (Path(source_path), int(stream_index))
            if key in seen:
                continue
            seen.add(key)
            resolved.append(key)
    return ResolvedSubtitleTracks(tuple(resolved), True)
