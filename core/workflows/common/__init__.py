"""Shared helpers and types for encode/remux workflows."""

from core.workflows.common.attachments import (
    ATTACHMENT_EXT_BY_MIME,
    ATTACHMENT_MIME_BY_EXT,
    attachment_filename_from_meta,
    extension_for_mime,
    mime_for_path,
    sanitize_filename,
)
from core.workflows.common.chapters import (
    ffmeta_escape,
    probe_media_duration_seconds,
    write_ffmetadata_chapters,
)
from core.workflows.common.ffmpeg_runtime import (
    cli_path,
    default_ffmpeg_thread_count,
    ffmpeg_progress_args,
    normalize_ffmpeg_thread_count,
    normalize_max_parallel_video_encodes,
)
from core.workflows.common.metadata import (
    STREAM_SPEC_BY_TRACK_TYPE,
    disposition_value,
    normalize_track_language,
    normalize_track_language_from_track,
    resolve_global_tags,
)
from core.workflows.common.timeline_sync import (
    append_strict_interleave_mux_flags,
    append_sync_inputs,
    needs_strict_interleave,
    sync_cleanup_paths,
)
from core.workflows.common.track_types import (
    TrackMetaEdit,
    TrackMetaPatch,
    TrackOffset,
    TrackRef,
    TrackTimeOffset,
    TrackType,
)

__all__ = [
    "ATTACHMENT_EXT_BY_MIME",
    "ATTACHMENT_MIME_BY_EXT",
    "STREAM_SPEC_BY_TRACK_TYPE",
    "TrackMetaEdit",
    "TrackMetaPatch",
    "TrackOffset",
    "TrackRef",
    "TrackTimeOffset",
    "TrackType",
    "append_strict_interleave_mux_flags",
    "append_sync_inputs",
    "attachment_filename_from_meta",
    "cli_path",
    "default_ffmpeg_thread_count",
    "disposition_value",
    "extension_for_mime",
    "ffmeta_escape",
    "ffmpeg_progress_args",
    "mime_for_path",
    "needs_strict_interleave",
    "normalize_ffmpeg_thread_count",
    "normalize_max_parallel_video_encodes",
    "normalize_track_language",
    "normalize_track_language_from_track",
    "probe_media_duration_seconds",
    "resolve_global_tags",
    "sanitize_filename",
    "sync_cleanup_paths",
    "write_ffmetadata_chapters",
]
