"""
core/workflows/remux.py — Workflow de remuxage MKV via FFmpeg.

Classes publiques :
    RemuxWorkflow        — construit et exécute le remuxage via FFmpeg

Re-exports depuis remux_models (pour compatibilité imports existants) :
    TrackEntry, SourceInput, RemuxConfig, RemuxError, tracks_from_file_info

Conventions :
    - Jamais shell=True
    - pathlib.Path pour tous les chemins
    - FFmpeg pour le remux principal
    - Signaux Qt thread-safe (QueuedConnection) pour la communication vers l'UI
"""

from __future__ import annotations

from typing import Callable
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from core.runner import TaskCancelledError, TaskSignals, ToolRunner
from core.version import APP_VERSION_LABEL
from core.workdir import (
    download_tmdb_cover,
    prepare_process_work_dir,
    relocate_tmdb_covers_to_process_dir,
    remove_path,
)
from core.workflows.matroska_header_editor import MatroskaMuxingAppPostAction
from core.workflows.matroska_language_editor import MatroskaLanguagePostAction
from core.workflows.common.chapters import (
    probe_media_duration_seconds,
    write_ffmetadata_chapters,
)
from core.workflows.common.ffmpeg_runtime import (
    cli_path as _cli_path,
    ffmpeg_progress_args,
    ffmpeg_thread_args as _common_ffmpeg_thread_args,
    normalize_ffmpeg_thread_count as _normalize_ffmpeg_thread_count,
)
from core.workflows.common.timeline_sync import needs_strict_interleave as _common_needs_strict_interleave
from core.workflows.common.metadata import STREAM_SPEC_BY_TRACK_TYPE as _STREAM_SPEC_BY_TYPE
from core.workflows.remux_attachments import (
    attachment_names as _attachment_names_helper,
    build_attachment_mapping as _build_attachment_mapping_helper,
    extract_attached_pics as _extract_attached_pics_helper,
    write_mediainfo_nfo as _write_mediainfo_nfo_helper,
)
from core.workflows.remux_command import (
    build_remux_command as _build_remux_command_helper,
    preview_remux_command as _preview_remux_command_helper,
)
from core.workflows.remux_mapping import (
    append_offset_inputs as _append_offset_inputs_helper,
    chapter_map_value as _chapter_map_value_helper,
    disposition_value_for_track as _disposition_value_helper,
    is_dir_writable as _is_dir_writable_helper,
    MappedTrack as _MappedTrack,
    metadata_map_value as _metadata_map_value_helper,
    normalized_language_value as _normalized_language_value_helper,
    offset_input_specs_from_mapped_tracks as _offset_input_specs_from_mapped_tracks_helper,
    offset_seconds as _offset_seconds_helper,
    requires_file_sync_fallback_for_offsets as _requires_file_sync_fallback_for_offsets_helper,
    resolve_mapped_tracks as _resolve_mapped_tracks_helper,
    resolved_global_tags as _resolved_global_tags_helper,
    track_order_parts as _track_order_parts_helper,
)
from core.workflows.remux_models import (
    RemuxConfig,
    RemuxError,
    SourceInput,
    TrackEntry,
    tracks_from_file_info,
)
from core.workflows.remux_sync import (
    bind_temp_cleanup as _bind_temp_cleanup_helper,
    decide_strict_interleave_with_prescan as _decide_strict_interleave_with_prescan_helper,
    prepare_timeline_sync_inputs as _prepare_timeline_sync_inputs_helper,
)
from core.workflows.remux_timeline_sync import (
    LiveSyncSession,
    FfmpegTimelineSync,
    SyncPreparedInput,
    TimelineSyncFallbackHelper,
)

# =============================================================================
# NFO helper
# =============================================================================

def write_mediainfo_nfo(
    output_path: Path,
    log_cb: Callable[[str, str], None],
    mediainfo_bin: str = "mediainfo",
) -> None:
    """Génère un fichier .nfo (même nom que le MKV) avec la sortie brute de mediainfo."""
    _write_mediainfo_nfo_helper(
        output_path,
        log_cb=log_cb,
        mediainfo_bin=mediainfo_bin,
        run_cmd=subprocess.run,
    )


