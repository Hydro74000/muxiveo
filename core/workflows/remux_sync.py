"""Timeline sync helpers for the remux workflow."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

from core.runner import TaskSignals
from core.workdir import remove_path
from core.workflows.remux_models import RemuxConfig
from core.workflows.remux_mapping import MappedTrack, needs_strict_interleave, requires_file_sync_fallback_for_offsets
from core.workflows.remux_timeline_sync import LiveSyncSession, SyncPreparedInput


def decide_strict_interleave_with_prescan(
    config: RemuxConfig,
    *,
    resolve_mapped_tracks: Callable[[RemuxConfig], list[MappedTrack]],
    log_cb: Callable[[str, str], None],
) -> bool:
    mapped_tracks = resolve_mapped_tracks(config)
    if requires_file_sync_fallback_for_offsets(mapped_tracks):
        log_cb(
            "INFO",
            "Décalage sur piste étrangère détecté : sync timeline activé.",
        )
        return True

    base_risk = needs_strict_interleave(mapped_tracks)
    if not base_risk:
        return False

    log_cb(
        "INFO",
        "Piste audio étrangère + sous-titres détectés : interleave strict activé.",
    )
    return True


def prepare_timeline_sync_inputs(
    config: RemuxConfig,
    mapped_tracks: list[MappedTrack],
    tmp_dir: Path,
    signals: TaskSignals,
    *,
    allow_live: bool,
    ffmpeg_bin: str,
    ffmpeg_thread_args: list[str],
    log_cb: Callable[[str, str], None],
    syncer_factory,
    fallback_helper_factory,
) -> tuple[list[MappedTrack], list[SyncPreparedInput], LiveSyncSession | None]:
    syncer = syncer_factory(
        ffmpeg_bin=ffmpeg_bin,
        ffmpeg_thread_args=ffmpeg_thread_args,
        log_cb=lambda msg: log_cb("INFO", msg),
    )
    prepared_result = fallback_helper_factory(
        syncer=syncer,
        work_dir=tmp_dir,
        ram_dir=fallback_helper_factory.default_ram_dir(),
        log_cb=lambda msg: log_cb("INFO", msg),
    ).prepare(
        mapped_tracks=mapped_tracks,
        sources=config.sources,
        base_input_idx=len(config.sources),
        allow_live=allow_live,
        cancel_cb=signals._cancel_event.is_set,
    )
    live_session = prepared_result.live_session
    prepared = prepared_result.prepared_inputs
    if live_session is not None:
        for proc in live_session.processes:
            signals._register_proc(proc)

    if not prepared:
        return mapped_tracks, [], live_session

    remap = {item.key: item for item in prepared}
    remapped: list[MappedTrack] = []
    for mapped_track in mapped_tracks:
        key = (mapped_track.source_file_index, mapped_track.stream_index, mapped_track.track.track_type)
        hit = remap.get(key)
        if hit is None:
            remapped.append(mapped_track)
            continue
        remapped.append(replace(
            mapped_track,
            source_input_idx=hit.input_idx,
            source_path=hit.path,
            stream_index=0,
        ))

    return remapped, prepared, live_session


def bind_temp_cleanup(signals: TaskSignals, cleanup_paths: list[Path]) -> None:
    if not cleanup_paths:
        return

    done = {"cleaned": False}

    def _cleanup(*_args) -> None:
        if done["cleaned"]:
            return
        done["cleaned"] = True
        for path in cleanup_paths:
            try:
                remove_path(path)
            except OSError:
                pass

    signals.finished.connect(_cleanup)
    signals.failed.connect(_cleanup)
    signals.cancelled.connect(_cleanup)


__all__ = [
    "bind_temp_cleanup",
    "decide_strict_interleave_with_prescan",
    "prepare_timeline_sync_inputs",
    "requires_file_sync_fallback_for_offsets",
]
