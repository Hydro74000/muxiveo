from __future__ import annotations

from pathlib import Path

from core.workflows.encode.models import EncodeConfig

from .plan_models import SourceLayout


def source_input_index_map(sources: list[Path], *, start_index: int = 0) -> dict[Path, int]:
    return {src: start_index + i for i, src in enumerate(sources)}


def resolve_source_layout(config: EncodeConfig) -> SourceLayout:
    sources: list[Path] = [config.source]

    video_tracks = list(config.video_tracks)
    if not video_tracks and config.video is not None:
        video_tracks = [config.video]

    for video in video_tracks:
        video_source = Path(video.source_path or config.source)
        if video_source not in sources:
            sources.append(video_source)
    for audio in config.audio_tracks:
        source_path = Path(audio.source_path or config.source)
        if source_path not in sources:
            sources.append(source_path)
    for source_path, _stream_index in config.subtitle_tracks:
        source_path = Path(source_path)
        if source_path not in sources:
            sources.append(source_path)
    for source_path, _stream_index in config.attachment_streams:
        source_path = Path(source_path)
        if source_path not in sources:
            sources.append(source_path)

    return SourceLayout(
        sources=tuple(sources),
        source_idx=source_input_index_map(sources),
    )
