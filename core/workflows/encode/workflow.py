"""
core/workflows/encode/workflow.py — FFmpeg encode workflow with optional HDR metadata injection.

Public:
    EncodeWorkflow
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Callable, cast

from PySide6.QtCore import QObject, Signal
from core.runner import TaskCancelledError, TaskSignals, ToolRunner
from core.subprocess_utils import subprocess_text_kwargs
from core.subtitle_codec import plan_subtitle_codec
from core.version import APP_VERSION_LABEL
from core.workdir import (
    download_tmdb_cover,
    prepare_process_work_dir,
    relocate_tmdb_covers_to_process_dir,
    remove_path,
)
from core.workflows.remux_models import SourceInput
from core.workflows.remux import write_mediainfo_nfo
from core.workflows.common.attachments import (
    extension_for_mime,
    mime_for_path,
)
from core.workflows.common.remux_postprocess import RemuxPostprocessService
from core.workflows.common.ffmpeg_runtime import (
    default_ffmpeg_thread_count as _default_ffmpeg_thread_count,
    ffmpeg_progress_args as _common_ffmpeg_progress_args,
    ffmpeg_thread_args as _common_ffmpeg_thread_args,
    normalize_ffmpeg_thread_count as _normalize_ffmpeg_thread_count,
    normalize_max_parallel_video_encodes as _normalize_max_parallel_video_encodes,
)
from core.workflows.common.metadata import (
    disposition_value as _common_disposition_value,
    normalize_track_language as _common_normalize_track_language,
    resolve_global_tags as _common_resolve_global_tags,
)
from core.workflows.common.timeline_sync import (
    append_strict_interleave_mux_flags as _common_append_strict_interleave_mux_flags,
    append_sync_inputs as _common_append_sync_inputs,
    sync_cleanup_paths as _common_sync_cleanup_paths,
)
from core.workflows.encode.catalog import (
    AMF_VIDEO_CODECS as _AMF_CODECS,
    H264_VIDEO_CODECS,
    NVENC_VIDEO_CODECS as _NVENC_CODECS,
    QSV_VIDEO_CODECS as _QSV_CODECS,
    VAAPI_VIDEO_CODECS as _VAAPI_CODECS,
    is_h264_video_codec,
)
from core.workflows.encode.domain import (
    EncodeCodecDomainCallbacks as _EncodeCodecDomainCallbacks,
    audio_codec_args as _audio_codec_args_domain,
    build_encoder_vf as _build_encoder_vf_domain,
    hardware_input_args as _hardware_input_args_domain,
    hdr_meta_args as _hdr_meta_args_domain,
    video_codec_args as _video_codec_args_domain,
    video_codec_args_bitrate as _video_codec_args_bitrate_domain,
)
from core.workflows.remux_timeline_sync import (
    LiveSyncSession,
    FfmpegTimelineSync,
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
    EncodeConfig, EncodeError, QualityMode,
    VideoEncodeSettings,
    normalize_audio_bitrate_kbps,
)
from core.workflows.encode.planning.plan_models import (
    EncodePlan as _EncodePlan,
    MaterializedContainerMetadataPlan as _MaterializedContainerMetadataPlan,
    ResolvedTrackAssembly as _ResolvedTrackAssembly,
)
_FALLBACK_HEVC_FRAME_RATE = "24000/1001"


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
    ) -> None:
        super().__init__(parent)
        self._ffmpeg = ffmpeg_bin
        self._bins: dict[str, str] = {
            "dovi_tool":      dovi_tool_bin,
            "hdr10plus_tool": hdr10plus_bin,
            "mediainfo":      mediainfo_bin,
        }
        # Cache mémoire : évite de ré-exécuter ffprobe/mediainfo à chaque
        # reconstruction d'aperçu (preview_command peut être appelé des dizaines
        # de fois pour le même fichier lors de changements UI).
        # Clé : (abs_path, mtime_ns, size). Invalide automatiquement si le
        # fichier a été modifié.
        self._ffprobe_payload_cache: dict[tuple[str, int, int], dict[str, object] | None] = {}
        self._ffprobe_frame_hdr_cache: dict[tuple[str, int, int], tuple[bool, bool] | None] = {}
        self._mediainfo_hdr_cache: dict[tuple[str, int, int], tuple[bool, bool] | None] = {}
        self._generate_nfo = generate_nfo
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

    def set_generate_nfo(self, generate_nfo: bool) -> None:
        self._generate_nfo = generate_nfo

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

    @classmethod
    def _needs_metadata_inject(cls, config: EncodeConfig) -> bool:
        return cls._wants_dynamic_hdr_copy(config) and not cls._is_video_passthrough(config)

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
        """
        Retourne (has_dv, has_hdr10plus) pour la source vidéo principale.

        None = détection impossible (ffprobe + mediainfo indisponibles), auquel
        cas on conserve le comportement demandé par l'utilisateur sans
        optimisation.
        """
        payload = self._ffprobe_streams_payload(source)
        has_dv = False
        has_hdr10plus = False
        frame_flags: tuple[bool, bool] | None = None
        if payload is not None:
            for stream in self._ffprobe_stream_dicts(payload):
                if stream.get("codec_type") != "video":
                    continue
                side_data_obj = stream.get("side_data_list")
                side_data: list[dict[str, object]] = []
                if isinstance(side_data_obj, list):
                    for item in side_data_obj:
                        if isinstance(item, dict):
                            side_data.append(cast(dict[str, object], item))
                if any(sd.get("side_data_type") == "DOVI configuration record" for sd in side_data):
                    has_dv = True
                if any(
                    sd.get("side_data_type") == "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"
                    for sd in side_data
                ):
                    has_hdr10plus = True
                if has_dv and has_hdr10plus:
                    break

        mediainfo_flags = self._mediainfo_hdr_flags(source)
        if mediainfo_flags is not None:
            mi_dv, mi_hdr10plus = mediainfo_flags
            has_dv = has_dv or mi_dv
            has_hdr10plus = has_hdr10plus or mi_hdr10plus

        if not has_dv or not has_hdr10plus:
            frame_flags = self._ffprobe_frame_dynamic_hdr_flags(source)
            if frame_flags is not None:
                frame_dv, frame_hdr10plus = frame_flags
                has_dv = has_dv or frame_dv
                has_hdr10plus = has_hdr10plus or frame_hdr10plus

        if payload is None and mediainfo_flags is None and frame_flags is None:
            return None
        return has_dv, has_hdr10plus

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
        cache_key = self._source_cache_key(source)
        if cache_key is not None and cache_key in self._ffprobe_payload_cache:
            return self._ffprobe_payload_cache[cache_key]

        ffprobe_bin = self._ffprobe_bin_from_ffmpeg(self._ffmpeg)
        cmd = [
            ffprobe_bin,
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(source),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=20,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            payload = None
        else:
            if result.returncode != 0:
                payload = None
            else:
                try:
                    payload = json.loads(result.stdout or "{}")
                except json.JSONDecodeError:
                    payload = None

        if cache_key is not None:
            self._ffprobe_payload_cache[cache_key] = payload
        return payload

    @staticmethod
    def _source_cache_key(source: Path) -> tuple[str, int, int] | None:
        """Clé de cache fondée sur (chemin absolu, mtime_ns, taille).

        Retourne None si le fichier n'existe pas encore (pas de cache possible).
        """
        try:
            st = source.stat()
        except OSError:
            return None
        return (str(source), st.st_mtime_ns, st.st_size)

    @staticmethod
    def _ffprobe_stream_dicts(payload: dict[str, object]) -> list[dict[str, object]]:
        streams_obj = payload.get("streams")
        if not isinstance(streams_obj, list):
            return []
        out: list[dict[str, object]] = []
        for item in streams_obj:
            if isinstance(item, dict):
                out.append(cast(dict[str, object], item))
        return out

    def _ffprobe_frame_dynamic_hdr_flags(
        self,
        source: Path,
        *,
        max_frames: int = 240,
    ) -> tuple[bool, bool] | None:
        cache_key = self._source_cache_key(source)
        if cache_key is not None and cache_key in self._ffprobe_frame_hdr_cache:
            return self._ffprobe_frame_hdr_cache[cache_key]

        ffprobe_bin = self._ffprobe_bin_from_ffmpeg(self._ffmpeg)
        cmd = [
            ffprobe_bin,
            "-v", "quiet",
            "-print_format", "json",
            "-select_streams", "v:0",
            "-read_intervals", f"%+#{max(1, int(max_frames))}",
            "-show_frames",
            "-show_entries", "frame_side_data=side_data_type",
            str(source),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=30,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            flags: tuple[bool, bool] | None = None
        else:
            if result.returncode != 0:
                flags = None
            else:
                try:
                    payload = json.loads(result.stdout or "{}")
                except json.JSONDecodeError:
                    flags = None
                else:
                    frames_obj = payload.get("frames")
                    has_dv = False
                    has_hdr10plus = False
                    if isinstance(frames_obj, list):
                        for frame in frames_obj:
                            if not isinstance(frame, dict):
                                continue
                            side_data_obj = frame.get("side_data_list")
                            if not isinstance(side_data_obj, list):
                                continue
                            for side_data in side_data_obj:
                                if not isinstance(side_data, dict):
                                    continue
                                side_type = str(side_data.get("side_data_type", "") or "")
                                side_type_lower = side_type.lower()
                                if ("dolby vision" in side_type_lower) or (side_type == "DOVI configuration record"):
                                    has_dv = True
                                if (
                                    "hdr dynamic metadata smpte2094-40" in side_type_lower
                                    or "hdr10+" in side_type_lower
                                    or "smpte st 2094" in side_type_lower
                                    or "smpte2094" in side_type_lower
                                ):
                                    has_hdr10plus = True
                                if has_dv and has_hdr10plus:
                                    break
                            if has_dv and has_hdr10plus:
                                break
                    flags = (has_dv, has_hdr10plus)

        if cache_key is not None:
            self._ffprobe_frame_hdr_cache[cache_key] = flags
        return flags

    def _mediainfo_hdr_flags(self, source: Path) -> tuple[bool, bool] | None:
        cache_key = self._source_cache_key(source)
        if cache_key is not None and cache_key in self._mediainfo_hdr_cache:
            return self._mediainfo_hdr_cache[cache_key]

        mediainfo_bin = self._bins.get("mediainfo") or "mediainfo"
        try:
            hdr_format = subprocess.run(
                [mediainfo_bin, "--Inform=Video;%HDR_Format%", str(source)],
                capture_output=True,
                check=False,
                timeout=20,
                **subprocess_text_kwargs(),
            )
            hdr_compat = subprocess.run(
                [mediainfo_bin, "--Inform=Video;%HDR_Format_Compatibility%", str(source)],
                capture_output=True,
                check=False,
                timeout=20,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            result: tuple[bool, bool] | None = None
        else:
            hdr_text = f"{hdr_format.stdout or ''}\n{hdr_compat.stdout or ''}".lower()
            result = (
                "dolby vision" in hdr_text,
                (
                    "hdr10+" in hdr_text
                    or "smpte st 2094" in hdr_text
                    or "smpte2094" in hdr_text
                ),
            )

        if cache_key is not None:
            self._mediainfo_hdr_cache[cache_key] = result
        return result

    def _normalize_dynamic_hdr_config(self, config: EncodeConfig) -> EncodeConfig:
        """
        Nettoie les demandes de copie DoVi/HDR10+ avant le routage principal.

        - Si rien n'est demandé : retourne la config telle quelle.
        - Si la source ne contient pas un format demandé : désactive uniquement ce format.
        - Si la détection échoue : conserve la demande telle quelle.
        """
        if not self._wants_dynamic_hdr_copy(config):
            return config

        detected = self._detect_source_dynamic_hdr_presence(self._video_source_path(config))
        if detected is None:
            self.log_message.emit(
                "WARN",
                "Détection DoVi/HDR10+ impossible sur la source — workflow demandé conservé.",
            )
            return config

        video = self._primary_video_settings(config)
        has_dv, has_hdr10plus = detected
        copy_dv = video.copy_dv and has_dv
        copy_hdr10plus = video.copy_hdr10plus and has_hdr10plus

        if video.copy_dv and not copy_dv:
            self.log_message.emit(
                "WARN",
                "Copy DoVi demandé mais aucune donnée DoVi détectée — option ignorée.",
            )
        if video.copy_hdr10plus and not copy_hdr10plus:
            self.log_message.emit(
                "WARN",
                "Copy HDR10+ demandé mais aucune donnée HDR10+ détectée — option ignorée.",
            )

        normalized_video = replace(
            video,
            copy_dv=copy_dv,
            copy_hdr10plus=copy_hdr10plus,
        )
        normalized_tracks = list(config.video_tracks)
        if normalized_tracks:
            normalized_tracks = [normalized_video, *normalized_tracks[1:]]
        else:
            normalized_tracks = [normalized_video]
        normalized = replace(
            config,
            video=normalized_video,
            video_tracks=normalized_tracks,
            copy_dv=copy_dv,
            copy_hdr10plus=copy_hdr10plus,
            dovi_profile=normalized_video.dovi_profile,
        )

        if not self._wants_dynamic_hdr_copy(normalized) and self._is_video_passthrough(config):
            self.log_message.emit(
                "INFO",
                "Aucun DoVi/HDR10+ utile à recopier — passthrough vidéo direct.",
            )
        return normalized

    def _normalize_dynamic_hdr_multi(self, config: EncodeConfig) -> EncodeConfig:
        videos: list[VideoEncodeSettings] = []
        for index, video in enumerate(self._video_tracks(config), start=1):
            if not (video.copy_dv or video.copy_hdr10plus):
                videos.append(video)
                continue
            detected = self._detect_source_dynamic_hdr_presence(
                self._video_source_from_settings(config, video)
            )
            if detected is None:
                self.log_message.emit(
                    "WARN",
                    f"Détection DoVi/HDR10+ impossible pour la piste vidéo #{index} — demande conservée.",
                )
                videos.append(video)
                continue
            has_dv, has_hdr10plus = detected
            copy_dv = video.copy_dv and has_dv
            copy_hdr10plus = video.copy_hdr10plus and has_hdr10plus
            if video.copy_dv and not copy_dv:
                self.log_message.emit(
                    "WARN",
                    f"Copy DoVi demandé mais aucune donnée DoVi détectée pour la piste vidéo #{index} — option ignorée.",
                )
            if video.copy_hdr10plus and not copy_hdr10plus:
                self.log_message.emit(
                    "WARN",
                    f"Copy HDR10+ demandé mais aucune donnée HDR10+ détectée pour la piste vidéo #{index} — option ignorée.",
                )
            videos.append(replace(video, copy_dv=copy_dv, copy_hdr10plus=copy_hdr10plus))

        primary = videos[0]
        return replace(
            config,
            video=primary,
            video_tracks=videos,
            copy_dv=primary.copy_dv,
            copy_hdr10plus=primary.copy_hdr10plus,
            dovi_profile=primary.dovi_profile,
        )

    # ------------------------------------------------------------------
    # Construction de la commande
    # ------------------------------------------------------------------

    def build_command(self, config: EncodeConfig) -> list[str] | list[list[str]]:
        """
        Retourne une commande (list[str]) ou deux commandes pour la double passe (list[list[str]]).
        """
        selection = _build_encode_command_selection_plan(
            config,
            plan=self._build_encode_plan(config),
            is_multi_video=self._is_multi_video,
            uses_two_pass=self._uses_two_pass,
            build_multi_video_preview=self._build_multi_video_command_preview,
            build_two_pass=self._build_two_pass,
            build_single_pass=self._build_single_pass,
        )
        if len(selection.commands) <= 1:
            return list(selection.preview_command)
        return [list(cmd) for cmd in selection.commands]

    def build_command_single(self, config: EncodeConfig) -> list[str]:
        """Toujours une seule commande — pour l'aperçu UI."""
        return list(
            _build_encode_command_selection_plan(
                config,
                plan=self._build_encode_plan(config),
                is_multi_video=self._is_multi_video,
                uses_two_pass=self._uses_two_pass,
                build_multi_video_preview=self._build_multi_video_command_preview,
                build_two_pass=self._build_two_pass,
                build_single_pass=self._build_single_pass,
            ).preview_command
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
        next_input_index = int(start_input_index)
        input_by_key: dict[tuple[str, int, int, str], int] = {}
        remap: dict[tuple[Path, int, str], tuple[int, int]] = {}

        for spec in specs:
            input_key = (
                str(spec.input_path),
                int(spec.input_stream_index),
                int(spec.offset_ms),
                str(spec.map_key[2]),
            )
            input_idx = input_by_key.get(input_key)
            if input_idx is None:
                if int(spec.offset_ms) > 0:
                    cmd.extend(["-itsoffset", _offset_seconds_plan(spec.offset_ms), "-i", str(spec.input_path)])
                else:
                    cmd.extend(["-ss", _offset_seconds_plan(spec.offset_ms), "-i", str(spec.input_path)])
                input_idx = next_input_index
                input_by_key[input_key] = input_idx
                next_input_index += 1

            remap[spec.map_key] = (int(input_idx), int(spec.input_stream_index))

        return next_input_index, remap

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
        """
        Prépare la normalisation timeline pour les flux multi-source
        dans le workflow encode.
        """
        encode_plan = plan
        source_idx_local = {p: i for i, p in enumerate(all_sources)}
        if len(source_idx_local) < 2:
            return {}, [], None, False
        self.log_message.emit(
            "INFO",
            "Analyse sync timeline multi-source : pré-scan/remap en cours…",
        )
        if encode_plan is None:
            encode_plan = self._build_encode_plan(config)
        sync_analysis = encode_plan.sync_analysis
        if not sync_analysis.enabled:
            return {}, [], None, False

        strict_interleave = False
        if sync_analysis.offset_requires_file_fallback:
            strict_interleave = True
            self.log_message.emit(
                "INFO",
                "Décalage sur piste étrangère détecté : sync timeline activé.",
            )
        elif sync_analysis.needs_subtitle_prescan and sync_analysis.probe_remux_config is not None:
            self.log_message.emit(
                "INFO",
                "Pré-scan ffprobe des sous-titres (décision interleave strict)…",
            )
            strict_interleave = self._postprocess_service.decide_strict_interleave_with_prescan(
                sync_analysis.probe_remux_config,
                log_cb=self.log_message.emit,
            )
        elif sync_analysis.strict_interleave_without_prescan:
            strict_interleave = True
            self.log_message.emit(
                "WARNING",
                "Pré-scan sous-titres indisponible en mode copy_subtitles ; "
                "activation sync timeline par sécurité.",
            )

        if not strict_interleave:
            return {}, [], None, False

        allow_live_sync = bool(allow_live)
        if allow_live_sync and not sync_analysis.allow_live_sync:
            allow_live_sync = False
            self.log_message.emit(
                "INFO",
                "Décalage sur piste étrangère détecté : sync live désactivé, fallback fichier forcé.",
            )

        sync_sources = [SourceInput(path=p, file_index=i, tracks=[]) for i, p in enumerate(all_sources)]
        syncer = FfmpegTimelineSync(
            ffmpeg_bin=self._ffmpeg,
            ffmpeg_thread_args=self._ffmpeg_thread_args(),
            log_cb=lambda msg: self.log_message.emit("INFO", msg),
        )

        cancel_cb = signals._cancel_event.is_set if signals is not None else None
        ram_dir: Path | None = None
        if self._ram_buffer_enabled:
            ram_dir = EncodeWorkflow._ram_buffer_dir()

        prepared_result = TimelineSyncFallbackHelper(
            syncer=syncer,
            work_dir=work_dir,
            ram_dir=ram_dir,
            log_cb=lambda msg: self.log_message.emit("INFO", msg),
        ).prepare(
            mapped_tracks=list(sync_analysis.mapped_tracks),
            sources=sync_sources,
            base_input_idx=sync_base_input_idx,
            allow_live=allow_live_sync,
            cancel_cb=cancel_cb,
        )
        prepared = prepared_result.prepared_inputs
        live_session = prepared_result.live_session

        sync_inputs: list[Path | str] = [item.path for item in prepared]
        remap: dict[tuple[Path, int, str], tuple[int, int]] = {}
        path_by_source_idx = {idx: path for path, idx in source_idx_local.items()}
        for item in prepared:
            src_file_idx, src_stream_idx, track_type = item.key
            src_path = path_by_source_idx.get(src_file_idx)
            if src_path is None:
                continue
            remap[(src_path, int(src_stream_idx), track_type)] = (int(item.input_idx), 0)

        return remap, sync_inputs, live_session, True

    @staticmethod
    def _append_strict_interleave_mux_flags(cmd: list[str]) -> None:
        _common_append_strict_interleave_mux_flags(cmd)

    @staticmethod
    def _append_sync_inputs(cmd: list[str], sync_inputs: list[Path | str]) -> None:
        _common_append_sync_inputs(cmd, sync_inputs)

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
        """Ajoute mapping audio/sous-titres/attachments et pièces jointes externes."""
        sync_remap = sync_remap or {}
        offset_remap = offset_remap or {}
        for i, a in enumerate(config.audio_tracks):
            src_path = a.source_path or config.source
            key = (Path(src_path), int(a.stream_index), "audio")
            remapped = offset_remap.get(key)
            if remapped is None:
                remapped = sync_remap.get((src_path, int(a.stream_index), "audio"))
            if remapped is not None:
                mapped_audio_inp_idx, stream_idx = remapped
            else:
                source_audio_inp_idx = source_idx.get(Path(src_path))
                if source_audio_inp_idx is None:
                    source_audio_inp_idx = source_idx.get(config.source)
                mapped_audio_inp_idx = source_audio_inp_idx if source_audio_inp_idx is not None else 0
                stream_idx = int(a.stream_index)
            cmd.extend(["-map", f"{mapped_audio_inp_idx}:{stream_idx}"])
            cmd.extend(_audio_codec_args_domain(i, a))

        subtitle_tracks = (
            subtitle_tracks_override
            if subtitle_tracks_override is not None
            else config.subtitle_tracks
        )
        if subtitle_tracks:
            for src_path, stream_idx in subtitle_tracks:
                key = (Path(src_path), int(stream_idx), "subtitle")
                remapped = offset_remap.get(key)
                if remapped is None:
                    remapped = sync_remap.get((src_path, int(stream_idx), "subtitle"))
                if remapped is not None:
                    mapped_subtitle_inp_idx, mapped_stream_idx = remapped
                    cmd.extend(["-map", f"{mapped_subtitle_inp_idx}:{mapped_stream_idx}"])
                    continue
                source_subtitle_inp_idx = source_idx.get(Path(src_path))
                if source_subtitle_inp_idx is None:
                    continue
                cmd.extend(["-map", f"{source_subtitle_inp_idx}:{stream_idx}"])
            # Routage par piste : copy quand MKV l'accepte, sinon conversion srt.
            cmd.extend(self._subtitle_codec_args([
                (t[0], int(t[1])) for t in subtitle_tracks
            ]))
        elif config.copy_subtitles and force_copy_subtitles_wildcard:
            for inp_i in subtitle_copy_input_indices:
                cmd.extend(["-map", f"{inp_i}:s?"])
            # Wildcard : on ne connaît pas les codecs à l'avance, copy par
            # défaut ; ffmpeg échouera sur mov_text / eia_608. Pour les cas
            # connus d'échec, l'utilisateur doit sélectionner explicitement
            # les pistes via subtitle_tracks.
            cmd.extend(["-c:s", "copy"])

        mapped_attachment_meta: list[tuple[int, dict[str, object]]] = []
        if config.attachment_streams:
            for src_path, stream_idx in config.attachment_streams:
                attachment_inp_idx = source_idx.get(Path(src_path))
                if attachment_inp_idx is None:
                    continue
                cmd.extend(["-map", f"{attachment_inp_idx}:{stream_idx}"])
                mapped_attachment_meta.append(
                    (stream_idx, self._describe_attachment_stream(src_path, stream_idx))
                )
            if mapped_attachment_meta:
                cmd.extend(["-c:t", "copy"])
                for out_idx, (stream_idx, meta) in enumerate(mapped_attachment_meta):
                    cmd.extend([
                        f"-metadata:s:t:{out_idx}",
                        f"mimetype={str(meta.get('mimetype') or 'application/octet-stream').strip() or 'application/octet-stream'}",
                    ])
                    cmd.extend([
                        f"-metadata:s:t:{out_idx}",
                        f"filename={_default_attachment_filename(meta, stream_idx)}",
                    ])

        existing_att = len(mapped_attachment_meta)
        for i, att_path in enumerate(config.extra_attachments):
            att_idx = existing_att + i
            att_name = "cover" if att_path.stem.lower() == "cover" else att_path.name
            cmd.extend(["-attach", str(att_path)])
            cmd.extend([f"-metadata:s:t:{att_idx}", f"mimetype={mime_for_path(att_path)}"])
            cmd.extend([f"-metadata:s:t:{att_idx}", f"filename={att_name}"])

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
        _next_input_index, offset_remap = self._append_offset_aux_inputs(
            cmd,
            _build_offset_specs_plan(
                config,
                track_mappings=list(track_assembly.track_mappings),
                offset_lookup=dict(plan.offset_lookup),
            ),
            start_input_index=start_input_index,
        )
        _ = _next_input_index
        return track_assembly, offset_remap

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

    def _video_resource_policy(self) -> _VideoPreparationResourcePolicy:
        effective_threads = self._ffmpeg_threads if self._ffmpeg_threads > 0 else _default_ffmpeg_thread_count()
        return _VideoPreparationResourcePolicy(
            vaapi_device=self._vaapi_device(),
            ffmpeg_threads=effective_threads,
        )

    def _video_encode_resource_key(self, video: VideoEncodeSettings) -> str:
        return self._video_resource_policy().resource_key(video)

    def _parallel_video_min_available_ram_bytes(self) -> int:
        total_ram = EncodeWorkflow._total_ram_bytes()
        if total_ram <= 0 or self._ram_buffer_threshold_pct <= 0:
            return 0
        return int(total_ram * self._ram_buffer_threshold_pct / 100)

    def _video_prep_estimated_ram_bytes(self, spec: _VideoTrackPrepSpec) -> int:
        source_size = 0
        try:
            if spec.source.exists():
                source_size = max(0, spec.source.stat().st_size)
        except OSError:
            source_size = 0
        return self._video_resource_policy().estimated_ram_bytes(
            spec.video,
            source_size=source_size,
        )

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
        safe_token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(token)).strip("._")
        if not safe_token:
            safe_token = "video"
        return work_dir / f"ffmpeg2pass-{safe_token}"

    def _build_video_track_base_cmd(
        self,
        *,
        video: VideoEncodeSettings,
        source: Path,
        stream_index: int,
        offset_ms: int = 0,
        thread_count: int | None = None,
    ) -> list[str]:
        cmd = [self._ffmpeg, "-hide_banner", "-y"]
        cmd.extend(self._ffmpeg_progress_args())
        cmd.extend(self._offset_input_args(offset_ms))
        cmd.extend(_hardware_input_args_domain(video, callbacks=self._codec_domain_callbacks()))
        cmd.extend(["-i", str(source)])
        vf = _build_encoder_vf_domain(video, callbacks=self._codec_domain_callbacks())
        if vf:
            cmd.extend(["-vf", vf])
        cmd.extend(self._ffmpeg_thread_args(thread_count))
        cmd.extend(["-map", f"0:{int(stream_index)}"])
        return cmd

    def _append_video_codec_and_hdr_args(
        self,
        cmd: list[str],
        video: VideoEncodeSettings,
        *,
        bitrate_kbps: int | None = None,
        include_hdr_meta: bool = True,
    ) -> None:
        callbacks = self._codec_domain_callbacks()
        if bitrate_kbps is None:
            cmd.extend(_video_codec_args_domain(video, video.bitrate_kbps, callbacks=callbacks))
        else:
            cmd.extend(_video_codec_args_bitrate_domain(video, bitrate_kbps, callbacks=callbacks))
        if include_hdr_meta and video.inject_hdr_meta and not video.tonemap_to_sdr:
            cmd.extend(_hdr_meta_args_domain(video))

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
        _ = for_preview
        stream_index = self._video_stream_from_settings(video)

        if video.quality_mode == QualityMode.SIZE:
            bitrate = self._size_to_bitrate_kbps_for_video(config, video)
            pass1 = self._build_video_track_base_cmd(
                video=video,
                source=source,
                stream_index=stream_index,
                offset_ms=offset_ms,
                thread_count=thread_count,
            )
            self._append_video_codec_and_hdr_args(pass1, video, bitrate_kbps=bitrate, include_hdr_meta=False)
            if passlog_prefix is not None:
                pass1.extend(["-passlogfile", str(passlog_prefix)])
            pass1.extend(["-pass", "1", "-an", "-sn", "-dn", "-f", "null", os.devnull])

            pass2 = self._build_video_track_base_cmd(
                video=video,
                source=source,
                stream_index=stream_index,
                offset_ms=offset_ms,
                thread_count=thread_count,
            )
            self._append_video_codec_and_hdr_args(pass2, video, bitrate_kbps=bitrate)
            if passlog_prefix is not None:
                pass2.extend(["-passlogfile", str(passlog_prefix)])
            pass2.extend(["-pass", "2"])
            pass2.extend(["-an", "-sn", "-dn", str(output_path)])
            return [pass1, pass2]

        cmd = self._build_video_track_base_cmd(
            video=video,
            source=source,
            stream_index=stream_index,
            offset_ms=offset_ms,
            thread_count=thread_count,
        )
        self._append_video_codec_and_hdr_args(cmd, video)
        cmd.extend(["-an", "-sn", "-dn", str(output_path)])
        return [cmd]

    def _build_video_only_cmd(self, config: EncodeConfig, output_hevc: Path) -> list[str]:
        """Construit la commande ffmpeg vidéo-seule vers un flux HEVC brut."""
        video = self._primary_video_settings(config)
        return self._build_video_only_cmd_for_track(
            config,
            video,
            self._video_source_path(config),
            output_hevc,
        )

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
        cmd = self._build_video_track_base_cmd(
            video=video,
            source=source,
            stream_index=self._video_stream_from_settings(video),
            offset_ms=offset_ms,
            thread_count=thread_count,
        )
        self._append_video_codec_and_hdr_args(cmd, video)
        cmd.extend(["-an", "-f", "hevc", str(output_hevc)])
        return cmd

    def _build_video_only_two_pass(
        self, config: EncodeConfig, output_hevc: Path
    ) -> list[list[str]]:
        """Construit les 2 passes ffmpeg vidéo-seule vers un flux HEVC brut."""
        video = self._primary_video_settings(config)
        return self._build_video_only_two_pass_for_track(
            config,
            video,
            self._video_source_path(config),
            output_hevc,
            bitrate_kbps=self._size_to_bitrate_kbps(config),
        )

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
        bitrate = bitrate_kbps if bitrate_kbps is not None else self._size_to_bitrate_kbps_for_video(config, video)
        stream_index = self._video_stream_from_settings(video)
        pass1 = self._build_video_track_base_cmd(
            video=video,
            source=source,
            stream_index=stream_index,
            offset_ms=offset_ms,
            thread_count=thread_count,
        )
        self._append_video_codec_and_hdr_args(pass1, video, bitrate_kbps=bitrate, include_hdr_meta=False)
        if passlog_prefix is not None:
            pass1.extend(["-passlogfile", str(passlog_prefix)])
        pass1.extend(["-pass", "1", "-an", "-f", "null", os.devnull])
        pass2 = self._build_video_track_base_cmd(
            video=video,
            source=source,
            stream_index=stream_index,
            offset_ms=offset_ms,
            thread_count=thread_count,
        )
        self._append_video_codec_and_hdr_args(pass2, video, bitrate_kbps=bitrate)
        if passlog_prefix is not None:
            pass2.extend(["-passlogfile", str(passlog_prefix)])
        pass2.extend(["-pass", "2"])
        pass2.extend(["-an", "-f", "hevc", str(output_hevc)])
        return [pass1, pass2]

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
        return _format_preview_selection_plan(
            _build_encode_command_selection_plan(
                config,
                plan=self._build_encode_plan(config),
                is_multi_video=self._is_multi_video,
                uses_two_pass=self._uses_two_pass,
                build_multi_video_preview=self._build_multi_video_command_preview,
                build_two_pass=self._build_two_pass,
                build_single_pass=self._build_single_pass,
            )
        )

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
        return _validate_encode_config_plan(
            config,
            planned_video_tracks=plan.video_tracks,
            dir_writable=_is_dir_writable_plan,
        )

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
        """
        Démarre la préparation encode hors thread UI.

        MainWindow valide déjà la configuration avant d'appeler le panneau encode.
        Cette variante retourne donc immédiatement un TaskSignals extérieur, puis
        relaie le TaskSignals réel créé après préparation workspace/attachments.
        """
        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)
        active_inner: dict[str, TaskSignals | None] = {"signals": None}
        original_cancel = signals.cancel

        def _cancel() -> None:
            original_cancel()
            inner = active_inner.get("signals")
            if inner is not None and inner is not signals:
                inner.cancel()

        signals.cancel = _cancel  # type: ignore[method-assign]

        def _task() -> None:
            try:
                if signals._cancel_event.is_set():
                    raise TaskCancelledError()

                inner = self._run_with_preparation(
                    config,
                    validate=False,
                    prep_signals=signals,
                )
                active_inner["signals"] = inner

                if inner is not signals:
                    inner.progress.connect(signals.progress.emit)
                    inner.finished.connect(signals.finished.emit)
                    inner.failed.connect(signals.failed.emit)
                    inner.cancelled.connect(signals.cancelled.emit)

                if signals._cancel_event.is_set() and inner is not signals:
                    inner.cancel()
            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                executor.shutdown(wait=False)

        executor.submit(_task)
        return signals

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
        self._check_cancelled(prep_signals)
        if validate:
            errors = self.validate(config)
            if errors:
                raise EncodeError("\n".join(errors))
        self._check_cancelled(prep_signals)

        self._log_workflow_type("ENCODE")
        self._log_step(1, "Validation configuration")
        self.log_message.emit("INFO", f"Encodage → {config.output.name}")

        self._log_step(2, "Préparation workspace et attachments")
        self._check_cancelled(prep_signals)
        work_root = config.work_dir or Path(tempfile.gettempdir())
        process_work_dir = prepare_process_work_dir(
            work_root,
            output_path=config.output,
            fallback_name="encode_job",
        )
        self._check_cancelled(prep_signals)
        relocated_attachments = relocate_tmdb_covers_to_process_dir(
            [Path(p) for p in config.extra_attachments],
            work_root=work_root,
            process_dir=process_work_dir,
        )
        self._check_cancelled(prep_signals)

        # Téléchargement différé de la cover TMDB (si présente)
        if config.tmdb_cover is not None:
            tmdb_url, tmdb_filename = config.tmdb_cover
            try:
                self._check_cancelled(prep_signals)
                self.log_message.emit(
                    "INFO",
                    f"Téléchargement cover TMDB : {tmdb_filename}",
                )
                cover_path = download_tmdb_cover(
                    tmdb_url,
                    tmdb_filename,
                    process_work_dir / "attachments",
                )
                relocated_attachments = [*relocated_attachments, cover_path]
            except Exception as exc:
                self.log_message.emit(
                    "WARN",
                    f"Impossible de télécharger la cover TMDB : {exc}",
                )
        self._check_cancelled(prep_signals)

        prepared_config = replace(
            config,
            work_dir=process_work_dir,
            extra_attachments=relocated_attachments,
        )

        prepared_config, cleanup_dir = self._prepare_attachment_config(
            prepared_config,
            work_dir=process_work_dir,
            signals=prep_signals,
        )
        self._check_cancelled(prep_signals)
        cleanup_paths: list[Path] = []
        if cleanup_dir is not None:
            cleanup_paths.append(cleanup_dir)
        relocated_attachment_dir = process_work_dir / "attachments"
        if relocated_attachment_dir.exists():
            cleanup_paths.append(relocated_attachment_dir)
        cleanup_paths.append(process_work_dir)

        self._log_step(3, "Normalisation des options HDR dynamiques")
        self._check_cancelled(prep_signals)
        if self._is_multi_video(prepared_config):
            prepared_config = self._normalize_dynamic_hdr_multi(prepared_config)
        elif not self._is_video_passthrough(prepared_config):
            prepared_config = self._normalize_dynamic_hdr_config(prepared_config)
        elif self._wants_dynamic_hdr_copy(prepared_config):
            # Codec COPY : les NAL units DoVi/HDR10+ sont déjà dans le bitstream source.
            # Extraction + réinjection inutiles sans réencodage — remux direct avec passthrough.
            self.log_message.emit(
                "INFO",
                "Codec COPY : injection DoVi/HDR10+ ignorée — "
                "métadonnées préservées par passthrough ffmpeg.",
            )

        self._check_cancelled(prep_signals)
        if self._is_multi_video(prepared_config):
            self._log_step(4, "Routage du workflow (pipeline multi-pistes vidéo)")
            plan = self._build_encode_plan(prepared_config)
            if prep_signals is not None:
                # En mode validate=False, le pipeline multi-pistes s'exécute
                # inline et peut émettre finished/failed avant le retour.
                # Les hooks doivent donc être branchés avant l'exécution.
                self._bind_output_hooks(
                    prep_signals,
                    output=prepared_config.output,
                    cleanup_paths=cleanup_paths,
                )
            signals = self._run_multi_video_pipeline(
                prepared_config,
                cleanup_paths,
                prep_signals=prep_signals,
                plan=plan,
            )
            if prep_signals is None or signals is not prep_signals:
                self._bind_output_hooks(
                    signals,
                    output=prepared_config.output,
                    cleanup_paths=cleanup_paths,
                )
            return signals

        self._check_cancelled(prep_signals)
        needs_inject = self._needs_metadata_inject(prepared_config)
        self._log_step(
            4,
            "Routage du workflow (sortie directe ou injection metadata)"
            + (" -> injection" if needs_inject else " -> sortie directe"),
        )
        self._check_cancelled(prep_signals)
        if needs_inject:
            self.log_message.emit(
                "INFO",
                "Injection DoVi/HDR10+: pipeline fichier (pas de pipe direct outillage).",
            )
            self._ensure_inject_storage_available(prepared_config)
        self._check_cancelled(prep_signals)

        if prep_signals is not None:
            # Chemin validate=False: certains sous-workflows peuvent exécuter
            # inline et émettre des signaux terminaux avant retour.
            self._bind_output_hooks(
                prep_signals,
                output=prepared_config.output,
                cleanup_paths=cleanup_paths,
            )

        plan = self._build_encode_plan(prepared_config)
        signals = (
            self._run_with_metadata_inject(
                prepared_config,
                prep_signals=prep_signals,
                plan=plan,
            )
            if needs_inject
            else self._run_direct_output(
                prepared_config,
                cleanup_paths,
                prep_signals=prep_signals,
                plan=plan,
            )
        )
        if prep_signals is None or signals is not prep_signals:
            self._bind_output_hooks(
                signals,
                output=prepared_config.output,
                cleanup_paths=cleanup_paths,
                include_segment_patch=False,
                include_nfo=False,
            )
        return signals

    def _run_direct_output(
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
        """Exécute deux commandes ffmpeg séquentiellement, retourne un TaskSignals commun."""
        if signals is None:
            signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            try:
                self.log_message.emit("INFO", "Passe 1/2 (analyse)…")
                self._runner._run_cmd(
                    cmds[0], cwd=cwd, label="ffmpeg-pass1",
                    progress_cb=lambda line: signals.progress.emit(line),
                    signals=signals,
                )
                self.log_message.emit("INFO", "Passe 2/2 (encodage)…")
                output = self._runner._run_cmd(
                    cmds[1], cwd=cwd, label="ffmpeg-pass2",
                    progress_cb=lambda line: signals.progress.emit(line),
                    signals=signals,
                )
                signals.finished.emit(output)
            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                self._cleanup_two_pass_logs(cwd)
                executor.shutdown(wait=False)

        executor.submit(_task)
        return signals

    @staticmethod
    def _cleanup_two_pass_logs(cwd: Path | None) -> None:
        base_dir = Path(cwd) if cwd is not None else Path.cwd()
        if not base_dir.exists():
            return
        for path in base_dir.glob("ffmpeg2pass-*.log*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _cleanup_two_pass_logs_for_prefix(prefix: Path) -> None:
        base_dir = prefix.parent
        if not base_dir.exists():
            return
        for path in base_dir.glob(f"{prefix.name}-*.log*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _normalize_frame_rate_expr(value: object) -> str | None:
        raw = str(value or "").strip()
        if raw in {"", "0", "0/0", "N/A"}:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return raw
        if re.fullmatch(r"\d+/\d+", raw):
            return raw
        return None

    def _source_video_fps_expr(self, source: Path) -> str:
        payload = self._ffprobe_streams_payload(source)
        if payload is None:
            return _FALLBACK_HEVC_FRAME_RATE
        for stream in self._ffprobe_stream_dicts(payload):
            if stream.get("codec_type") != "video":
                continue
            for key in ("avg_frame_rate", "r_frame_rate"):
                fps_expr = self._normalize_frame_rate_expr(stream.get(key))
                if fps_expr is not None:
                    return fps_expr
            break
        return _FALLBACK_HEVC_FRAME_RATE

    def _wrap_injected_hevc_for_reconstruction(
        self,
        *,
        source: Path,
        hevc_input: Path,
        mkv_output: Path,
    ) -> list[str]:
        fps_expr = self._source_video_fps_expr(source)
        return [
            self._ffmpeg,
            "-hide_banner",
            "-y",
            *self._ffmpeg_progress_args(),
            "-f",
            "hevc",
            "-framerate",
            fps_expr,
            "-i",
            str(hevc_input),
            *self._ffmpeg_thread_args(),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-bsf:v",
            f"setts=pts=N/({fps_expr}*TB)",
            str(mkv_output),
        ]

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
        video_inputs = prepared_video_inputs or [
            {
                "input_args": [],
                "path": (
                    self._video_source_from_settings(config, video)
                    if video.codec == "copy"
                    else Path(f"<video_{idx}.mkv>")
                ),
                "map_arg": (
                    f"{idx - 1}:{self._video_stream_from_settings(video)}"
                    if video.codec == "copy"
                    else f"{idx - 1}:v:0"
                ),
            }
            for idx, video in enumerate(self._video_tracks(config), start=1)
        ]

        cmd: list[str] = [self._ffmpeg, "-hide_banner", "-y"]
        cmd.extend(self._ffmpeg_progress_args())
        for spec in video_inputs:
            raw_input_args = spec.get("input_args", ())
            if isinstance(raw_input_args, (list, tuple)):
                input_args = [str(arg) for arg in raw_input_args]
            else:
                input_args = []
            cmd.extend([*input_args, "-i", str(spec["path"])])

        plan = plan or self._build_encode_plan(config)
        all_sources = list(plan.all_sources)
        source_idx = _source_input_index_map_plan(all_sources, start_index=len(video_inputs))
        for src in all_sources:
            cmd.extend(["-i", str(src)])

        next_input_index, chapter_input_index, tag_input_index = self._prepare_container_metadata_inputs(
            cmd,
            config,
            source_idx=source_idx,
            next_input_index=len(video_inputs) + len(all_sources),
            plan=plan,
            chapter_materialize_dir=chapter_materialize_dir,
            chapter_probe_source=config.source,
        )
        _ = next_input_index
        cmd.extend(self._ffmpeg_thread_args())

        for out_idx, spec in enumerate(video_inputs):
            cmd.extend(["-map", str(spec["map_arg"])])
            cmd.extend([f"-c:v:{out_idx}", "copy"])

        resolved_subtitle_tracks = list(plan.resolved_subtitle_tracks)
        self._append_stream_maps_and_attachments(
            cmd,
            config,
            source_idx=source_idx,
            subtitle_copy_input_indices=list(source_idx.values()),
            subtitle_tracks_override=resolved_subtitle_tracks,
            force_copy_subtitles_wildcard=(config.copy_subtitles and not plan.subtitles_resolved),
        )

        default_source_index = source_idx.get(config.source, len(video_inputs))
        self._append_container_metadata_args(
            cmd,
            config,
            default_metadata_input_index=default_source_index,
            default_chapter_input_index=default_source_index,
            chapter_input_index=chapter_input_index,
            tag_input_index=tag_input_index,
            include_copy_video_stream_passthrough=False,
            plan=plan,
        )
        cmd.append(str(config.output))
        return cmd

    @staticmethod
    def _track_spec_for_track_order(track_order: int, video_count: int, audio_count: int) -> tuple[str, int] | None:
        if track_order <= 0:
            return None
        if track_order <= max(1, video_count):
            return ("v", track_order - 1)

        first_audio = max(1, video_count) + 1
        last_audio = first_audio + max(0, audio_count) - 1
        if first_audio <= track_order <= last_audio:
            return ("a", track_order - first_audio)

        first_sub = last_audio + 1
        if track_order >= first_sub:
            return ("s", track_order - first_sub)
        return None

    @staticmethod
    def _normalized_track_language_value(language: str, title: str | None = None) -> str | None:
        return _common_normalize_track_language(language, title)

    @staticmethod
    def _disposition_value_from_edit(edit) -> str | None:
        return _common_disposition_value(
            flag_default=edit.flag_default,
            flag_forced=edit.flag_forced,
            flag_hearing_impaired=edit.flag_hearing_impaired,
            flag_visual_impaired=edit.flag_visual_impaired,
            flag_original=edit.flag_original,
            flag_commentary=edit.flag_commentary,
        )

    def _build_track_meta_args(self, config: EncodeConfig) -> list[str]:
        args: list[str] = []
        if not config.track_meta_edits:
            return args

        video_count = max(1, len(self._video_tracks(config)))
        audio_count = len(config.audio_tracks)
        for edit in config.track_meta_edits:
            spec = self._track_spec_for_track_order(int(edit.track_order), video_count, audio_count)
            if spec is None:
                self.log_message.emit("WARN", f"Piste invalide en édition metadata: @{edit.track_order}")
                continue
            stream_type, out_idx = spec
            stream_spec = f"-metadata:s:{stream_type}:{out_idx}"
            disposition_spec = f"-disposition:{stream_type}:{out_idx}"

            language = (edit.language or "").strip()
            if language:
                lang_value = self._normalized_track_language_value(language, edit.title)
                if lang_value:
                    # Écrit la langue utile en BCP-47 et purge l'ancien champ IETF
                    # pour éviter les doublons ISO+IETF incohérents.
                    args.extend([stream_spec, f"language={lang_value}"])
                    args.extend([stream_spec, "language-ietf="])

            if edit.title is not None:
                args.extend([stream_spec, f"title={edit.title}"])

            disposition = self._disposition_value_from_edit(edit)
            if disposition is not None:
                args.extend([disposition_spec, disposition])
        return args

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
