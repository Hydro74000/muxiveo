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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from PySide6.QtCore import QObject, Signal
from core.lang_tags import Rfc5646LanguageTags as LangTags
from core.runner import TaskCancelledError, TaskSignals, ToolRunner
from core.subprocess_utils import subprocess_text_kwargs
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
    MkvmergeLikeTimelineSync,
    TimelineSyncFallbackHelper,
)
from core.workflows.encode.models import (
    EncodeConfig, EncodeError, QualityMode,
    VideoEncodeSettings, AudioTrackSettings, TrackTimeOffset,
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

_VAAPI_CODECS = {"hevc_vaapi", "h264_vaapi"}
_FALLBACK_HEVC_FRAME_RATE = "24000/1001"


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


def _default_ffmpeg_thread_count() -> int:
    """Default FFmpeg thread count: logical CPU count × 0.75, rounded up."""
    cpu_count = os.cpu_count() or 1
    return max(1, (cpu_count * 3 + 3) // 4)


def _normalize_ffmpeg_thread_count(value: int | None) -> int:
    """Return a safe FFmpeg thread count, preserving 0 as ffmpeg auto mode."""
    if value is None or value < 0:
        return _default_ffmpeg_thread_count()
    return value


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
        self._generate_nfo = generate_nfo
        self._runner = ToolRunner(max_workers=1, parent=self)
        self._ram_buffer_enabled       = ram_buffer_enabled
        self._ram_buffer_threshold_pct = max(0, min(ram_buffer_threshold_pct, 90))
        self._ffmpeg_threads = _normalize_ffmpeg_thread_count(ffmpeg_threads)
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
        self._muxing_post_action = MatroskaMuxingAppPostAction(
            app_prefix=MatroskaMuxingAppPostAction.default_prefix(APP_VERSION_LABEL),
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

    def set_mediainfo_bin(self, mediainfo_bin: str) -> None:
        self._bins["mediainfo"] = mediainfo_bin

    def set_generate_nfo(self, generate_nfo: bool) -> None:
        self._generate_nfo = generate_nfo

    def _ffmpeg_thread_args(self) -> list[str]:
        return ["-threads", str(self._ffmpeg_threads)]

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
        return config.copy_dv or config.copy_hdr10plus

    @classmethod
    def _needs_metadata_inject(cls, config: EncodeConfig) -> bool:
        return cls._wants_dynamic_hdr_copy(config) and not cls._is_video_passthrough(config)

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
        if mediainfo_flags is None:
            return (has_dv, has_hdr10plus) if payload is not None else None

        mi_dv, mi_hdr10plus = mediainfo_flags
        return has_dv or mi_dv, has_hdr10plus or mi_hdr10plus

    def _ffprobe_streams_payload(self, source: Path) -> dict[str, object] | None:
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
            return None

        if result.returncode != 0:
            return None

        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return None

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

    def _mediainfo_hdr_flags(self, source: Path) -> tuple[bool, bool] | None:
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
            return None

        return (
            "dolby vision" in (hdr_format.stdout or "").lower(),
            "hdr10+" in (hdr_compat.stdout or "").lower(),
        )

    def _normalize_dynamic_hdr_config(self, config: EncodeConfig) -> EncodeConfig:
        """
        Nettoie les demandes de copie DoVi/HDR10+ avant le routage principal.

        - Si rien n'est demandé : retourne la config telle quelle.
        - Si la source ne contient pas un format demandé : désactive uniquement ce format.
        - Si la détection échoue : conserve la demande telle quelle.
        """
        if not self._wants_dynamic_hdr_copy(config):
            return config

        detected = self._detect_source_dynamic_hdr_presence(config.source)
        if detected is None:
            self.log_message.emit(
                "WARN",
                "Détection DoVi/HDR10+ impossible sur la source — workflow demandé conservé.",
            )
            return config

        has_dv, has_hdr10plus = detected
        copy_dv = config.copy_dv and has_dv
        copy_hdr10plus = config.copy_hdr10plus and has_hdr10plus

        if config.copy_dv and not copy_dv:
            self.log_message.emit(
                "WARN",
                "Copy DoVi demandé mais aucune donnée DoVi détectée — option ignorée.",
            )
        if config.copy_hdr10plus and not copy_hdr10plus:
            self.log_message.emit(
                "WARN",
                "Copy HDR10+ demandé mais aucune donnée HDR10+ détectée — option ignorée.",
            )

        normalized = replace(
            config,
            copy_dv=copy_dv,
            copy_hdr10plus=copy_hdr10plus,
        )

        if not self._wants_dynamic_hdr_copy(normalized) and self._is_video_passthrough(config):
            self.log_message.emit(
                "INFO",
                "Aucun DoVi/HDR10+ utile à recopier — passthrough vidéo direct.",
            )
        return normalized

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
        if self._uses_two_pass(config):
            return self._build_two_pass(config)[1]
        return self._build_single_pass(config)

    def _build_direct_output_commands(
        self,
        config: EncodeConfig,
        *,
        chapter_materialize_dir: Path | None = None,
    ) -> list[str] | list[list[str]]:
        if self._uses_two_pass(config):
            return self._build_two_pass(
                config,
                chapter_materialize_dir=chapter_materialize_dir,
            )
        return self._build_single_pass(
            config,
            chapter_materialize_dir=chapter_materialize_dir,
        )

    @staticmethod
    def _collect_all_sources(config: EncodeConfig) -> list[Path]:
        """Retourne les sources uniques (source principale puis extras)."""
        all_sources: list[Path] = [config.source]
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
    ) -> int:
        key = (str(track_type).strip().lower(), Path(source_path), int(stream_index))
        if key in lookup:
            return int(lookup[key])
        if key[0] == "video":
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
            return f"{int(default_map[0])}:v:{int(default_map[1])}"
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
        track_order: list[tuple[int, int]] = []
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
        Prépare la normalisation timeline mkvmerge-like pour les flux multi-source
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
        syncer = MkvmergeLikeTimelineSync(
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
            cmd.extend(["-c:s", "copy"])
        elif config.copy_subtitles and force_copy_subtitles_wildcard:
            for inp_i in subtitle_copy_input_indices:
                cmd.extend(["-map", f"{inp_i}:s?"])
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

        video_input_idx = source_idx.get(config.source, 0)
        video_default_map = (video_input_idx, 0)
        track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]] = [
            ((Path(config.source), 0, "video"), all_sources[video_input_idx], 0)
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

        video_map_key = (Path(config.source), 0, "video")
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

        video_input_idx = source_idx.get(config.source, 0)
        video_default_map = (video_input_idx, 0)

        pass1 = _base()
        _next1, pass1_offset_remap = self._append_offset_aux_inputs(
            pass1,
            self._build_offset_specs(
                config,
                track_mappings=[((Path(config.source), 0, "video"), all_sources[video_input_idx], 0)],
                offset_lookup=offset_lookup,
            ),
            start_input_index=len(all_sources),
        )
        _ = _next1
        pass1_video_map_key = (Path(config.source), 0, "video")
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
            ((Path(config.source), 0, "video"), all_sources[video_input_idx], 0)
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

        pass2_video_map_key = (Path(config.source), 0, "video")
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

        video_default_map = sync_remap.get(
            (config.source, 0, "video"),
            (source_idx.get(config.source, 0), 0),
        )
        track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]] = [
            (
                (Path(config.source), 0, "video"),
                _input_path(int(video_default_map[0]), config.source),
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

        video_map_key = (Path(config.source), 0, "video")
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

        video_default_map = (source_idx.get(config.source, 0), 0)

        pass1 = _base(False)
        _next1, pass1_offset_remap = self._append_offset_aux_inputs(
            pass1,
            self._build_offset_specs(
                config,
                track_mappings=[((Path(config.source), 0, "video"), config.source, 0)],
                offset_lookup=offset_lookup,
            ),
            start_input_index=len(all_sources),
        )
        _ = _next1
        pass1_video_map_key = (Path(config.source), 0, "video")
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

        video_base_map = sync_remap.get((config.source, 0, "video"), video_default_map)
        track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]] = [
            (
                (Path(config.source), 0, "video"),
                _input_path(int(video_base_map[0]), config.source),
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

        pass2_video_map_key = (Path(config.source), 0, "video")
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
                return args
            case "libsvtav1":
                args = ["-c:v", "libsvtav1", "-crf", str(v.crf), "-preset", v.preset]
                if v.extra_params:
                    args.extend(["-svtav1-params", v.extra_params])
                return args
            case "hevc_nvenc":
                return ["-c:v", "hevc_nvenc", "-rc:v", "vbr", "-cq:v", str(v.crf), "-preset:v", v.preset]
            case "hevc_amf":
                return ["-c:v", "hevc_amf", "-rc", "cqp", "-qp_p", str(v.crf), "-qp_i", str(v.crf)]
            case "hevc_qsv":
                return ["-c:v", "hevc_qsv", "-global_quality", str(v.crf), "-look_ahead", "1"]
            case "h264_nvenc":
                return ["-c:v", "h264_nvenc", "-rc:v", "vbr", "-cq:v", str(v.crf), "-preset:v", v.preset]
            case "h264_amf":
                return ["-c:v", "h264_amf", "-rc", "cqp", "-qp_p", str(v.crf), "-qp_i", str(v.crf)]
            case "h264_qsv":
                return ["-c:v", "h264_qsv", "-global_quality", str(v.crf)]
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
                return ["-c:v", "libx264", "-b:v", f"{bitrate_kbps}k", "-preset", v.preset]
            case "libsvtav1":
                return ["-c:v", "libsvtav1", "-b:v", f"{bitrate_kbps}k", "-preset", v.preset]
            case "hevc_nvenc":
                return ["-c:v", "hevc_nvenc", "-b:v", f"{bitrate_kbps}k", "-preset:v", v.preset]
            case "hevc_amf":
                return ["-c:v", "hevc_amf", "-b:v", f"{bitrate_kbps}k"]
            case "hevc_qsv":
                return ["-c:v", "hevc_qsv", "-b:v", f"{bitrate_kbps}k"]
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

        Les encodeurs VAAPI ont besoin d'un upload explicite vers le device
        matériel. On ajoute donc `format=nv12,hwupload` uniquement pour les
        réencodages `_vaapi`, en conservant les autres filtres inchangés.
        """
        vf = self._build_vf(v)
        if v.codec not in _VAAPI_CODECS:
            return vf
        vaapi_upload = "format=nv12,hwupload"
        return f"{vf},{vaapi_upload}" if vf else vaapi_upload

    def _hardware_input_args(self, v: VideoEncodeSettings) -> list[str]:
        """Flags ffmpeg requis avant les entrées pour certains encodeurs matériels."""
        if v.codec not in _VAAPI_CODECS:
            return []
        vaapi_device = self._vaapi_device()
        return ["-vaapi_device", vaapi_device] if vaapi_device else []

    @staticmethod
    def _vaapi_device() -> str | None:
        """Retourne le premier render node VAAPI disponible, ou None."""
        for i in range(8):
            node = Path(f"/dev/dri/renderD{128 + i}")
            if node.exists():
                return str(node)
        return None

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
        match a.codec:
            case "copy":
                args.extend([f"-c:a:{out_idx}", "copy"])
                # truehd_core est un bitstream filter de passthrough.
                # Sur une piste réencodée, ffmpeg l'applique à la sortie encodée
                # (ex. eac3) et échoue car le codec n'est plus TrueHD.
                if a.extract_truehd_core:
                    args.extend([f"-bsf:a:{out_idx}", "truehd_core"])
            case "aac":
                args.extend([f"-c:a:{out_idx}", "aac", f"-b:a:{out_idx}", f"{a.bitrate_kbps}k"])
            case "ac3":
                args.extend([f"-c:a:{out_idx}", "ac3", f"-b:a:{out_idx}", f"{a.bitrate_kbps}k"])
            case "eac3":
                args.extend([f"-c:a:{out_idx}", "eac3", f"-b:a:{out_idx}", f"{a.bitrate_kbps}k"])
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

    def _build_video_only_cmd(self, config: EncodeConfig, output_hevc: Path) -> list[str]:
        """
        ffmpeg : vidéo seule, sortie HEVC brut (-f hevc, sans container).
        Pas d'audio ni de subs. Utilisé pour encoder directement vers un
        flux HEVC injectable, sans passer par un MKV intermédiaire.
        """
        cmd = [self._ffmpeg, "-hide_banner", "-y"]
        cmd.extend(self._ffmpeg_progress_args())
        cmd.extend(self._hardware_input_args(config.video))
        cmd.extend(["-i", str(config.source)])
        vf = self._build_encoder_vf(config.video)
        if vf:
            cmd.extend(["-vf", vf])
        cmd.extend(self._ffmpeg_thread_args())
        cmd.extend(["-map", "0:v:0"])
        cmd.extend(self._video_codec_args(config.video, config.video.bitrate_kbps))
        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            cmd.extend(self._hdr_meta_args(config.video))
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
            c.extend(["-i", str(config.source)])
            if vf:
                c.extend(["-vf", vf])
            c.extend(self._ffmpeg_thread_args())
            c.extend(["-map", "0:v:0"])
            c.extend(self._video_codec_args_bitrate(config.video, bitrate))
            return c

        pass1 = _base() + ["-pass", "1", "-an", "-f", "null", os.devnull]
        pass2 = _base() + ["-pass", "2"]
        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            pass2.extend(self._hdr_meta_args(config.video))
        pass2.extend(["-an", "-f", "hevc", str(output_hevc)])
        return [pass1, pass2]

    def _size_to_bitrate_kbps(self, config: EncodeConfig) -> int:
        duration = config.duration_s or 3600.0
        total_bits = config.video.target_size_mb * 8 * 1024 * 1024
        audio_bps = sum(
            a.bitrate_kbps * 1000
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
        if self._uses_two_pass(config) and not (config.duration_s or 0) > 0:
            errors.append("Durée du fichier source inconnue — mode taille cible impossible.")
        if config.video.inject_hdr_meta and not config.video.tonemap_to_sdr:
            if config.video.master_display and not re.match(
                r"^G\(\d+,\d+\)B\(\d+,\d+\)R\(\d+,\d+\)WP\(\d+,\d+\)L\(\d+,\d+\)$",
                config.video.master_display.strip(),
            ):
                errors.append(
                    "Format master_display invalide. "
                    "Attendu : G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)"
                )
            if config.video.max_cll and not re.match(r"^\d+,\d+$", config.video.max_cll.strip()):
                errors.append("Format MaxCLL invalide. Attendu : MaxCLL,MaxFALL  ex. 1000,400")

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

    def run(self, config: EncodeConfig) -> TaskSignals:
        """
        Lance l'encodage dans un thread secondaire.

        Le mode taille cible exécute deux passes séquentiellement
        dans le même thread et retourne un unique TaskSignals.
        """
        errors = self.validate(config)
        if errors:
            raise EncodeError("\n".join(errors))

        self._log_workflow_type("ENCODE")
        self._log_step(1, "Validation configuration")
        self.log_message.emit("INFO", f"Encodage → {config.output.name}")

        self._log_step(2, "Préparation workspace et attachments")
        work_root = config.work_dir or Path(tempfile.gettempdir())
        process_work_dir = prepare_process_work_dir(
            work_root,
            output_path=config.output,
            fallback_name="encode_job",
        )
        relocated_attachments = relocate_tmdb_covers_to_process_dir(
            [Path(p) for p in config.extra_attachments],
            work_root=work_root,
            process_dir=process_work_dir,
        )

        # Téléchargement différé de la cover TMDB (si présente)
        if config.tmdb_cover is not None:
            tmdb_url, tmdb_filename = config.tmdb_cover
            try:
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

        prepared_config = replace(
            config,
            work_dir=process_work_dir,
            extra_attachments=relocated_attachments,
        )

        prepared_config, cleanup_dir = self._prepare_attachment_config(
            prepared_config,
            work_dir=process_work_dir,
        )
        cleanup_paths: list[Path] = []
        if cleanup_dir is not None:
            cleanup_paths.append(cleanup_dir)
        relocated_attachment_dir = process_work_dir / "attachments"
        if relocated_attachment_dir.exists():
            cleanup_paths.append(relocated_attachment_dir)
        cleanup_paths.append(process_work_dir)

        self._log_step(3, "Normalisation des options HDR dynamiques")
        if not self._is_video_passthrough(prepared_config):
            prepared_config = self._normalize_dynamic_hdr_config(prepared_config)
        elif self._wants_dynamic_hdr_copy(prepared_config):
            # Codec COPY : les NAL units DoVi/HDR10+ sont déjà dans le bitstream source.
            # Extraction + réinjection inutiles sans réencodage — remux direct avec passthrough.
            self.log_message.emit(
                "INFO",
                "Codec COPY : injection DoVi/HDR10+ ignorée — "
                "métadonnées préservées par passthrough ffmpeg.",
            )

        needs_inject = self._needs_metadata_inject(prepared_config)
        self._log_step(
            4,
            "Routage du workflow (sortie directe ou injection metadata)"
            + (" -> injection" if needs_inject else " -> sortie directe"),
        )
        if needs_inject:
            self.log_message.emit(
                "INFO",
                "Injection DoVi/HDR10+: pipeline fichier (pas de pipe direct outillage).",
            )
            self._ensure_inject_storage_available(prepared_config)

        signals = (
            self._run_with_metadata_inject(prepared_config)
            if needs_inject
            else self._run_direct_output(prepared_config, cleanup_paths)
        )
        self._bind_temp_cleanup(signals, cleanup_paths)
        return signals

    def _run_direct_output(
        self,
        config: EncodeConfig,
        cleanup_paths: list[Path],
    ) -> TaskSignals:
        self._log_step(5, "Construction de la commande ffmpeg (sortie directe)")
        cwd = config.work_dir or config.source.parent

        # Multi-source: le pré-scan ffprobe peut être coûteux.
        # On déporte build+exécution dans un worker dédié pour éviter de bloquer l'UI.
        if len(self._collect_all_sources(config)) > 1:
            signals = self._run_direct_output_multisource_async(
                config=config,
                cleanup_paths=cleanup_paths,
                cwd=cwd,
            )
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

        if self._uses_two_pass(config):
            self._log_step(6, "Préparation sync/remap + commandes ffmpeg (2 passes)")
            cmds: list[list[str]]
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []
            try:
                cmds, live_sync_session, sync_cleanup_paths = self._build_runtime_two_pass_with_sync(
                    config,
                    chapter_materialize_dir=chapter_dir,
                )
                cleanup_paths.extend(sync_cleanup_paths)
                self._log_step(7, "Exécution ffmpeg en 2 passes (sortie directe)")
                signals = self._run_two_pass(cmds, cwd=cwd)
            except Exception:
                if live_sync_session is not None:
                    live_sync_session.close()
                raise
            self._bind_live_sync_cleanup(signals, live_sync_session)
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
            )
            cleanup_paths.extend(sync_cleanup_paths)
            self._log_step(7, "Exécution ffmpeg en single pass (sortie directe)")
            signals = self._runner.run(cmd, cwd=cwd, label="ffmpeg")
        except Exception:
            if live_sync_session is not None:
                live_sync_session.close()
            raise
        self._bind_live_sync_cleanup(signals, live_sync_session)
        self._bind_matroska_segment_muxing_patch(signals, config.output)
        self._bind_nfo_write(signals, config.output)
        return signals

    def _run_direct_output_multisource_async(
        self,
        *,
        config: EncodeConfig,
        cleanup_paths: list[Path],
        cwd: Path,
    ) -> TaskSignals:
        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            chapter_dir: Path | None = None
            live_sync_session: LiveSyncSession | None = None
            sync_cleanup_paths: list[Path] = []
            is_two_pass = self._uses_two_pass(config)

            try:
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
                    self._log_step(7, "Exécution ffmpeg en 2 passes (sortie directe)")

                    self.log_message.emit("INFO", "Passe 1/2 (analyse)…")
                    self._runner._run_cmd(
                        cmds[0],
                        cwd=cwd,
                        label="ffmpeg-pass1",
                        progress_cb=lambda line: signals.progress.emit(line),
                        signals=signals,
                    )
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
        if config.copy_dv:
            sidecars += 64 * 1024 * 1024
        if config.copy_hdr10plus:
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
                kbps = max(32, int(audio.bitrate_kbps or 0))
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
                src_path, stream_idx = selection[:2]
                meta = self._describe_attachment_stream(src_path, stream_idx)
                if not meta["is_attached_pic"]:
                    direct_streams.append(selection)
                    continue

                created_any = True
                filename = self._attachment_filename(meta, stream_idx)
                dest = self._unique_attachment_path(tmp_dir, filename)
                self._extract_attached_pic(src_path, stream_idx, dest)
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

    def _extract_attached_pic(self, source: Path, stream_idx: int, dest: Path) -> None:
        """Extrait un ``attached_pic`` vers un vrai fichier image."""
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
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=60,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0 or not dest.exists():
            stderr = (result.stderr or "").strip()
            raise EncodeError(
                f"Extraction attachment échouée pour le stream {stream_idx} de {source.name}: {stderr}"
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

    @staticmethod
    def _track_spec_for_track_order(track_order: int, audio_count: int) -> tuple[str, int] | None:
        if track_order <= 0:
            return None
        if track_order == 1:
            return ("v", 0)

        first_audio = 2
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

        audio_count = len(config.audio_tracks)
        for edit in config.track_meta_edits:
            spec = self._track_spec_for_track_order(int(edit.track_order), audio_count)
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

    def _run_with_metadata_inject(self, config: EncodeConfig) -> TaskSignals:
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
             Pas de dépendance MKVToolNix, pas de fichier audio intermédiaire.
             La source n'est jamais modifiée.
        """
        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

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
                if config.copy_dv:
                    signals.progress.emit("Extraction RPU Dolby Vision…")
                    _run([
                        self._bins["dovi_tool"], "extract-rpu",
                        "-i", str(config.source), "-o", str(rpu_bin),
                    ])
                    _check()

                # ── 2. HDR10+ ────────────────────────────────────────────
                hdr10p_json = tmp / "hdr10p.json"
                if config.copy_hdr10plus:
                    signals.progress.emit("Extraction métadonnées HDR10+…")
                    _run([
                        self._bins["hdr10plus_tool"], "extract",
                        str(config.source), "-o", str(hdr10p_json),
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
                if config.copy_hdr10plus and hdr10p_json.exists():
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
                if config.copy_dv and rpu_bin.exists():
                    cur_size = current_hevc.stat().st_size
                    out_dv = _alloc("enc_dv.hevc", cur_size)
                    signals.progress.emit("Injection RPU Dolby Vision…")
                    _run([
                        self._bins["dovi_tool"],
                        "-m", config.dovi_profile,
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

                video_default_map = (0, 0)
                track_mappings: list[tuple[tuple[Path, int, str], Path | str, int]] = [
                    ((Path(config.source), 0, "video"), _input_path(0, current_video_input), 0)
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

                video_map_key = (Path(config.source), 0, "video")
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

        executor.submit(_task)
        self._bind_matroska_segment_muxing_patch(signals, config.output)
        self._bind_nfo_write(signals, config.output)
        return signals
