from __future__ import annotations

from pathlib import Path

from core.workflows.encode.models import EncodeConfig, EncodeError
from core.workflows.common.track_types import TrackTimeOffset
from core.workflows.common.sync_rewrite import normalized_sync_rewrite_mode
from core.workflows.encode.runtime_helpers import EncodeOffsetInputSpec

from .plan_models import MapKey, TrackMapping


def offset_seconds(offset_ms: int) -> str:
    return f"{abs(int(offset_ms)) / 1000.0:.3f}"


def track_time_offset_lookup(config: EncodeConfig) -> dict[tuple[str, Path, int], int]:
    lookup: dict[tuple[str, Path, int], int] = {}
    for raw in config.track_time_offsets:
        if not isinstance(raw, TrackTimeOffset):
            continue
        track_type = str(raw.track_type or "").strip().lower()
        if track_type not in {"video", "audio", "subtitle"}:
            continue
        lookup[(track_type, Path(raw.source_path), int(raw.stream_index))] = int(raw.offset_ms)
    return lookup


def track_time_offset_mode_lookup(config: EncodeConfig) -> dict[tuple[str, Path, int], str]:
    lookup: dict[tuple[str, Path, int], str] = {}
    for raw in config.track_time_offsets:
        if not isinstance(raw, TrackTimeOffset):
            continue
        track_type = str(raw.track_type or "").strip().lower()
        if track_type not in {"video", "audio", "subtitle"}:
            continue
        mode = normalized_sync_rewrite_mode(str(getattr(raw, "sync_rewrite_mode", "") or ""))
        if mode:
            lookup[(track_type, Path(raw.source_path), int(raw.stream_index))] = mode
    return lookup


def track_offset_ms(
    lookup: dict[tuple[str, Path, int], int],
    *,
    track_type: str,
    source_path: Path,
    stream_index: int,
    allow_single_video_source_fallback: bool = True,
) -> int:
    key = (str(track_type).strip().lower(), Path(source_path), int(stream_index))
    if key in lookup:
        return int(lookup[key])
    if allow_single_video_source_fallback and key[0] == "video":
        matches = [
            int(value)
            for (candidate_type, candidate_source, _candidate_stream), value in lookup.items()
            if candidate_type == "video" and candidate_source == key[1]
        ]
        if len(matches) == 1:
            return matches[0]
    return 0


def build_offset_specs(
    config: EncodeConfig,
    *,
    track_mappings: list[TrackMapping],
    offset_lookup: dict[tuple[str, Path, int], int] | None = None,
) -> list[EncodeOffsetInputSpec]:
    lookup = offset_lookup if offset_lookup is not None else track_time_offset_lookup(config)
    specs: list[EncodeOffsetInputSpec] = []
    for map_key, input_path, input_stream_index in track_mappings:
        source_path, source_stream_index, track_type = map_key
        offset_ms = track_offset_ms(
            lookup,
            track_type=track_type,
            source_path=source_path,
            stream_index=source_stream_index,
        )
        if offset_ms == 0:
            continue
        if track_type == "video" and offset_ms < 0:
            raise EncodeError(
                "Décalage vidéo négatif interdit : "
                f"source={source_path}, stream={source_stream_index}, offset={offset_ms} ms"
            )
        specs.append(
            EncodeOffsetInputSpec(
                map_key=(Path(source_path), int(source_stream_index), str(track_type)),
                input_path=input_path,
                input_stream_index=int(input_stream_index),
                offset_ms=int(offset_ms),
            )
        )
    return specs


def video_map_arg(
    default_map: tuple[int, int],
    *,
    offset_remap: dict[MapKey, tuple[int, int]],
    map_key: MapKey,
) -> str:
    remapped = offset_remap.get(map_key)
    if remapped is None:
        stream_index = int(default_map[1])
        if stream_index == 0:
            return f"{int(default_map[0])}:v:0"
        return f"{int(default_map[0])}:{stream_index}"
    return f"{int(remapped[0])}:{int(remapped[1])}"
