"""
core/workflows/encode/workflow.py — FFmpeg encode workflow with optional HDR metadata injection.

Public:
    EncodeWorkflow
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal
from core.runner import TaskCancelledError, TaskSignals, ToolRunner
from core.subprocess_utils import (
    subprocess_text_kwargs,
)
from core.subtitle_codec import plan_subtitle_codec
from core.version import APP_VERSION_LABEL
from core.workdir import (
    download_tmdb_cover,
    prepare_process_work_dir,
    relocate_tmdb_covers_to_process_dir,
    remove_path,
)
from core.workflows.remux import write_mediainfo_nfo
from core.workflows.common.remux_postprocess import RemuxPostprocessService
from core.workflows.common.ffmpeg_runtime import (
    default_ffmpeg_thread_count as _default_ffmpeg_thread_count,
    ffmpeg_progress_args as _common_ffmpeg_progress_args,
    ffmpeg_thread_args as _common_ffmpeg_thread_args,
    normalize_ffmpeg_thread_count as _normalize_ffmpeg_thread_count,
    normalize_max_parallel_video_encodes as _normalize_max_parallel_video_encodes,
)
from core.workflows.common.metadata import (
    resolve_global_tags as _common_resolve_global_tags,
)
from core.workflows.common.sync_rewrite import (
    SYNC_REWRITE_MODE_OFFSET,
    SyncRewriteService,
    normalized_sync_rewrite_mode,
    sync_rewrite_output_token,
)
from core.workflows.common.timeline_sync import (
    sync_cleanup_paths as _common_sync_cleanup_paths,
)
from core.workflows.encode.catalog import (
    is_h264_video_codec,
)
from core.workflows.encode.domain import (
    EncodeCodecDomainCallbacks as _EncodeCodecDomainCallbacks,
    needs_static_hdr_bitstream_patch as _needs_static_hdr_bitstream_patch_domain,
)
from core.workflows.remux_timeline_sync import (
    FfmpegTimelineSync,
    LiveSyncSession,
    TimelineSyncFallbackHelper,
)
from core.workflows.encode.runtime_helpers import (
    EncodeOffsetInputSpec as _EncodeOffsetInputSpec,
    VideoPreparationResourcePolicy as _VideoPreparationResourcePolicy,
    VideoTrackPreparationOrchestrator as _VideoTrackPreparationOrchestrator,
    VideoTrackPrepSpec as _VideoTrackPrepSpec,
    VideoTrackPrepTask as _VideoTrackPrepTask,
    ui_encode_progress_message as _ui_encode_progress_message,
)
from core.workflows.encode.runtime import ram_buffer as _ram_buffer_module
from core.workflows.encode.runtime import (
    AttachmentPreparationService as _AttachmentPreparationService,
    AttachmentPreparationServiceCallbacks as _AttachmentPreparationServiceCallbacks,
    DirectOutputRunner as _DirectOutputRunner,
    DirectOutputRunnerCallbacks as _DirectOutputRunnerCallbacks,
    MetadataInjectRunner as _MetadataInjectRunner,
    MetadataInjectRunnerCallbacks as _MetadataInjectRunnerCallbacks,
    MultiVideoPipelineRunner as _MultiVideoPipelineRunner,
    MultiVideoPipelineRunnerCallbacks as _MultiVideoPipelineRunnerCallbacks,
    SignalBindingService as _SignalBindingService,
    SignalBindingServiceCallbacks as _SignalBindingServiceCallbacks,
    default_attachment_filename as _default_attachment_filename,
    ensure_inject_storage_available as _ensure_inject_storage_available_runtime,
    estimate_duration_seconds as _estimate_duration_seconds_runtime,
    estimate_inject_storage_requirements as _estimate_inject_storage_requirements_runtime,
    estimate_inject_video_bytes as _estimate_inject_video_bytes_runtime,
    extract_attached_pic as _extract_attached_pic_runtime,
    format_bytes as _format_bytes_runtime,
    probe_attachment_stream as _probe_attachment_stream_runtime,
    unique_attachment_path as _unique_attachment_path_runtime,
)
from core.workflows.encode.runtime.dynamic_hdr import (
    DynamicHdrConfigNormalizer,
    DynamicHdrNormalizerCallbacks,
)
from core.workflows.encode.runtime.hdr_metadata import HdrMetadataProbeService
from core.workflows.encode.runtime.metadata_inject import (
    _build_dovi_record_from_rpu as _build_dovi_record_from_rpu_runtime,
)
from core.workflows.encode.runtime.mux_assembly import (
    EncodeFinalMuxBuilder as _EncodeFinalMuxBuilder,
    EncodeFinalMuxBuilderCallbacks as _EncodeFinalMuxBuilderCallbacks,
    EncodeStreamMappingCallbacks as _EncodeStreamMappingCallbacks,
    EncodeStreamMappingService as _EncodeStreamMappingService,
    TrackMetadataArgsBuilder as _TrackMetadataArgsBuilder,
    TrackMetadataArgsBuilderCallbacks as _TrackMetadataArgsBuilderCallbacks,
    build_injected_hevc_wrap_command as _build_injected_hevc_wrap_command_runtime,
    disposition_value_from_edit as _disposition_value_from_edit_runtime,
    normalized_track_language_value as _normalized_track_language_value_runtime,
    track_spec_for_track_order as _track_spec_for_track_order_runtime,
)
from core.workflows.encode.runtime.multisource_sync import (
    EncodeMultisourceSyncCallbacks as _EncodeMultisourceSyncCallbacks,
    EncodeMultisourceSyncService as _EncodeMultisourceSyncService,
    append_offset_aux_inputs as _append_offset_aux_inputs_runtime,
    append_strict_interleave_mux_flags as _append_strict_interleave_mux_flags_runtime,
    append_sync_inputs as _append_sync_inputs_runtime,
)
from core.workflows.encode.runtime.nvencc import (
    build_decode_pipe_cmd as _build_decode_pipe_nvencc,
    is_nvencc_codec as _is_nvencc_codec_runtime,
    nvencc_supports_dynamic_hdr as _nvencc_supports_dynamic_hdr_runtime,
)
from core.workflows.encode.runtime.nvencc_execution import (
    NvenccAssetPreparationCallbacks as _NvenccAssetPreparationCallbacks,
    NvenccAssetPreparationService as _NvenccAssetPreparationService,
    NvenccDirectOutputRunner as _NvenccDirectOutputRunner,
    NvenccDirectOutputRunnerCallbacks as _NvenccDirectOutputRunnerCallbacks,
    NvenccPipeExecutor as _NvenccPipeExecutor,
    NvenccRuntimeRemuxBuilder as _NvenccRuntimeRemuxBuilder,
    NvenccRuntimeRemuxBuilderCallbacks as _NvenccRuntimeRemuxBuilderCallbacks,
    build_nvencc_pipeline_commands as _build_nvencc_pipeline_commands_runtime,
)
from core.workflows.encode.runtime.nvencc_routing import (
    NvenccInputRouter as _NvenccInputRouter,
    NvenccInputRouting as _NvenccInputRouting,
    NvenccRoutingCallbacks as _NvenccRoutingCallbacks,
    fps_expr_to_float as _fps_expr_to_float_runtime,
    mediainfo_video_fps_expr as _mediainfo_video_fps_expr_runtime,
    mediainfo_video_is_vfr as _mediainfo_video_is_vfr_runtime,
    normalize_frame_rate_expr as _normalize_frame_rate_expr_runtime,
    nvencc_can_use_native_timestamps as _nvencc_can_use_native_timestamps_runtime,
    nvencc_crop_offsets_from_extra_params as _nvencc_crop_offsets_from_extra_params_runtime,
    nvencc_dovi_rpu_prm as _nvencc_dovi_rpu_prm_runtime,
    nvencc_raw_input_needs_fps_hint as _nvencc_raw_input_needs_fps_hint_runtime,
    source_is_vfr as _source_is_vfr_runtime,
    source_video_dimensions as _source_video_dimensions_runtime,
    source_video_fps_expr as _source_video_fps_expr_runtime,
)
from core.workflows.encode.runtime.preparation import (
    EncodePreparationRunner as _EncodePreparationRunner,
    EncodePreparationRunnerCallbacks as _EncodePreparationRunnerCallbacks,
)
from core.workflows.encode.runtime.video_preparation import (
    TwoPassLogCleanupService as _TwoPassLogCleanupService,
    TwoPassRunner as _TwoPassRunner,
    TwoPassRunnerCallbacks as _TwoPassRunnerCallbacks,
    VideoOnlyCommandBuilder as _VideoOnlyCommandBuilder,
    VideoOnlyCommandBuilderCallbacks as _VideoOnlyCommandBuilderCallbacks,
    VideoPreparationPolicyCallbacks as _VideoPreparationPolicyCallbacks,
    VideoPreparationPolicyService as _VideoPreparationPolicyService,
)
from core.workflows.encode.backends import (
    BackendContext as _BackendContext,
    ProgressEvent as _ProgressEvent,
    backend_for_codec as _backend_for_codec_runtime,
)
from core.workflows.encode.runtime.command_builders import (
    EncodeCommandBuilderCallbacks as _EncodeCommandBuilderCallbacks,
    build_runtime_single_pass_with_sync as _build_runtime_single_pass_with_sync_runtime,
    build_runtime_two_pass_with_sync as _build_runtime_two_pass_with_sync_runtime,
    build_single_pass as _build_single_pass_runtime,
    build_two_pass as _build_two_pass_runtime,
)
from core.workflows.encode.hw_devices import (
    select_linux_hwaccel_device,
    select_windows_hwaccel_device,
)
from core.workflows.encode.planning.command_plan import (
    build_encode_command_selection as _build_encode_command_selection_plan,
)
from core.workflows.encode.planning.metadata_plan import (
    append_container_metadata_args as _append_container_metadata_args_plan,
    materialize_container_metadata_inputs as _materialize_container_metadata_inputs_plan,
    prepare_container_metadata_inputs as _prepare_container_metadata_inputs_plan,
)
from core.workflows.encode.planning.encode_plan import build_encode_plan as _build_encode_plan_data
from core.workflows.encode.planning.offsets import (
    build_offset_specs as _build_offset_specs_plan,
    offset_seconds as _offset_seconds_plan,
    track_offset_ms as _track_offset_ms_plan,
    track_time_offset_mode_lookup as _track_time_offset_mode_lookup_plan,
    track_time_offset_lookup as _track_time_offset_lookup_plan,
    video_map_arg as _video_map_arg_plan,
)
from core.workflows.encode.planning.preview import (
    format_preview_command as _format_preview_command_plan,
    format_preview_commands as _format_preview_commands_plan,
    format_preview_selection as _format_preview_selection_plan,
)
from core.workflows.encode.planning.track_assembly import (
    build_track_input_paths as _build_track_input_paths_plan,
    resolve_track_assembly as _resolve_track_assembly_plan,
)
from core.workflows.encode.planning.sources import (
    resolve_source_layout as _resolve_source_layout,
    source_input_index_map as _source_input_index_map_plan,
)
from core.workflows.encode.planning.subtitles import (
    probe_stream_indices as _probe_stream_indices_plan,
    resolve_subtitle_tracks_for_encode as _resolve_subtitle_tracks_for_encode_plan,
)
from core.workflows.encode.planning.sync_plan import (
    build_sync_analysis_plan as _build_sync_analysis_plan,
)
from core.workflows.encode.planning.validation import (
    is_dir_writable as _is_dir_writable_plan,
    validate_encode_config as _validate_encode_config_plan,
)
from core.workflows.encode.models import (
    EncodeConfig, QualityMode,
    VideoEncodeSettings,
    normalize_audio_bitrate_kbps,
)
from core.workflows.matroska_dovi_block_addition import (
    MatroskaDoviBlockAdditionEditor,
)
from core.workflows.encode.planning.plan_models import (
    EncodePlan as _EncodePlan,
    MaterializedContainerMetadataPlan as _MaterializedContainerMetadataPlan,
    ResolvedTrackAssembly as _ResolvedTrackAssembly,
)
class EncodeWorkflow(QObject):
    """
    Construit et exécute un encodage ffmpeg.

    Usage :
        wf = EncodeWorkflow(ffmpeg_bin="ffmpeg")
        cmd  = wf.build_command_single(config)   # list[str] — aperçu
        cmds = wf.build_command(config)           # list[str] ou list[list[str]]
        errors = wf.validate(config)
        signals = wf.run(config)

    Signaux :
        log_message(level, message)

    API étendue (préfixe `_`, testable mais non publique) :
      - Hooks d'orchestration : `_run_with_preparation`, `_run_with_metadata_inject`,
        `_run_multi_video_pipeline`, `_run_two_pass`, `_check_cancelled`.
      - Builders FFmpeg internes : `_build_video_only_cmd`,
        `_build_video_only_cmd_for_track`, `_build_video_only_two_pass`,
        `_build_video_only_two_pass_for_track`,
        `_build_multi_video_track_encode_commands`,
        `_build_runtime_single_pass_with_sync`,
        `_build_runtime_two_pass_with_sync`, `_build_track_meta_args`.
      - Sondes / décisions : `_prepare_multisource_sync`,
        `_detect_source_dynamic_hdr_presence`, `_bind_nfo_write`.
      - Attributs init exposés pour assertion de configuration : `_runner`,
        `_ffmpeg_threads`, `_max_parallel_video_encodes`, `_ram_buffer_threshold_pct`,
        `_generate_nfo`, `_postprocess_service`, `_bins`, `_mediainfo_bin`,
        `_muxing_post_action`.

    Toutes les autres méthodes `_xxx` sont des wrappers triviaux vers
    `core/workflows/common/`, `domain/`, `planning/` ou `runtime/` ; leur
    suppression est planifiée au Step 9 du PLAN_REFONTE.
    """

    log_message = Signal(str, str)

    def __init__(
        self,
        ffmpeg_bin:                str  = "ffmpeg",
        dovi_tool_bin:             str  = "dovi_tool",
        hdr10plus_bin:             str  = "hdr10plus_tool",
        mediainfo_bin:             str  = "mediainfo",
        ram_buffer_enabled:        bool = True,
        ram_buffer_threshold_pct:  int  = 15,
        ffmpeg_threads:            int | None = None,
        max_parallel_video_encodes: int | None = 1,
        parent: QObject | None         = None,
        *,
        writing_application:       str  = "",
        generate_nfo:              bool = True,
        nvencc_bin:                str | None = None,
        sync_rewrite_enabled:      bool = False,
        aac_bitrate_per_channel_kbps: int = 96,
        eac3_bitrate_per_channel_kbps: int = 96,
    ) -> None:
        super().__init__(parent)
        self._ffmpeg = ffmpeg_bin
        self._bins: dict[str, str] = {
            "dovi_tool":      dovi_tool_bin,
            "hdr10plus_tool": hdr10plus_bin,
            "mediainfo":      mediainfo_bin,
        }
        # NVEncC est optionnel : None signifie "pas configuré". Stocké séparément
        # pour permettre une vérification explicite avant d'invoquer le pipeline
        # ffmpeg → NVEncC → ffmpeg.
        self._nvencc_bin: str | None = nvencc_bin
        # Cache mémoire : évite de ré-exécuter ffprobe/mediainfo à chaque
        # reconstruction d'aperçu (preview_command peut être appelé des dizaines
        # de fois pour le même fichier lors de changements UI).
        # Clé : (abs_path, mtime_ns, size). Invalide automatiquement si le
        # fichier a été modifié.
        self._hdr_metadata_service = HdrMetadataProbeService(
            ffmpeg_bin=lambda: self._ffmpeg,
            tool_bin=lambda name: self._bins.get(name) or name,
        )
        self._generate_nfo = generate_nfo
        self._sync_rewrite_enabled = bool(sync_rewrite_enabled)
        self._sync_rewrite_audio_bitrates = {
            "aac": int(aac_bitrate_per_channel_kbps or 96),
            "ac3": int(eac3_bitrate_per_channel_kbps or 96),
            "eac3": int(eac3_bitrate_per_channel_kbps or 96),
        }
        self._runner = ToolRunner(max_workers=1, parent=self)
        self._ram_buffer_enabled       = ram_buffer_enabled
        self._ram_buffer_threshold_pct = max(0, min(ram_buffer_threshold_pct, 90))
        self._ffmpeg_threads = _normalize_ffmpeg_thread_count(ffmpeg_threads)
        self._max_parallel_video_encodes = _normalize_max_parallel_video_encodes(max_parallel_video_encodes)
        self._writing_application = writing_application.strip()
        self._postprocess_service = RemuxPostprocessService(
            ffprobe_bin=self._ffprobe_bin_from_ffmpeg(ffmpeg_bin),
        )
        from core.workflows.matroska_header_editor import MatroskaMuxingAppPostAction
        from core.workflows.matroska_language_editor import MatroskaLanguagePostAction
        self._muxing_post_action = MatroskaMuxingAppPostAction(
            app_prefix=MatroskaMuxingAppPostAction.default_prefix(APP_VERSION_LABEL),
            log_cb=self.log_message.emit,
        )
        self._language_post_action = MatroskaLanguagePostAction(
            log_cb=self.log_message.emit,
        )
        self._signal_binding_service = _SignalBindingService(
            _SignalBindingServiceCallbacks(
                muxing_bind_on_success=lambda signals, output: self._muxing_post_action.bind_on_success(signals, output),
                language_bind_on_success=lambda signals, output: self._language_post_action.bind_on_success(signals, output),
                write_nfo=lambda output: write_mediainfo_nfo(
                    output,
                    log_cb=self.log_message.emit,
                    mediainfo_bin=self._bins.get("mediainfo") or "mediainfo",
                ),
                remove_path=remove_path,
            )
        )

    def set_ffmpeg(self, ffmpeg_bin: str) -> None:
        """Met à jour le binaire ffmpeg utilisé pour l'encodage (ex: ffmpeg système pour HW)."""
        self._ffmpeg = ffmpeg_bin
        self._postprocess_service.set_ffprobe_bin(self._ffprobe_bin_from_ffmpeg(ffmpeg_bin))

    def set_writing_application(self, writing_application: str) -> None:
        """Met à jour la valeur du tag Multiplexing Application."""
        self._writing_application = writing_application.strip()

    def set_ffmpeg_threads(self, ffmpeg_threads: int | None) -> None:
        """Met à jour le nombre de threads passé à FFmpeg via `-threads`."""
        self._ffmpeg_threads = _normalize_ffmpeg_thread_count(ffmpeg_threads)

    def set_max_parallel_video_encodes(self, max_parallel_video_encodes: int | None) -> None:
        """Met à jour le niveau max de parallélisme pour la préparation multi-pistes vidéo."""
        self._max_parallel_video_encodes = _normalize_max_parallel_video_encodes(max_parallel_video_encodes)

    def set_mediainfo_bin(self, mediainfo_bin: str) -> None:
        self._bins["mediainfo"] = mediainfo_bin

    def set_nvencc_bin(self, nvencc_bin: str | None) -> None:
        """Met à jour le chemin vers NVEncC (None = pipeline NVEncC indisponible)."""
        self._nvencc_bin = nvencc_bin or None

    def set_generate_nfo(self, generate_nfo: bool) -> None:
        self._generate_nfo = generate_nfo

    def set_sync_rewrite_enabled(self, enabled: bool) -> None:
        self._sync_rewrite_enabled = bool(enabled)

    def set_sync_rewrite_audio_bitrates(
        self,
        *,
        aac_bitrate_per_channel_kbps: int,
        eac3_bitrate_per_channel_kbps: int,
    ) -> None:
        self._sync_rewrite_audio_bitrates = {
            "aac": int(aac_bitrate_per_channel_kbps or 96),
            "ac3": int(eac3_bitrate_per_channel_kbps or 96),
            "eac3": int(eac3_bitrate_per_channel_kbps or 96),
        }

    def _ffmpeg_thread_args(self, thread_count: int | None = None) -> list[str]:
        effective = self._ffmpeg_threads if thread_count is None else thread_count
        return _common_ffmpeg_thread_args(effective)

    def _parallel_video_worker_thread_count(
        self,
        *,
        resource_keys: list[str],
        max_parallel: int,
    ) -> int | None:
        worker_count = min(
            max(1, len(set(resource_keys))),
            max(1, int(max_parallel)),
        )
        if worker_count <= 1:
            return None

        base_threads = (
            self._ffmpeg_threads
            if self._ffmpeg_threads > 0
            else _default_ffmpeg_thread_count()
        )
        return max(1, base_threads // worker_count)

    @staticmethod
    def _ffmpeg_progress_args() -> list[str]:
        """
        Force une progression machine stable pour l'UI.

        Les stats texte classiques (`time=...`) dépendent du build FFmpeg et du
        codec utilisé. `-progress pipe:1` garantit une sortie structurée
        (`out_time=...`) que l'UI peut parser de façon fiable.
        """
        return _common_ffmpeg_progress_args()

    @staticmethod
    def _ffprobe_bin_from_ffmpeg(ffmpeg_bin: str) -> str:
        ffmpeg_path = Path(ffmpeg_bin)
        name = ffmpeg_path.name.lower()
        if name in {"ffmpeg", "ffmpeg.exe"}:
            return str(ffmpeg_path.with_name("ffprobe" + ffmpeg_path.suffix))
        return "ffprobe"

    @classmethod
    def _primary_video_settings(cls, config: EncodeConfig) -> VideoEncodeSettings:
        videos = cls._video_tracks(config)
        if videos:
            return videos[0]
        return VideoEncodeSettings()

    @staticmethod
    def _is_video_passthrough(config: EncodeConfig) -> bool:
        return EncodeWorkflow._primary_video_settings(config).codec == "copy"

    @classmethod
    def _uses_two_pass(cls, config: EncodeConfig) -> bool:
        video = cls._primary_video_settings(config)
        return video.codec != "copy" and video.quality_mode == QualityMode.SIZE

    @staticmethod
    def _wants_dynamic_hdr_copy(config: EncodeConfig) -> bool:
        video = EncodeWorkflow._primary_video_settings(config)
        return bool(video.copy_dv or video.copy_hdr10plus)

    @staticmethod
    def _needs_static_hdr_bitstream_patch(config: EncodeConfig) -> bool:
        video = EncodeWorkflow._primary_video_settings(config)
        if video.codec == "copy":
            return False
        return _needs_static_hdr_bitstream_patch_domain(video)

    @classmethod
    def _needs_metadata_inject(cls, config: EncodeConfig) -> bool:
        if cls._is_video_passthrough(config):
            return False
        if _is_nvencc_codec_runtime(EncodeWorkflow._primary_video_settings(config).codec):
            return False
        return cls._wants_dynamic_hdr_copy(config) or cls._needs_static_hdr_bitstream_patch(config)

    @staticmethod
    def _video_source_path(config: EncodeConfig) -> Path:
        video = EncodeWorkflow._primary_video_settings(config)
        return Path(video.source_path or config.source)

    @staticmethod
    def _video_stream_index(config: EncodeConfig) -> int:
        video = EncodeWorkflow._primary_video_settings(config)
        return int(getattr(video, "stream_index", 0) or 0)

    @classmethod
    def _video_map_key(cls, config: EncodeConfig) -> tuple[Path, int, str]:
        return (cls._video_source_path(config), cls._video_stream_index(config), "video")

    @classmethod
    def _backend_for_codec(cls, codec: str | None):
        return _backend_for_codec_runtime(codec)

    @classmethod
    def _backend_for_config(cls, config: EncodeConfig):
        return cls._backend_for_codec(cls._primary_video_settings(config).codec)

    def _backend_context(self, *, plan: _EncodePlan | None = None) -> _BackendContext:
        return _BackendContext(workflow=self, plan=plan)

    def _nvencc_validation_errors(
        self,
        config: EncodeConfig,
        *,
        plan: _EncodePlan | None = None,
    ) -> list[str]:
        all_video_tracks = self._video_tracks(config)
        videos = [video for video in all_video_tracks if video.codec != "copy"]
        if not any(_is_nvencc_codec_runtime(video.codec) for video in videos):
            return []

        errors: list[str] = []
        if len(all_video_tracks) != 1:
            errors.append(
                "NVEncC ne supporte pas le mode multi-pistes vidéo dans cette version."
            )
            return errors
        if len(videos) != 1 or not _is_nvencc_codec_runtime(videos[0].codec):
            errors.append(
                "NVEncC ne supporte qu'une seule piste vidéo encodée dans cette version."
            )
            return errors

        video = videos[0]
        if not self._nvencc_bin:
            errors.append("NVEncC est sélectionné mais le binaire n'est pas configuré.")
        if video.quality_mode == QualityMode.SIZE:
            errors.append("NVEncC ne supporte pas le mode taille cible (2 passes) dans cette version.")
        if video.inject_hdr_meta and video.codec == "nvencc_h264":
            errors.append("NVEncC H.264 ne supporte pas les métadonnées HDR statiques.")
        if (video.copy_dv or video.copy_hdr10plus) and not _nvencc_supports_dynamic_hdr_runtime(video.codec):
            errors.append("Le codec NVEncC sélectionné ne supporte pas DoVi/HDR10+.")
        _ = plan
        return errors

    def _load_mediainfo_video_track(self, path: Path) -> dict | None:
        return self._hdr_metadata_service.load_mediainfo_video_track(path)

    @classmethod
    def _video_track_mapping(
        cls,
        config: EncodeConfig,
        input_path: Path | str,
        mapped_stream_index: int | None = None,
    ) -> tuple[tuple[Path, int, str], Path | str, int]:
        stream_index = cls._video_stream_index(config)
        return (
            cls._video_map_key(config),
            input_path,
            stream_index if mapped_stream_index is None else int(mapped_stream_index),
        )

    def _detect_source_dynamic_hdr_presence(self, source: Path) -> tuple[bool, bool] | None:
        return self._hdr_metadata_service.detect_source_dynamic_hdr_presence(
            source,
            ffprobe_streams_payload=self._ffprobe_streams_payload,
            ffprobe_stream_dicts=self._ffprobe_stream_dicts,
            mediainfo_hdr_flags=self._mediainfo_hdr_flags,
            ffprobe_frame_dynamic_hdr_flags=self._ffprobe_frame_dynamic_hdr_flags,
        )

    def _subtitle_codec_of(self, source: Path, stream_index: int) -> str:
        """Retourne le codec (ffprobe ``codec_name``) du stream ``stream_index``.

        Retourne chaîne vide si non résolvable : le routage fera un fallback copy.
        """
        payload = self._ffprobe_streams_payload(Path(source))
        if not payload:
            return ""
        for stream in self._ffprobe_stream_dicts(payload):
            raw_idx = stream.get("index", -1)
            if isinstance(raw_idx, bool):
                continue
            if not isinstance(raw_idx, (int, float, str, bytes, bytearray)):
                continue
            try:
                idx_val = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if idx_val == int(stream_index):
                return str(stream.get("codec_name", "") or "")
        return ""

    def _stream_codec_of(self, source: Path, stream_index: int, track_type: str = "") -> str:
        payload = self._ffprobe_streams_payload(Path(source))
        if not payload:
            return ""
        for stream in self._ffprobe_stream_dicts(payload):
            raw_idx = stream.get("index", -1)
            try:
                idx_val = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if idx_val != int(stream_index):
                continue
            if track_type and str(stream.get("codec_type") or "") != str(track_type):
                continue
            return str(stream.get("codec_name", "") or "")
        return ""

    def _video_codec_of(self, source: Path, stream_index: int) -> str:
        """Retourne le codec vidéo ffprobe du flux demandé, ou chaîne vide."""
        payload = self._ffprobe_streams_payload(Path(source))
        if not payload:
            return ""
        for stream in self._ffprobe_stream_dicts(payload):
            raw_idx = stream.get("index", -1)
            if isinstance(raw_idx, bool):
                continue
            if not isinstance(raw_idx, (int, float, str, bytes, bytearray)):
                continue
            try:
                idx_val = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if idx_val != int(stream_index):
                continue
            if str(stream.get("codec_type") or "") != "video":
                continue
            return str(stream.get("codec_name", "") or "").strip().lower()
        return ""

    def _subtitle_codec_args(
        self, subtitle_tracks: list[tuple[object, int]]
    ) -> list[str]:
        """Construit les args ``-c:s:N`` par piste selon son codec source.

        Si toutes les pistes passent en copy → retourne ``["-c:s", "copy"]``.
        Sinon : ``-c:s copy`` par défaut + ``-c:s:N srt`` pour chaque piste à
        convertir. Ordre de ``subtitle_tracks`` = ordre de sortie.
        """
        per_index: list[str] = []
        any_convert = False
        for out_idx, (src_path, stream_idx) in enumerate(subtitle_tracks):
            source_path = Path(str(src_path))
            codec = self._subtitle_codec_of(source_path, int(stream_idx))
            codec_arg, _ = plan_subtitle_codec(codec)
            if codec_arg != "copy":
                any_convert = True
                per_index.extend([f"-c:s:{out_idx}", codec_arg])
        if not any_convert:
            return ["-c:s", "copy"]
        return ["-c:s", "copy", *per_index]

    def _ffprobe_streams_payload(self, source: Path) -> dict[str, object] | None:
        return self._hdr_metadata_service.ffprobe_streams_payload(source)

    @staticmethod
    def _source_cache_key(source: Path) -> tuple[str, int, int] | None:
        return HdrMetadataProbeService.source_cache_key(source)

    @staticmethod
    def _ffprobe_stream_dicts(payload: dict[str, object]) -> list[dict[str, object]]:
        return HdrMetadataProbeService.ffprobe_stream_dicts(payload)

    def _ffprobe_frame_dynamic_hdr_flags(
        self,
        source: Path,
        *,
        max_frames: int = 240,
    ) -> tuple[bool, bool] | None:
        return self._hdr_metadata_service.ffprobe_frame_dynamic_hdr_flags(
            source,
            max_frames=max_frames,
        )

    def _mediainfo_hdr_flags(self, source: Path) -> tuple[bool, bool] | None:
        return self._hdr_metadata_service.mediainfo_hdr_flags(source)

    def _build_master_display_for_primaries(self, primaries_label: str) -> str:
        return self._hdr_metadata_service.build_master_display_for_primaries(primaries_label)

    def _color_primaries_label(self, source: Path) -> str:
        return self._hdr_metadata_service.color_primaries_label(source)

    def _extract_static_hdr_via_ffprobe(self, source: Path) -> tuple[str, str]:
        return self._hdr_metadata_service.extract_static_hdr_via_ffprobe(source)

    def _extract_static_hdr_metadata(self, source: Path) -> tuple[str, str]:
        return self._hdr_metadata_service.extract_static_hdr_metadata(source)

    def _normalize_dynamic_hdr_config(self, config: EncodeConfig) -> EncodeConfig:
        return DynamicHdrConfigNormalizer(
            self._dynamic_hdr_normalizer_callbacks()
        ).normalize_single(config)

    def _normalize_dynamic_hdr_multi(self, config: EncodeConfig) -> EncodeConfig:
        return DynamicHdrConfigNormalizer(
            self._dynamic_hdr_normalizer_callbacks()
        ).normalize_multi(config)

    def _dynamic_hdr_normalizer_callbacks(self) -> DynamicHdrNormalizerCallbacks:
        return DynamicHdrNormalizerCallbacks(
            log=self.log_message.emit,
            wants_dynamic_hdr_copy=self._wants_dynamic_hdr_copy,
            is_video_passthrough=self._is_video_passthrough,
            primary_video_settings=self._primary_video_settings,
            video_tracks=self._video_tracks,
            video_source_path=self._video_source_path,
            video_source_from_settings=self._video_source_from_settings,
            detect_source_dynamic_hdr_presence=self._detect_source_dynamic_hdr_presence,
            extract_static_hdr_metadata=self._extract_static_hdr_metadata,
            extract_static_hdr_via_ffprobe=self._extract_static_hdr_via_ffprobe,
            color_primaries_label=self._color_primaries_label,
            build_master_display_for_primaries=self._build_master_display_for_primaries,
        )

    # ------------------------------------------------------------------
    # Construction de la commande
    # ------------------------------------------------------------------

    def build_command(self, config: EncodeConfig) -> list[str] | list[list[str]]:
        """
        Retourne une commande (list[str]) ou deux commandes pour la double passe (list[list[str]]).
        """
        plan = self._build_encode_plan(config)
        commands = self._backend_for_config(config).build_preview(
            config,
            ctx=self._backend_context(plan=plan),
        )
        if len(commands) <= 1:
            return list(commands[0]) if commands else []
        return [list(cmd) for cmd in commands]

    def build_command_single(self, config: EncodeConfig) -> list[str]:
        """Toujours une seule commande — pour l'aperçu UI.

        En mode NVEncC, l'aperçu retourne la commande d'encode native, suivie
        au runtime d'un remux ffmpeg séparé.
        """
        plan = self._build_encode_plan(config)
        return list(
            self._backend_for_config(config).build_single_preview(
                config,
                ctx=self._backend_context(plan=plan),
            )
        )

    def _build_nvencc_pipeline_commands(
        self, config: EncodeConfig,
    ) -> list[list[str]] | None:
        return _build_nvencc_pipeline_commands_runtime(
            config,
            nvencc_bin=self._nvencc_bin,
            ffmpeg_bin=self._ffmpeg,
            video_tracks=self._video_tracks,
            resolve_input_routing=self._resolve_nvencc_input_routing,
        )

    def _build_runtime_nvencc_remux_cmd(
        self,
        config: EncodeConfig,
        encoded_video: Path,
        *,
        video_offset_ms: int = 0,
        chapter_materialize_dir: Path | None = None,
        signals: TaskSignals | None = None,
        plan: _EncodePlan | None = None,
    ) -> tuple[list[str], LiveSyncSession | None, list[Path]]:
        return _NvenccRuntimeRemuxBuilder(
            _NvenccRuntimeRemuxBuilderCallbacks(
                ffmpeg_bin=self._ffmpeg,
                ffmpeg_progress_args=self._ffmpeg_progress_args,
                ffmpeg_thread_args=self._ffmpeg_thread_args,
                offset_input_args=self._offset_input_args,
                build_encode_plan=self._build_encode_plan,
                prepare_multisource_sync=self._prepare_multisource_sync,
                append_sync_inputs=self._append_sync_inputs,
                materialize_container_metadata_inputs=self._materialize_container_metadata_inputs,
                append_offset_aux_inputs=self._append_offset_aux_inputs,
                append_stream_maps_and_attachments=self._append_stream_maps_and_attachments,
                append_strict_interleave_mux_flags=self._append_strict_interleave_mux_flags,
                append_container_metadata_args=self._append_container_metadata_args,
            )
        ).build(
            config,
            encoded_video,
            video_offset_ms=video_offset_ms,
            chapter_materialize_dir=chapter_materialize_dir,
            signals=signals,
            plan=plan,
        )

    def _build_direct_output_commands(
        self,
        config: EncodeConfig,
        *,
        chapter_materialize_dir: Path | None = None,
    ) -> list[str] | list[list[str]]:
        selection = _build_encode_command_selection_plan(
            config,
            plan=self._build_encode_plan(config),
            is_multi_video=self._is_multi_video,
            uses_two_pass=self._uses_two_pass,
            build_multi_video_preview=self._build_multi_video_command_preview,
            build_two_pass=self._build_two_pass,
            build_single_pass=self._build_single_pass,
            chapter_materialize_dir=chapter_materialize_dir,
        )
        if len(selection.commands) <= 1:
            return list(selection.preview_command)
        return [list(cmd) for cmd in selection.commands]

    def _prepare_nvencc_dynamic_hdr_assets(
        self,
        config: EncodeConfig,
        *,
        work_dir: Path,
        signals: TaskSignals,
        run_cmd: Callable[[list[str], str], str],
        cleanup_paths: list[Path] | None = None,
    ) -> tuple[Path, int, Path | None, Path | None, bool, list[Path]]:
        return _NvenccAssetPreparationService(
            _NvenccAssetPreparationCallbacks(
                ffmpeg_bin=self._ffmpeg,
                bins=dict(self._bins),
                log=self.log_message.emit,
                primary_video_settings=self._primary_video_settings,
                video_source_path=self._video_source_path,
                video_stream_index=self._video_stream_index,
                source_is_vfr=self._source_is_vfr,
                load_mediainfo_video_track=self._load_mediainfo_video_track,
            )
        ).prepare(
            config,
            work_dir=work_dir,
            signals=signals,
            run_cmd=run_cmd,
            cleanup_paths=cleanup_paths,
        )

    def _run_nvencc_pipe_commands(
        self,
        *,
        decode_cmd: list[str],
        encode_cmd: list[str],
        cwd: Path,
        signals: TaskSignals,
    ) -> str:
        return _NvenccPipeExecutor().run(
            decode_cmd=decode_cmd,
            encode_cmd=encode_cmd,
            cwd=cwd,
            signals=signals,
        )

    def _run_nvencc_direct_output(
        self,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        *,
        prep_signals: TaskSignals | None = None,
        plan: _EncodePlan | None = None,
    ) -> TaskSignals:
        return _NvenccDirectOutputRunner(
            _NvenccDirectOutputRunnerCallbacks(
                nvencc_bin=self._nvencc_bin,
                check_cancelled=self._check_cancelled,
                log_step=self._log_step,
                log_info=lambda message: self.log_message.emit("INFO", message),
                primary_video_settings=self._primary_video_settings,
                video_source_path=self._video_source_path,
                video_stream_index=self._video_stream_index,
                build_encode_plan=self._build_encode_plan,
                resolve_input_routing=self._resolve_nvencc_input_routing,
                build_runtime_remux_cmd=self._build_runtime_nvencc_remux_cmd,
                run_cmd=lambda cmd, cwd, label, progress_cb, signals: self._runner._run_cmd(
                    cmd,
                    cwd=cwd,
                    label=label,
                    progress_cb=progress_cb,
                    signals=signals,
                ),
            )
        ).run(
            config,
            cleanup_paths,
            prep_signals=prep_signals,
            plan=plan,
        )

    def _build_encode_plan(self, config: EncodeConfig) -> _EncodePlan:
        return _build_encode_plan_data(
            config,
            resolve_subtitle_tracks=self._resolved_subtitle_tracks_for_encode,
            resolve_global_tags=self._resolve_global_tags,
            video_tracks=self._video_tracks,
            video_source_from_settings=self._video_source_from_settings,
            video_source_path=self._video_source_path,
            video_stream_index=self._video_stream_index,
            video_map_key=self._video_map_key,
        )

    def _build_multi_video_command_preview(
        self,
        config: EncodeConfig,
        *,
        plan: _EncodePlan | None = None,
    ) -> list[list[str]]:
        commands: list[list[str]] = []
        plan = plan or self._build_encode_plan(config)
        resource_keys = [
            self._video_encode_resource_key(video)
            for video in self._video_tracks(config)
            if video.codec != "copy"
        ]
        thread_count = self._parallel_video_worker_thread_count(
            resource_keys=resource_keys,
            max_parallel=self._max_parallel_video_encodes,
        )
        for idx, video in enumerate(self._video_tracks(config), start=1):
            source = self._video_source_from_settings(config, video)
            if video.codec == "copy":
                continue
            if video.quality_mode == QualityMode.SIZE:
                commands.extend(
                    self._build_multi_video_track_encode_commands(
                        config,
                        video,
                        source,
                        Path(f"<video_{idx}.mkv>"),
                        thread_count=thread_count,
                        for_preview=True,
                    )
                )
            else:
                commands.append(
                    self._build_multi_video_track_encode_commands(
                        config,
                        video,
                        source,
                        Path(f"<video_{idx}.mkv>"),
                        thread_count=thread_count,
                        for_preview=True,
                    )[-1]
                )
        commands.append(self._build_multi_video_final_mux_command(config, [], plan=plan))
        return commands

    @staticmethod
    def _collect_all_sources(config: EncodeConfig) -> list[Path]:
        """Retourne les sources uniques (source principale puis extras)."""
        return list(_resolve_source_layout(config).sources)

    @staticmethod
    def _video_tracks(config: EncodeConfig) -> list[VideoEncodeSettings]:
        if config.video_tracks:
            return list(config.video_tracks)
        if config.video is not None:
            return [config.video]
        return []

    @classmethod
    def _is_multi_video(cls, config: EncodeConfig) -> bool:
        return len(cls._video_tracks(config)) > 1

    @staticmethod
    def _video_source_from_settings(config: EncodeConfig, video: VideoEncodeSettings) -> Path:
        return Path(video.source_path or config.source)

    @staticmethod
    def _video_stream_from_settings(video: VideoEncodeSettings) -> int:
        return int(getattr(video, "stream_index", 0) or 0)

    def _append_offset_aux_inputs(
        self,
        cmd: list[str],
        specs: list[_EncodeOffsetInputSpec],
        *,
        start_input_index: int,
    ) -> tuple[int, dict[tuple[Path, int, str], tuple[int, int]]]:
        return _append_offset_aux_inputs_runtime(
            cmd,
            specs,
            start_input_index=start_input_index,
        )

    def _probe_stream_indices(self, source: Path, codec_type: str) -> list[int] | None:
        return _probe_stream_indices_plan(
            source,
            codec_type,
            ffprobe_streams_payload=self._ffprobe_streams_payload,
            ffprobe_stream_dicts=self._ffprobe_stream_dicts,
        )

    def _resolved_subtitle_tracks_for_encode(
        self,
        config: EncodeConfig,
        all_sources: list[Path],
    ) -> tuple[list[tuple[Path, int]], bool]:
        """
        Retourne la liste explicite des sous-titres à mapper et un indicateur
        de complétude de résolution.

        - subtitle_tracks explicite: résolution complète.
        - copy_subtitles=True: tentative de résolution via ffprobe sur chaque source.
          Si une source n'est pas sondable, on renvoie complete=False.
        """
        resolved = _resolve_subtitle_tracks_for_encode_plan(
            config,
            all_sources,
            probe_indices=self._probe_stream_indices,
        )
        return list(resolved.tracks), resolved.complete

    def _prepare_multisource_sync(
        self,
        *,
        config: EncodeConfig,
        all_sources: list[Path],
        sync_base_input_idx: int,
        work_dir: Path,
        signals: TaskSignals | None = None,
        allow_live: bool = True,
        plan: _EncodePlan | None = None,
    ) -> tuple[dict[tuple[Path, int, str], tuple[int, int]], list[Path | str], LiveSyncSession | None, bool]:
        return _EncodeMultisourceSyncService(
            _EncodeMultisourceSyncCallbacks(
                ffmpeg_bin=self._ffmpeg,
                ffmpeg_thread_args=self._ffmpeg_thread_args,
                log=self.log_message.emit,
                build_encode_plan=self._build_encode_plan,
                decide_strict_interleave_with_prescan=self._postprocess_service.decide_strict_interleave_with_prescan,
                ram_buffer_enabled=self._ram_buffer_enabled,
                ram_buffer_dir=EncodeWorkflow._ram_buffer_dir,
                syncer_factory=FfmpegTimelineSync,
                fallback_helper_factory=TimelineSyncFallbackHelper,
            )
        ).prepare(
            config=config,
            all_sources=all_sources,
            sync_base_input_idx=sync_base_input_idx,
            work_dir=work_dir,
            signals=signals,
            allow_live=allow_live,
            plan=plan,
        )

    @staticmethod
    def _append_strict_interleave_mux_flags(cmd: list[str]) -> None:
        _append_strict_interleave_mux_flags_runtime(cmd)

    @staticmethod
    def _append_sync_inputs(cmd: list[str], sync_inputs: list[Path | str]) -> None:
        _append_sync_inputs_runtime(cmd, sync_inputs)

    def _append_stream_maps_and_attachments(
        self,
        cmd: list[str],
        config: EncodeConfig,
        *,
        source_idx: dict[Path, int],
        subtitle_copy_input_indices: list[int],
        sync_remap: dict[tuple[Path, int, str], tuple[int, int]] | None = None,
        offset_remap: dict[tuple[Path, int, str], tuple[int, int]] | None = None,
        subtitle_tracks_override: list[tuple[Path, int]] | None = None,
        force_copy_subtitles_wildcard: bool = True,
    ) -> None:
        _EncodeStreamMappingService(
            _EncodeStreamMappingCallbacks(
                subtitle_codec_args=self._subtitle_codec_args,
                describe_attachment_stream=self._describe_attachment_stream,
                default_attachment_filename=_default_attachment_filename,
            )
        ).append(
            cmd,
            config,
            source_idx=source_idx,
            subtitle_copy_input_indices=subtitle_copy_input_indices,
            sync_remap=sync_remap,
            offset_remap=offset_remap,
            subtitle_tracks_override=subtitle_tracks_override,
            force_copy_subtitles_wildcard=force_copy_subtitles_wildcard,
        )

    def _prepare_container_metadata_inputs(
        self,
        cmd: list[str],
        config: EncodeConfig,
        *,
        source_idx: dict[Path, int],
        next_input_index: int,
        plan: _EncodePlan | None = None,
        chapter_materialize_dir: Path | None = None,
        chapter_probe_source: Path | None = None,
    ) -> tuple[int, int | None, int | None]:
        """Ajoute les inputs nécessaires aux metadata (chapitres/tags) et retourne leurs index."""
        planned = _prepare_container_metadata_inputs_plan(
            cmd,
            config,
            source_idx=source_idx,
            next_input_index=next_input_index,
            container_metadata_plan=(plan.container_metadata if plan is not None else None),
            chapter_materialize_dir=chapter_materialize_dir,
            chapter_probe_source=chapter_probe_source,
            probe_duration_seconds=self._postprocess_service.probe_duration_seconds,
            write_ffmetadata_chapters=lambda entries, out_dir, duration_s: self._postprocess_service.write_ffmetadata_chapters(
                entries=entries,
                out_dir=out_dir,
                duration_s=duration_s,
            ),
        )
        return (
            planned.next_input_index,
            planned.chapter_input_index,
            planned.tag_input_index,
        )

    def _materialize_container_metadata_inputs(
        self,
        config: EncodeConfig,
        *,
        source_idx: dict[Path, int],
        next_input_index: int,
        plan: _EncodePlan | None = None,
        chapter_materialize_dir: Path | None = None,
        chapter_probe_source: Path | None = None,
    ) -> _MaterializedContainerMetadataPlan:
        return _materialize_container_metadata_inputs_plan(
            config,
            source_idx=source_idx,
            next_input_index=next_input_index,
            container_metadata_plan=(plan.container_metadata if plan is not None else None),
            chapter_materialize_dir=chapter_materialize_dir,
            chapter_probe_source=chapter_probe_source,
            probe_duration_seconds=self._postprocess_service.probe_duration_seconds,
            write_ffmetadata_chapters=lambda entries, out_dir, duration_s: self._postprocess_service.write_ffmetadata_chapters(
                entries=entries,
                out_dir=out_dir,
                duration_s=duration_s,
            ),
        )

    def _append_container_metadata_args(
        self,
        cmd: list[str],
        config: EncodeConfig,
        *,
        default_metadata_input_index: int,
        default_chapter_input_index: int,
        chapter_input_index: int | None,
        tag_input_index: int | None,
        include_copy_video_stream_passthrough: bool = False,
        plan: _EncodePlan | None = None,
    ) -> None:
        """Ajoute les options metadata/chapitres/tags/track-meta en une passe."""
        _append_container_metadata_args_plan(
            cmd,
            config,
            default_metadata_input_index=default_metadata_input_index,
            default_chapter_input_index=default_chapter_input_index,
            chapter_input_index=chapter_input_index,
            tag_input_index=tag_input_index,
            include_copy_video_stream_passthrough=include_copy_video_stream_passthrough,
            is_video_passthrough=self._is_video_passthrough,
            resolve_global_tags=self._resolve_global_tags,
            build_track_meta_args=self._build_track_meta_args,
            container_metadata_plan=(plan.container_metadata if plan is not None else None),
        )

    def _resolve_track_assembly_and_offset_remap(
        self,
        *,
        cmd: list[str],
        config: EncodeConfig,
        plan: _EncodePlan,
        source_idx: dict[Path, int],
        track_input_paths: tuple[Path | str, ...],
        start_input_index: int,
        sync_remap: dict[tuple[Path, int, str], tuple[int, int]] | None = None,
        video_default_map: tuple[int, int] | None = None,
        video_fallback_input: Path | str | None = None,
        allow_sync_rewrite: bool = False,
        sync_rewrite_work_dir: Path | None = None,
        signals: TaskSignals | None = None,
    ) -> tuple[_ResolvedTrackAssembly, dict[tuple[Path, int, str], tuple[int, int]]]:
        track_assembly = _resolve_track_assembly_plan(
            config,
            plan,
            source_idx=source_idx,
            track_input_paths=track_input_paths,
            sync_remap=sync_remap,
            video_default_map=video_default_map,
            video_fallback_input=video_fallback_input,
        )
        offset_lookup = dict(plan.offset_lookup)
        offset_mode_lookup = _track_time_offset_mode_lookup_plan(config)
        rewrite_remap: dict[tuple[Path, int, str], tuple[int, int]] = {}
        next_input_index = int(start_input_index)
        if allow_sync_rewrite and self._sync_rewrite_enabled:
            audio_settings_by_key = {
                (Path(audio.source_path or config.source), int(audio.stream_index), "audio"): audio
                for audio in config.audio_tracks
            }
            rewrite_service = SyncRewriteService(
                ffmpeg_bin=self._ffmpeg,
                ffprobe_bin=self._ffprobe_bin_from_ffmpeg(self._ffmpeg),
                ffmpeg_progress_args=self._ffmpeg_progress_args(),
                ffmpeg_thread_args=self._ffmpeg_thread_args(None),
                audio_bitrate_per_channel=self._sync_rewrite_audio_bitrates,
                log_cb=lambda message: self.log_message.emit("INFO", message),
                progress_cb=(signals.progress.emit if signals is not None else None),
            )
            work_dir = sync_rewrite_work_dir or config.work_dir or config.source.parent
            for map_key, input_path, input_stream_index in track_assembly.track_mappings:
                source_path, source_stream_index, track_type = map_key
                if track_type not in {"audio", "subtitle"}:
                    continue
                offset_ms = _track_offset_ms_plan(
                    offset_lookup,
                    track_type=track_type,
                    source_path=Path(source_path),
                    stream_index=int(source_stream_index),
                )
                if offset_ms == 0:
                    continue
                mode = normalized_sync_rewrite_mode(
                    offset_mode_lookup.get((track_type, Path(source_path), int(source_stream_index)), "")
                )
                if mode == SYNC_REWRITE_MODE_OFFSET:
                    continue
                codec = ""
                input_path_obj = Path(str(input_path))
                if input_path_obj.exists():
                    codec = self._stream_codec_of(input_path_obj, int(input_stream_index), track_type)
                if not codec and track_type == "subtitle":
                    codec = self._subtitle_codec_of(Path(source_path), int(source_stream_index))
                audio_settings = audio_settings_by_key.get(map_key) if track_type == "audio" else None
                audio_target_codec = str(getattr(audio_settings, "codec", "") or "")
                audio_has_manual_encoding = bool(
                    audio_settings is not None
                    and audio_target_codec.strip().lower()
                    and audio_target_codec.strip().lower() != "copy"
                )
                prepared = rewrite_service.maybe_materialize(
                    source_path=input_path,
                    stream_index=int(input_stream_index),
                    track_type=track_type,
                    codec=codec,
                    offset_ms=offset_ms,
                    tmp_dir=Path(work_dir),
                    input_idx=next_input_index,
                    token=sync_rewrite_output_token(source_path, int(source_stream_index), track_type),
                    preserve_source_audio_params=not audio_has_manual_encoding,
                    audio_target_codec=audio_target_codec,
                    audio_target_bitrate_kbps=(
                        int(getattr(audio_settings, "bitrate_kbps", 0) or 0)
                        if audio_has_manual_encoding
                        else None
                    ),
                    cancel_cb=(signals._cancel_event.is_set if signals is not None else None),
                )
                if prepared is None:
                    continue
                cmd.extend(["-f", "matroska", "-i", str(prepared.path)])
                rewrite_remap[map_key] = (next_input_index, 0)
                offset_lookup.pop((track_type, Path(source_path), int(source_stream_index)), None)
                next_input_index += 1
        _next_input_index, offset_remap = self._append_offset_aux_inputs(
            cmd,
            _build_offset_specs_plan(
                config,
                track_mappings=list(track_assembly.track_mappings),
                offset_lookup=offset_lookup,
            ),
            start_input_index=next_input_index,
        )
        _ = _next_input_index
        return track_assembly, {**rewrite_remap, **offset_remap}

    def _append_primary_video_map_and_codec(
        self,
        cmd: list[str],
        *,
        plan: _EncodePlan,
        video_map: tuple[int, int],
        offset_remap: dict[tuple[Path, int, str], tuple[int, int]],
        video: VideoEncodeSettings,
        bitrate_kbps: int | None = None,
        include_hdr_meta: bool = True,
    ) -> None:
        cmd.extend(["-map", _video_map_arg_plan(
            video_map,
            offset_remap=offset_remap,
            map_key=plan.video_key,
        )])
        self._append_video_codec_and_hdr_args(
            cmd,
            video,
            bitrate_kbps=bitrate_kbps,
            include_hdr_meta=include_hdr_meta,
        )

    def _append_common_streams_and_metadata(
        self,
        cmd: list[str],
        *,
        config: EncodeConfig,
        source_idx: dict[Path, int],
        all_sources_count: int,
        plan: _EncodePlan,
        metadata_inputs: _MaterializedContainerMetadataPlan,
        offset_remap: dict[tuple[Path, int, str], tuple[int, int]],
        sync_remap: dict[tuple[Path, int, str], tuple[int, int]] | None = None,
        strict_interleave: bool = False,
    ) -> None:
        self._append_stream_maps_and_attachments(
            cmd,
            config,
            source_idx=source_idx,
            subtitle_copy_input_indices=list(range(all_sources_count)),
            sync_remap=sync_remap,
            offset_remap=offset_remap,
            subtitle_tracks_override=list(plan.resolved_subtitle_tracks),
            force_copy_subtitles_wildcard=(config.copy_subtitles and not plan.subtitles_resolved),
        )
        if strict_interleave:
            self._append_strict_interleave_mux_flags(cmd)
        self._append_container_metadata_args(
            cmd,
            config,
            default_metadata_input_index=0,
            default_chapter_input_index=0,
            chapter_input_index=metadata_inputs.chapter_input_index,
            tag_input_index=metadata_inputs.tag_input_index,
            include_copy_video_stream_passthrough=True,
            plan=plan,
        )

    def _encode_command_builder_callbacks(self) -> _EncodeCommandBuilderCallbacks:
        return _EncodeCommandBuilderCallbacks(
            ffmpeg_bin=self._ffmpeg,
            ffmpeg_progress_args=self._ffmpeg_progress_args,
            ffmpeg_thread_args=self._ffmpeg_thread_args,
            primary_video_settings=self._primary_video_settings,
            build_encode_plan=self._build_encode_plan,
            size_to_bitrate_kbps=self._size_to_bitrate_kbps,
            codec_domain_callbacks=self._codec_domain_callbacks,
            materialize_container_metadata_inputs=self._materialize_container_metadata_inputs,
            resolve_track_assembly_and_offset_remap=self._resolve_track_assembly_and_offset_remap,
            append_primary_video_map_and_codec=self._append_primary_video_map_and_codec,
            append_common_streams_and_metadata=self._append_common_streams_and_metadata,
            prepare_multisource_sync=self._prepare_multisource_sync,
            append_sync_inputs=self._append_sync_inputs,
            append_offset_aux_inputs=self._append_offset_aux_inputs,
            video_track_mapping=self._video_track_mapping,
        )

    def _build_single_pass(
        self,
        config: EncodeConfig,
        *,
        chapter_materialize_dir: Path | None = None,
        plan: _EncodePlan | None = None,
    ) -> list[str]:
        return _build_single_pass_runtime(
            self._encode_command_builder_callbacks(),
            config,
            chapter_materialize_dir=chapter_materialize_dir,
            plan=plan,
        )

    def _build_two_pass(
        self,
        config: EncodeConfig,
        *,
        chapter_materialize_dir: Path | None = None,
        plan: _EncodePlan | None = None,
    ) -> list[list[str]]:
        return _build_two_pass_runtime(
            self._encode_command_builder_callbacks(),
            config,
            chapter_materialize_dir=chapter_materialize_dir,
            plan=plan,
        )

    def _build_runtime_single_pass_with_sync(
        self,
        config: EncodeConfig,
        *,
        chapter_materialize_dir: Path | None = None,
        signals: TaskSignals | None = None,
        plan: _EncodePlan | None = None,
    ) -> tuple[list[str], LiveSyncSession | None, list[Path]]:
        return _build_runtime_single_pass_with_sync_runtime(
            self._encode_command_builder_callbacks(),
            config,
            chapter_materialize_dir=chapter_materialize_dir,
            signals=signals,
            plan=plan,
        )

    def _build_runtime_two_pass_with_sync(
        self,
        config: EncodeConfig,
        *,
        chapter_materialize_dir: Path | None = None,
        signals: TaskSignals | None = None,
        plan: _EncodePlan | None = None,
    ) -> tuple[list[list[str]], LiveSyncSession | None, list[Path]]:
        return _build_runtime_two_pass_with_sync_runtime(
            self._encode_command_builder_callbacks(),
            config,
            chapter_materialize_dir=chapter_materialize_dir,
            signals=signals,
            plan=plan,
        )

    # ------------------------------------------------------------------
    # Arguments par codec
    # ------------------------------------------------------------------

    def _codec_domain_callbacks(self) -> _EncodeCodecDomainCallbacks:
        return _EncodeCodecDomainCallbacks(
            platform=sys.platform,
            vaapi_device=self._vaapi_device(),
            qsv_device=self._qsv_device(),
            amf_device=self._amf_device(),
            nvenc_device=self._nvenc_device(),
        )

    @staticmethod
    def _is_h264_codec(codec: str) -> bool:
        return is_h264_video_codec(codec)

    def _nvenc_device_args(self) -> list[str]:
        if sys.platform != "win32":
            return []
        nvenc_device = self._nvenc_device()
        if not nvenc_device:
            return []
        return ["-gpu", nvenc_device]

    @staticmethod
    def _vaapi_device() -> str | None:
        """Retourne le render node Linux ciblé pour VAAPI, ou None."""
        return select_linux_hwaccel_device("hevc_vaapi")

    def _qsv_device(self) -> str | None:
        """Retourne le device ciblé pour QSV selon l'OS, ou None."""
        if sys.platform == "win32":
            return select_windows_hwaccel_device("hevc_qsv", ffmpeg_bin=self._ffmpeg)
        return select_linux_hwaccel_device("hevc_qsv")

    def _amf_device(self) -> str | None:
        if sys.platform != "win32":
            return None
        return select_windows_hwaccel_device("hevc_amf", ffmpeg_bin=self._ffmpeg)

    def _nvenc_device(self) -> str | None:
        if sys.platform != "win32":
            return None
        return select_windows_hwaccel_device("hevc_nvenc", ffmpeg_bin=self._ffmpeg)

    def _video_preparation_policy_service(self) -> _VideoPreparationPolicyService:
        return _VideoPreparationPolicyService(
            _VideoPreparationPolicyCallbacks(
                vaapi_device=self._vaapi_device,
                ffmpeg_thread_count=lambda: (
                    self._ffmpeg_threads
                    if self._ffmpeg_threads > 0
                    else _default_ffmpeg_thread_count()
                ),
                ram_buffer_threshold_pct=self._ram_buffer_threshold_pct,
                total_ram_bytes=EncodeWorkflow._total_ram_bytes,
            )
        )

    def _video_resource_policy(self) -> _VideoPreparationResourcePolicy:
        return self._video_preparation_policy_service().resource_policy()

    def _video_encode_resource_key(self, video: VideoEncodeSettings) -> str:
        return self._video_preparation_policy_service().video_encode_resource_key(video)

    def _parallel_video_min_available_ram_bytes(self) -> int:
        return self._video_preparation_policy_service().parallel_video_min_available_ram_bytes()

    def _video_prep_estimated_ram_bytes(self, spec: _VideoTrackPrepSpec) -> int:
        return self._video_preparation_policy_service().video_prep_estimated_ram_bytes(spec)

    # ------------------------------------------------------------------
    # Commandes spécialisées pour _run_with_metadata_inject
    # ------------------------------------------------------------------

    @staticmethod
    def _offset_input_args(offset_ms: int) -> list[str]:
        if offset_ms == 0:
            return []
        if offset_ms > 0:
            return ["-itsoffset", f"{offset_ms / 1000.0:.3f}"]
        return ["-ss", f"{abs(offset_ms) / 1000.0:.3f}"]

    def _size_to_bitrate_kbps_for_video(self, config: EncodeConfig, video: VideoEncodeSettings) -> int:
        duration = config.duration_s or 3600.0
        total_bits = video.target_size_mb * 8 * 1024 * 1024
        video_bits = max(total_bits, int(duration * 500_000))
        return max(500, int(video_bits / duration / 1000))

    @staticmethod
    def _two_pass_log_prefix(work_dir: Path, token: str) -> Path:
        return _TwoPassLogCleanupService.log_prefix(work_dir, token)

    def _video_only_command_builder(self) -> _VideoOnlyCommandBuilder:
        return _VideoOnlyCommandBuilder(
            _VideoOnlyCommandBuilderCallbacks(
                ffmpeg_bin=self._ffmpeg,
                ffmpeg_progress_args=self._ffmpeg_progress_args,
                ffmpeg_thread_args=self._ffmpeg_thread_args,
                offset_input_args=self._offset_input_args,
                codec_domain_callbacks=self._codec_domain_callbacks,
                primary_video_settings=self._primary_video_settings,
                video_source_path=self._video_source_path,
                video_stream_from_settings=self._video_stream_from_settings,
                size_to_bitrate_kbps=self._size_to_bitrate_kbps,
                size_to_bitrate_kbps_for_video=self._size_to_bitrate_kbps_for_video,
            )
        )

    def _build_video_track_base_cmd(
        self,
        *,
        video: VideoEncodeSettings,
        source: Path,
        stream_index: int,
        offset_ms: int = 0,
        thread_count: int | None = None,
    ) -> list[str]:
        return self._video_only_command_builder().build_video_track_base_cmd(
            video=video,
            source=source,
            stream_index=stream_index,
            offset_ms=offset_ms,
            thread_count=thread_count,
        )

    def _append_video_codec_and_hdr_args(
        self,
        cmd: list[str],
        video: VideoEncodeSettings,
        *,
        bitrate_kbps: int | None = None,
        include_hdr_meta: bool = True,
    ) -> None:
        self._video_only_command_builder().append_video_codec_and_hdr_args(
            cmd,
            video,
            bitrate_kbps=bitrate_kbps,
            include_hdr_meta=include_hdr_meta,
        )

    def _build_multi_video_track_encode_commands(
        self,
        config: EncodeConfig,
        video: VideoEncodeSettings,
        source: Path,
        output_path: Path,
        *,
        offset_ms: int = 0,
        passlog_prefix: Path | None = None,
        thread_count: int | None = None,
        for_preview: bool = False,
    ) -> list[list[str]]:
        return self._video_only_command_builder().build_multi_video_track_encode_commands(
            config,
            video,
            source,
            output_path,
            offset_ms=offset_ms,
            passlog_prefix=passlog_prefix,
            thread_count=thread_count,
            for_preview=for_preview,
        )

    def _build_video_only_cmd(self, config: EncodeConfig, output_hevc: Path) -> list[str]:
        return self._video_only_command_builder().build_video_only_cmd(config, output_hevc)

    def _build_video_only_cmd_for_track(
        self,
        config: EncodeConfig,
        video: VideoEncodeSettings,
        source: Path,
        output_hevc: Path,
        *,
        offset_ms: int = 0,
        thread_count: int | None = None,
    ) -> list[str]:
        return self._video_only_command_builder().build_video_only_cmd_for_track(
            config,
            video,
            source,
            output_hevc,
            offset_ms=offset_ms,
            thread_count=thread_count,
        )

    def _build_video_only_two_pass(
        self, config: EncodeConfig, output_hevc: Path
    ) -> list[list[str]]:
        return self._video_only_command_builder().build_video_only_two_pass(config, output_hevc)

    def _build_video_only_two_pass_for_track(
        self,
        config: EncodeConfig,
        video: VideoEncodeSettings,
        source: Path,
        output_hevc: Path,
        *,
        offset_ms: int = 0,
        passlog_prefix: Path | None = None,
        thread_count: int | None = None,
        bitrate_kbps: int | None = None,
    ) -> list[list[str]]:
        return self._video_only_command_builder().build_video_only_two_pass_for_track(
            config,
            video,
            source,
            output_hevc,
            offset_ms=offset_ms,
            passlog_prefix=passlog_prefix,
            thread_count=thread_count,
            bitrate_kbps=bitrate_kbps,
        )

    def _size_to_bitrate_kbps(self, config: EncodeConfig) -> int:
        video = self._primary_video_settings(config)
        duration = config.duration_s or 3600.0
        total_bits = video.target_size_mb * 8 * 1024 * 1024
        audio_bps = sum(
            normalize_audio_bitrate_kbps(
                a.codec,
                a.bitrate_kbps,
                a.input_channels,
                None,
                a.input_channel_layout,
            ) * 1000
            for a in config.audio_tracks
            if a.codec not in ("copy", "flac")
        )
        video_bits = total_bits - audio_bps * duration
        return max(500, int(video_bits / duration / 1000))

    # ------------------------------------------------------------------
    # Helpers RAM / buffer — cross-platform (Linux · macOS · Windows)
    # ------------------------------------------------------------------

    _total_ram_bytes      = staticmethod(_ram_buffer_module.total_ram_bytes)
    _available_ram_bytes  = staticmethod(_ram_buffer_module.available_ram_bytes)
    _macos_available_ram  = staticmethod(_ram_buffer_module.macos_available_ram)
    _ram_buffer_dir       = staticmethod(_ram_buffer_module.ram_buffer_dir)

    def _shm_path(self, tmp: Path, name: str, file_size: int) -> Path:
        """Wrapper de `runtime.ram_buffer.shm_path` lié à la config de l'instance."""
        return _ram_buffer_module.shm_path(
            tmp,
            name,
            file_size,
            enabled=self._ram_buffer_enabled,
            threshold_pct=self._ram_buffer_threshold_pct,
        )

    # ------------------------------------------------------------------
    # Aperçu lisible
    # ------------------------------------------------------------------

    def preview_command(self, config: EncodeConfig) -> str:
        commands = self._backend_for_config(config).build_preview(
            config,
            ctx=self._backend_context(plan=self._build_encode_plan(config)),
        )
        if len(commands) <= 1:
            return _format_preview_command_plan(commands[0]) if commands else ""
        return _format_preview_commands_plan(commands)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, config: EncodeConfig) -> list[str]:
        plan = _build_encode_plan_data(
            config,
            resolve_subtitle_tracks=lambda _config, _all_sources: ([], False),
            resolve_global_tags=self._resolve_global_tags,
            video_tracks=self._video_tracks,
            video_source_from_settings=self._video_source_from_settings,
            video_source_path=self._video_source_path,
            video_stream_index=self._video_stream_index,
            video_map_key=self._video_map_key,
        )
        errors = _validate_encode_config_plan(
            config,
            planned_video_tracks=plan.video_tracks,
            dir_writable=_is_dir_writable_plan,
        )
        errors.extend(
            self._backend_for_config(config).validate(
                config,
                plan=plan,
                ctx=self._backend_context(plan=plan),
            )
        )
        return errors

    def parse_progress(
        self,
        config: EncodeConfig,
        line: str,
    ) -> _ProgressEvent | None:
        return self._backend_for_config(config).parse_progress(line)

    # ------------------------------------------------------------------
    # Exécution
    # ------------------------------------------------------------------

    def _log_workflow_type(self, workflow_kind: str) -> None:
        self.log_message.emit("INFO", f"WORKFLOW TYPE - {workflow_kind}")

    def _log_step(self, step_index: int, step_name: str) -> None:
        self.log_message.emit("INFO", f"STEP {step_index} - {step_name}")

    def run(self, config: EncodeConfig, *, validate: bool = True) -> TaskSignals:
        """
        Lance l'encodage dans un thread secondaire.

        Le mode taille cible exécute deux passes séquentiellement
        dans le même thread et retourne un unique TaskSignals.
        """
        if not validate:
            return self._run_async_preparation(config)

        return self._run_with_preparation(config, validate=True)

    def _run_async_preparation(self, config: EncodeConfig) -> TaskSignals:
        return self._preparation_runner().run_async_preparation(config)

    @staticmethod
    def _check_cancelled(signals: TaskSignals | None) -> None:
        if signals is not None and signals._cancel_event.is_set():
            raise TaskCancelledError()

    def _run_with_preparation(
        self,
        config: EncodeConfig,
        *,
        validate: bool,
        prep_signals: TaskSignals | None = None,
    ) -> TaskSignals:
        return self._preparation_runner().run_with_preparation(
            config,
            validate=validate,
            prep_signals=prep_signals,
        )

    def _preparation_runner(self) -> _EncodePreparationRunner:
        return _EncodePreparationRunner(
            _EncodePreparationRunnerCallbacks(
                run_with_preparation=self._run_with_preparation,
                validate_config=self.validate,
                check_cancelled=self._check_cancelled,
                log_workflow_type=self._log_workflow_type,
                log_step=self._log_step,
                log=self.log_message.emit,
                prepare_attachment_config=self._prepare_attachment_config,
                prepare_process_work_dir=prepare_process_work_dir,
                relocate_tmdb_covers_to_process_dir=relocate_tmdb_covers_to_process_dir,
                download_tmdb_cover=download_tmdb_cover,
                is_multi_video=self._is_multi_video,
                normalize_dynamic_hdr_multi=self._normalize_dynamic_hdr_multi,
                normalize_dynamic_hdr_config=self._normalize_dynamic_hdr_config,
                is_video_passthrough=self._is_video_passthrough,
                wants_dynamic_hdr_copy=self._wants_dynamic_hdr_copy,
                needs_metadata_inject=self._needs_metadata_inject,
                ensure_inject_storage_available=self._ensure_inject_storage_available,
                build_encode_plan=self._build_encode_plan,
                bind_output_hooks=self._bind_output_hooks,
                run_multi_video_pipeline=self._run_multi_video_pipeline,
                run_with_metadata_inject=self._run_with_metadata_inject,
                run_direct_output=self._run_direct_output,
            )
        )

    def _run_direct_output(
        self,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        *,
        prep_signals: TaskSignals | None = None,
        plan: _EncodePlan | None = None,
    ) -> TaskSignals:
        backend = self._backend_for_config(config)
        if getattr(backend, "backend_id", "ffmpeg") != "ffmpeg":
            signals = backend.run(
                config,
                cleanup_paths,
                prep_signals=prep_signals,
                ctx=self._backend_context(plan=plan or self._build_encode_plan(config)),
            )
            if prep_signals is None:
                self._bind_matroska_segment_muxing_patch(signals, config.output)
                self._bind_nfo_write(signals, config.output)
            return signals

        return self._run_ffmpeg_direct_output(
            config,
            cleanup_paths,
            prep_signals=prep_signals,
            plan=plan,
        )

    def _run_ffmpeg_direct_output(
        self,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        *,
        prep_signals: TaskSignals | None = None,
        plan: _EncodePlan | None = None,
    ) -> TaskSignals:

        cwd = config.work_dir or config.source.parent
        plan = plan or self._build_encode_plan(config)
        signals = _DirectOutputRunner(
            _DirectOutputRunnerCallbacks(
                check_cancelled=self._check_cancelled,
                log_step=self._log_step,
                log_info=lambda message: self.log_message.emit("INFO", message),
                uses_two_pass=self._uses_two_pass,
                build_encode_plan=self._build_encode_plan,
                build_runtime_two_pass_with_sync=self._build_runtime_two_pass_with_sync,
                build_runtime_single_pass_with_sync=self._build_runtime_single_pass_with_sync,
                run_two_pass=self._run_two_pass,
                run_cmd=lambda cmd, cwd, label, progress_cb, signals: self._runner._run_cmd(
                    cmd,
                    signals=signals,
                    cwd=cwd,
                    label=label,
                    progress_cb=progress_cb,
                ),
                run_tool=lambda cmd, cwd, label: self._runner.run(cmd, cwd=cwd, label=label),
                bind_live_sync_cleanup=self._bind_live_sync_cleanup,
                cleanup_two_pass_logs=self._cleanup_two_pass_logs,
            )
        ).run(
            config=config,
            cleanup_paths=cleanup_paths,
            cwd=cwd,
            prep_signals=prep_signals,
            plan=plan,
        )
        if prep_signals is None:
            self._bind_matroska_segment_muxing_patch(signals, config.output)
            self._bind_nfo_write(signals, config.output)
        return signals

    def _run_direct_output_multisource_async(
        self,
        *,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        cwd: Path,
        prep_signals: TaskSignals | None = None,
        plan: _EncodePlan | None = None,
    ) -> TaskSignals:
        return _DirectOutputRunner(
            _DirectOutputRunnerCallbacks(
                check_cancelled=self._check_cancelled,
                log_step=self._log_step,
                log_info=lambda message: self.log_message.emit("INFO", message),
                uses_two_pass=self._uses_two_pass,
                build_encode_plan=self._build_encode_plan,
                build_runtime_two_pass_with_sync=self._build_runtime_two_pass_with_sync,
                build_runtime_single_pass_with_sync=self._build_runtime_single_pass_with_sync,
                run_two_pass=self._run_two_pass,
                run_cmd=lambda cmd, cwd, label, progress_cb, signals: self._runner._run_cmd(
                    cmd,
                    signals=signals,
                    cwd=cwd,
                    label=label,
                    progress_cb=progress_cb,
                ),
                run_tool=lambda cmd, cwd, label: self._runner.run(cmd, cwd=cwd, label=label),
                bind_live_sync_cleanup=self._bind_live_sync_cleanup,
                cleanup_two_pass_logs=self._cleanup_two_pass_logs,
            )
        ).run_multisource_async(
            config=config,
            cleanup_paths=cleanup_paths,
            cwd=cwd,
            prep_signals=prep_signals,
            plan=plan,
        )

    def _run_multi_video_pipeline(
        self,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        *,
        prep_signals: TaskSignals | None = None,
        plan: _EncodePlan | None = None,
    ) -> TaskSignals:
        return _MultiVideoPipelineRunner(
            _MultiVideoPipelineRunnerCallbacks(
                ffmpeg_bin=self._ffmpeg,
                bins=dict(self._bins),
                max_parallel_video_encodes=_normalize_max_parallel_video_encodes(self._max_parallel_video_encodes),
                check_cancelled=self._check_cancelled,
                build_encode_plan=self._build_encode_plan,
                video_tracks=self._video_tracks,
                video_source_from_settings=self._video_source_from_settings,
                video_stream_from_settings=self._video_stream_from_settings,
                track_offset_ms=lambda lookup, **kwargs: _track_offset_ms_plan(lookup, **kwargs),
                offset_input_args=self._offset_input_args,
                parallel_video_worker_thread_count=self._parallel_video_worker_thread_count,
                video_encode_resource_key=self._video_encode_resource_key,
                parallel_video_min_available_ram_bytes=self._parallel_video_min_available_ram_bytes,
                video_prep_estimated_ram_bytes=self._video_prep_estimated_ram_bytes,
                format_bytes=_format_bytes_runtime,
                available_ram_bytes=EncodeWorkflow._available_ram_bytes,
                source_input_index_map=lambda sources, start_index: _source_input_index_map_plan(
                    sources,
                    start_index=start_index,
                ),
                prepare_multisource_sync=self._prepare_multisource_sync,
                sync_cleanup_paths=_common_sync_cleanup_paths,
                append_sync_inputs=self._append_sync_inputs,
                prepare_container_metadata_inputs=self._prepare_container_metadata_inputs,
                ffmpeg_thread_args=self._ffmpeg_thread_args,
                append_offset_aux_inputs=self._append_offset_aux_inputs,
                build_offset_specs=lambda config, **kwargs: _build_offset_specs_plan(config, **kwargs),
                append_stream_maps_and_attachments=self._append_stream_maps_and_attachments,
                append_strict_interleave_mux_flags=self._append_strict_interleave_mux_flags,
                append_container_metadata_args=self._append_container_metadata_args,
                ffmpeg_progress_args=self._ffmpeg_progress_args,
                run_cmd=lambda cmd, cwd, label, progress_cb, signals: self._runner._run_cmd(
                    cmd,
                    cwd=cwd,
                    label=label,
                    progress_cb=progress_cb,
                    signals=signals,
                ),
                log_step=self._log_step,
                log_info=lambda message: self.log_message.emit("INFO", message),
                ui_encode_progress_message=_ui_encode_progress_message,
                build_video_only_two_pass_for_track=self._build_video_only_two_pass_for_track,
                cleanup_two_pass_logs_for_prefix=self._cleanup_two_pass_logs_for_prefix,
                build_video_only_cmd_for_track=self._build_video_only_cmd_for_track,
                wrap_injected_hevc_for_reconstruction=self._wrap_injected_hevc_for_reconstruction,
                build_multi_video_track_encode_commands=self._build_multi_video_track_encode_commands,
                two_pass_log_prefix=self._two_pass_log_prefix,
            )
        ).run(
            config,
            cleanup_paths,
            prep_signals=prep_signals,
            plan=plan,
        )

    def _bind_live_sync_cleanup(
        self,
        signals: TaskSignals,
        session: LiveSyncSession | None,
    ) -> None:
        self._signal_binding_service.bind_live_sync_cleanup(signals, session)

    def _bind_temp_cleanup(self, signals: TaskSignals, cleanup_paths: list[Path]) -> None:
        """Supprime les fichiers/dossiers temporaires quand le workflow se termine."""
        self._signal_binding_service.bind_temp_cleanup(signals, cleanup_paths)

    def _bind_matroska_segment_muxing_patch(self, signals: TaskSignals, output: Path) -> None:
        self._signal_binding_service.bind_matroska_segment_muxing_patch(signals, output)

    def _bind_nfo_write(self, signals: TaskSignals, output: Path) -> None:
        if not self._generate_nfo:
            return
        self._signal_binding_service.bind_nfo_write(signals, output)

    def _bind_output_hooks(
        self,
        signals: TaskSignals,
        *,
        output: Path,
        cleanup_paths: list[Path] | None = None,
        include_temp_cleanup: bool = True,
        include_segment_patch: bool = True,
        include_nfo: bool = True,
    ) -> None:
        if include_temp_cleanup and cleanup_paths is not None:
            self._bind_temp_cleanup(signals, cleanup_paths)
        if include_segment_patch:
            self._bind_matroska_segment_muxing_patch(signals, output)
        if include_nfo and self._generate_nfo:
            self._bind_nfo_write(signals, output)

    def _estimate_duration_seconds(self, config: EncodeConfig) -> float:
        return _estimate_duration_seconds_runtime(
            config,
            probe_duration_seconds=self._postprocess_service.probe_duration_seconds,
        )

    def _estimate_inject_video_bytes(
        self,
        config: EncodeConfig,
        *,
        duration_s: float,
        source_size: int,
    ) -> int:
        return _estimate_inject_video_bytes_runtime(
            config,
            duration_s=duration_s,
            source_size=source_size,
            size_to_bitrate_kbps=self._size_to_bitrate_kbps,
        )

    def _estimate_inject_storage_requirements(self, config: EncodeConfig) -> tuple[int, int]:
        return _estimate_inject_storage_requirements_runtime(
            config,
            probe_duration_seconds=self._postprocess_service.probe_duration_seconds,
            size_to_bitrate_kbps=self._size_to_bitrate_kbps,
        )

    def _ensure_inject_storage_available(self, config: EncodeConfig) -> None:
        _ensure_inject_storage_available_runtime(
            config,
            estimate_requirements=self._estimate_inject_storage_requirements,
            log_info=lambda message: self.log_message.emit("INFO", message),
            ram_buffer_enabled=self._ram_buffer_enabled,
            ram_buffer_dir=EncodeWorkflow._ram_buffer_dir,
            disk_usage=shutil.disk_usage,
            stat=os.stat,
            temp_dir=tempfile.gettempdir,
            format_bytes_fn=_format_bytes_runtime,
        )

    def _prepare_attachment_config(
        self,
        config: EncodeConfig,
        *,
        work_dir: Path,
        signals: TaskSignals | None = None,
    ) -> tuple[EncodeConfig, Path | None]:
        return _AttachmentPreparationService(
            _AttachmentPreparationServiceCallbacks(
                check_cancelled=self._check_cancelled,
                describe_attachment_stream=self._describe_attachment_stream,
                attachment_filename=_default_attachment_filename,
                unique_attachment_path=_unique_attachment_path_runtime,
                extract_attached_pic=lambda source, stream_idx, dest, task_signals: self._extract_attached_pic(
                    source,
                    stream_idx,
                    dest,
                    signals=task_signals,
                ),
            )
        ).prepare(
            config,
            work_dir=work_dir,
            signals=signals,
        )

    def _describe_attachment_stream(self, source: Path, stream_idx: int) -> dict[str, object]:
        return _probe_attachment_stream_runtime(
            source,
            stream_idx,
            ffprobe_bin=self._ffprobe_bin_from_ffmpeg(self._ffmpeg),
            subprocess_run=subprocess.run,
            text_kwargs_factory=subprocess_text_kwargs,
        )

    def _extract_attached_pic(
        self,
        source: Path,
        stream_idx: int,
        dest: Path,
        *,
        signals: TaskSignals | None = None,
    ) -> None:
        _extract_attached_pic_runtime(
            source,
            stream_idx,
            dest,
            ffmpeg_bin=self._ffmpeg,
            ffmpeg_thread_args=self._ffmpeg_thread_args,
            check_cancelled=self._check_cancelled,
            log_info=lambda message: self.log_message.emit("INFO", message),
            run_cmd=lambda cmd, label, task_signals: self._runner._run_cmd(
                cmd,
                label=label,
                signals=task_signals,
            ),
            signals=signals,
        )

    def _run_two_pass(
        self,
        cmds: list[list[str]],
        cwd: Path | None,
        signals: TaskSignals | None = None,
    ) -> TaskSignals:
        return _TwoPassRunner(
            _TwoPassRunnerCallbacks(
                log_info=lambda message: self.log_message.emit("INFO", message),
                run_cmd=self._runner._run_cmd,
                cleanup_two_pass_logs=self._cleanup_two_pass_logs,
            )
        ).run(cmds, cwd, signals)

    @staticmethod
    def _cleanup_two_pass_logs(cwd: Path | None) -> None:
        _TwoPassLogCleanupService.cleanup(cwd)

    @staticmethod
    def _cleanup_two_pass_logs_for_prefix(prefix: Path) -> None:
        _TwoPassLogCleanupService.cleanup_for_prefix(prefix)

    @staticmethod
    def _normalize_frame_rate_expr(value: object) -> str | None:
        return _normalize_frame_rate_expr_runtime(value)

    def _mediainfo_video_fps_expr(self, source: Path) -> str | None:
        return _mediainfo_video_fps_expr_runtime(
            source,
            load_mediainfo_video_track=self._load_mediainfo_video_track,
        )

    def _mediainfo_video_is_vfr(self, source: Path) -> bool | None:
        return _mediainfo_video_is_vfr_runtime(
            source,
            load_mediainfo_video_track=self._load_mediainfo_video_track,
        )

    def _source_video_fps_expr(self, source: Path) -> str:
        return _source_video_fps_expr_runtime(
            source,
            ffprobe_streams_payload=self._ffprobe_streams_payload,
            ffprobe_stream_dicts=self._ffprobe_stream_dicts,
            mediainfo_fps_expr=self._mediainfo_video_fps_expr,
        )

    @staticmethod
    def _nvencc_raw_input_needs_fps_hint(path: Path | str | None) -> bool:
        return _nvencc_raw_input_needs_fps_hint_runtime(path)

    def _nvencc_input_fps_hint(
        self,
        *,
        source_for_fps: Path,
        input_path: Path | str | None,
    ) -> str | None:
        if not self._nvencc_raw_input_needs_fps_hint(input_path):
            return None
        fps_expr = self._source_video_fps_expr(source_for_fps)
        return fps_expr or None

    def _nvencc_input_avsync_mode(
        self,
        *,
        source_for_timing: Path,
        input_path: Path | str | None,
    ) -> str | None:
        if input_path is None or self._nvencc_raw_input_needs_fps_hint(input_path):
            return None
        if self._source_is_vfr(source_for_timing):
            return "vfr"
        return None

    @staticmethod
    def _nvencc_can_use_native_timestamps(path: Path | str | None) -> bool:
        return _nvencc_can_use_native_timestamps_runtime(path)

    @staticmethod
    def _nvencc_crop_offsets_from_extra_params(extra_params: str) -> tuple[int, int, int, int] | None:
        return _nvencc_crop_offsets_from_extra_params_runtime(extra_params)

    def _nvencc_dovi_rpu_prm(self, video: VideoEncodeSettings) -> str | None:
        return _nvencc_dovi_rpu_prm_runtime(video)

    def _resolve_nvencc_input_routing(self, config: EncodeConfig) -> _NvenccInputRouting:
        return _NvenccInputRouter(self._nvencc_routing_callbacks()).resolve(config)

    def _nvencc_routing_callbacks(self) -> _NvenccRoutingCallbacks:
        return _NvenccRoutingCallbacks(
            primary_video_settings=self._primary_video_settings,
            video_source_path=self._video_source_path,
            video_stream_index=self._video_stream_index,
            video_codec_of=self._video_codec_of,
            source_video_fps_expr=self._source_video_fps_expr,
            source_is_vfr=self._source_is_vfr,
            nvencc_input_fps_hint=lambda source_for_fps, input_path: self._nvencc_input_fps_hint(
                source_for_fps=source_for_fps,
                input_path=input_path,
            ),
            nvencc_input_avsync_mode=lambda source_for_timing, input_path: self._nvencc_input_avsync_mode(
                source_for_timing=source_for_timing,
                input_path=input_path,
            ),
            nvencc_dovi_rpu_prm=self._nvencc_dovi_rpu_prm,
        )

    def _source_video_dimensions(self, source: Path) -> tuple[int, int]:
        return _source_video_dimensions_runtime(
            source,
            ffprobe_streams_payload=self._ffprobe_streams_payload,
            ffprobe_stream_dicts=self._ffprobe_stream_dicts,
        )

    def _source_is_vfr(self, source: Path, *, tolerance: float = 0.01) -> bool:
        return _source_is_vfr_runtime(
            source,
            ffprobe_streams_payload=self._ffprobe_streams_payload,
            ffprobe_stream_dicts=self._ffprobe_stream_dicts,
            mediainfo_is_vfr=self._mediainfo_video_is_vfr,
            tolerance=tolerance,
        )

    @staticmethod
    def _fps_expr_to_float(value: object) -> float | None:
        return _fps_expr_to_float_runtime(value)

    def _wrap_injected_hevc_for_reconstruction(
        self,
        *,
        source: Path,
        hevc_input: Path,
        mkv_output: Path,
    ) -> list[str]:
        return _build_injected_hevc_wrap_command_runtime(
            ffmpeg_bin=self._ffmpeg,
            source=source,
            hevc_input=hevc_input,
            mkv_output=mkv_output,
            source_video_fps_expr=self._source_video_fps_expr,
            ffmpeg_progress_args=self._ffmpeg_progress_args,
            ffmpeg_thread_args=self._ffmpeg_thread_args,
        )

    def _resolve_global_tags(self, config: EncodeConfig) -> dict[str, str]:
        tags = _common_resolve_global_tags(config.tag_overrides, "")
        tags["title"] = config.file_title
        return tags

    def _build_multi_video_final_mux_command(
        self,
        config: EncodeConfig,
        prepared_video_inputs: list[dict[str, object]],
        *,
        chapter_materialize_dir: Path | None = None,
        plan: _EncodePlan | None = None,
    ) -> list[str]:
        return _EncodeFinalMuxBuilder(
            _EncodeFinalMuxBuilderCallbacks(
                ffmpeg_bin=self._ffmpeg,
                ffmpeg_progress_args=self._ffmpeg_progress_args,
                ffmpeg_thread_args=self._ffmpeg_thread_args,
                video_tracks=self._video_tracks,
                video_source_from_settings=self._video_source_from_settings,
                video_stream_from_settings=self._video_stream_from_settings,
                build_encode_plan=self._build_encode_plan,
                prepare_container_metadata_inputs=self._prepare_container_metadata_inputs,
                append_stream_maps_and_attachments=self._append_stream_maps_and_attachments,
                append_container_metadata_args=self._append_container_metadata_args,
            )
        ).build(
            config,
            prepared_video_inputs,
            chapter_materialize_dir=chapter_materialize_dir,
            plan=plan,
        )

    @staticmethod
    def _track_spec_for_track_order(track_order: int, video_count: int, audio_count: int) -> tuple[str, int] | None:
        return _track_spec_for_track_order_runtime(track_order, video_count, audio_count)

    @staticmethod
    def _normalized_track_language_value(language: str, title: str | None = None) -> str | None:
        return _normalized_track_language_value_runtime(language, title)

    @staticmethod
    def _disposition_value_from_edit(edit) -> str | None:
        return _disposition_value_from_edit_runtime(edit)

    def _build_track_meta_args(self, config: EncodeConfig) -> list[str]:
        return _TrackMetadataArgsBuilder(
            _TrackMetadataArgsBuilderCallbacks(
                video_tracks=self._video_tracks,
                log_warn=lambda message: self.log_message.emit("WARN", message),
            )
        ).build(config)

    def _run_with_metadata_inject(
        self,
        config: EncodeConfig,
        *,
        prep_signals: TaskSignals | None = None,
        plan: _EncodePlan | None = None,
    ) -> TaskSignals:
        return _MetadataInjectRunner(
            _MetadataInjectRunnerCallbacks(
                ffmpeg_bin=self._ffmpeg,
                bins=dict(self._bins),
                log_step=self._log_step,
                log_info=lambda message: self.log_message.emit("INFO", message),
                check_cancelled=self._check_cancelled,
                video_source_path=self._video_source_path,
                build_video_only_two_pass=self._build_video_only_two_pass,
                build_video_only_cmd=self._build_video_only_cmd,
                wrap_injected_hevc_for_reconstruction=self._wrap_injected_hevc_for_reconstruction,
                source_is_vfr=self._source_is_vfr,
                source_video_dimensions=self._source_video_dimensions,
                build_encode_plan=self._build_encode_plan,
                source_input_index_map=lambda sources: _source_input_index_map_plan(sources, start_index=1),
                prepare_multisource_sync=self._prepare_multisource_sync,
                sync_cleanup_paths=_common_sync_cleanup_paths,
                append_sync_inputs=self._append_sync_inputs,
                prepare_container_metadata_inputs=self._prepare_container_metadata_inputs,
                ffmpeg_thread_args=self._ffmpeg_thread_args,
                ffmpeg_progress_args=self._ffmpeg_progress_args,
                video_map_key=self._video_map_key,
                append_offset_aux_inputs=self._append_offset_aux_inputs,
                build_offset_specs=lambda config, **kwargs: _build_offset_specs_plan(config, **kwargs),
                video_map_arg=_video_map_arg_plan,
                append_stream_maps_and_attachments=self._append_stream_maps_and_attachments,
                append_strict_interleave_mux_flags=self._append_strict_interleave_mux_flags,
                append_container_metadata_args=self._append_container_metadata_args,
                run_cmd=lambda cmd, signals, cwd, progress_cb: self._runner._run_cmd(
                    cmd,
                    signals=signals,
                    cwd=cwd,
                    progress_cb=progress_cb,
                ),
                bind_matroska_segment_muxing_patch=self._bind_matroska_segment_muxing_patch,
                bind_nfo_write=self._bind_nfo_write,
            )
        ).run(
            config,
            prep_signals=prep_signals,
            plan=plan,
        )
