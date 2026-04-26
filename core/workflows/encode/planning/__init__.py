"""Planning helpers for the encode workflow."""

from .command_plan import build_encode_command_selection
from .metadata_plan import (
    PreparedContainerMetadataInputs,
    append_container_metadata_args,
    build_container_metadata_plan,
    container_chapter_map_value,
    container_metadata_map_value,
    materialize_container_metadata_inputs,
    prepare_container_metadata_inputs,
)
from .encode_plan import build_encode_plan
from .offsets import (
    build_offset_specs,
    offset_seconds,
    track_offset_ms,
    track_time_offset_lookup,
    video_map_arg,
)
from .preview import format_preview_command, format_preview_commands
from .preview import format_preview_selection
from .plan_models import (
    ContainerMetadataPlan,
    EncodeCommandSelection,
    EncodePlan,
    MaterializedContainerMetadataPlan,
    PlannedVideoTrack,
    ResolvedTrackAssembly,
    ResolvedSubtitleTracks,
    SyncAnalysisPlan,
    SyncMappedTrackPlan,
    SourceLayout,
    TrackMapping,
)
from .sources import resolve_source_layout, source_input_index_map
from .subtitles import probe_stream_indices, resolve_subtitle_tracks_for_encode
from .sync_plan import (
    build_sync_analysis_plan,
    build_probe_remux_config,
    build_sync_mapped_tracks,
    needs_strict_interleave_for_encode,
    requires_file_sync_fallback_for_offsets,
)
from .track_assembly import build_track_input_paths, resolve_track_assembly
from .validation import is_dir_writable, validate_encode_config

__all__ = [
    "EncodePlan",
    "EncodeCommandSelection",
    "ContainerMetadataPlan",
    "PreparedContainerMetadataInputs",
    "MaterializedContainerMetadataPlan",
    "PlannedVideoTrack",
    "ResolvedTrackAssembly",
    "ResolvedSubtitleTracks",
    "SourceLayout",
    "SyncAnalysisPlan",
    "SyncMappedTrackPlan",
    "TrackMapping",
    "append_container_metadata_args",
    "build_encode_command_selection",
    "build_container_metadata_plan",
    "build_encode_plan",
    "build_sync_analysis_plan",
    "build_probe_remux_config",
    "build_sync_mapped_tracks",
    "build_track_input_paths",
    "build_offset_specs",
    "container_chapter_map_value",
    "container_metadata_map_value",
    "format_preview_command",
    "format_preview_commands",
    "format_preview_selection",
    "is_dir_writable",
    "materialize_container_metadata_inputs",
    "needs_strict_interleave_for_encode",
    "offset_seconds",
    "prepare_container_metadata_inputs",
    "probe_stream_indices",
    "resolve_source_layout",
    "resolve_subtitle_tracks_for_encode",
    "resolve_track_assembly",
    "requires_file_sync_fallback_for_offsets",
    "source_input_index_map",
    "track_offset_ms",
    "track_time_offset_lookup",
    "validate_encode_config",
    "video_map_arg",
]