# =============================================================================
# Workflow
# =============================================================================

class RemuxWorkflow(QObject):
    """
    Remux MKV via FFmpeg.

    API publique :
      - build_command(config)
      - preview_command(config)
      - validate(config)
      - run(config)
    """

    log_message = Signal(str, str)

    def __init__(
        self,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        ffmpeg_threads: int | None = None,
        parent: QObject | None = None,
        *,
        writing_application: str = "",
        generate_nfo: bool = True,
        mediainfo_bin: str = "mediainfo",
    ) -> None:
        super().__init__(parent)
        self._ffmpeg = ffmpeg_bin
        self._ffprobe = ffprobe_bin
        self._ffmpeg_threads = _normalize_ffmpeg_thread_count(ffmpeg_threads)
        self._generate_nfo = generate_nfo
        self._mediainfo_bin = mediainfo_bin
        self._runner = ToolRunner(max_workers=1, parent=self)
        self._writing_application = writing_application.strip()
        self._muxing_post_action = MatroskaMuxingAppPostAction(
            app_prefix=MatroskaMuxingAppPostAction.default_prefix(APP_VERSION_LABEL),
            log_cb=self.log_message.emit,
        )
        self._language_post_action = MatroskaLanguagePostAction(
            log_cb=self.log_message.emit,
        )

    def set_ffmpeg_bin(self, ffmpeg_bin: str) -> None:
        self._ffmpeg = ffmpeg_bin

    def set_ffprobe_bin(self, ffprobe_bin: str) -> None:
        self._ffprobe = ffprobe_bin

    def set_ffmpeg_threads(self, ffmpeg_threads: int | None) -> None:
        self._ffmpeg_threads = _normalize_ffmpeg_thread_count(ffmpeg_threads)

    def set_writing_application(self, writing_application: str) -> None:
        self._writing_application = writing_application.strip()

    def set_generate_nfo(self, generate_nfo: bool) -> None:
        self._generate_nfo = generate_nfo

    def set_mediainfo_bin(self, mediainfo_bin: str) -> None:
        self._mediainfo_bin = mediainfo_bin

    def _ffmpeg_thread_args(self) -> list[str]:
        return _common_ffmpeg_thread_args(self._ffmpeg_threads)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, config: RemuxConfig) -> list[str]:
        errors: list[str] = []

        if not config.sources:
            errors.append("Aucun fichier source.")
            return errors

        if config.output.suffix.lower() != ".mkv":
            errors.append("Le backend FFmpeg remux ne supporte que la sortie .mkv.")

        file_indexes = [src.file_index for src in config.sources]
        if len(set(file_indexes)) != len(file_indexes):
            errors.append("Indices de source dupliqués dans la configuration (file_index).")

        for src in config.sources:
            if not src.path.is_file():
                errors.append(f"Fichier source introuvable : {src.path}")
            if src.path == config.output:
                errors.append(f"Le fichier de sortie doit être différent de la source : {src.path.name}")

            seen_attachment_indexes: set[int] = set()
            seen_attachment_local_indexes: set[int] = set()
            for att in src.selected_attachments:
                if att.index < 0:
                    errors.append(
                        "Pièce jointe source invalide : "
                        f"index négatif ({att.index}) dans {src.path.name}"
                    )
                if att.local_index < 0:
                    errors.append(
                        "Pièce jointe source invalide : "
                        f"local_index négatif ({att.local_index}) dans {src.path.name}"
                    )
                if att.index in seen_attachment_indexes:
                    errors.append(
                        "Pièce jointe source dupliquée : "
                        f"stream {att.index} dans {src.path.name}"
                    )
                if att.local_index in seen_attachment_local_indexes:
                    errors.append(
                        "Pièce jointe source dupliquée : "
                        f"local_index {att.local_index} dans {src.path.name}"
                    )
                seen_attachment_indexes.add(att.index)
                seen_attachment_local_indexes.add(att.local_index)

        output_dir = config.output.parent
        if not output_dir.exists():
            errors.append(f"Dossier de sortie inexistant : {output_dir}")
        elif not self._is_dir_writable(output_dir):
            errors.append(
                "Dossier de sortie non inscriptible : "
                f"{output_dir} (vérifiez les protections Windows sur les dossiers Bibliothèques)."
            )

        if not config.track_order:
            errors.append("Aucune piste sélectionnée.")

        track_map_by_id = {
            (src.file_index, t.entry_id): t
            for src in config.sources
            for t in src.tracks
        }
        track_map_by_pair: dict[tuple[int, int], list[TrackEntry]] = {}
        for src in config.sources:
            for track in src.tracks:
                track_map_by_pair.setdefault((src.file_index, track.mkv_tid), []).append(track)
        valid_file_indexes = {src.file_index for src in config.sources}

        for order_item in config.track_order:
            file_index, mkv_tid, entry_id = self._track_order_parts(order_item)
            if file_index not in valid_file_indexes:
                errors.append(f"track_order référence une source inconnue : file_index={file_index}")
                continue
            track = (
                track_map_by_id.get((file_index, entry_id))
                if entry_id
                else next(iter(track_map_by_pair.get((file_index, mkv_tid), [])), None)
            )
            if track is None:
                errors.append(
                    "track_order référence une piste introuvable : "
                    f"file_index={file_index}, stream={mkv_tid}"
                )
                continue
            if track.track_type not in _STREAM_SPEC_BY_TYPE:
                errors.append(
                    "Type de piste non supporté par le backend FFmpeg : "
                    f"{track.track_type} (file_index={file_index}, stream={mkv_tid})"
                )
                continue
            if track.track_type == "video" and int(track.time_shift_ms) < 0:
                errors.append(
                    "Décalage vidéo négatif interdit : "
                    f"file_index={file_index}, stream={mkv_tid}, offset={track.time_shift_ms} ms"
                )

        for extra in config.extra_attachments:
            if not extra.is_file():
                errors.append(f"Pièce jointe manuelle introuvable : {extra}")

        if config.chapter_overrides is not None:
            for idx, chapter in enumerate(config.chapter_overrides):
                try:
                    tc = float(getattr(chapter, "timecode_s", 0.0))
                except (TypeError, ValueError):
                    errors.append(f"Chapitre #{idx + 1} invalide : timecode non numérique.")
                    continue
                if tc < 0:
                    errors.append(f"Chapitre #{idx + 1} invalide : timecode négatif ({tc}).")

        return errors

    @staticmethod
    def _is_dir_writable(path: Path) -> bool:
        return _is_dir_writable_helper(path)

    # ------------------------------------------------------------------
    # Construction de commande
    # ------------------------------------------------------------------

    def build_command(
        self,
        config: RemuxConfig,
        *,
        sync_inputs: list[Path | str] | None = None,
        sync_input_formats: list[str] | None = None,
        extra_inputs: list[Path | str] | None = None,
        chapter_input_index: int | None = None,
        strict_interleave_override: bool | None = None,
        mapped_tracks_override: list[_MappedTrack] | None = None,
    ) -> list[str]:
        return _build_remux_command_helper(
            config,
            ffmpeg_bin=self._ffmpeg,
            ffmpeg_progress_args=ffmpeg_progress_args(),
            ffmpeg_thread_args=self._ffmpeg_thread_args(),
            cli_path=_cli_path,
            sync_inputs=sync_inputs,
            sync_input_formats=sync_input_formats,
            extra_inputs=extra_inputs,
            chapter_input_index=chapter_input_index,
            strict_interleave_override=strict_interleave_override,
            mapped_tracks_override=mapped_tracks_override,
            resolve_mapped_tracks_fn=_resolve_mapped_tracks_helper,
            needs_strict_interleave_fn=_common_needs_strict_interleave,
        )

    def preview_command(self, config: RemuxConfig) -> str:
        return _preview_remux_command_helper(config, build_command=self.build_command)

    # ------------------------------------------------------------------
    # Exécution
    # ------------------------------------------------------------------

    def _log_workflow_type(self, workflow_kind: str) -> None:
        self.log_message.emit("INFO", f"WORKFLOW TYPE - {workflow_kind}")

    def _log_step(self, step_index: int, step_name: str) -> None:
        self.log_message.emit("INFO", f"STEP {step_index} - {step_name}")

    def run(self, config: RemuxConfig) -> TaskSignals:
        self._log_workflow_type("REMUX")
        self._log_step(1, "Validation configuration")
        errors = self.validate(config)
        if errors:
            raise RemuxError("\n".join(errors))

        self.log_message.emit("INFO", f"Remuxage (FFmpeg) \u2192 {config.output.name}")
        self._log_step(2, "Pr\u00e9paration workspace et attachments")
        work_root = config.work_dir or Path(tempfile.gettempdir())
        process_work_dir = prepare_process_work_dir(
            work_root,
            output_path=config.output,
            fallback_name="remux_job",
        )
        relocated_attachments = relocate_tmdb_covers_to_process_dir(
            [Path(p) for p in config.extra_attachments],
            work_root=work_root,
            process_dir=process_work_dir,
        )

        if config.tmdb_cover is not None:
            tmdb_url, tmdb_filename = config.tmdb_cover
            try:
                self.log_message.emit(
                    "INFO",
                    f"T\u00e9l\u00e9chargement cover TMDB : {tmdb_filename}",
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
                    f"Impossible de t\u00e9l\u00e9charger la cover TMDB : {exc}",
                )

        run_config = replace(config, extra_attachments=relocated_attachments)
        cwd = process_work_dir

        signals = TaskSignals()
        self._bind_temp_cleanup(signals, [process_work_dir])
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            nonlocal run_config
            tmp_dir = process_work_dir
            chapter_meta_file: Path | None = None
            live_sync_session: LiveSyncSession | None = None
            sync_prepared: list[SyncPreparedInput] = []
            sync_cleanup_paths: list[Path] = []
            try:
                extra_inputs: list[Path | str] = []
                self._log_step(3, "Analyse du mapping pistes + pr\u00e9-scan de risque")
                mapped_tracks = _resolve_mapped_tracks_helper(run_config)
                strict_interleave = _decide_strict_interleave_with_prescan_helper(
                    run_config,
                    resolve_mapped_tracks=_resolve_mapped_tracks_helper,
                    log_cb=self.log_message.emit,
                )

                extracted_pics = _extract_attached_pics_helper(
                    run_config,
                    tmp_dir,
                    signals,
                    ffmpeg_bin=self._ffmpeg,
                    cli_path=_cli_path,
                    log_cb=self.log_message.emit,
                )
                if extracted_pics:
                    run_config = replace(run_config, extra_attachments=[*run_config.extra_attachments, *extracted_pics])

                if strict_interleave:
                    self._log_step(4, "Synchronisation timeline multi-source (live/fallback)")
                    allow_live_sync = True
                    if _requires_file_sync_fallback_for_offsets_helper(mapped_tracks):
                        allow_live_sync = False
                        self.log_message.emit(
                            "INFO",
                            "D\u00e9calage sur piste \u00e9trang\u00e8re d\u00e9tect\u00e9 : sync live d\u00e9sactiv\u00e9, fallback fichier forc\u00e9.",
                        )
                    mapped_tracks, sync_prepared, live_sync_session = _prepare_timeline_sync_inputs_helper(
                        run_config,
                        mapped_tracks,
                        tmp_dir,
                        signals,
                        allow_live=allow_live_sync,
                        ffmpeg_bin=self._ffmpeg,
                        ffmpeg_thread_args=self._ffmpeg_thread_args(),
                        log_cb=self.log_message.emit,
                        syncer_factory=FfmpegTimelineSync,
                        fallback_helper_factory=TimelineSyncFallbackHelper,
                    )
                    sync_cleanup_paths = [p for p in (item.path for item in sync_prepared) if isinstance(p, Path)]
                else:
                    self._log_step(4, "Synchronisation timeline multi-source (non requise)")

                sync_inputs: list[Path | str] = [item.path for item in sync_prepared]
                sync_input_formats: list[str] = [item.container_format for item in sync_prepared]

                chapter_input_index: int | None = None
                if run_config.chapter_overrides:
                    self._log_step(5, "Mat\u00e9rialisation des chapitres FFMetadata")
                    duration_s = probe_media_duration_seconds(self._ffprobe, run_config.sources[0].path)
                    chapter_meta_file = write_ffmetadata_chapters(
                        entries=run_config.chapter_overrides,
                        out_dir=tmp_dir,
                        duration_s=duration_s,
                    )
                    extra_inputs.append(chapter_meta_file)
                    chapter_input_index = len(run_config.sources) + len(sync_inputs) + len(extra_inputs) - 1
                else:
                    self._log_step(5, "Chapitres: copie source ou d\u00e9sactiv\u00e9 (pas d'override)")

                self._log_step(6, "Construction de la commande ffmpeg remux")
                cmd = self.build_command(
                    run_config,
                    sync_inputs=sync_inputs,
                    sync_input_formats=sync_input_formats,
                    extra_inputs=extra_inputs,
                    chapter_input_index=chapter_input_index,
                    strict_interleave_override=strict_interleave,
                    mapped_tracks_override=mapped_tracks,
                )
                self.log_message.emit("INFO", "$ " + " ".join(str(c) for c in cmd))

                self._log_step(7, "Ex\u00e9cution du remux ffmpeg")
                output = self._runner._run_cmd(
                    cmd,
                    cwd=cwd,
                    label="ffmpeg-remux",
                    progress_cb=lambda line: signals.progress.emit(line),
                    signals=signals,
                )
                self._log_step(8, "Post-action: Patch & Cleanup")
                self._muxing_post_action.apply_if_mkv(run_config.output)
                self._language_post_action.apply_if_mkv(run_config.output)
                self._write_nfo(run_config.output)
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
                try:
                    if chapter_meta_file is not None:
                        chapter_meta_file.unlink(missing_ok=True)
                except Exception:
                    pass
                for path in sync_cleanup_paths:
                    try:
                        remove_path(path)
                    except OSError:
                        pass
                executor.shutdown(wait=False)

        executor.submit(_task)
        return signals

    def _write_nfo(self, output_path: Path) -> None:
        if self._generate_nfo:
            write_mediainfo_nfo(output_path, log_cb=self.log_message.emit, mediainfo_bin=self._mediainfo_bin)

    def _bind_temp_cleanup(self, signals: TaskSignals, cleanup_paths: list[Path]) -> None:
        """Supprime les dossiers temporaires du workflow quand le traitement se termine."""
        _bind_temp_cleanup_helper(signals, cleanup_paths)

    @staticmethod
    def _track_order_parts(
        item: tuple[int, int] | tuple[int, int, str],
    ) -> tuple[int, int, str | None]:
        return _track_order_parts_helper(item)

    def _decide_strict_interleave_with_prescan(self, config: RemuxConfig) -> bool:
        return _decide_strict_interleave_with_prescan_helper(
            config,
            resolve_mapped_tracks=_resolve_mapped_tracks_helper,
            log_cb=self.log_message.emit,
        )

    # ------------------------------------------------------------------
    # Helpers: chapitres (contrat interne encode -> remux)
    # ------------------------------------------------------------------

    def _probe_duration_seconds(self, source: Path) -> float | None:
        return probe_media_duration_seconds(self._ffprobe, source)

    def _write_ffmetadata_chapters(
        self,
        entries: list,
        out_dir: Path,
        duration_s: float | None,
    ) -> Path:
        return write_ffmetadata_chapters(entries, out_dir, duration_s)
