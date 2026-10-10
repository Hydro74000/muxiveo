"""Separate sparse subtitles from audio when muxing prepared video inputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from core.workflows.encode.models import EncodeConfig

from .plan_models import EncodePlan, MapKey


@dataclass(frozen=True)
class IsolatedSubtitleInputs:
    paths: tuple[Path, ...]
    remap: dict[MapKey, tuple[int, int]]
    copy_input_indices: tuple[int, ...]


def plan_isolated_subtitle_inputs(
    config: EncodeConfig,
    plan: EncodePlan,
    *,
    source_idx: Mapping[Path, int],
    sync_remap: Mapping[MapKey, tuple[int, int]],
    start_input_index: int,
) -> IsolatedSubtitleInputs:
    """Reopen sources whose selected audio and subtitles share a demuxer.

    The video is already on a separate input in these assembly routes. Reading
    a late subtitle from the audio input can make FFmpeg write minutes of audio
    ahead of that video, even with max_interleave_delta set. Keep the subtitle
    reader independent, without extracting or transforming any media.

    Run after timeline sync and before metadata/offset inputs. Existing sync
    remaps remain authoritative; subsequent per-track offsets still apply.
    """
    remap = dict(sync_remap)
    audio_inputs = {
        remap.get(
            (Path(audio.source_path or config.source), int(audio.stream_index), "audio"),
            (source_idx.get(Path(audio.source_path or config.source), source_idx.get(config.source, 0)), 0),
        )[0]
        for audio in config.audio_tracks
    }
    wildcard = bool(config.copy_subtitles and not plan.subtitles_resolved and not plan.resolved_subtitle_tracks)
    paths: list[Path] = []
    copy_indices: list[int] = []
    for source, input_index in source_idx.items():
        keys = [
            (Path(path), int(stream), "subtitle")
            for path, stream in plan.resolved_subtitle_tracks
            if Path(path) == source and (Path(path), int(stream), "subtitle") not in remap
        ]
        subtitle_input_index = input_index
        if input_index in audio_inputs and (keys or wildcard):
            subtitle_input_index = start_input_index + len(paths)
            paths.append(source)
            for key in keys:
                remap[key] = (subtitle_input_index, key[1])
        copy_indices.append(subtitle_input_index)
    return IsolatedSubtitleInputs(tuple(paths), remap, tuple(copy_indices))
