from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Mapping

from core.workflows.encode.models import EncodeConfig

from .plan_models import EncodePlan, ResolvedTrackAssembly


def build_track_input_paths(
    *,
    leading_inputs: Sequence[Path | str] = (),
    all_sources: Sequence[Path] = (),
    sync_inputs: Sequence[Path | str] = (),
) -> tuple[Path | str, ...]:
    return tuple([*leading_inputs, *all_sources, *sync_inputs])


def resolve_track_assembly(
    config: EncodeConfig,
    plan: EncodePlan,
    *,
    source_idx: Mapping[Path, int],
    track_input_paths: Sequence[Path | str],
    sync_remap: Mapping[tuple[Path, int, str], tuple[int, int]] | None = None,
    video_default_map: tuple[int, int] | None = None,
    video_fallback_input: Path | str | None = None,
    include_video: bool = True,
) -> ResolvedTrackAssembly:
    sync_remap = sync_remap or {}
    resolved_subtitle_tracks = list(plan.resolved_subtitle_tracks)
    track_inputs = tuple(track_input_paths)

    def _input_path(idx: int, fallback: Path | str) -> Path | str:
        if 0 <= idx < len(track_inputs):
            return track_inputs[idx]
        return fallback

    video_base_map = sync_remap.get(plan.video_key, video_default_map or plan.video_default_map)
    video_fallback = (
        video_fallback_input
        if video_fallback_input is not None
        else plan.all_sources[plan.video_input_idx]
    )
    track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]] = []
    if include_video:
        track_mappings.append(
            (
                plan.video_key,
                _input_path(int(video_base_map[0]), video_fallback),
                int(video_base_map[1]),
            )
        )

    for audio in config.audio_tracks:
        src_path = Path(audio.source_path or config.source)
        remapped = sync_remap.get((src_path, int(audio.stream_index), "audio"))
        if remapped is not None:
            input_index, stream_idx = remapped
        else:
            source_input_index = source_idx.get(src_path)
            input_index = (
                source_input_index
                if source_input_index is not None
                else source_idx.get(config.source, 0)
            )
            stream_idx = int(audio.stream_index)
        track_mappings.append(
            (
                (src_path, int(audio.stream_index), "audio"),
                _input_path(int(input_index), src_path),
                int(stream_idx),
            )
        )

    for src_path_raw, stream_idx_raw in resolved_subtitle_tracks:
        src_path = Path(src_path_raw)
        remapped = sync_remap.get((src_path, int(stream_idx_raw), "subtitle"))
        if remapped is not None:
            subtitle_input_index, stream_idx = remapped
        else:
            source_input_index = source_idx.get(src_path)
            if source_input_index is None:
                continue
            subtitle_input_index = source_input_index
            stream_idx = int(stream_idx_raw)
        track_mappings.append(
            (
                (src_path, int(stream_idx_raw), "subtitle"),
                _input_path(int(subtitle_input_index), src_path),
                int(stream_idx),
            )
        )

    return ResolvedTrackAssembly(
        track_mappings=tuple(track_mappings),
        video_map=(int(video_base_map[0]), int(video_base_map[1])),
    )
