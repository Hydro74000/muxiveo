"""
core/workflows/encode/workflow.py — FFmpeg encode workflow with optional HDR metadata injection.

Public:
    EncodeWorkflow
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, cast

from PySide6.QtCore import QObject, Signal
from core.lang_tags import Rfc5646LanguageTags as LangTags
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
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from core.workflows.remux import RemuxWorkflow, write_mediainfo_nfo
from core.workflows.remux_timeline_sync import (
    LiveSyncSession,
    FfmpegTimelineSync,
    TimelineSyncFallbackHelper,
)
from core.workflows.encode.hw_devices import (
    select_linux_hwaccel_device,
    select_windows_hwaccel_device,
)
from core.workflows.encode.models import (
    EncodeConfig, EncodeError, QualityMode,
    VideoEncodeSettings, AudioTrackSettings, TrackTimeOffset,
    normalize_audio_bitrate_kbps,
)


_MIME_BY_EXT: dict[str, str] = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".gif":  "image/gif",
    ".bmp":  "image/bmp",
    ".webp": "image/webp",
    ".tif":  "image/tiff",
    ".tiff": "image/tiff",
}

_EXT_BY_MIME: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
}

_VAAPI_CODECS = {"hevc_vaapi", "h264_vaapi", "av1_vaapi"}
_QSV_CODECS = {"hevc_qsv", "h264_qsv", "av1_qsv"}
_NVENC_CODECS = {"hevc_nvenc", "h264_nvenc", "av1_nvenc"}
_AMF_CODECS = {"hevc_amf", "h264_amf", "av1_amf"}
_FALLBACK_HEVC_FRAME_RATE = "24000/1001"
_UI_ENCODE_PROGRESS_PREFIX = "__MRE_PROGRESS__ "


def _ui_encode_progress_message(*, label: str, event: str, line: str = "") -> str:
    payload = {
        "kind": "encode_ffmpeg",
        "label": str(label),
        "event": str(event),
        "line": str(line),
    }
    return _UI_ENCODE_PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class _EncodeSyncTrack:
    track_type: str


@dataclass(frozen=True)
class _EncodeSyncMappedTrack:
    source_file_index: int
    stream_index: int
    track: _EncodeSyncTrack


@dataclass(frozen=True)
class _EncodeOffsetInputSpec:
    map_key: tuple[Path, int, str]
    input_path: Path | str
    input_stream_index: int
    offset_ms: int


@dataclass(frozen=True)
class _VideoTrackPrepSpec:
    order: int
    video: VideoEncodeSettings
    source: Path
    stream_index: int
    offset_ms: int


@dataclass(frozen=True)
class _VideoTrackPrepTask:
    order: int
    resource_key: str
    estimated_ram_bytes: int
    run: Callable[[], tuple[dict[str, object], list[Path]]]


@dataclass(frozen=True)
class _VideoPreparationResourcePolicy:
    """Politique explicite d'allocation des ressources encodeur."""

    vaapi_device: str | None = None
    ffmpeg_threads: int = 1

    def resource_key(self, video: VideoEncodeSettings) -> str:
        codec = str(video.codec or "").strip().lower()
        if codec in {"hevc_nvenc", "h264_nvenc", "av1_nvenc"}:
            return "gpu:nvenc"
        if codec in _VAAPI_CODECS:
            return f"gpu:vaapi:{self.vaapi_device or 'auto'}"
        if codec in {"hevc_qsv", "h264_qsv", "av1_qsv"}:
            return "gpu:qsv"
        if codec in {"hevc_amf", "h264_amf", "av1_amf"}:
            return "gpu:amf"
        return "cpu"

    def estimated_ram_bytes(
        self,
        video: VideoEncodeSettings,
        *,
        source_size: int,
    ) -> int:
        """
        Heuristique conservative pour éviter un sur-engagement RAM en parallèle.

        Le but n'est pas de prédire finement le working set réel de FFmpeg, mais
        d'empêcher le lancement simultané de plusieurs encodes lourds quand la
        marge mémoire restante est trop faible.
        """
        mib = 1024 * 1024
        resource_key = self.resource_key(video)
        threads = max(1, int(self.ffmpeg_threads or 1))

        if resource_key == "cpu":
            base = 768 * mib
            per_thread = 96 * mib
        else:
            base = 384 * mib
            per_thread = 32 * mib

        if video.quality_mode == QualityMode.SIZE:
            base += 128 * mib
        if video.copy_dv or video.copy_hdr10plus:
            base += 256 * mib

        source_component = 0
        if source_size > 0:
            source_component = min(max(source_size // 32, 128 * mib), 1024 * mib)

        return base + source_component + (threads * per_thread)


class _VideoTrackPreparationOrchestrator:
    """
    Orchestrateur de préparation des pistes vidéo avec contrôle de collisions.

    - `max_parallel` limite le nombre total de workers.
    - Les tâches partageant la même `resource_key` sont sérialisées.
    """

    def __init__(
        self,
        *,
        max_parallel: int,
        cancel_cb: Callable[[], None],
        on_worker_failure: Callable[[], None] | None = None,
        min_available_ram_bytes: int = 0,
        available_ram_cb: Callable[[], int] | None = None,
        on_ram_wait: Callable[[int, int, int], None] | None = None,
        ram_wait_timeout_s: float = 0.25,
    ) -> None:
        self._max_parallel = max(1, int(max_parallel))
        self._cancel_cb = cancel_cb
        self._on_worker_failure = on_worker_failure
        self._min_available_ram_bytes = max(0, int(min_available_ram_bytes))
        self._available_ram_cb = available_ram_cb
        self._on_ram_wait = on_ram_wait
        self._ram_wait_timeout_s = max(0.05, float(ram_wait_timeout_s))
        self._resource_semaphores: dict[str, threading.Semaphore] = {}
        self._resource_guard = threading.Lock()
        self._ram_guard = threading.Condition()
        self._reserved_ram_bytes = 0

    def _semaphore(self, resource_key: str) -> threading.Semaphore:
        with self._resource_guard:
            semaphore = self._resource_semaphores.get(resource_key)
            if semaphore is None:
                semaphore = threading.Semaphore(1)
                self._resource_semaphores[resource_key] = semaphore
            return semaphore

    def _claim_ram_budget(self, task: _VideoTrackPrepTask) -> int:
        if (
            self._available_ram_cb is None
            or self._min_available_ram_bytes <= 0
            or task.estimated_ram_bytes <= 0
        ):
            return 0

        warned = False
        with self._ram_guard:
            while True:
                self._cancel_cb()
                available = max(0, int(self._available_ram_cb() or 0))
                if available <= 0:
                    return 0

                required = (
                    self._min_available_ram_bytes
                    + self._reserved_ram_bytes
                    + task.estimated_ram_bytes
                )
                if available >= required:
                    self._reserved_ram_bytes += task.estimated_ram_bytes
                    return task.estimated_ram_bytes

                if self._reserved_ram_bytes == 0 and available > self._min_available_ram_bytes:
                    claim = max(1, available - self._min_available_ram_bytes)
                    self._reserved_ram_bytes += claim
                    return claim

                if (not warned) and self._on_ram_wait is not None:
                    self._on_ram_wait(task.order, required, available)
                    warned = True

                self._ram_guard.wait(timeout=self._ram_wait_timeout_s)

    def _release_ram_budget(self, reserved_bytes: int) -> None:
        if reserved_bytes <= 0:
            return
        with self._ram_guard:
            self._reserved_ram_bytes = max(0, self._reserved_ram_bytes - int(reserved_bytes))
            self._ram_guard.notify_all()

    def _run_task(
        self,
        task: _VideoTrackPrepTask,
    ) -> tuple[int, dict[str, object], list[Path]]:
        self._cancel_cb()
        semaphore = self._semaphore(task.resource_key)
        with semaphore:
            reserved_bytes = self._claim_ram_budget(task)
            try:
                self._cancel_cb()
                prepared_input, cleanup = task.run()
                return task.order, prepared_input, cleanup
            finally:
                self._release_ram_budget(reserved_bytes)

    def execute(
        self,
        tasks: list[_VideoTrackPrepTask],
    ) -> list[tuple[int, dict[str, object], list[Path]]]:
        if not tasks:
            return []

        if self._max_parallel <= 1 or len(tasks) == 1:
            return [self._run_task(task) for task in tasks]

        results: list[tuple[int, dict[str, object], list[Path]]] = []
        worker_count = min(self._max_parallel, len(tasks))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(self._run_task, task) for task in tasks]
            for future in as_completed(futures):
                self._cancel_cb()
                try:
                    results.append(future.result())
                except Exception:
                    if self._on_worker_failure is not None:
                        self._on_worker_failure()
                    raise
        return results


def _default_ffmpeg_thread_count() -> int:
    """Default FFmpeg thread count: logical CPU count × 0.75, rounded up."""
    cpu_count = os.cpu_count() or 1
    return max(1, (cpu_count * 3 + 3) // 4)


def _normalize_ffmpeg_thread_count(value: int | None) -> int:
    """Return a safe FFmpeg thread count, preserving 0 as ffmpeg auto mode."""
    if value is None or value < 0:
        return _default_ffmpeg_thread_count()
    return value


def _normalize_max_parallel_video_encodes(value: int | None) -> int:
    """Return a safe per-workflow parallelism value for multi-video preparation."""
    if value is None:
        return 1
    return max(1, int(value))


def _mime_for(path: Path) -> str:
    return _MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


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
        self._postproc_helper = RemuxWorkflow(
            ffmpeg_bin=ffmpeg_bin,
            ffprobe_bin=self._ffprobe_bin_from_ffmpeg(ffmpeg_bin),
            parent=self,
            writing_application=writing_application,
            generate_nfo=False,  # NFO géré directement par EncodeWorkflow via _bind_nfo_write
            mediainfo_bin=mediainfo_bin,
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

    def set_ffmpeg(self, ffmpeg_bin: str) -> None:
        """Met à jour le binaire ffmpeg utilisé pour l'encodage (ex: ffmpeg système pour HW)."""
        self._ffmpeg = ffmpeg_bin
        self._postproc_helper.set_ffmpeg_bin(ffmpeg_bin)
        self._postproc_helper.set_ffprobe_bin(self._ffprobe_bin_from_ffmpeg(ffmpeg_bin))

    def set_writing_application(self, writing_application: str) -> None:
        """Met à jour la valeur du tag Multiplexing Application."""
        self._writing_application = writing_application.strip()
        self._postproc_helper.set_writing_application(self._writing_application)

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
        effective = self._ffmpeg_threads if thread_count is None else max(0, int(thread_count))
        return ["-threads", str(effective)]

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
        return ["-progress", "pipe:1", "-nostats"]

    @staticmethod
    def _ffprobe_bin_from_ffmpeg(ffmpeg_bin: str) -> str:
        ffmpeg_path = Path(ffmpeg_bin)
        name = ffmpeg_path.name.lower()
        if name in {"ffmpeg", "ffmpeg.exe"}:
            return str(ffmpeg_path.with_name("ffprobe" + ffmpeg_path.suffix))
        return "ffprobe"

    @staticmethod
    def _is_video_passthrough(config: EncodeConfig) -> bool:
        return config.video.codec == "copy"

    @classmethod
    def _uses_two_pass(cls, config: EncodeConfig) -> bool:
        return not cls._is_video_passthrough(config) and config.video.quality_mode == QualityMode.SIZE

    @staticmethod
    def _wants_dynamic_hdr_copy(config: EncodeConfig) -> bool:
        return bool(config.video.copy_dv or config.video.copy_hdr10plus)

    @classmethod
    def _needs_metadata_inject(cls, config: EncodeConfig) -> bool:
        return cls._wants_dynamic_hdr_copy(config) and not cls._is_video_passthrough(config)

    @staticmethod
    def _video_source_path(config: EncodeConfig) -> Path:
        return Path(config.video.source_path or config.source)

    @staticmethod
    def _video_stream_index(config: EncodeConfig) -> int:
        return int(getattr(config.video, "stream_index", 0) or 0)

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
            try:
                idx_val = int(raw_idx)  # type: ignore[arg-type]
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

        has_dv, has_hdr10plus = detected
        copy_dv = config.video.copy_dv and has_dv
        copy_hdr10plus = config.video.copy_hdr10plus and has_hdr10plus

        if config.video.copy_dv and not copy_dv:
            self.log_message.emit(
                "WARN",
                "Copy DoVi demandé mais aucune donnée DoVi détectée — option ignorée.",
            )
        if config.video.copy_hdr10plus and not copy_hdr10plus:
            self.log_message.emit(
                "WARN",
                "Copy HDR10+ demandé mais aucune donnée HDR10+ détectée — option ignorée.",
            )

        normalized_video = replace(
            config.video,
            copy_dv=copy_dv,
            copy_hdr10plus=copy_hdr10plus,
        )
        normalized = replace(
            config,
            video=normalized_video,
            video_tracks=[normalized_video, *config.video_tracks[1:]],
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
        return self._build_direct_output_commands(config)

    def build_command_single(self, config: EncodeConfig) -> list[str]:
        """Toujours une seule commande — pour l'aperçu UI."""
        if self._is_multi_video(config):
            commands = self._build_multi_video_command_preview(config)
            return commands[-1] if commands else []
        if self._uses_two_pass(config):
            return self._build_two_pass(config)[1]
        return self._build_single_pass(config)

    def _build_direct_output_commands(
        self,
        config: EncodeConfig,
        *,
        chapter_materialize_dir: Path | None = None,
    ) -> list[str] | list[list[str]]:
        if self._is_multi_video(config):
            return self._build_multi_video_command_preview(config)
        if self._uses_two_pass(config):
            return self._build_two_pass(
                config,
                chapter_materialize_dir=chapter_materialize_dir,
            )
        return self._build_single_pass(
            config,
            chapter_materialize_dir=chapter_materialize_dir,
        )

    def _build_multi_video_command_preview(self, config: EncodeConfig) -> list[list[str]]:
        commands: list[list[str]] = []
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
        commands.append(self._build_multi_video_final_mux_command(config, []))
        return commands

    @staticmethod
    def _collect_all_sources(config: EncodeConfig) -> list[Path]:
        """Retourne les sources uniques (source principale puis extras)."""
        all_sources: list[Path] = [config.source]
        for video in EncodeWorkflow._video_tracks(config):
            video_source = Path(video.source_path or config.source)
            if video_source not in all_sources:
                all_sources.append(video_source)
        for a in config.audio_tracks:
            sp = a.source_path or config.source
            if sp not in all_sources:
                all_sources.append(sp)
        for src_path, _idx in config.subtitle_tracks:
            if src_path not in all_sources:
                all_sources.append(src_path)
        for src_path, _idx in config.attachment_streams:
            if src_path not in all_sources:
                all_sources.append(src_path)
        return all_sources

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

    @staticmethod
    def _source_input_index_map(sources: list[Path], *, start_index: int = 0) -> dict[Path, int]:
        """Construit un mapping source -> index d'input ffmpeg."""
        return {src: start_index + i for i, src in enumerate(sources)}

    @staticmethod
    def _offset_seconds(offset_ms: int) -> str:
        return f"{abs(int(offset_ms)) / 1000.0:.3f}"

    @staticmethod
    def _track_time_offset_lookup(config: EncodeConfig) -> dict[tuple[str, Path, int], int]:
        lookup: dict[tuple[str, Path, int], int] = {}
        for raw in config.track_time_offsets:
            if not isinstance(raw, TrackTimeOffset):
                continue
            track_type = str(raw.track_type or "").strip().lower()
            if track_type not in {"video", "audio", "subtitle"}:
                continue
            lookup[(track_type, Path(raw.source_path), int(raw.stream_index))] = int(raw.offset_ms)
        return lookup

    @staticmethod
    def _track_offset_ms(
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
                int(v)
                for (tt, sp, _), v in lookup.items()
                if tt == "video" and sp == key[1]
            ]
            if len(matches) == 1:
                return matches[0]
        return 0

    def _build_offset_specs(
        self,
        config: EncodeConfig,
        *,
        track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]],
        offset_lookup: dict[tuple[str, Path, int], int] | None = None,
    ) -> list[_EncodeOffsetInputSpec]:
        lookup = offset_lookup if offset_lookup is not None else self._track_time_offset_lookup(config)
        specs: list[_EncodeOffsetInputSpec] = []
        for map_key, input_path, input_stream_index in track_mappings:
            src_path, src_stream_idx, track_type = map_key
            offset_ms = self._track_offset_ms(
                lookup,
                track_type=track_type,
                source_path=src_path,
                stream_index=src_stream_idx,
            )
            if offset_ms == 0:
                continue
            if track_type == "video" and offset_ms < 0:
                raise EncodeError(
                    "Décalage vidéo négatif interdit : "
                    f"source={src_path}, stream={src_stream_idx}, offset={offset_ms} ms"
                )
            specs.append(_EncodeOffsetInputSpec(
                map_key=(Path(src_path), int(src_stream_idx), str(track_type)),
                input_path=input_path,
                input_stream_index=int(input_stream_index),
                offset_ms=int(offset_ms),
            ))
        return specs

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
                    cmd.extend(["-itsoffset", self._offset_seconds(spec.offset_ms), "-i", str(spec.input_path)])
                else:
                    cmd.extend(["-ss", self._offset_seconds(spec.offset_ms), "-i", str(spec.input_path)])
                input_idx = next_input_index
                input_by_key[input_key] = input_idx
                next_input_index += 1

            remap[spec.map_key] = (int(input_idx), int(spec.input_stream_index))

        return next_input_index, remap

    @staticmethod
    def _video_map_arg(
        default_map: tuple[int, int],
        *,
        offset_remap: dict[tuple[Path, int, str], tuple[int, int]],
        map_key: tuple[Path, int, str],
    ) -> str:
        remapped = offset_remap.get(map_key)
        if remapped is None:
            stream_index = int(default_map[1])
            if stream_index == 0:
                return f"{int(default_map[0])}:v:0"
            return f"{int(default_map[0])}:{stream_index}"
        return f"{int(remapped[0])}:{int(remapped[1])}"

    def _probe_stream_indices(self, source: Path, codec_type: str) -> list[int] | None:
        payload = self._ffprobe_streams_payload(source)
        if payload is None:
            return None
        indices: list[int] = []
        for stream in self._ffprobe_stream_dicts(payload):
            if stream.get("codec_type") != codec_type:
                continue
            idx_raw = stream.get("index")
            if not isinstance(idx_raw, (int, str)):
                continue
            try:
                indices.append(int(idx_raw))
            except (TypeError, ValueError):
                continue
        return sorted(set(indices))

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
        if config.subtitle_tracks:
            deduped: list[tuple[Path, int]] = []
            seen: set[tuple[Path, int]] = set()
            for src_path, stream_idx in config.subtitle_tracks:
                key = (src_path, int(stream_idx))
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(key)
            return deduped, True

        if not config.copy_subtitles:
            return [], True

        resolved: list[tuple[Path, int]] = []
        seen: set[tuple[Path, int]] = set()
        for src_path in all_sources:
            subtitle_indices = self._probe_stream_indices(src_path, "subtitle")
            if subtitle_indices is None:
                return [], False
            for stream_idx in subtitle_indices:
                key = (src_path, int(stream_idx))
                if key in seen:
                    continue
                seen.add(key)
                resolved.append(key)
        return resolved, True

    def _build_probe_remux_config(
        self,
        config: EncodeConfig,
        all_sources: list[Path],
        source_idx_local: dict[Path, int],
        resolved_subtitle_tracks: list[tuple[Path, int]],
    ) -> RemuxConfig:
        """
        Construit une config Remux minimale pour réutiliser la logique de détection
        de risque multi-source (strict interleave + pré-scan sous-titres).
        """
        tracks_by_source: dict[int, list[TrackEntry]] = {i: [] for i in range(len(all_sources))}

        def _push_track(src_idx: int, stream_idx: int, track_type: str) -> None:
            bucket = tracks_by_source.setdefault(src_idx, [])
            if any(t.mkv_tid == stream_idx and t.track_type == track_type for t in bucket):
                return
            bucket.append(TrackEntry(
                mkv_tid=stream_idx,
                track_type=track_type,
                codec="COPY",
                display_info="",
                language="",
                title="",
            ))

        # Vidéo de référence : input source principal.
        _push_track(0, 0, "video")
        for a in config.audio_tracks:
            src = a.source_path or config.source
            src_idx = source_idx_local.get(src, 0)
            _push_track(src_idx, int(a.stream_index), "audio")
        for src, stream_idx in resolved_subtitle_tracks:
            src_idx = source_idx_local.get(src)
            if src_idx is None:
                continue
            _push_track(src_idx, int(stream_idx), "subtitle")

        sources: list[SourceInput] = []
        track_order: list[tuple[int, int] | tuple[int, int, str]] = []
        for i, src in enumerate(all_sources):
            source_tracks = tracks_by_source.get(i, [])
            sources.append(SourceInput(path=src, file_index=i, tracks=source_tracks))
            for t in source_tracks:
                track_order.append((i, int(t.mkv_tid)))

        return RemuxConfig(
            sources=sources,
            output=config.output,
            track_order=track_order,
            keep_chapters=config.keep_chapters,
        )

    @staticmethod
    def _build_sync_mapped_tracks(
        config: EncodeConfig,
        source_idx_local: dict[Path, int],
        resolved_subtitle_tracks: list[tuple[Path, int]],
    ) -> list[_EncodeSyncMappedTrack]:
        mapped: list[_EncodeSyncMappedTrack] = [
            _EncodeSyncMappedTrack(
                source_file_index=0,
                stream_index=0,
                track=_EncodeSyncTrack("video"),
            )
        ]
        for a in config.audio_tracks:
            src = a.source_path or config.source
            src_idx = source_idx_local.get(src, 0)
            mapped.append(_EncodeSyncMappedTrack(
                source_file_index=src_idx,
                stream_index=int(a.stream_index),
                track=_EncodeSyncTrack("audio"),
            ))
        for src, stream_idx in resolved_subtitle_tracks:
            src_idx = source_idx_local.get(src)
            if src_idx is None:
                continue
            mapped.append(_EncodeSyncMappedTrack(
                source_file_index=src_idx,
                stream_index=int(stream_idx),
                track=_EncodeSyncTrack("subtitle"),
            ))
        return mapped

    @staticmethod
    def _needs_strict_interleave_for_encode(
        mapped_tracks: list[_EncodeSyncMappedTrack],
    ) -> bool:
        """
        Heuristique rapide (sans pré-scan ffprobe) pour éviter de bloquer l'UI
        au lancement:
          - multi-source effectif,
          - présence de sous-titres en sortie,
          - au moins un audio provenant d'une autre source que la vidéo primaire.
        """
        used_sources = {mt.source_file_index for mt in mapped_tracks}
        if len(used_sources) < 2:
            return False

        has_subtitle_output = any(mt.track.track_type == "subtitle" for mt in mapped_tracks)
        if not has_subtitle_output:
            return False

        primary_video = next((mt for mt in mapped_tracks if mt.track.track_type == "video"), None)
        if primary_video is None:
            return False

        return any(
            mt.track.track_type == "audio" and mt.source_file_index != primary_video.source_file_index
            for mt in mapped_tracks
        )

    def _requires_file_sync_fallback_for_offsets(
        self,
        config: EncodeConfig,
        mapped_tracks: list[_EncodeSyncMappedTrack],
        source_by_index: dict[int, Path],
        *,
        offset_lookup: dict[tuple[str, Path, int], int] | None = None,
    ) -> bool:
        """Forcer le fallback fichier si offsets étrangers incompatibles avec le mode live."""
        primary_video = next((mt for mt in mapped_tracks if mt.track.track_type == "video"), None)
        if primary_video is None:
            return False

        lookup = offset_lookup if offset_lookup is not None else self._track_time_offset_lookup(config)
        for mt in mapped_tracks:
            if mt.track.track_type not in {"audio", "subtitle"}:
                continue
            if mt.source_file_index == primary_video.source_file_index:
                continue
            src_path = source_by_index.get(mt.source_file_index)
            if src_path is None:
                continue
            offset_ms = self._track_offset_ms(
                lookup,
                track_type=mt.track.track_type,
                source_path=src_path,
                stream_index=mt.stream_index,
            )
            if offset_ms != 0:
                return True
        return False

    def _prepare_multisource_sync(
        self,
        *,
        config: EncodeConfig,
        all_sources: list[Path],
        sync_base_input_idx: int,
        work_dir: Path,
        signals: TaskSignals | None = None,
        allow_live: bool = True,
    ) -> tuple[dict[tuple[Path, int, str], tuple[int, int]], list[Path | str], LiveSyncSession | None, bool]:
        """
        Prépare la normalisation timeline pour les flux multi-source
        dans le workflow encode.
        """
        source_idx_local = {p: i for i, p in enumerate(all_sources)}
        if len(source_idx_local) < 2:
            return {}, [], None, False
        self.log_message.emit(
            "INFO",
            "Analyse sync timeline multi-source : pré-scan/remap en cours…",
        )

        resolved_subtitle_tracks, subtitles_resolved = self._resolved_subtitle_tracks_for_encode(
            config,
            all_sources,
        )
        mapped_tracks = self._build_sync_mapped_tracks(
            config,
            source_idx_local,
            resolved_subtitle_tracks,
        )
        path_by_source_idx = {idx: path for path, idx in source_idx_local.items()}
        offset_lookup = self._track_time_offset_lookup(config)
        offset_requires_sync = self._requires_file_sync_fallback_for_offsets(
            config,
            mapped_tracks,
            path_by_source_idx,
            offset_lookup=offset_lookup,
        )

        strict_interleave = False
        if offset_requires_sync:
            strict_interleave = True
            self.log_message.emit(
                "INFO",
                "Décalage sur piste étrangère détecté : sync timeline activé.",
            )
        elif subtitles_resolved:
            self.log_message.emit(
                "INFO",
                "Pré-scan ffprobe des sous-titres (décision interleave strict)…",
            )
            strict_interleave = self._postproc_helper._decide_strict_interleave_with_prescan(
                self._build_probe_remux_config(
                    config,
                    all_sources,
                    source_idx_local,
                    resolved_subtitle_tracks,
                )
            )
        elif config.copy_subtitles:
            # copy_subtitles sans résolution explicite des streams: on reste
            # conservateur dès qu'il y a audio étranger multi-source.
            strict_interleave = self._needs_strict_interleave_for_encode(mapped_tracks)
            if strict_interleave:
                self.log_message.emit(
                    "WARNING",
                    "Pré-scan sous-titres indisponible en mode copy_subtitles ; "
                    "activation sync timeline par sécurité.",
                )

        if not strict_interleave:
            return {}, [], None, False

        allow_live_sync = bool(allow_live)
        if allow_live_sync and offset_requires_sync:
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
            mapped_tracks=mapped_tracks,
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
        cmd.extend(["-max_interleave_delta", "0"])
        cmd.extend(["-max_muxing_queue_size", "9999"])

    @staticmethod
    def _append_sync_inputs(cmd: list[str], sync_inputs: list[Path | str]) -> None:
        """
        Ajoute les entrées synchronisées (FIFO/fichiers temporaires) en forçant
        le demuxer Matroska pour éviter les blocages de probing sur flux live.
        """
        for sync_input in sync_inputs:
            cmd.extend(["-f", "matroska", "-i", str(sync_input)])

    @staticmethod
    def _sync_cleanup_paths(sync_inputs: list[Path | str]) -> list[Path]:
        return [p for p in sync_inputs if isinstance(p, Path)]

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
                inp, stream_idx = remapped
            else:
                inp = source_idx.get(src_path)
                if inp is None:
                    inp = source_idx.get(config.source, 0)
                stream_idx = int(a.stream_index)
            cmd.extend(["-map", f"{inp}:{stream_idx}"])
            cmd.extend(self._audio_codec_args(i, a))

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
                    inp, mapped_stream_idx = remapped
                    cmd.extend(["-map", f"{inp}:{mapped_stream_idx}"])
                    continue
                inp = source_idx.get(src_path)
                if inp is None:
                    continue
                cmd.extend(["-map", f"{inp}:{stream_idx}"])
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
                inp = source_idx.get(src_path)
                if inp is None:
                    continue
                cmd.extend(["-map", f"{inp}:{stream_idx}"])
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
                        f"filename={self._attachment_filename(meta, stream_idx)}",
                    ])

        existing_att = len(mapped_attachment_meta)
        for i, att_path in enumerate(config.extra_attachments):
            att_idx = existing_att + i
            att_name = "cover" if att_path.stem.lower() == "cover" else att_path.name
            cmd.extend(["-attach", str(att_path)])
            cmd.extend([f"-metadata:s:t:{att_idx}", f"mimetype={_mime_for(att_path)}"])
            cmd.extend([f"-metadata:s:t:{att_idx}", f"filename={att_name}"])

    def _prepare_container_metadata_inputs(
        self,
        cmd: list[str],
        config: EncodeConfig,
        *,
        source_idx: dict[Path, int],
        next_input_index: int,
        chapter_materialize_dir: Path | None = None,
        chapter_probe_source: Path | None = None,
    ) -> tuple[int, int | None, int | None]:
        """Ajoute les inputs nécessaires aux metadata (chapitres/tags) et retourne leurs index."""
        chapter_input_index: int | None = None
        if config.chapter_overrides:
            if chapter_materialize_dir is not None:
                duration_s = self._postproc_helper._probe_duration_seconds(
                    chapter_probe_source or config.source
                )
                chapter_file = self._postproc_helper._write_ffmetadata_chapters(
                    entries=config.chapter_overrides,
                    out_dir=chapter_materialize_dir,
                    duration_s=duration_s,
                )
                chapter_ref = str(chapter_file)
            else:
                chapter_ref = "<chapitres.ffmetadata>"
            chapter_input_index = next_input_index
            next_input_index += 1
            cmd.extend(["-i", chapter_ref])

        tag_input_index: int | None = None
        if config.tag_overrides is None and config.tag_sources:
            tag_source = Path(config.tag_sources[-1])
            mapped_idx = source_idx.get(tag_source)
            if mapped_idx is not None:
                tag_input_index = mapped_idx
            else:
                tag_input_index = next_input_index
                next_input_index += 1
                cmd.extend(["-i", str(tag_source)])
        return next_input_index, chapter_input_index, tag_input_index

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
    ) -> None:
        """Ajoute les options metadata/chapitres/tags/track-meta en une passe."""
        chapter_map = self._container_chapter_map_value(
            config,
            default_chapter_input_index=default_chapter_input_index,
            chapter_input_index=chapter_input_index,
        )
        metadata_map = self._container_metadata_map_value(
            config,
            default_metadata_input_index=default_metadata_input_index,
            chapter_input_index=chapter_input_index,
            tag_input_index=tag_input_index,
            include_copy_video_stream_passthrough=include_copy_video_stream_passthrough,
            chapter_map=chapter_map,
        )
        if metadata_map is not None:
            cmd.extend(["-map_metadata", metadata_map])
            if include_copy_video_stream_passthrough and self._is_video_passthrough(config):
                cmd.extend([
                    "-map_metadata:s:v:0",
                    f"{default_metadata_input_index}:s:v:0",
                ])

        cmd.extend(["-map_chapters", chapter_map])

        global_tags = self._resolve_global_tags(config)
        title_value = global_tags.pop("title", None)
        if title_value is not None:
            cmd.extend(["-metadata", f"title={title_value}"])

        # Suppression des balises ENCODER et CREATION_TIME transportées depuis la source.
        # On garde ces resets avant les autres tags pour qu'un éventuel override utilisateur
        # puisse les redéfinir ensuite.
        cmd.extend(["-metadata", "encoder=", "-metadata", "creation_time="])

        for key, value in global_tags.items():
            cmd.extend(["-metadata", f"{key}={value}"])
        cmd.extend(self._build_track_meta_args(config))

    def _container_metadata_map_value(
        self,
        config: EncodeConfig,
        *,
        default_metadata_input_index: int,
        chapter_input_index: int | None,
        tag_input_index: int | None,
        include_copy_video_stream_passthrough: bool,
        chapter_map: str | None = None,
    ) -> str | None:
        if config.tag_overrides is not None:
            if chapter_input_index is not None:
                return str(chapter_input_index)
            if chapter_map is not None and chapter_map not in ("-1", ""):
                return chapter_map
            return "-1"
        if tag_input_index is not None:
            return str(tag_input_index)
        if include_copy_video_stream_passthrough and self._is_video_passthrough(config):
            return str(default_metadata_input_index)
        if config.chapter_overrides is not None or bool(config.track_meta_edits):
            return str(default_metadata_input_index)
        return None

    @staticmethod
    def _container_chapter_map_value(
        config: EncodeConfig,
        *,
        default_chapter_input_index: int,
        chapter_input_index: int | None,
    ) -> str:
        if config.chapter_overrides is not None:
            if config.chapter_overrides and chapter_input_index is not None:
                return str(chapter_input_index)
            return "-1"
        return str(default_chapter_input_index) if config.keep_chapters else "-1"

    def _build_single_pass(
        self,
        config: EncodeConfig,
        *,
        chapter_materialize_dir: Path | None = None,
    ) -> list[str]:
        all_sources = self._collect_all_sources(config)
        source_idx = self._source_input_index_map(all_sources)
        offset_lookup = self._track_time_offset_lookup(config)

        cmd: list[str] = [self._ffmpeg, "-hide_banner", "-y"]
        cmd.extend(self._ffmpeg_progress_args())
        cmd.extend(self._hardware_input_args(config.video))
        for src in all_sources:
            cmd.extend(["-i", str(src)])
        next_input_index, chapter_input_index, tag_input_index = self._prepare_container_metadata_inputs(
            cmd,
            config,
            source_idx=source_idx,
            next_input_index=len(all_sources),
            chapter_materialize_dir=chapter_materialize_dir,
            chapter_probe_source=config.source,
        )

        vf = self._build_encoder_vf(config.video)
        if vf:
            cmd.extend(["-vf", vf])

        cmd.extend(self._ffmpeg_thread_args())

        resolved_subtitle_tracks, subtitles_resolved = self._resolved_subtitle_tracks_for_encode(
            config,
            all_sources,
        )

        video_source = self._video_source_path(config)
        video_stream = self._video_stream_index(config)
        video_input_idx = source_idx.get(video_source, source_idx.get(config.source, 0))
        video_default_map = (video_input_idx, video_stream)
        track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]] = [
            self._video_track_mapping(config, all_sources[video_input_idx])
        ]

        for a in config.audio_tracks:
            src_path = Path(a.source_path or config.source)
            inp = source_idx.get(src_path)
            if inp is None:
                inp = source_idx.get(config.source, 0)
            track_mappings.append(((src_path, int(a.stream_index), "audio"), all_sources[inp], int(a.stream_index)))

        for sub_path, stream_idx in resolved_subtitle_tracks:
            src_path = Path(sub_path)
            inp = source_idx.get(src_path)
            if inp is None:
                continue
            track_mappings.append(((src_path, int(stream_idx), "subtitle"), all_sources[inp], int(stream_idx)))

        next_input_index, offset_remap = self._append_offset_aux_inputs(
            cmd,
            self._build_offset_specs(
                config,
                track_mappings=track_mappings,
                offset_lookup=offset_lookup,
            ),
            start_input_index=next_input_index,
        )
        _ = next_input_index

        video_map_key = self._video_map_key(config)
        cmd.extend(["-map", self._video_map_arg(
            video_default_map,
            offset_remap=offset_remap,
            map_key=video_map_key,
        )])
        cmd.extend(self._video_codec_args(config.video, config.video.bitrate_kbps))

        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            cmd.extend(self._hdr_meta_args(config.video))

        self._append_stream_maps_and_attachments(
            cmd,
            config,
            source_idx=source_idx,
            subtitle_copy_input_indices=list(range(len(all_sources))),
            offset_remap=offset_remap,
            subtitle_tracks_override=resolved_subtitle_tracks,
            force_copy_subtitles_wildcard=(config.copy_subtitles and not subtitles_resolved),
        )

        self._append_container_metadata_args(
            cmd,
            config,
            default_metadata_input_index=0,
            default_chapter_input_index=0,
            chapter_input_index=chapter_input_index,
            tag_input_index=tag_input_index,
            include_copy_video_stream_passthrough=True,
        )
        cmd.append(str(config.output))
        return cmd

    def _build_two_pass(
        self,
        config: EncodeConfig,
        *,
        chapter_materialize_dir: Path | None = None,
    ) -> list[list[str]]:
        bitrate = self._size_to_bitrate_kbps(config)
        vf = self._build_encoder_vf(config.video)

        all_sources = self._collect_all_sources(config)
        source_idx = self._source_input_index_map(all_sources)
        offset_lookup = self._track_time_offset_lookup(config)

        def _base() -> list[str]:
            c = [self._ffmpeg, "-hide_banner", "-y"]
            c.extend(self._ffmpeg_progress_args())
            c.extend(self._hardware_input_args(config.video))
            for src in all_sources:
                c.extend(["-i", str(src)])
            if vf:
                c.extend(["-vf", vf])
            c.extend(self._ffmpeg_thread_args())
            return c

        video_source = self._video_source_path(config)
        video_stream = self._video_stream_index(config)
        video_input_idx = source_idx.get(video_source, source_idx.get(config.source, 0))
        video_default_map = (video_input_idx, video_stream)

        pass1 = _base()
        _next1, pass1_offset_remap = self._append_offset_aux_inputs(
            pass1,
            self._build_offset_specs(
                config,
                track_mappings=[self._video_track_mapping(config, all_sources[video_input_idx])],
                offset_lookup=offset_lookup,
            ),
            start_input_index=len(all_sources),
        )
        _ = _next1
        pass1_video_map_key = self._video_map_key(config)
        pass1.extend(["-map", self._video_map_arg(
            video_default_map,
            offset_remap=pass1_offset_remap,
            map_key=pass1_video_map_key,
        )])
        pass1.extend(self._video_codec_args_bitrate(config.video, bitrate))
        pass1.extend(["-pass", "1", "-an", "-f", "null", os.devnull])

        pass2 = _base()
        next_input_index, chapter_input_index, tag_input_index = self._prepare_container_metadata_inputs(
            pass2,
            config,
            source_idx=source_idx,
            next_input_index=len(all_sources),
            chapter_materialize_dir=chapter_materialize_dir,
            chapter_probe_source=config.source,
        )

        resolved_subtitle_tracks, subtitles_resolved = self._resolved_subtitle_tracks_for_encode(
            config,
            all_sources,
        )
        track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]] = [
            self._video_track_mapping(config, all_sources[video_input_idx])
        ]
        for a in config.audio_tracks:
            src_path = Path(a.source_path or config.source)
            inp = source_idx.get(src_path)
            if inp is None:
                inp = source_idx.get(config.source, 0)
            track_mappings.append(((src_path, int(a.stream_index), "audio"), all_sources[inp], int(a.stream_index)))
        for sub_path, stream_idx in resolved_subtitle_tracks:
            src_path = Path(sub_path)
            inp = source_idx.get(src_path)
            if inp is None:
                continue
            track_mappings.append(((src_path, int(stream_idx), "subtitle"), all_sources[inp], int(stream_idx)))

        next_input_index, pass2_offset_remap = self._append_offset_aux_inputs(
            pass2,
            self._build_offset_specs(
                config,
                track_mappings=track_mappings,
                offset_lookup=offset_lookup,
            ),
            start_input_index=next_input_index,
        )
        _ = next_input_index

        pass2_video_map_key = self._video_map_key(config)
        pass2.extend(["-map", self._video_map_arg(
            video_default_map,
            offset_remap=pass2_offset_remap,
            map_key=pass2_video_map_key,
        )])
        pass2.extend(self._video_codec_args_bitrate(config.video, bitrate))
        pass2.extend(["-pass", "2"])

        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            pass2.extend(self._hdr_meta_args(config.video))
        self._append_stream_maps_and_attachments(
            pass2,
            config,
            source_idx=source_idx,
            subtitle_copy_input_indices=list(range(len(all_sources))),
            offset_remap=pass2_offset_remap,
            subtitle_tracks_override=resolved_subtitle_tracks,
            force_copy_subtitles_wildcard=(config.copy_subtitles and not subtitles_resolved),
        )

        self._append_container_metadata_args(
            pass2,
            config,
            default_metadata_input_index=0,
            default_chapter_input_index=0,
            chapter_input_index=chapter_input_index,
            tag_input_index=tag_input_index,
            include_copy_video_stream_passthrough=True,
        )
        pass2.append(str(config.output))

        return [pass1, pass2]

    def _build_runtime_single_pass_with_sync(
        self,
        config: EncodeConfig,
        *,
        chapter_materialize_dir: Path | None = None,
        signals: TaskSignals | None = None,
    ) -> tuple[list[str], LiveSyncSession | None, list[Path]]:
        all_sources = self._collect_all_sources(config)
        source_idx = self._source_input_index_map(all_sources)
        work_dir = config.work_dir or config.source.parent
        offset_lookup = self._track_time_offset_lookup(config)

        sync_remap, sync_inputs, live_session, strict_interleave = self._prepare_multisource_sync(
            config=config,
            all_sources=all_sources,
            sync_base_input_idx=len(all_sources),
            work_dir=work_dir,
            signals=signals,
            allow_live=True,
        )

        cmd: list[str] = [self._ffmpeg, "-hide_banner", "-y"]
        cmd.extend(self._ffmpeg_progress_args())
        cmd.extend(self._hardware_input_args(config.video))
        for src in all_sources:
            cmd.extend(["-i", str(src)])
        self._append_sync_inputs(cmd, sync_inputs)

        next_input_index, chapter_input_index, tag_input_index = self._prepare_container_metadata_inputs(
            cmd,
            config,
            source_idx=source_idx,
            next_input_index=len(all_sources) + len(sync_inputs),
            chapter_materialize_dir=chapter_materialize_dir,
            chapter_probe_source=config.source,
        )

        vf = self._build_encoder_vf(config.video)
        if vf:
            cmd.extend(["-vf", vf])

        cmd.extend(self._ffmpeg_thread_args())

        resolved_subtitle_tracks, subtitles_resolved = self._resolved_subtitle_tracks_for_encode(
            config,
            all_sources,
        )

        track_input_paths: list[Path | str] = [*all_sources, *sync_inputs]

        def _input_path(idx: int, fallback: Path | str) -> Path | str:
            if 0 <= idx < len(track_input_paths):
                return track_input_paths[idx]
            return fallback

        video_key = self._video_map_key(config)
        video_source = self._video_source_path(config)
        video_stream = self._video_stream_index(config)
        video_default_map = sync_remap.get(
            video_key,
            (source_idx.get(video_source, source_idx.get(config.source, 0)), video_stream),
        )
        track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]] = [
            (
                video_key,
                _input_path(int(video_default_map[0]), video_source),
                int(video_default_map[1]),
            )
        ]

        for a in config.audio_tracks:
            src_path = Path(a.source_path or config.source)
            remapped = sync_remap.get((src_path, int(a.stream_index), "audio"))
            if remapped is not None:
                inp, stream_idx = remapped
            else:
                inp = source_idx.get(src_path)
                if inp is None:
                    inp = source_idx.get(config.source, 0)
                stream_idx = int(a.stream_index)
            track_mappings.append(((src_path, int(a.stream_index), "audio"), _input_path(int(inp), src_path), int(stream_idx)))

        for src_path_raw, stream_idx_raw in resolved_subtitle_tracks:
            src_path = Path(src_path_raw)
            remapped = sync_remap.get((src_path, int(stream_idx_raw), "subtitle"))
            if remapped is not None:
                inp, stream_idx = remapped
            else:
                inp = source_idx.get(src_path)
                if inp is None:
                    continue
                stream_idx = int(stream_idx_raw)
            track_mappings.append(((src_path, int(stream_idx_raw), "subtitle"), _input_path(int(inp), src_path), int(stream_idx)))

        next_input_index, offset_remap = self._append_offset_aux_inputs(
            cmd,
            self._build_offset_specs(
                config,
                track_mappings=track_mappings,
                offset_lookup=offset_lookup,
            ),
            start_input_index=next_input_index,
        )
        _ = next_input_index

        video_map_key = video_key
        cmd.extend(["-map", self._video_map_arg(
            (int(video_default_map[0]), int(video_default_map[1])),
            offset_remap=offset_remap,
            map_key=video_map_key,
        )])
        cmd.extend(self._video_codec_args(config.video, config.video.bitrate_kbps))

        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            cmd.extend(self._hdr_meta_args(config.video))

        self._append_stream_maps_and_attachments(
            cmd,
            config,
            source_idx=source_idx,
            subtitle_copy_input_indices=list(range(len(all_sources))),
            sync_remap=sync_remap,
            offset_remap=offset_remap,
            subtitle_tracks_override=resolved_subtitle_tracks,
            force_copy_subtitles_wildcard=(config.copy_subtitles and not subtitles_resolved),
        )

        if strict_interleave:
            self._append_strict_interleave_mux_flags(cmd)

        self._append_container_metadata_args(
            cmd,
            config,
            default_metadata_input_index=0,
            default_chapter_input_index=0,
            chapter_input_index=chapter_input_index,
            tag_input_index=tag_input_index,
            include_copy_video_stream_passthrough=True,
        )
        cmd.append(str(config.output))
        return cmd, live_session, self._sync_cleanup_paths(sync_inputs)

    def _build_runtime_two_pass_with_sync(
        self,
        config: EncodeConfig,
        *,
        chapter_materialize_dir: Path | None = None,
        signals: TaskSignals | None = None,
    ) -> tuple[list[list[str]], LiveSyncSession | None, list[Path]]:
        bitrate = self._size_to_bitrate_kbps(config)
        vf = self._build_encoder_vf(config.video)

        all_sources = self._collect_all_sources(config)
        source_idx = self._source_input_index_map(all_sources)
        work_dir = config.work_dir or config.source.parent
        offset_lookup = self._track_time_offset_lookup(config)

        sync_remap, sync_inputs, live_session, strict_interleave = self._prepare_multisource_sync(
            config=config,
            all_sources=all_sources,
            sync_base_input_idx=len(all_sources),
            work_dir=work_dir,
            signals=signals,
            allow_live=False,
        )

        def _base(include_sync_inputs: bool) -> list[str]:
            c = [self._ffmpeg, "-hide_banner", "-y"]
            c.extend(self._ffmpeg_progress_args())
            c.extend(self._hardware_input_args(config.video))
            for src in all_sources:
                c.extend(["-i", str(src)])
            if include_sync_inputs:
                self._append_sync_inputs(c, sync_inputs)
            if vf:
                c.extend(["-vf", vf])
            c.extend(self._ffmpeg_thread_args())
            return c

        video_key = self._video_map_key(config)
        video_source = self._video_source_path(config)
        video_stream = self._video_stream_index(config)
        video_default_map = (
            source_idx.get(video_source, source_idx.get(config.source, 0)),
            video_stream,
        )

        pass1 = _base(False)
        _next1, pass1_offset_remap = self._append_offset_aux_inputs(
            pass1,
            self._build_offset_specs(
                config,
                track_mappings=[self._video_track_mapping(config, video_source)],
                offset_lookup=offset_lookup,
            ),
            start_input_index=len(all_sources),
        )
        _ = _next1
        pass1_video_map_key = video_key
        pass1.extend(["-map", self._video_map_arg(
            video_default_map,
            offset_remap=pass1_offset_remap,
            map_key=pass1_video_map_key,
        )])
        pass1.extend(self._video_codec_args_bitrate(config.video, bitrate))
        pass1.extend(["-pass", "1", "-an", "-f", "null", os.devnull])

        pass2 = _base(True)
        next_input_index, chapter_input_index, tag_input_index = self._prepare_container_metadata_inputs(
            pass2,
            config,
            source_idx=source_idx,
            next_input_index=len(all_sources) + len(sync_inputs),
            chapter_materialize_dir=chapter_materialize_dir,
            chapter_probe_source=config.source,
        )

        resolved_subtitle_tracks, subtitles_resolved = self._resolved_subtitle_tracks_for_encode(
            config,
            all_sources,
        )

        track_input_paths: list[Path | str] = [*all_sources, *sync_inputs]

        def _input_path(idx: int, fallback: Path | str) -> Path | str:
            if 0 <= idx < len(track_input_paths):
                return track_input_paths[idx]
            return fallback

        video_base_map = sync_remap.get(video_key, video_default_map)
        track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]] = [
            (
                video_key,
                _input_path(int(video_base_map[0]), video_source),
                int(video_base_map[1]),
            )
        ]

        for a in config.audio_tracks:
            src_path = Path(a.source_path or config.source)
            remapped = sync_remap.get((src_path, int(a.stream_index), "audio"))
            if remapped is not None:
                inp, stream_idx = remapped
            else:
                inp = source_idx.get(src_path)
                if inp is None:
                    inp = source_idx.get(config.source, 0)
                stream_idx = int(a.stream_index)
            track_mappings.append(((src_path, int(a.stream_index), "audio"), _input_path(int(inp), src_path), int(stream_idx)))

        for src_path_raw, stream_idx_raw in resolved_subtitle_tracks:
            src_path = Path(src_path_raw)
            remapped = sync_remap.get((src_path, int(stream_idx_raw), "subtitle"))
            if remapped is not None:
                inp, stream_idx = remapped
            else:
                inp = source_idx.get(src_path)
                if inp is None:
                    continue
                stream_idx = int(stream_idx_raw)
            track_mappings.append(((src_path, int(stream_idx_raw), "subtitle"), _input_path(int(inp), src_path), int(stream_idx)))

        next_input_index, pass2_offset_remap = self._append_offset_aux_inputs(
            pass2,
            self._build_offset_specs(
                config,
                track_mappings=track_mappings,
                offset_lookup=offset_lookup,
            ),
            start_input_index=next_input_index,
        )
        _ = next_input_index

        pass2_video_map_key = video_key
        pass2.extend(["-map", self._video_map_arg(
            (int(video_base_map[0]), int(video_base_map[1])),
            offset_remap=pass2_offset_remap,
            map_key=pass2_video_map_key,
        )])
        pass2.extend(self._video_codec_args_bitrate(config.video, bitrate))
        pass2.extend(["-pass", "2"])

        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            pass2.extend(self._hdr_meta_args(config.video))
        self._append_stream_maps_and_attachments(
            pass2,
            config,
            source_idx=source_idx,
            subtitle_copy_input_indices=list(range(len(all_sources))),
            sync_remap=sync_remap,
            offset_remap=pass2_offset_remap,
            subtitle_tracks_override=resolved_subtitle_tracks,
            force_copy_subtitles_wildcard=(config.copy_subtitles and not subtitles_resolved),
        )
        if strict_interleave:
            self._append_strict_interleave_mux_flags(pass2)
        self._append_container_metadata_args(
            pass2,
            config,
            default_metadata_input_index=0,
            default_chapter_input_index=0,
            chapter_input_index=chapter_input_index,
            tag_input_index=tag_input_index,
            include_copy_video_stream_passthrough=True,
        )
        pass2.append(str(config.output))
        return [pass1, pass2], live_session, self._sync_cleanup_paths(sync_inputs)

    # ------------------------------------------------------------------
    # Arguments par codec
    # ------------------------------------------------------------------

    def _video_codec_args(self, v: VideoEncodeSettings, bitrate_kbps: int) -> list[str]:
        if v.quality_mode == QualityMode.CRF:
            return self._video_codec_args_crf(v)
        return self._video_codec_args_bitrate(v, bitrate_kbps)

    @staticmethod
    def _is_h264_codec(codec: str) -> bool:
        normalized = str(codec or "").strip().lower()
        return normalized == "libx264" or normalized.startswith("h264_")

    def _force_h264_8bit(self, v: VideoEncodeSettings) -> bool:
        return bool(getattr(v, "force_8bit", False)) and self._is_h264_codec(v.codec)

    def _h264_8bit_pix_fmt_args(self, v: VideoEncodeSettings) -> list[str]:
        if not self._force_h264_8bit(v):
            return []
        if v.codec == "libx264":
            return ["-pix_fmt", "yuv420p"]
        return ["-pix_fmt", "nv12"]

    def _video_codec_args_crf(self, v: VideoEncodeSettings) -> list[str]:
        match v.codec:
            case "copy":
                return ["-c:v", "copy"]
            case "libx265":
                args = ["-c:v", "libx265", "-crf", str(v.crf), "-preset", v.preset]
                x265 = self._x265_params(v)
                if x265:
                    args.extend(["-x265-params", x265])
                return args
            case "libx264":
                args = ["-c:v", "libx264", "-crf", str(v.crf), "-preset", v.preset]
                args.extend(self._h264_8bit_pix_fmt_args(v))
                return args
            case "libsvtav1":
                args = ["-c:v", "libsvtav1", "-crf", str(v.crf), "-preset", v.preset]
                if v.extra_params:
                    args.extend(["-svtav1-params", v.extra_params])
                return args
            case "hevc_nvenc":
                return [
                    "-c:v", "hevc_nvenc", "-rc:v", "vbr", "-cq:v", str(v.crf), "-preset:v", v.preset,
                    *self._nvenc_device_args(),
                ]
            case "hevc_amf":
                args = ["-c:v", "hevc_amf", "-rc", "cqp", "-qp_p", str(v.crf), "-qp_i", str(v.crf)]
                if v.preset:
                    args.extend(["-quality", v.preset])
                return args
            case "hevc_qsv":
                args = ["-c:v", "hevc_qsv", "-global_quality", str(v.crf),
                        "-look_ahead", "1", "-async_depth", "4"]
                if v.preset:
                    args.extend(["-preset", v.preset])
                return args
            case "hevc_vaapi":
                return ["-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", str(v.crf),
                        "-compression_level", (v.preset or "4"),
                        "-async_depth", "4"]
            case "h264_nvenc":
                return [
                    "-c:v", "h264_nvenc", "-rc:v", "vbr", "-cq:v", str(v.crf), "-preset:v", v.preset,
                    *self._nvenc_device_args(),
                    *self._h264_8bit_pix_fmt_args(v),
                ]
            case "h264_amf":
                args = ["-c:v", "h264_amf", "-rc", "cqp", "-qp_p", str(v.crf), "-qp_i", str(v.crf)]
                if v.preset:
                    args.extend(["-quality", v.preset])
                args.extend(self._h264_8bit_pix_fmt_args(v))
                return args
            case "h264_qsv":
                args = ["-c:v", "h264_qsv", "-global_quality", str(v.crf), "-async_depth", "4"]
                if v.preset:
                    args.extend(["-preset", v.preset])
                args.extend(self._h264_8bit_pix_fmt_args(v))
                return args
            case "h264_vaapi":
                return [
                    "-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", str(v.crf),
                    "-compression_level", (v.preset or "4"),
                    "-async_depth", "4",
                    *self._h264_8bit_pix_fmt_args(v),
                ]
            case "av1_nvenc":
                return [
                    "-c:v", "av1_nvenc", "-rc:v", "vbr", "-cq:v", str(v.crf), "-preset:v", v.preset,
                    *self._nvenc_device_args(),
                ]
            case "av1_amf":
                args = ["-c:v", "av1_amf", "-rc", "cqp", "-qp_p", str(v.crf), "-qp_i", str(v.crf)]
                if v.preset:
                    args.extend(["-quality", v.preset])
                return args
            case "av1_qsv":
                args = ["-c:v", "av1_qsv", "-global_quality", str(v.crf), "-async_depth", "4"]
                if v.preset:
                    args.extend(["-preset", v.preset])
                return args
            case "av1_vaapi":
                return ["-c:v", "av1_vaapi", "-rc_mode", "CQP", "-qp", str(v.crf),
                        "-compression_level", (v.preset or "4"),
                        "-async_depth", "4"]
            case _:
                return ["-c:v", v.codec, "-crf", str(v.crf)]

    def _video_codec_args_bitrate(self, v: VideoEncodeSettings, bitrate_kbps: int) -> list[str]:
        match v.codec:
            case "copy":
                return ["-c:v", "copy"]
            case "libx265":
                args = ["-c:v", "libx265", "-b:v", f"{bitrate_kbps}k", "-preset", v.preset]
                x265 = self._x265_params(v)
                if x265:
                    args.extend(["-x265-params", x265])
                return args
            case "libx264":
                return [
                    "-c:v", "libx264", "-b:v", f"{bitrate_kbps}k", "-preset", v.preset,
                    *self._h264_8bit_pix_fmt_args(v),
                ]
            case "libsvtav1":
                return ["-c:v", "libsvtav1", "-b:v", f"{bitrate_kbps}k", "-preset", v.preset]
            case "hevc_nvenc":
                return [
                    "-c:v", "hevc_nvenc", "-b:v", f"{bitrate_kbps}k", "-preset:v", v.preset,
                    *self._nvenc_device_args(),
                ]
            case "hevc_amf":
                args = ["-c:v", "hevc_amf", "-b:v", f"{bitrate_kbps}k"]
                if v.preset:
                    args.extend(["-quality", v.preset])
                return args
            case "hevc_qsv":
                args = ["-c:v", "hevc_qsv", "-b:v", f"{bitrate_kbps}k", "-async_depth", "4"]
                if v.preset:
                    args.extend(["-preset", v.preset])
                return args
            case "hevc_vaapi":
                return ["-c:v", "hevc_vaapi", "-rc_mode", "VBR", "-b:v", f"{bitrate_kbps}k",
                        "-compression_level", (v.preset or "4"),
                        "-async_depth", "4"]
            case "h264_nvenc":
                return [
                    "-c:v", "h264_nvenc", "-b:v", f"{bitrate_kbps}k", "-preset:v", v.preset,
                    *self._nvenc_device_args(),
                    *self._h264_8bit_pix_fmt_args(v),
                ]
            case "h264_amf":
                args = ["-c:v", "h264_amf", "-b:v", f"{bitrate_kbps}k"]
                if v.preset:
                    args.extend(["-quality", v.preset])
                args.extend(self._h264_8bit_pix_fmt_args(v))
                return args
            case "h264_qsv":
                args = ["-c:v", "h264_qsv", "-b:v", f"{bitrate_kbps}k", "-async_depth", "4"]
                if v.preset:
                    args.extend(["-preset", v.preset])
                args.extend(self._h264_8bit_pix_fmt_args(v))
                return args
            case "h264_vaapi":
                return [
                    "-c:v", "h264_vaapi", "-rc_mode", "VBR", "-b:v", f"{bitrate_kbps}k",
                    "-compression_level", (v.preset or "4"),
                    "-async_depth", "4",
                    *self._h264_8bit_pix_fmt_args(v),
                ]
            case "av1_nvenc":
                return [
                    "-c:v", "av1_nvenc", "-b:v", f"{bitrate_kbps}k", "-preset:v", v.preset,
                    *self._nvenc_device_args(),
                ]
            case "av1_amf":
                args = ["-c:v", "av1_amf", "-b:v", f"{bitrate_kbps}k"]
                if v.preset:
                    args.extend(["-quality", v.preset])
                return args
            case "av1_qsv":
                args = ["-c:v", "av1_qsv", "-b:v", f"{bitrate_kbps}k", "-async_depth", "4"]
                if v.preset:
                    args.extend(["-preset", v.preset])
                return args
            case "av1_vaapi":
                return ["-c:v", "av1_vaapi", "-rc_mode", "VBR", "-b:v", f"{bitrate_kbps}k",
                        "-compression_level", (v.preset or "4"),
                        "-async_depth", "4"]
            case _:
                return ["-c:v", v.codec, "-b:v", f"{bitrate_kbps}k"]

    def _build_vf(self, v: VideoEncodeSettings) -> str:
        """Filtre vidéo pour le tone mapping HDR→SDR (BT.2020 PQ → BT.709)."""
        if not v.tonemap_to_sdr:
            return ""
        algo = v.tonemap_algorithm or "hable"
        return (
            "zscale=transfer=linear:npl=100,"
            "format=gbrpf32le,"
            "zscale=primaries=bt709,"
            f"tonemap=tonemap={algo}:desat=0,"
            "zscale=transfer=bt709:matrix=bt709:range=tv,"
            "format=yuv420p"
        )

    def _build_encoder_vf(self, v: VideoEncodeSettings) -> str:
        """
        Retourne la chaîne de filtres finale adaptée au codec de sortie.

        Encodeurs VAAPI :
          - Sans tone-mapping : décodage déjà fait en VAAPI (frames sur GPU),
            pas besoin de `hwupload` — l'encodeur `*_vaapi` consomme la
            surface directement.
          - Avec tone-mapping : zscale/tonemap tournent en CPU et produisent
            des frames nv12 CPU, il faut alors remonter sur GPU via
            `format=nv12,hwupload`.

        Précheck H.264 10-bit :
          - si `force_8bit` est actif, on force une conversion 8-bit.
        """
        vf = self._build_vf(v)
        force_h264_8bit = self._force_h264_8bit(v)
        if v.codec not in _VAAPI_CODECS:
            if (
                sys.platform == "win32"
                and v.codec in _AMF_CODECS
                and self._amf_device() is not None
                and (v.tonemap_to_sdr or force_h264_8bit)
            ):
                amf_upload = "format=nv12,hwupload"
                if force_h264_8bit:
                    if vf and "format=yuv420p" not in {part.strip() for part in vf.split(",")}:
                        vf = f"{vf},format=yuv420p"
                    elif not vf:
                        vf = "format=yuv420p"
                return f"{vf},{amf_upload}" if vf else amf_upload
            if force_h264_8bit:
                force_8bit_filter = "format=yuv420p"
                if vf and force_8bit_filter in {part.strip() for part in vf.split(",")}:
                    return vf
                return f"{vf},{force_8bit_filter}" if vf else force_8bit_filter
            return vf
        if v.tonemap_to_sdr or force_h264_8bit:
            vaapi_upload = "format=nv12,hwupload"
            return f"{vf},{vaapi_upload}" if vf else vaapi_upload
        return vf

    def _hardware_input_args(self, v: VideoEncodeSettings) -> list[str]:
        """Flags ffmpeg requis avant les entrées pour certains encodeurs matériels.

        - `-vaapi_device` : option globale pour les encodeurs `_vaapi`.
        - `-hwaccel` : option d'entrée appliquée au prochain `-i` uniquement
          (la source vidéo principale est toujours le premier input). Permet
          de décoder en GPU et d'envoyer les frames directement à l'encodeur
          sans passer par le CPU.
        - Tone-mapping HDR→SDR : utilise le pipeline zscale CPU, donc on
          désactive l'option `hwaccel_output_format` (le décodage hardware
          reste possible mais ffmpeg fait un download automatique).
        """
        args: list[str] = []
        tonemap = bool(v.tonemap_to_sdr)
        force_h264_8bit = self._force_h264_8bit(v)

        if v.codec in _VAAPI_CODECS:
            vaapi_device = self._vaapi_device()
            if vaapi_device:
                args.extend(["-vaapi_device", vaapi_device])
                if not tonemap and not force_h264_8bit:
                    args.extend([
                        "-hwaccel", "vaapi",
                        "-hwaccel_output_format", "vaapi",
                    ])
            return args

        if v.codec in _QSV_CODECS:
            qsv_device = self._qsv_device()
            if qsv_device:
                args.extend(["-qsv_device", qsv_device])
            if tonemap or force_h264_8bit:
                return args
            args.extend([
                "-hwaccel", "qsv",
                "-hwaccel_output_format", "qsv",
            ])
            return args

        if v.codec in _AMF_CODECS and sys.platform == "win32":
            amf_device = self._amf_device()
            if amf_device:
                args.extend([
                    "-init_hw_device", f"d3d11va=mre_amf:{amf_device}",
                    "-filter_hw_device", "mre_amf",
                ])
            if tonemap or force_h264_8bit:
                return args
            if amf_device:
                args.extend([
                    "-hwaccel", "d3d11va",
                    "-hwaccel_device", "mre_amf",
                    "-hwaccel_output_format", "d3d11",
                ])
            else:
                args.extend([
                    "-hwaccel", "d3d11va",
                    "-hwaccel_output_format", "d3d11",
                ])
            return args

        if tonemap or force_h264_8bit:
            return args

        if v.codec in _NVENC_CODECS:
            if sys.platform == "win32":
                nvenc_device = self._nvenc_device()
                if nvenc_device:
                    args.extend(["-hwaccel_device", nvenc_device])
            args.extend([
                "-hwaccel", "cuda",
                "-hwaccel_output_format", "cuda",
            ])

        return args

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

    def _x265_params(self, v: VideoEncodeSettings) -> str:
        """
        Construit la valeur de -x265-params en fusionnant extra_params et les
        métadonnées HDR10 statiques (master-display, max-cll) si inject_hdr_meta est actif.

        Retourne une chaîne vide si aucun paramètre n'est à passer.
        """
        parts: list[str] = []
        if v.extra_params:
            parts.append(v.extra_params.strip(":"))
        if v.inject_hdr_meta and not v.tonemap_to_sdr:
            if v.master_display:
                parts.append(f"master-display={v.master_display}")
            if v.max_cll:
                parts.append(f"max-cll={v.max_cll}")
        return ":".join(p for p in parts if p)

    def _hdr_meta_args(self, v: VideoEncodeSettings) -> list[str]:
        """
        Flags de couleur container-level + métadonnées SEI selon le codec.

        Couleur (valides pour tout codec HEVC/AV1 re-encodé) :
            -color_primaries bt2020  -color_trc smpte2084  -colorspace bt2020nc

        master_display / max_cll par codec :
            libx265          → injectés via -x265-params (dans _video_codec_args_crf/bitrate)
            hevc_nvenc        → options privées du codec (-master_display / -max_cll)
            hevc_amf, hevc_qsv, libsvtav1 → pas de mécanisme standardisé → ignorés
            copy, h264_*, libx264 → couleur non applicable / pas de HDR10 → rien
        """
        if v.codec in ("copy", "libx264", "h264_nvenc", "h264_amf", "h264_qsv"):
            return []
        args = ["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"]
        if v.codec == "hevc_nvenc":
            if v.master_display:
                args.extend(["-master_display", v.master_display])
            if v.max_cll:
                args.extend(["-max_cll", v.max_cll])
        # libx265 : master_display/max_cll déjà fusionnés dans -x265-params
        # hevc_amf, hevc_qsv, libsvtav1 : couleur only, SEI non géré
        return args

    def _audio_codec_args(self, out_idx: int, a: AudioTrackSettings) -> list[str]:
        args: list[str] = []
        needs_downmix_51 = self._needs_ac3_51_downmix(a)
        bitrate_kbps = normalize_audio_bitrate_kbps(
            a.codec,
            a.bitrate_kbps,
            a.input_channels,
            None,
            a.input_channel_layout,
        )
        match a.codec:
            case "copy":
                args.extend([f"-c:a:{out_idx}", "copy"])
                # truehd_core est un bitstream filter de passthrough.
                # Sur une piste réencodée, ffmpeg l'applique à la sortie encodée
                # (ex. eac3) et échoue car le codec n'est plus TrueHD.
                if a.extract_truehd_core:
                    args.extend([f"-bsf:a:{out_idx}", "truehd_core"])
            case "aac":
                args.extend([f"-c:a:{out_idx}", "aac", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
            case "ac3":
                args.extend([f"-c:a:{out_idx}", "ac3", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
            case "eac3":
                args.extend([f"-c:a:{out_idx}", "eac3", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
            case "flac":
                args.extend([f"-c:a:{out_idx}", "flac"])
            case _:
                args.extend([f"-c:a:{out_idx}", a.codec])
        if needs_downmix_51:
            args.extend([f"-ac:a:{out_idx}", "6", f"-channel_layout:a:{out_idx}", "5.1"])
        return args

    @staticmethod
    def _needs_ac3_51_downmix(a: AudioTrackSettings) -> bool:
        """
        Retourne True si la piste source doit être ramenée en 5.1 pour AC-3/E-AC-3.

        Règle métier :
          - si codec cible ∈ {ac3, eac3}
          - et source 7.1 (8 canaux ou layout contenant "7.1")
        """
        if a.codec not in {"ac3", "eac3"}:
            return False
        if (a.input_channels or 0) >= 8:
            return True
        layout = (a.input_channel_layout or "").lower()
        return "7.1" in layout

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
        vf = self._build_encoder_vf(video)

        def _base() -> list[str]:
            cmd = [self._ffmpeg, "-hide_banner", "-y"]
            cmd.extend(self._ffmpeg_progress_args())
            cmd.extend(self._offset_input_args(offset_ms))
            cmd.extend(self._hardware_input_args(video))
            cmd.extend(["-i", str(source)])
            if vf:
                cmd.extend(["-vf", vf])
            cmd.extend(self._ffmpeg_thread_args(thread_count))
            cmd.extend(["-map", f"0:{self._video_stream_from_settings(video)}"])
            return cmd

        if video.quality_mode == QualityMode.SIZE:
            bitrate = self._size_to_bitrate_kbps_for_video(config, video)
            pass1 = _base()
            pass1.extend(self._video_codec_args_bitrate(video, bitrate))
            if passlog_prefix is not None:
                pass1.extend(["-passlogfile", str(passlog_prefix)])
            pass1.extend(["-pass", "1", "-an", "-sn", "-dn", "-f", "null", os.devnull])

            pass2 = _base()
            pass2.extend(self._video_codec_args_bitrate(video, bitrate))
            if passlog_prefix is not None:
                pass2.extend(["-passlogfile", str(passlog_prefix)])
            pass2.extend(["-pass", "2"])
            if video.inject_hdr_meta and not video.tonemap_to_sdr:
                pass2.extend(self._hdr_meta_args(video))
            pass2.extend(["-an", "-sn", "-dn", str(output_path)])
            return [pass1, pass2]

        cmd = _base()
        cmd.extend(self._video_codec_args(video, video.bitrate_kbps))
        if video.inject_hdr_meta and not video.tonemap_to_sdr:
            cmd.extend(self._hdr_meta_args(video))
        cmd.extend(["-an", "-sn", "-dn", str(output_path)])
        return [cmd]

    def _build_video_only_cmd(self, config: EncodeConfig, output_hevc: Path) -> list[str]:
        """
        ffmpeg : vidéo seule, sortie HEVC brut (-f hevc, sans container).
        Pas d'audio ni de subs. Utilisé pour encoder directement vers un
        flux HEVC injectable, sans passer par un MKV intermédiaire.
        """
        cmd = [self._ffmpeg, "-hide_banner", "-y"]
        cmd.extend(self._ffmpeg_progress_args())
        cmd.extend(self._hardware_input_args(config.video))
        cmd.extend(["-i", str(self._video_source_path(config))])
        vf = self._build_encoder_vf(config.video)
        if vf:
            cmd.extend(["-vf", vf])
        cmd.extend(self._ffmpeg_thread_args())
        cmd.extend(["-map", f"0:{self._video_stream_index(config)}"])
        cmd.extend(self._video_codec_args(config.video, config.video.bitrate_kbps))
        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            cmd.extend(self._hdr_meta_args(config.video))
        cmd.extend(["-an", "-f", "hevc", str(output_hevc)])
        return cmd

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
        cmd = [self._ffmpeg, "-hide_banner", "-y"]
        cmd.extend(self._ffmpeg_progress_args())
        cmd.extend(self._offset_input_args(offset_ms))
        cmd.extend(self._hardware_input_args(video))
        cmd.extend(["-i", str(source)])
        vf = self._build_encoder_vf(video)
        if vf:
            cmd.extend(["-vf", vf])
        cmd.extend(self._ffmpeg_thread_args(thread_count))
        cmd.extend(["-map", f"0:{self._video_stream_from_settings(video)}"])
        cmd.extend(self._video_codec_args(video, video.bitrate_kbps))
        if video.inject_hdr_meta and not video.tonemap_to_sdr:
            cmd.extend(self._hdr_meta_args(video))
        cmd.extend(["-an", "-f", "hevc", str(output_hevc)])
        return cmd

    def _build_video_only_two_pass(
        self, config: EncodeConfig, output_hevc: Path
    ) -> list[list[str]]:
        """
        Deux passes ffmpeg : vidéo seule, sortie HEVC brut.
        Utilisé en mode SIZE pour l'étape vidéo de _run_with_metadata_inject.
        """
        bitrate = self._size_to_bitrate_kbps(config)
        vf = self._build_encoder_vf(config.video)

        def _base() -> list[str]:
            c = [self._ffmpeg, "-hide_banner", "-y"]
            c.extend(self._ffmpeg_progress_args())
            c.extend(self._hardware_input_args(config.video))
            c.extend(["-i", str(self._video_source_path(config))])
            if vf:
                c.extend(["-vf", vf])
            c.extend(self._ffmpeg_thread_args())
            c.extend(["-map", f"0:{self._video_stream_index(config)}"])
            c.extend(self._video_codec_args_bitrate(config.video, bitrate))
            return c

        pass1 = _base() + ["-pass", "1", "-an", "-f", "null", os.devnull]
        pass2 = _base() + ["-pass", "2"]
        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            pass2.extend(self._hdr_meta_args(config.video))
        pass2.extend(["-an", "-f", "hevc", str(output_hevc)])
        return [pass1, pass2]

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
    ) -> list[list[str]]:
        bitrate = self._size_to_bitrate_kbps_for_video(config, video)
        vf = self._build_encoder_vf(video)

        def _base() -> list[str]:
            c = [self._ffmpeg, "-hide_banner", "-y"]
            c.extend(self._ffmpeg_progress_args())
            c.extend(self._offset_input_args(offset_ms))
            c.extend(self._hardware_input_args(video))
            c.extend(["-i", str(source)])
            if vf:
                c.extend(["-vf", vf])
            c.extend(self._ffmpeg_thread_args(thread_count))
            c.extend(["-map", f"0:{self._video_stream_from_settings(video)}"])
            c.extend(self._video_codec_args_bitrate(video, bitrate))
            return c

        pass1 = _base()
        if passlog_prefix is not None:
            pass1.extend(["-passlogfile", str(passlog_prefix)])
        pass1.extend(["-pass", "1", "-an", "-f", "null", os.devnull])
        pass2 = _base()
        if passlog_prefix is not None:
            pass2.extend(["-passlogfile", str(passlog_prefix)])
        pass2.extend(["-pass", "2"])
        if video.inject_hdr_meta and not video.tonemap_to_sdr:
            pass2.extend(self._hdr_meta_args(video))
        pass2.extend(["-an", "-f", "hevc", str(output_hevc)])
        return [pass1, pass2]

    def _size_to_bitrate_kbps(self, config: EncodeConfig) -> int:
        duration = config.duration_s or 3600.0
        total_bits = config.video.target_size_mb * 8 * 1024 * 1024
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

    @staticmethod
    def _total_ram_bytes() -> int:
        """
        Retourne la RAM physique totale en octets.
        Linux : /proc/meminfo · macOS : sysctl hw.memsize · Windows : ctypes GlobalMemoryStatusEx.
        Retourne 0 si la valeur ne peut pas être lue.
        """
        try:
            if sys.platform == "linux":
                text = Path("/proc/meminfo").read_text(encoding="ascii")
                m = re.search(r"MemTotal:\s+(\d+)\s+kB", text)
                return int(m.group(1)) * 1024 if m else 0
            if sys.platform == "darwin":
                r = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, check=False, timeout=5, **subprocess_text_kwargs(),
                )
                v = r.stdout.strip()
                return int(v) if r.returncode == 0 and v.isdigit() else 0
            if sys.platform == "win32":
                return EncodeWorkflow._win_mem_status().ullTotalPhys
        except Exception:
            pass
        return 0

    @staticmethod
    def _available_ram_bytes() -> int:
        """
        Retourne la RAM disponible en octets (MemAvailable sur Linux, équivalent sur macOS/Windows).
        Retourne 0 si non déterminable.
        """
        try:
            if sys.platform == "linux":
                text = Path("/proc/meminfo").read_text(encoding="ascii")
                m = re.search(r"MemAvailable:\s+(\d+)\s+kB", text)
                return int(m.group(1)) * 1024 if m else 0
            if sys.platform == "darwin":
                return EncodeWorkflow._macos_available_ram()
            if sys.platform == "win32":
                return EncodeWorkflow._win_mem_status().ullAvailPhys
        except Exception:
            pass
        return 0

    @staticmethod
    def _macos_available_ram() -> int:
        """RAM disponible sur macOS via vm_stat (free + inactive + speculative + purgeable)."""
        r = subprocess.run(
            ["vm_stat"], capture_output=True, check=False, timeout=5, **subprocess_text_kwargs()
        )
        if r.returncode != 0:
            return 0
        page_m = re.search(r"page size of (\d+) bytes", r.stdout)
        page = int(page_m.group(1)) if page_m else 4096
        pages = 0
        for field in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
            m = re.search(rf"{re.escape(field)}:\s*(\d+)", r.stdout)
            if m:
                pages += int(m.group(1))
        return pages * page

    @staticmethod
    def _win_mem_status():
        """Retourne une structure MEMORYSTATUSEX remplie (Windows uniquement)."""
        class _MEMSTATEX(ctypes.Structure):
            _fields_ = [
                ("dwLength",                ctypes.c_ulong),
                ("dwMemoryLoad",            ctypes.c_ulong),
                ("ullTotalPhys",            ctypes.c_ulonglong),
                ("ullAvailPhys",            ctypes.c_ulonglong),
                ("ullTotalPageFile",        ctypes.c_ulonglong),
                ("ullAvailPageFile",        ctypes.c_ulonglong),
                ("ullTotalVirtual",         ctypes.c_ulonglong),
                ("ullAvailVirtual",         ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = _MEMSTATEX()
        stat.dwLength = ctypes.sizeof(stat)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        return stat

    @staticmethod
    def _ram_buffer_dir() -> Path | None:
        """
        Retourne le répertoire RAM-backed disponible sur cette plateforme, ou None.

        · Linux  : /dev/shm (tmpfs kernel, taille = RAM physique)
        · macOS  : /dev/shm (POSIX shm namespace, writable sur macOS ≥ 10.15)
        · Windows: aucun équivalent standard → None (buffer sur disque uniquement)
        """
        if sys.platform in ("linux", "darwin"):
            shm = Path("/dev/shm")
            if shm.is_dir() and os.access(shm, os.W_OK):
                return shm
        return None

    def _shm_path(self, tmp: Path, name: str, file_size: int) -> Path:
        """
        Retourne un chemin dans le répertoire RAM si les conditions sont réunies,
        sinon un chemin dans tmp (disque).

        Conditions (toutes requises) :
          1. ram_buffer_enabled = True (configuration)
          2. Un répertoire RAM existe sur cette plateforme (_ram_buffer_dir())
          3. RAM disponible après chargement ≥ threshold_pct % de la RAM totale
             formule : available_before - file_size ≥ total_ram × threshold_pct / 100

        La décision est réévaluée à chaque appel (RAM dynamique).
        """
        if not self._ram_buffer_enabled:
            return tmp / name
        ram_dir = EncodeWorkflow._ram_buffer_dir()
        if ram_dir is None:
            return tmp / name
        total     = EncodeWorkflow._total_ram_bytes()
        available = EncodeWorkflow._available_ram_bytes()
        if total <= 0 or available <= 0:
            return tmp / name
        min_free_after = int(total * self._ram_buffer_threshold_pct / 100)
        if available - file_size >= min_free_after:
            return ram_dir / name
        return tmp / name

    # ------------------------------------------------------------------
    # Aperçu lisible
    # ------------------------------------------------------------------

    def preview_command(self, config: EncodeConfig) -> str:
        if self._is_multi_video(config):
            commands = self._build_multi_video_command_preview(config)
            blocks: list[str] = []
            for index, cmd in enumerate(commands, start=1):
                if not cmd:
                    continue
                lines = [cmd[0]]
                i = 1
                while i < len(cmd):
                    p = cmd[i]
                    if p.startswith("-") and i + 1 < len(cmd) and not cmd[i + 1].startswith("-"):
                        lines.append(f"    {p} {cmd[i + 1]}")
                        i += 2
                    else:
                        lines.append(f"    {p}")
                        i += 1
                blocks.append(f"# Commande {index}\n" + " \\\n".join(lines))
            return "\n\n".join(blocks)

        cmd = self.build_command_single(config)
        if not cmd:
            return ""
        prefix = (
            "# Mode taille cible : passe 1 omise de cet aperçu\n"
            if self._uses_two_pass(config)
            else ""
        )
        lines = [cmd[0]]
        i = 1
        while i < len(cmd):
            p = cmd[i]
            if p.startswith("-") and i + 1 < len(cmd) and not cmd[i + 1].startswith("-"):
                lines.append(f"    {p} {cmd[i + 1]}")
                i += 2
            else:
                lines.append(f"    {p}")
                i += 1
        return prefix + " \\\n".join(lines)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, config: EncodeConfig) -> list[str]:
        errors: list[str] = []
        if not config.source.is_file():
            errors.append(f"Fichier source introuvable : {config.source}")
        video_tracks = self._video_tracks(config)
        if not video_tracks:
            errors.append("Aucune piste vidéo sélectionnée.")
            return errors
        for index, video in enumerate(video_tracks, start=1):
            source = self._video_source_from_settings(config, video)
            if not source.is_file():
                errors.append(f"Piste vidéo #{index} — source introuvable : {source}")
            if video.codec == "copy" and (video.inject_hdr_meta or video.tonemap_to_sdr):
                errors.append(
                    f"Piste vidéo #{index} — codec copy incompatible avec HDR statique ou tone-mapping."
                )
            if (video.copy_dv or video.copy_hdr10plus) and video.codec not in {"copy", "libx265", "hevc_nvenc", "hevc_amf", "hevc_qsv", "hevc_vaapi"}:
                errors.append(
                    f"Piste vidéo #{index} — DoVi/HDR10+ exige une sortie HEVC ou copy."
                )
        output_dir = config.output.parent
        if not output_dir.exists():
            errors.append(f"Dossier de sortie inexistant : {output_dir}")
        elif not self._is_dir_writable(output_dir):
            errors.append(
                "Dossier de sortie non inscriptible : "
                f"{output_dir} (vérifiez les protections Windows sur les dossiers Bibliothèques)."
            )
        if config.source == config.output:
            errors.append("Le fichier de sortie doit être différent du fichier source.")
        if any(v.quality_mode == QualityMode.SIZE and v.codec != "copy" for v in video_tracks) and not (config.duration_s or 0) > 0:
            errors.append("Durée du fichier source inconnue — mode taille cible impossible.")
        for index, video in enumerate(video_tracks, start=1):
            if video.inject_hdr_meta and not video.tonemap_to_sdr:
                if video.master_display and not re.match(
                r"^G\(\d+,\d+\)B\(\d+,\d+\)R\(\d+,\d+\)WP\(\d+,\d+\)L\(\d+,\d+\)$",
                    video.master_display.strip(),
            ):
                    errors.append(
                        f"Piste vidéo #{index} — format master_display invalide. "
                        "Attendu : G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)"
                    )
                if video.max_cll and not re.match(r"^\d+,\d+$", video.max_cll.strip()):
                    errors.append(
                        f"Piste vidéo #{index} — format MaxCLL invalide. Attendu : MaxCLL,MaxFALL  ex. 1000,400"
                    )

        for raw in config.track_time_offsets:
            if not isinstance(raw, TrackTimeOffset):
                continue
            track_type = str(raw.track_type or "").strip().lower()
            if track_type == "video" and int(raw.offset_ms) < 0:
                errors.append(
                    "Décalage vidéo négatif interdit : "
                    f"source={Path(raw.source_path)}, stream={int(raw.stream_index)}, "
                    f"offset={int(raw.offset_ms)} ms"
                )
        return errors

    @staticmethod
    def _is_dir_writable(path: Path) -> bool:
        """
        Vérifie qu'un fichier temporaire peut être créé dans ``path``.

        Sous Windows, certains dossiers protégés (Documents/Vidéos, etc.) peuvent
        exister mais refuser la création de nouveaux fichiers.
        """
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path,
                prefix="mrecode_write_probe_",
                delete=True,
            ):
                pass
            return True
        except OSError:
            return False

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
            if prep_signals is not None:
                # En mode validate=False, le pipeline multi-pistes s'exécute
                # inline et peut émettre finished/failed avant le retour.
                # Les hooks doivent donc être branchés avant l'exécution.
                self._bind_temp_cleanup(prep_signals, cleanup_paths)
                self._bind_matroska_segment_muxing_patch(prep_signals, prepared_config.output)
                self._bind_nfo_write(prep_signals, prepared_config.output)
            signals = self._run_multi_video_pipeline(prepared_config, cleanup_paths, prep_signals=prep_signals)
            if prep_signals is None or signals is not prep_signals:
                self._bind_temp_cleanup(signals, cleanup_paths)
                self._bind_matroska_segment_muxing_patch(signals, prepared_config.output)
                self._bind_nfo_write(signals, prepared_config.output)
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
            self._bind_temp_cleanup(prep_signals, cleanup_paths)
            self._bind_matroska_segment_muxing_patch(prep_signals, prepared_config.output)
            self._bind_nfo_write(prep_signals, prepared_config.output)

        signals = (
            self._run_with_metadata_inject(prepared_config, prep_signals=prep_signals)
            if needs_inject
            else self._run_direct_output(prepared_config, cleanup_paths, prep_signals=prep_signals)
        )
        if prep_signals is None or signals is not prep_signals:
            self._bind_temp_cleanup(signals, cleanup_paths)
        return signals

    def _run_direct_output(
        self,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        *,
        prep_signals: TaskSignals | None = None,
    ) -> TaskSignals:
        self._check_cancelled(prep_signals)
        self._log_step(5, "Construction de la commande ffmpeg (sortie directe)")
        cwd = config.work_dir or config.source.parent

        # Multi-source: le pré-scan ffprobe peut être coûteux.
        # On déporte build+exécution dans un worker dédié pour éviter de bloquer l'UI.
        if len(self._collect_all_sources(config)) > 1:
            signals = self._run_direct_output_multisource_async(
                config=config,
                cleanup_paths=cleanup_paths,
                cwd=cwd,
                prep_signals=prep_signals,
            )
            if prep_signals is None:
                self._bind_matroska_segment_muxing_patch(signals, config.output)
                self._bind_nfo_write(signals, config.output)
            return signals

        chapter_dir: Path | None = None
        if config.chapter_overrides:
            chapter_dir = Path(
                tempfile.mkdtemp(
                    prefix="enc_chapters_",
                    dir=str(config.work_dir) if config.work_dir else None,
                )
            )
            cleanup_paths.append(chapter_dir)

        self._check_cancelled(prep_signals)
        if self._uses_two_pass(config):
            self._log_step(6, "Préparation sync/remap + commandes ffmpeg (2 passes)")
            cmds: list[list[str]]
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []
            try:
                cmds, live_sync_session, sync_cleanup_paths = self._build_runtime_two_pass_with_sync(
                    config,
                    chapter_materialize_dir=chapter_dir,
                    signals=prep_signals,
                )
                cleanup_paths.extend(sync_cleanup_paths)
                self._check_cancelled(prep_signals)
                self._log_step(7, "Exécution ffmpeg en 2 passes (sortie directe)")
                signals = self._run_two_pass(cmds, cwd=cwd, signals=prep_signals)
            except Exception:
                if live_sync_session is not None:
                    live_sync_session.close()
                raise
            self._bind_live_sync_cleanup(signals, live_sync_session)
            if prep_signals is None:
                self._bind_matroska_segment_muxing_patch(signals, config.output)
                self._bind_nfo_write(signals, config.output)
            return signals

        self._log_step(6, "Préparation sync/remap + commande ffmpeg (single pass)")
        cmd: list[str]
        live_sync_session = None
        sync_cleanup_paths = []
        try:
            cmd, live_sync_session, sync_cleanup_paths = self._build_runtime_single_pass_with_sync(
                config,
                chapter_materialize_dir=chapter_dir,
                signals=prep_signals,
            )
            cleanup_paths.extend(sync_cleanup_paths)
            self._check_cancelled(prep_signals)
            self._log_step(7, "Exécution ffmpeg en single pass (sortie directe)")
            if prep_signals is not None:
                output = self._runner._run_cmd(
                    cmd,
                    cwd=cwd,
                    label="ffmpeg",
                    progress_cb=lambda line: prep_signals.progress.emit(line),
                    signals=prep_signals,
                )
                prep_signals.finished.emit(output)
                signals = prep_signals
            else:
                signals = self._runner.run(cmd, cwd=cwd, label="ffmpeg")
        except Exception:
            if live_sync_session is not None:
                live_sync_session.close()
            raise
        self._bind_live_sync_cleanup(signals, live_sync_session)
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
    ) -> TaskSignals:
        signals = prep_signals or TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            chapter_dir: Path | None = None
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []
            is_two_pass = self._uses_two_pass(config)

            try:
                self._check_cancelled(signals)
                if config.chapter_overrides:
                    chapter_dir = Path(
                        tempfile.mkdtemp(
                            prefix="enc_chapters_",
                            dir=str(config.work_dir) if config.work_dir else None,
                        )
                    )
                    cleanup_paths.append(chapter_dir)

                if is_two_pass:
                    self._log_step(6, "Préparation sync/remap + commandes ffmpeg (2 passes)")
                    cmds, live_sync_session, sync_cleanup_paths = self._build_runtime_two_pass_with_sync(
                        config,
                        chapter_materialize_dir=chapter_dir,
                        signals=signals,
                    )
                    cleanup_paths.extend(sync_cleanup_paths)
                    if live_sync_session is not None:
                        for proc in live_sync_session.processes:
                            signals._register_proc(proc)
                    self._check_cancelled(signals)
                    self._log_step(7, "Exécution ffmpeg en 2 passes (sortie directe)")

                    self.log_message.emit("INFO", "Passe 1/2 (analyse)…")
                    self._runner._run_cmd(
                        cmds[0],
                        cwd=cwd,
                        label="ffmpeg-pass1",
                        progress_cb=lambda line: signals.progress.emit(line),
                        signals=signals,
                    )
                    self._check_cancelled(signals)
                    self.log_message.emit("INFO", "Passe 2/2 (encodage)…")
                    output = self._runner._run_cmd(
                        cmds[1],
                        cwd=cwd,
                        label="ffmpeg-pass2",
                        progress_cb=lambda line: signals.progress.emit(line),
                        signals=signals,
                    )
                    signals.finished.emit(output)
                else:
                    self._log_step(6, "Préparation sync/remap + commande ffmpeg (single pass)")
                    cmd, live_sync_session, sync_cleanup_paths = self._build_runtime_single_pass_with_sync(
                        config,
                        chapter_materialize_dir=chapter_dir,
                        signals=signals,
                    )
                    cleanup_paths.extend(sync_cleanup_paths)
                    if live_sync_session is not None:
                        for proc in live_sync_session.processes:
                            signals._register_proc(proc)
                    self._check_cancelled(signals)
                    self._log_step(7, "Exécution ffmpeg en single pass (sortie directe)")
                    output = self._runner._run_cmd(
                        cmd,
                        cwd=cwd,
                        label="ffmpeg",
                        progress_cb=lambda line: signals.progress.emit(line),
                        signals=signals,
                    )
                    signals.finished.emit(output)
            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._unregister_proc(proc)
                    live_sync_session.close()
                if is_two_pass:
                    self._cleanup_two_pass_logs(cwd)
                executor.shutdown(wait=False)

        executor.submit(_task)
        return signals

    def _prepare_multi_video_track(
        self,
        *,
        config: EncodeConfig,
        spec: _VideoTrackPrepSpec,
        work_dir: Path,
        total_tracks: int,
        thread_count: int | None,
        signals: TaskSignals,
        run_cmd: Callable[[list[str], str], str],
    ) -> tuple[dict[str, object], list[Path]]:
        """
        Prépare une piste vidéo unique pour le pipeline multi-pistes.

        Retourne:
          - l'input prêt à mapper dans la reconstruction finale
          - la liste des artefacts temporaires à nettoyer
        """
        order = spec.order
        video = spec.video
        source = spec.source
        offset_ms = spec.offset_ms
        index = order + 1
        local_cleanup: list[Path] = []

        self._check_cancelled(signals)
        self.log_message.emit("INFO", f"Préparation vidéo {index}/{total_tracks}…")

        if video.copy_dv or video.copy_hdr10plus:
            rpu_bin = work_dir / f"video_{index}.rpu.bin"
            hdr10p_json = work_dir / f"video_{index}.hdr10plus.json"
            current_hevc = work_dir / f"video_{index}.enc.hevc"
            if video.copy_dv:
                run_cmd([
                    self._bins["dovi_tool"], "extract-rpu",
                    "-i", str(source), "-o", str(rpu_bin),
                ], f"dovi-extract-{index}")
                local_cleanup.append(rpu_bin)
            if video.copy_hdr10plus:
                run_cmd([
                    self._bins["hdr10plus_tool"], "extract",
                    str(source), "-o", str(hdr10p_json),
                ], f"hdr10plus-extract-{index}")
                local_cleanup.append(hdr10p_json)

            if video.quality_mode == QualityMode.SIZE:
                passlog_prefix = self._two_pass_log_prefix(work_dir, f"video_{index}")
                try:
                    for pass_index, cmd in enumerate(
                        self._build_video_only_two_pass_for_track(
                            config,
                            video,
                            source,
                            current_hevc,
                            offset_ms=offset_ms,
                            passlog_prefix=passlog_prefix,
                            thread_count=thread_count,
                        ),
                        start=1,
                    ):
                        run_cmd(cmd, f"ffmpeg-video-{index}-pass{pass_index}")
                finally:
                    self._cleanup_two_pass_logs_for_prefix(passlog_prefix)
            else:
                run_cmd(
                    self._build_video_only_cmd_for_track(
                        config,
                        video,
                        source,
                        current_hevc,
                        offset_ms=offset_ms,
                        thread_count=thread_count,
                    ),
                    f"ffmpeg-video-{index}",
                )
            local_cleanup.append(current_hevc)

            if video.copy_hdr10plus and hdr10p_json.exists():
                hdr10_out = work_dir / f"video_{index}.hdr10plus.hevc"
                run_cmd([
                    self._bins["hdr10plus_tool"], "inject",
                    "-i", str(current_hevc),
                    "-j", str(hdr10p_json),
                    "-o", str(hdr10_out),
                ], f"hdr10plus-inject-{index}")
                local_cleanup.append(hdr10_out)
                current_hevc = hdr10_out
            if video.copy_dv and rpu_bin.exists():
                dovi_out = work_dir / f"video_{index}.dovi.hevc"
                run_cmd([
                    self._bins["dovi_tool"],
                    "-m", video.dovi_profile,
                    "inject-rpu",
                    "-i", str(current_hevc),
                    "-r", str(rpu_bin),
                    "-o", str(dovi_out),
                ], f"dovi-inject-{index}")
                local_cleanup.append(dovi_out)
                current_hevc = dovi_out

            wrapped = work_dir / f"video_{index}.wrapped.mkv"
            run_cmd(
                self._wrap_injected_hevc_for_reconstruction(
                    source=source,
                    hevc_input=current_hevc,
                    mkv_output=wrapped,
                ),
                f"ffmpeg-wrap-video-{index}",
            )
            local_cleanup.append(wrapped)
            return {
                "input_args": [],
                "path": wrapped,
                "map_arg": f"{order}:v:0",
            }, local_cleanup

        output_path = work_dir / f"video_{index}.mkv"
        passlog_prefix = (
            self._two_pass_log_prefix(work_dir, f"video_{index}")
            if video.quality_mode == QualityMode.SIZE
            else None
        )
        commands = self._build_multi_video_track_encode_commands(
            config,
            video,
            source,
            output_path,
            offset_ms=offset_ms,
            passlog_prefix=passlog_prefix,
            thread_count=thread_count,
        )
        try:
            for pass_index, cmd in enumerate(commands, start=1):
                label = (
                    f"ffmpeg-video-{index}"
                    if len(commands) == 1
                    else f"ffmpeg-video-{index}-pass{pass_index}"
                )
                run_cmd(cmd, label)
        finally:
            if passlog_prefix is not None:
                self._cleanup_two_pass_logs_for_prefix(passlog_prefix)
        local_cleanup.append(output_path)
        return {
            "input_args": [],
            "path": output_path,
            "map_arg": f"{order}:v:0",
        }, local_cleanup

    def _run_multi_video_pipeline(
        self,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        *,
        prep_signals: TaskSignals | None = None,
    ) -> TaskSignals:
        signals = prep_signals or TaskSignals()
        executor = None if prep_signals is not None else ThreadPoolExecutor(max_workers=1)

        def _run_pipeline() -> None:
            work_dir = config.work_dir or config.source.parent
            offset_lookup = self._track_time_offset_lookup(config)
            chapter_dir: Path | None = None
            prepared_inputs: list[dict[str, object] | None] = []
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []

            def _run(cmd: list[str], *, cwd: Path | None = work_dir, label: str = "ffmpeg") -> str:
                is_multi_video_ffmpeg = label.startswith("ffmpeg-video-")

                def _progress(line: str) -> None:
                    if is_multi_video_ffmpeg and not line.startswith("$ "):
                        signals.progress.emit(
                            _ui_encode_progress_message(label=label, event="line", line=line)
                        )
                        return
                    signals.progress.emit(line)

                output = self._runner._run_cmd(
                    cmd,
                    cwd=cwd,
                    label=label,
                    progress_cb=_progress,
                    signals=signals,
                )
                if is_multi_video_ffmpeg:
                    signals.progress.emit(
                        _ui_encode_progress_message(label=label, event="done")
                    )
                return output

            try:
                if config.chapter_overrides:
                    chapter_dir = Path(
                        tempfile.mkdtemp(
                            prefix="enc_multi_video_chapters_",
                            dir=str(config.work_dir) if config.work_dir else None,
                        )
                    )
                    cleanup_paths.append(chapter_dir)

                video_tracks = self._video_tracks(config)
                if not video_tracks:
                    raise EncodeError("Aucune piste vidéo configurée pour le pipeline multi-pistes.")
                prepared_inputs = [None] * len(video_tracks)

                track_specs: list[_VideoTrackPrepSpec] = []
                for order, video in enumerate(video_tracks):
                    source = self._video_source_from_settings(config, video)
                    stream_index = self._video_stream_from_settings(video)
                    offset_ms = self._track_offset_ms(
                        offset_lookup,
                        track_type="video",
                        source_path=source,
                        stream_index=stream_index,
                        allow_single_video_source_fallback=False,
                    )
                    track_specs.append(
                        _VideoTrackPrepSpec(
                            order=order,
                            video=video,
                            source=source,
                            stream_index=stream_index,
                            offset_ms=offset_ms,
                        )
                    )

                transcode_specs: list[_VideoTrackPrepSpec] = []
                for spec in track_specs:
                    if spec.video.codec == "copy":
                        order = spec.order
                        prepared_inputs[order] = {
                            "input_args": self._offset_input_args(spec.offset_ms),
                            "path": spec.source,
                            "map_arg": f"{order}:{spec.stream_index}",
                        }
                        continue
                    transcode_specs.append(spec)

                max_parallel = _normalize_max_parallel_video_encodes(self._max_parallel_video_encodes)
                prep_thread_count = self._parallel_video_worker_thread_count(
                    resource_keys=[
                        self._video_encode_resource_key(spec.video)
                        for spec in transcode_specs
                    ],
                    max_parallel=max_parallel,
                )
                if transcode_specs and max_parallel > 1:
                    self.log_message.emit(
                        "INFO",
                        f"Préparation vidéo parallèle activée ({min(max_parallel, len(transcode_specs))} piste(s) max).",
                    )
                if prep_thread_count is not None:
                    self.log_message.emit(
                        "INFO",
                        "Répartition threads FFmpeg sur la préparation vidéo parallèle: "
                        f"{prep_thread_count} thread(s) par worker.",
                    )

                if transcode_specs:
                    min_available_ram = self._parallel_video_min_available_ram_bytes()
                    if max_parallel > 1 and min_available_ram > 0:
                        self.log_message.emit(
                            "INFO",
                            "Garde-fou RAM parallèle actif: réserve minimale "
                            f"{self._format_bytes(min_available_ram)}.",
                        )

                    ram_wait_notified: set[int] = set()

                    def _on_ram_wait(order: int, required: int, available: int) -> None:
                        if order in ram_wait_notified:
                            return
                        ram_wait_notified.add(order)
                        self.log_message.emit(
                            "INFO",
                            "Préparation vidéo différée par le garde-fou RAM "
                            f"(piste #{order + 1}, libre={self._format_bytes(available)}, "
                            f"requis={self._format_bytes(required)}).",
                        )

                    orchestrator = _VideoTrackPreparationOrchestrator(
                        max_parallel=max_parallel,
                        cancel_cb=lambda: self._check_cancelled(signals),
                        on_worker_failure=signals.cancel,
                        min_available_ram_bytes=min_available_ram,
                        available_ram_cb=EncodeWorkflow._available_ram_bytes,
                        on_ram_wait=_on_ram_wait,
                    )

                    tasks = [
                        _VideoTrackPrepTask(
                            order=spec.order,
                            resource_key=self._video_encode_resource_key(spec.video),
                            estimated_ram_bytes=self._video_prep_estimated_ram_bytes(spec),
                            run=(
                                lambda spec=spec: self._prepare_multi_video_track(
                                    config=config,
                                    spec=spec,
                                    work_dir=work_dir,
                                    total_tracks=len(video_tracks),
                                    thread_count=prep_thread_count,
                                    signals=signals,
                                    run_cmd=lambda cmd, label: _run(cmd, label=label),
                                )
                            ),
                        )
                        for spec in transcode_specs
                    ]

                    for order, prepared, local_cleanup in orchestrator.execute(tasks):
                        prepared_inputs[order] = prepared
                        cleanup_paths.extend(local_cleanup)

                if any(spec is None for spec in prepared_inputs):
                    raise EncodeError("Préparation vidéo incomplète: au moins une piste n'a pas été préparée.")
                prepared_inputs = [cast(dict[str, object], spec) for spec in prepared_inputs]

                self._log_step(5, "Reconstruction finale multi-pistes vidéo")
                all_sources = self._collect_all_sources(config)
                source_idx = self._source_input_index_map(all_sources, start_index=len(prepared_inputs))
                sync_remap: dict[tuple[Path, int, str], tuple[int, int]] = {}
                sync_inputs: list[Path | str] = []
                strict_interleave = False

                final_cmd: list[str] = [self._ffmpeg, "-hide_banner", "-y"]
                final_cmd.extend(self._ffmpeg_progress_args())
                for spec in prepared_inputs:
                    final_cmd.extend([*cast(list[str], spec.get("input_args", [])), "-i", str(spec["path"])])
                for src in all_sources:
                    final_cmd.extend(["-i", str(src)])

                sync_remap, sync_inputs, live_sync_session, strict_interleave = self._prepare_multisource_sync(
                    config=config,
                    all_sources=all_sources,
                    sync_base_input_idx=len(prepared_inputs) + len(all_sources),
                    work_dir=work_dir,
                    signals=signals,
                    allow_live=True,
                )
                sync_cleanup_paths = self._sync_cleanup_paths(sync_inputs)
                self._append_sync_inputs(final_cmd, sync_inputs)
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._register_proc(proc)

                next_input_index, chapter_input_index, tag_input_index = self._prepare_container_metadata_inputs(
                    final_cmd,
                    config,
                    source_idx=source_idx,
                    next_input_index=len(prepared_inputs) + len(all_sources) + len(sync_inputs),
                    chapter_materialize_dir=chapter_dir,
                    chapter_probe_source=config.source,
                )
                final_cmd.extend(self._ffmpeg_thread_args())

                resolved_subtitle_tracks, subtitles_resolved = self._resolved_subtitle_tracks_for_encode(
                    config,
                    all_sources,
                )
                track_input_paths: list[Path | str] = [
                    *[cast(Path | str, spec["path"]) for spec in prepared_inputs],
                    *all_sources,
                    *sync_inputs,
                ]

                def _input_path(idx: int, fallback: Path | str) -> Path | str:
                    if 0 <= idx < len(track_input_paths):
                        return track_input_paths[idx]
                    return fallback

                track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]] = []
                for a in config.audio_tracks:
                    src_path = Path(a.source_path or config.source)
                    remapped = sync_remap.get((src_path, int(a.stream_index), "audio"))
                    if remapped is not None:
                        inp, stream_idx = remapped
                    else:
                        inp = source_idx.get(src_path)
                        if inp is None:
                            inp = source_idx.get(config.source, len(prepared_inputs))
                        stream_idx = int(a.stream_index)
                    track_mappings.append(
                        (
                            (src_path, int(a.stream_index), "audio"),
                            _input_path(int(inp), src_path),
                            int(stream_idx),
                        )
                    )

                for src_path_raw, stream_idx_raw in resolved_subtitle_tracks:
                    src_path = Path(src_path_raw)
                    remapped = sync_remap.get((src_path, int(stream_idx_raw), "subtitle"))
                    if remapped is not None:
                        inp, stream_idx = remapped
                    else:
                        inp = source_idx.get(src_path)
                        if inp is None:
                            continue
                        stream_idx = int(stream_idx_raw)
                    track_mappings.append(
                        (
                            (src_path, int(stream_idx_raw), "subtitle"),
                            _input_path(int(inp), src_path),
                            int(stream_idx),
                        )
                    )

                next_input_index, offset_remap = self._append_offset_aux_inputs(
                    final_cmd,
                    self._build_offset_specs(
                        config,
                        track_mappings=track_mappings,
                        offset_lookup=offset_lookup,
                    ),
                    start_input_index=next_input_index,
                )
                _ = next_input_index

                for out_idx, spec in enumerate(prepared_inputs):
                    final_cmd.extend(["-map", str(spec["map_arg"])])
                    final_cmd.extend([f"-c:v:{out_idx}", "copy"])

                self._append_stream_maps_and_attachments(
                    final_cmd,
                    config,
                    source_idx=source_idx,
                    subtitle_copy_input_indices=list(source_idx.values()),
                    sync_remap=sync_remap,
                    offset_remap=offset_remap,
                    subtitle_tracks_override=resolved_subtitle_tracks,
                    force_copy_subtitles_wildcard=(config.copy_subtitles and not subtitles_resolved),
                )
                if strict_interleave:
                    self._append_strict_interleave_mux_flags(final_cmd)

                default_source_index = source_idx.get(config.source, len(prepared_inputs))
                self._append_container_metadata_args(
                    final_cmd,
                    config,
                    default_metadata_input_index=default_source_index,
                    default_chapter_input_index=default_source_index,
                    chapter_input_index=chapter_input_index,
                    tag_input_index=tag_input_index,
                    include_copy_video_stream_passthrough=False,
                )
                final_cmd.append(str(config.output))
                output = _run(final_cmd, label="ffmpeg-multi-video")
                signals.finished.emit(output)
            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._unregister_proc(proc)
                    live_sync_session.close()
                for path in sync_cleanup_paths:
                    try:
                        remove_path(path)
                    except OSError:
                        pass
                if executor is not None:
                    executor.shutdown(wait=False)

        if prep_signals is not None:
            _run_pipeline()
            return signals

        assert executor is not None
        executor.submit(_run_pipeline)
        return signals

    def _bind_live_sync_cleanup(
        self,
        signals: TaskSignals,
        session: LiveSyncSession | None,
    ) -> None:
        if session is None:
            return

        done = {"closed": False}
        for proc in session.processes:
            signals._register_proc(proc)

        def _cleanup(*_args) -> None:
            if done["closed"]:
                return
            done["closed"] = True
            for proc in session.processes:
                signals._unregister_proc(proc)
            session.close()

        signals.finished.connect(_cleanup)
        signals.failed.connect(_cleanup)
        signals.cancelled.connect(_cleanup)

    def _bind_temp_cleanup(self, signals: TaskSignals, cleanup_paths: list[Path]) -> None:
        """Supprime les fichiers/dossiers temporaires quand le workflow se termine."""
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

    def _bind_matroska_segment_muxing_patch(self, signals: TaskSignals, output: Path) -> None:
        self._muxing_post_action.bind_on_success(signals, output)
        self._language_post_action.bind_on_success(signals, output)

    def _bind_nfo_write(self, signals: TaskSignals, output: Path) -> None:
        if not self._generate_nfo:
            return
        mediainfo_bin = self._bins.get("mediainfo") or "mediainfo"
        log_cb = self.log_message.emit

        def _write(*_args) -> None:
            write_mediainfo_nfo(output, log_cb=log_cb, mediainfo_bin=mediainfo_bin)

        signals.finished.connect(_write)

    @staticmethod
    def _format_bytes(value: int) -> str:
        size = float(max(0, value))
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        idx = 0
        while size >= 1024.0 and idx < len(units) - 1:
            size /= 1024.0
            idx += 1
        return f"{size:.1f} {units[idx]}"

    def _estimate_duration_seconds(self, config: EncodeConfig) -> float:
        duration_s = config.duration_s
        if duration_s is not None and duration_s > 0:
            return float(duration_s)
        probed = self._postproc_helper._probe_duration_seconds(config.source)
        if probed is not None and probed > 0:
            return float(probed)
        return 3600.0

    def _estimate_inject_video_bytes(
        self,
        config: EncodeConfig,
        *,
        duration_s: float,
        source_size: int,
    ) -> int:
        if config.video.quality_mode == QualityMode.SIZE:
            video_kbps = self._size_to_bitrate_kbps(config)
            return int((video_kbps * 1000 / 8) * duration_s)
        if config.video.quality_mode == QualityMode.BITRATE:
            video_kbps = max(1, int(config.video.bitrate_kbps or 1))
            return int((video_kbps * 1000 / 8) * duration_s)
        # CRF : pas de débit cible explicite ; base conservative sur la taille source.
        return max(source_size, int(source_size * 3 / 4))

    def _estimate_inject_storage_requirements(self, config: EncodeConfig) -> tuple[int, int]:
        """
        Retourne (work_required_bytes, output_required_bytes) pour le chemin injection.

        work_required_bytes : pic d'espace requis pour les temporaires du workflow
                              d'injection (HEVC + sidecars) sur le FS de travail.
        output_required_bytes : espace requis pour produire le MKV final sur le FS
                                de sortie.
        """
        source_size = max(0, config.source.stat().st_size if config.source.exists() else 0)
        duration_s = self._estimate_duration_seconds(config)
        video_bytes = max(
            128 * 1024 * 1024,
            self._estimate_inject_video_bytes(
                config,
                duration_s=duration_s,
                source_size=source_size,
            ),
        )

        sidecars = 32 * 1024 * 1024
        if config.video.copy_dv:
            sidecars += 64 * 1024 * 1024
        if config.video.copy_hdr10plus:
            sidecars += 64 * 1024 * 1024
        if config.chapter_overrides:
            sidecars += 16 * 1024 * 1024

        work_required = (2 * video_bytes) + sidecars

        encoded_audio_bytes = 0
        for audio in config.audio_tracks:
            if audio.codec == "copy":
                continue
            if audio.codec == "flac":
                kbps = max(1000, int(audio.bitrate_kbps or 0))
            else:
                kbps = normalize_audio_bitrate_kbps(
                    audio.codec,
                    audio.bitrate_kbps,
                    audio.input_channels,
                    None,
                    audio.input_channel_layout,
                )
            encoded_audio_bytes += int((kbps * 1000 / 8) * duration_s)

        extra_attachments_bytes = sum(
            max(0, Path(p).stat().st_size)
            for p in config.extra_attachments
            if Path(p).exists()
        )
        output_required = max(
            source_size,
            video_bytes + encoded_audio_bytes + extra_attachments_bytes,
        ) + (64 * 1024 * 1024)
        return work_required, output_required

    def _ensure_inject_storage_available(self, config: EncodeConfig) -> None:
        """
        Vérifie l'espace libre avant le chemin d'injection DV/HDR10+.

        On fait une estimation conservative :
          - temporaires d'injection (HEVC + sidecars) sur le volume de travail,
          - fichier final sur le volume de sortie.
        """
        work_required, output_required = self._estimate_inject_storage_requirements(config)

        work_root = config.work_dir or Path(tempfile.gettempdir())
        work_root.mkdir(parents=True, exist_ok=True)
        output_root = config.output.parent
        output_root.mkdir(parents=True, exist_ok=True)

        work_free = shutil.disk_usage(work_root).free
        output_free = shutil.disk_usage(output_root).free

        self.log_message.emit(
            "INFO",
            "Estimation espace injection: "
            f"temp≈{self._format_bytes(work_required)} sur {work_root} ; "
            f"sortie≈{self._format_bytes(output_required)} sur {output_root}.",
        )

        if self._ram_buffer_enabled and EncodeWorkflow._ram_buffer_dir() is not None:
            self.log_message.emit(
                "INFO",
                "Buffer RAM actif: estimation disque conservative (fallback disque).",
            )

        same_fs = False
        try:
            same_fs = os.stat(work_root).st_dev == os.stat(output_root).st_dev
        except OSError:
            same_fs = False

        if same_fs:
            required = work_required + output_required
            free = min(work_free, output_free)
            if free < required:
                raise EncodeError(
                    "Espace disque insuffisant pour l'injection DoVi/HDR10+ "
                    f"(requis≈{self._format_bytes(required)}, libre≈{self._format_bytes(free)} "
                    f"sur {output_root})."
                )
            return

        if work_free < work_required:
            raise EncodeError(
                "Espace disque insuffisant pour les temporaires d'injection "
                f"(requis≈{self._format_bytes(work_required)}, "
                f"libre≈{self._format_bytes(work_free)} sur {work_root})."
            )
        if output_free < output_required:
            raise EncodeError(
                "Espace disque insuffisant pour le fichier de sortie "
                f"(requis≈{self._format_bytes(output_required)}, "
                f"libre≈{self._format_bytes(output_free)} sur {output_root})."
            )

    def _prepare_attachment_config(
        self,
        config: EncodeConfig,
        *,
        work_dir: Path,
        signals: TaskSignals | None = None,
    ) -> tuple[EncodeConfig, Path | None]:
        """
        Convertit les ``attached_pic`` sélectionnés en fichiers joints temporaires.

        FFmpeg expose fréquemment un ``cover.jpg`` MKV comme stream vidéo MJPEG
        avec ``disposition.attached_pic=1``. Si on le remappe via ``-map``,
        la sortie perd ce flag et l'image devient une vraie piste vidéo.
        Pour conserver le comportement "attachment", on extrait d'abord l'image
        vers un fichier temporaire puis on la ré-attache avec ``-attach``.
        """
        if not config.attachment_streams:
            return config, None

        tmp_dir = Path(tempfile.mkdtemp(prefix="enc_attachments_", dir=str(work_dir)))
        direct_streams: list = []
        extracted_files: list[Path] = []
        created_any = False

        try:
            for selection in config.attachment_streams:
                self._check_cancelled(signals)
                src_path, stream_idx = selection[:2]
                meta = self._describe_attachment_stream(src_path, stream_idx)
                self._check_cancelled(signals)
                if not meta["is_attached_pic"]:
                    direct_streams.append(selection)
                    continue

                created_any = True
                filename = self._attachment_filename(meta, stream_idx)
                dest = self._unique_attachment_path(tmp_dir, filename)
                self._extract_attached_pic(src_path, stream_idx, dest, signals=signals)
                self._check_cancelled(signals)
                extracted_files.append(dest)

            if not created_any:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return config, None

            prepared = replace(
                config,
                attachment_streams=direct_streams,
                extra_attachments=[*extracted_files, *config.extra_attachments],
            )
            return prepared, tmp_dir
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    def _describe_attachment_stream(self, source: Path, stream_idx: int) -> dict[str, object]:
        """Retourne les métadonnées ffprobe minimales d'un stream potentiellement attachment."""
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
                timeout=30,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            return {
                "is_attached_pic": False,
                "filename": f"attachment_{stream_idx}.bin",
                "mimetype": "application/octet-stream",
            }

        if result.returncode != 0:
            return {
                "is_attached_pic": False,
                "filename": f"attachment_{stream_idx}.bin",
                "mimetype": "application/octet-stream",
            }

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            payload = {}

        for stream in payload.get("streams", []):
            if int(stream.get("index", -1)) != int(stream_idx):
                continue
            tags = stream.get("tags", {}) or {}
            disposition = stream.get("disposition", {}) or {}
            return {
                "is_attached_pic": bool(disposition.get("attached_pic", 0)),
                "filename": tags.get("filename") or f"attachment_{stream_idx}.bin",
                "mimetype": tags.get("mimetype") or "application/octet-stream",
            }

        return {
            "is_attached_pic": False,
            "filename": f"attachment_{stream_idx}.bin",
            "mimetype": "application/octet-stream",
        }

    def _attachment_filename(self, meta: dict[str, object], stream_idx: int) -> str:
        """Construit un nom de fichier exploitable pour un attachment extrait."""
        raw_name = str(meta.get("filename") or "").strip()
        name = Path(raw_name).name if raw_name else f"attachment_{stream_idx}"
        if Path(name).suffix:
            return name
        mime = str(meta.get("mimetype") or "").lower()
        suffix = _EXT_BY_MIME.get(mime, ".bin")
        return f"{name}{suffix}"

    def _unique_attachment_path(self, tmp_dir: Path, filename: str) -> Path:
        """Retourne un chemin unique dans ``tmp_dir`` pour éviter les collisions de nom."""
        candidate = tmp_dir / filename
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        for idx in range(1, 1000):
            alt = tmp_dir / f"{stem}_{idx}{suffix}"
            if not alt.exists():
                return alt
        return tmp_dir / f"{stem}_{os.getpid()}{suffix}"

    def _extract_attached_pic(
        self,
        source: Path,
        stream_idx: int,
        dest: Path,
        *,
        signals: TaskSignals | None = None,
    ) -> None:
        """Extrait un ``attached_pic`` vers un vrai fichier image."""
        self._check_cancelled(signals)
        cmd = [
            self._ffmpeg,
            "-hide_banner", "-y",
            "-i", str(source),
            "-map", f"0:{stream_idx}",
            *self._ffmpeg_thread_args(),
            "-c", "copy",
            "-frames:v", "1",
            str(dest),
        ]
        self.log_message.emit("INFO", "$ " + " ".join(cmd))
        try:
            self._runner._run_cmd(
                cmd,
                label="extract-attached-pic",
                signals=signals,
            )
        except TaskCancelledError:
            dest.unlink(missing_ok=True)
            raise
        except Exception as exc:
            dest.unlink(missing_ok=True)
            stderr = str(exc).strip()
            raise EncodeError(
                f"Extraction attachment échouée pour le stream {stream_idx} de {source.name}: {stderr}"
            ) from exc
        self._check_cancelled(signals)
        if not dest.exists():
            raise EncodeError(
                f"Extraction attachment échouée pour le stream {stream_idx} de {source.name}: fichier absent"
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
        tags: dict[str, str] = {}

        if config.tag_overrides is not None:
            for key, value in config.tag_overrides.items():
                key_s = str(key).strip()
                value_s = str(value).strip()
                if not key_s or not value_s:
                    continue
                tags[key_s] = value_s

        tags["title"] = config.file_title
        return tags

    def _build_multi_video_final_mux_command(
        self,
        config: EncodeConfig,
        prepared_video_inputs: list[dict[str, object]],
        *,
        chapter_materialize_dir: Path | None = None,
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
            cmd.extend([*spec.get("input_args", []), "-i", str(spec["path"])])

        all_sources = self._collect_all_sources(config)
        source_idx = self._source_input_index_map(all_sources, start_index=len(video_inputs))
        for src in all_sources:
            cmd.extend(["-i", str(src)])

        next_input_index, chapter_input_index, tag_input_index = self._prepare_container_metadata_inputs(
            cmd,
            config,
            source_idx=source_idx,
            next_input_index=len(video_inputs) + len(all_sources),
            chapter_materialize_dir=chapter_materialize_dir,
            chapter_probe_source=config.source,
        )
        _ = next_input_index
        cmd.extend(self._ffmpeg_thread_args())

        for out_idx, spec in enumerate(video_inputs):
            cmd.extend(["-map", str(spec["map_arg"])])
            cmd.extend([f"-c:v:{out_idx}", "copy"])

        resolved_subtitle_tracks, subtitles_resolved = self._resolved_subtitle_tracks_for_encode(
            config,
            all_sources,
        )
        self._append_stream_maps_and_attachments(
            cmd,
            config,
            source_idx=source_idx,
            subtitle_copy_input_indices=list(source_idx.values()),
            subtitle_tracks_override=resolved_subtitle_tracks,
            force_copy_subtitles_wildcard=(config.copy_subtitles and not subtitles_resolved),
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
        raw = (language or "").strip()
        if not raw:
            return None
        canonical = LangTags.normalize(raw) or raw
        regional = LangTags.regionalize_track_language(canonical, title) or canonical
        if regional.lower() == "und":
            return "und"
        if LangTags.is_valid(regional):
            return regional
        if LangTags.is_valid(canonical):
            return canonical
        return "und"

    @staticmethod
    def _disposition_value_from_edit(edit) -> str | None:
        values = (
            edit.flag_default,
            edit.flag_forced,
            edit.flag_hearing_impaired,
            edit.flag_visual_impaired,
            edit.flag_original,
            edit.flag_commentary,
        )
        if all(v is None for v in values):
            return None
        # Évite les états partiels : la disposition ffmpeg remplace l'ensemble.
        if any(v is None for v in values):
            return None

        flags: list[str] = []
        if edit.flag_default:
            flags.append("default")
        if edit.flag_forced:
            flags.append("forced")
        if edit.flag_hearing_impaired:
            flags.append("hearing_impaired")
        if edit.flag_visual_impaired:
            flags.append("visual_impaired")
        if edit.flag_original:
            flags.append("original")
        if edit.flag_commentary:
            flags.append("comment")
        return "+".join(flags) if flags else "0"

    def _build_track_meta_args(self, config: EncodeConfig) -> list[str]:
        args: list[str] = []
        if not config.track_meta_edits:
            return args

        video_count = max(1, len(config.video_tracks or ([config.video] if config.video is not None else [])))
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
    ) -> TaskSignals:
        """
        Workflow d'encodage avec injection DV RPU / HDR10+ en une seule passe de sortie.
        Appelé uniquement quand copy_dv ou copy_hdr10plus est actif ET codec ≠ copy.

        Gestion des fichiers de travail :
          • Pas de encoded.mkv : la vidéo est encodée directement en HEVC brut (enc.hevc).
          • Pas de src.hevc : les extractions RPU/HDR10+ lisent directement la source.
          • L'audio copy est pris directement depuis la source via FFmpeg — aucun
            traitement intermédiaire.
          • La source n'est jamais modifiée.
          • Tous les HEVC intermédiaires sont écrits dans le dossier process
            dédié au job courant (sous work_dir).
          • Chaque intermédiaire HEVC est supprimé immédiatement dès que l'étape
            suivante l'a consommé.
          • Le dossier temporaire process est supprimé en fin de workflow.

        Ordre d'injection (contrainte HDR10+ avant DV) :
          HDR10+ en premier : hdr10plus_tool ne tolère pas les NAL RPU DV existants.
          dovi_tool préserve tous les types de NAL.

        Étapes :
          1. Extraction RPU DoVi (dovi_tool extract-rpu) si copy_dv
          2. Extraction HDR10+ (hdr10plus_tool extract) si copy_hdr10plus
          3. Encodage vidéo seule → enc.hevc (HEVC brut, sans container)
             Mode SIZE : deux passes (analyse + encodage direct en .hevc)
          4. Injection HDR10+ si applicable → nouveau current_hevc, ancien supprimé
          5. Injection RPU DV si applicable → nouveau current_hevc, ancien supprimé
          6. Reconstitution finale via ffmpeg (une seule commande, depuis la source) :
             ffmpeg -i current_hevc -i source
               -map 0:v:0 -c:v copy             (vidéo injectée)
               -map 1:stream_idx [codec args]   (audio depuis source, copy ou réencodage)
               -map 1:s? -c:s copy              (subs depuis source)
               -map_metadata/-map_chapters/...  (tags/chapitres/track-meta)
               output.mkv
             Pas de fichier audio intermédiaire. La source n'est jamais modifiée.
        """
        signals = prep_signals or TaskSignals()
        executor = None if prep_signals is not None else ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            work = config.work_dir or Path(tempfile.gettempdir())
            work.mkdir(parents=True, exist_ok=True)
            tmp_dir = tempfile.mkdtemp(
                prefix="mediarecode_encode_",
                dir=str(work),
            )
            tmp = Path(tmp_dir)
            # Conservé pour compatibilité : les intermédiaires sont désormais
            # forcés dans tmp, donc cette liste reste vide.
            ext_files: list[Path] = []
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []

            def _run(cmd: list[str]) -> str:
                return self._runner._run_cmd(
                    cmd, signals=signals, cwd=tmp,
                    progress_cb=lambda line: signals.progress.emit(line),
                )

            def _check() -> None:
                if signals._cancel_event.is_set():
                    raise TaskCancelledError()

            def _alloc(name: str, ref_size: int) -> Path:
                """
                Alloue un chemin de travail HEVC.
                Tous les intermédiaires sont forcés dans le dossier process.
                """
                _ = ref_size
                return tmp / name

            def _free(path: Path) -> None:
                """
                Supprime immédiatement un fichier intermédiaire.
                Le retire de ext_files si présent.
                Silencieux sur toute erreur OS.
                """
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    ext_files.remove(path)
                except ValueError:
                    pass   # chemin disque, pas dans ext_files

            try:
                src_size_est = config.source.stat().st_size
                # ── 1. RPU Dolby Vision ──────────────────────────────────
                self._log_step(5, "Extraction des métadonnées dynamiques (DoVi/HDR10+)")
                rpu_bin = tmp / "rpu.bin"
                if config.video.copy_dv:
                    signals.progress.emit("Extraction RPU Dolby Vision…")
                    _run([
                        self._bins["dovi_tool"], "extract-rpu",
                        "-i", str(self._video_source_path(config)), "-o", str(rpu_bin),
                    ])
                    _check()

                # ── 2. HDR10+ ────────────────────────────────────────────
                hdr10p_json = tmp / "hdr10p.json"
                if config.video.copy_hdr10plus:
                    signals.progress.emit("Extraction métadonnées HDR10+…")
                    _run([
                        self._bins["hdr10plus_tool"], "extract",
                        str(self._video_source_path(config)), "-o", str(hdr10p_json),
                    ])
                    _check()

                # ── 3. Encodage vidéo → enc.hevc brut ───────────────────
                # Encodage direct en HEVC sans container ni audio.
                # Élimine encoded.mkv et garde un seul intermédiaire vidéo utile.
                # La taille estimée = taille source (approximation conservative).
                self._log_step(6, "Encodage vidéo seule (HEVC brut)")
                enc_hevc = _alloc("enc.hevc", src_size_est)
                signals.progress.emit("Encodage vidéo…")
                if config.video.quality_mode == QualityMode.SIZE:
                    v_cmds = self._build_video_only_two_pass(config, enc_hevc)
                    self.log_message.emit("INFO", "Passe 1/2 (analyse)…")
                    _run(v_cmds[0])
                    _check()
                    self.log_message.emit("INFO", "Passe 2/2 (encodage)…")
                    _run(v_cmds[1])
                else:
                    _run(self._build_video_only_cmd(config, enc_hevc))
                _check()
                current_hevc = enc_hevc

                # ── 4. Injection HDR10+ ──────────────────────────────────
                # HDR10+ avant DV : hdr10plus_tool ne tolère pas les NAL RPU DV.
                self._log_step(7, "Injection HDR10+ puis DoVi (si demandé)")
                if config.video.copy_hdr10plus and hdr10p_json.exists():
                    cur_size = current_hevc.stat().st_size
                    out_hdr10p = _alloc("enc_hdr10p.hevc", cur_size)
                    signals.progress.emit("Injection métadonnées HDR10+…")
                    _run([
                        self._bins["hdr10plus_tool"], "inject",
                        "-i", str(current_hevc),
                        "-j", str(hdr10p_json),
                        "-o", str(out_hdr10p),
                    ])
                    _free(current_hevc)   # libère enc.hevc immédiatement
                    current_hevc = out_hdr10p
                    _check()

                # ── 5. Injection RPU DV ──────────────────────────────────
                if config.video.copy_dv and rpu_bin.exists():
                    cur_size = current_hevc.stat().st_size
                    out_dv = _alloc("enc_dv.hevc", cur_size)
                    signals.progress.emit("Injection RPU Dolby Vision…")
                    _run([
                        self._bins["dovi_tool"],
                        "-m", config.video.dovi_profile,
                        "inject-rpu",
                        "-i", str(current_hevc),
                        "-r", str(rpu_bin),
                        "-o", str(out_dv),
                    ])
                    _free(current_hevc)   # libère enc_hdr10p.hevc (ou enc.hevc)
                    current_hevc = out_dv
                    _check()

                # ── 6. Encapsulation FFmpeg-only du HEVC injecté ──────────
                self._log_step(8, "Encapsulation timeline vidéo injectée")
                wrapped_video = _alloc("enc_wrapped.mkv", current_hevc.stat().st_size)
                signals.progress.emit("Encapsulation vidéo injectée…")
                _run(
                    self._wrap_injected_hevc_for_reconstruction(
                        source=config.source,
                        hevc_input=current_hevc,
                        mkv_output=wrapped_video,
                    )
                )
                _free(current_hevc)
                current_video_input = wrapped_video
                _check()

                # ── 7. Reconstitution finale ─────────────────────────────
                # ffmpeg multi-input :
                #   input 0 = vidéo encapsulée et horodatée, input 1 = source principale,
                #   input 2+ = sources audio / sous-titres / attachements supplémentaires.
                self._log_step(9, "Reconstruction finale du conteneur MKV")
                signals.progress.emit("Reconstitution finale…")
                all_sources = self._collect_all_sources(config)
                extra_sources = all_sources[1:]
                recon_source_idx = self._source_input_index_map(all_sources, start_index=1)
                sync_remap: dict[tuple[Path, int, str], tuple[int, int]] = {}
                sync_inputs: list[Path | str] = []
                strict_interleave = False

                recon_cmd = [self._ffmpeg, "-hide_banner", "-y",
                             *self._ffmpeg_progress_args(),
                             "-i", str(current_video_input),
                             "-i", str(config.source)]
                for sp in extra_sources:
                    recon_cmd.extend(["-i", str(sp)])

                sync_remap, sync_inputs, live_sync_session, strict_interleave = self._prepare_multisource_sync(
                    config=config,
                    all_sources=all_sources,
                    sync_base_input_idx=2 + len(extra_sources),
                    work_dir=tmp,
                    signals=signals,
                    allow_live=True,
                )
                sync_cleanup_paths = self._sync_cleanup_paths(sync_inputs)
                self._append_sync_inputs(recon_cmd, sync_inputs)
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._register_proc(proc)

                next_input_index, chapter_input_index, tag_input_index = self._prepare_container_metadata_inputs(
                    recon_cmd,
                    config,
                    source_idx=recon_source_idx,
                    next_input_index=2 + len(extra_sources) + len(sync_inputs),
                    chapter_materialize_dir=tmp,
                    chapter_probe_source=config.source,
                )

                recon_cmd.extend(self._ffmpeg_thread_args())
                resolved_subtitle_tracks, subtitles_resolved = self._resolved_subtitle_tracks_for_encode(
                    config,
                    all_sources,
                )

                offset_lookup = self._track_time_offset_lookup(config)
                track_input_paths: list[Path | str] = [current_video_input, *all_sources, *sync_inputs]

                def _input_path(idx: int, fallback: Path | str) -> Path | str:
                    if 0 <= idx < len(track_input_paths):
                        return track_input_paths[idx]
                    return fallback

                video_key = self._video_map_key(config)
                video_default_map = (0, 0)
                track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]] = [
                    (video_key, _input_path(0, current_video_input), 0)
                ]

                for a in config.audio_tracks:
                    src_path = Path(a.source_path or config.source)
                    remapped = sync_remap.get((src_path, int(a.stream_index), "audio"))
                    if remapped is not None:
                        inp, stream_idx = remapped
                    else:
                        inp = recon_source_idx.get(src_path)
                        if inp is None:
                            inp = recon_source_idx.get(config.source, 1)
                        stream_idx = int(a.stream_index)
                    track_mappings.append(((src_path, int(a.stream_index), "audio"), _input_path(int(inp), src_path), int(stream_idx)))

                for src_path_raw, stream_idx_raw in resolved_subtitle_tracks:
                    src_path = Path(src_path_raw)
                    remapped = sync_remap.get((src_path, int(stream_idx_raw), "subtitle"))
                    if remapped is not None:
                        inp, stream_idx = remapped
                    else:
                        inp = recon_source_idx.get(src_path)
                        if inp is None:
                            continue
                        stream_idx = int(stream_idx_raw)
                    track_mappings.append(((src_path, int(stream_idx_raw), "subtitle"), _input_path(int(inp), src_path), int(stream_idx)))

                next_input_index, offset_remap = self._append_offset_aux_inputs(
                    recon_cmd,
                    self._build_offset_specs(
                        config,
                        track_mappings=track_mappings,
                        offset_lookup=offset_lookup,
                    ),
                    start_input_index=next_input_index,
                )
                _ = next_input_index

                video_map_key = video_key
                recon_cmd.extend([
                    "-map",
                    self._video_map_arg(
                        video_default_map,
                        offset_remap=offset_remap,
                        map_key=video_map_key,
                    ),
                    "-c:v",
                    "copy",
                ])
                self._append_stream_maps_and_attachments(
                    recon_cmd,
                    config,
                    source_idx=recon_source_idx,
                    subtitle_copy_input_indices=list(range(1, 2 + len(extra_sources))),
                    sync_remap=sync_remap,
                    offset_remap=offset_remap,
                    subtitle_tracks_override=resolved_subtitle_tracks,
                    force_copy_subtitles_wildcard=(config.copy_subtitles and not subtitles_resolved),
                )
                if strict_interleave:
                    self._append_strict_interleave_mux_flags(recon_cmd)

                self._append_container_metadata_args(
                    recon_cmd,
                    config,
                    default_metadata_input_index=0,
                    default_chapter_input_index=1,
                    chapter_input_index=chapter_input_index,
                    tag_input_index=tag_input_index,
                )
                recon_cmd.append(str(config.output))
                _run(recon_cmd)

                signals.finished.emit(f"Encodage terminé → {config.output.name}")

            except TaskCancelledError:
                signals.cancelled.emit()
            except Exception as exc:
                signals.failed.emit(str(exc), exc)
            finally:
                if executor is not None:
                    executor.shutdown(wait=False)
                if live_sync_session is not None:
                    for proc in live_sync_session.processes:
                        signals._unregister_proc(proc)
                    live_sync_session.close()
                for path in sync_cleanup_paths:
                    try:
                        remove_path(path)
                    except OSError:
                        pass
                # Compatibilité : ext_files est vide en mode "tout dans work_dir".
                for p in list(ext_files):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
                shutil.rmtree(tmp_dir, ignore_errors=True)

        if prep_signals is None:
            self._bind_matroska_segment_muxing_patch(signals, config.output)
            self._bind_nfo_write(signals, config.output)
            assert executor is not None
            executor.submit(_task)
        else:
            _task()
        return signals
