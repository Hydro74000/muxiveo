"""
core/workflows/remux_ffmpeg.py — Workflow de remuxage MKV via FFmpeg.

Objectif : fournir un backend sans mkvmerge pour les besoins suivants :
  - langues de piste (ISO + IETF),
  - chapitres (copie ou overrides),
  - tags globaux choisis,
  - attachements (existants + manuels), y compris attached_pic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from core.inspector import AttachmentInfo
from core.lang_tags import Rfc5646LanguageTags as LangTags
from core.runner import TaskCancelledError, TaskSignals, ToolRunner
from core.subprocess_utils import subprocess_text_kwargs
from core.workdir import prepare_process_work_dir, relocate_tmdb_covers_to_process_dir, remove_path
from core.workflows.remux import RemuxConfig, RemuxError, TrackEntry
from core.workflows.remux_timeline_sync import (
    LiveSyncSession,
    MkvmergeLikeTimelineSync,
    TimelineSyncFallbackHelper,
)


_MIME_BY_EXT: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ttf": "application/x-truetype-font",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain",
    ".xml": "application/xml",
}

_EXT_BY_MIME: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
    "application/x-truetype-font": ".ttf",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "text/plain": ".txt",
    "application/xml": ".xml",
}

_STREAM_SPEC_BY_TYPE: dict[str, str] = {
    "video": "v",
    "audio": "a",
    "subtitle": "s",
}


@dataclass(frozen=True)
class _MappedTrack:
    source_input_idx: int
    source_file_index: int
    source_path: Path | str
    stream_index: int
    track: TrackEntry
    out_type_index: int


@dataclass(frozen=True)
class _OffsetInputSpec:
    map_key: tuple[int, int, str, int]
    input_path: Path | str
    input_stream_index: int
    offset_ms: int


@dataclass(frozen=True)
class _AttachmentSpec:
    path: Path
    filename: str
    mimetype: str


def _cli_path(path: Path | str) -> str:
    if isinstance(path, str):
        return path
    text = str(path)
    if text.startswith("\\\\.\\pipe\\"):
        return text
    return path.as_posix()


def _mime_for(path: Path) -> str:
    return _MIME_BY_EXT.get(path.suffix.lower(), "application/octet-stream")


def _sanitize_filename(name: str, fallback: str) -> str:
    clean = Path((name or "").strip()).name
    return clean or fallback


def _ffmeta_escape(value: str) -> str:
    text = str(value).replace("\\", "\\\\")
    text = text.replace("\n", " ")
    text = text.replace(";", "\\;").replace("#", "\\#").replace("=", "\\=")
    return text


def _default_ffmpeg_thread_count() -> int:
    """Default FFmpeg thread count: logical CPU count × 0.75, rounded up."""
    cpu_count = os.cpu_count() or 1
    return max(1, (cpu_count * 3 + 3) // 4)


def _normalize_ffmpeg_thread_count(value: int | None) -> int:
    """Return a safe FFmpeg thread count, preserving 0 as ffmpeg auto mode."""
    if value is None or value < 0:
        return _default_ffmpeg_thread_count()
    return value


class FfmpegRemuxWorkflow(QObject):
    """
    Remux MKV via FFmpeg (sans mkvmerge).

    API volontairement alignée avec RemuxWorkflow :
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
    ) -> None:
        super().__init__(parent)
        self._ffmpeg = ffmpeg_bin
        self._ffprobe = ffprobe_bin
        self._ffmpeg_threads = _normalize_ffmpeg_thread_count(ffmpeg_threads)
        self._runner = ToolRunner(max_workers=1, parent=self)
        self._writing_application = writing_application.strip()

    def set_ffmpeg_bin(self, ffmpeg_bin: str) -> None:
        self._ffmpeg = ffmpeg_bin

    def set_ffprobe_bin(self, ffprobe_bin: str) -> None:
        self._ffprobe = ffprobe_bin

    def set_ffmpeg_threads(self, ffmpeg_threads: int | None) -> None:
        self._ffmpeg_threads = _normalize_ffmpeg_thread_count(ffmpeg_threads)

    def set_writing_application(self, writing_application: str) -> None:
        self._writing_application = writing_application.strip()

    def _ffmpeg_thread_args(self) -> list[str]:
        return ["-threads", str(self._ffmpeg_threads)]

    @staticmethod
    def _ffmpeg_progress_args() -> list[str]:
        return ["-progress", "pipe:1", "-nostats"]

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

        track_map = {
            (src.file_index, t.mkv_tid): t
            for src in config.sources
            for t in src.tracks
        }
        valid_file_indexes = {src.file_index for src in config.sources}

        for file_index, mkv_tid in config.track_order:
            if file_index not in valid_file_indexes:
                errors.append(f"track_order référence une source inconnue : file_index={file_index}")
                continue
            track = track_map.get((file_index, mkv_tid))
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

    @staticmethod
    def _offset_seconds(offset_ms: int) -> str:
        return f"{abs(int(offset_ms)) / 1000.0:.3f}"

    def _offset_input_specs_from_mapped_tracks(
        self,
        mapped_tracks: list[_MappedTrack],
    ) -> list[_OffsetInputSpec]:
        specs: list[_OffsetInputSpec] = []
        for mt in mapped_tracks:
            offset_ms = int(getattr(mt.track, "time_shift_ms", 0) or 0)
            if offset_ms == 0:
                continue
            if mt.track.track_type == "video" and offset_ms < 0:
                raise RemuxError(
                    "Décalage vidéo négatif interdit : "
                    f"file_index={mt.source_file_index}, stream={mt.stream_index}, offset={offset_ms} ms"
                )
            specs.append(_OffsetInputSpec(
                map_key=(
                    int(mt.source_file_index),
                    int(mt.stream_index),
                    str(mt.track.track_type),
                    int(mt.out_type_index),
                ),
                input_path=mt.source_path,
                input_stream_index=int(mt.stream_index),
                offset_ms=offset_ms,
            ))
        return specs

    def _append_offset_inputs(
        self,
        cmd: list[str],
        specs: list[_OffsetInputSpec],
        *,
        start_input_index: int,
    ) -> tuple[int, dict[tuple[int, int, str, int], tuple[int, int]]]:
        next_input_index = start_input_index
        input_by_key: dict[tuple[str, int, int, str], int] = {}
        remap: dict[tuple[int, int, str, int], tuple[int, int]] = {}

        for spec in specs:
            input_key = (
                _cli_path(spec.input_path),
                int(spec.input_stream_index),
                int(spec.offset_ms),
                str(spec.map_key[2]),
            )
            input_idx = input_by_key.get(input_key)
            if input_idx is None:
                if int(spec.offset_ms) > 0:
                    cmd.extend(["-itsoffset", self._offset_seconds(spec.offset_ms), "-i", _cli_path(spec.input_path)])
                else:
                    cmd.extend(["-ss", self._offset_seconds(spec.offset_ms), "-i", _cli_path(spec.input_path)])
                input_idx = next_input_index
                input_by_key[input_key] = input_idx
                next_input_index += 1

            remap[spec.map_key] = (int(input_idx), int(spec.input_stream_index))

        return next_input_index, remap

    # ------------------------------------------------------------------
    # Construction de commande
    # ------------------------------------------------------------------

    def build_command(
        self,
        config: RemuxConfig,
        *,
        sync_inputs: list[Path | str] | None = None,
        extra_inputs: list[Path | str] | None = None,
        chapter_input_index: int | None = None,
        attachments: list[_AttachmentSpec] | None = None,
        strict_interleave_override: bool | None = None,
        mapped_tracks_override: list[_MappedTrack] | None = None,
    ) -> list[str]:
        mapped_tracks = (
            mapped_tracks_override
            if mapped_tracks_override is not None
            else self._resolve_mapped_tracks(config)
        )
        needs_strict_interleave = (
            self._needs_strict_interleave(mapped_tracks)
            if strict_interleave_override is None
            else strict_interleave_override
        )

        cmd: list[str] = [self._ffmpeg, "-hide_banner", "-y"]
        cmd.extend(self._ffmpeg_progress_args())
        cmd.extend(self._ffmpeg_thread_args())

        for src in config.sources:
            cmd.extend(["-i", _cli_path(src.path)])
        for p in (sync_inputs or []):
            # Les entrées sync live (FIFO/fichiers normalisés) sont Matroska.
            # Forcer le demuxer évite les blocages de probing sur flux live.
            cmd.extend(["-f", "matroska", "-i", _cli_path(p)])
        for p in (extra_inputs or []):
            cmd.extend(["-i", _cli_path(p)])

        sync_count = len(sync_inputs or [])
        extra_count = len(extra_inputs or [])
        offset_specs = self._offset_input_specs_from_mapped_tracks(mapped_tracks)
        _, offset_remap = self._append_offset_inputs(
            cmd,
            offset_specs,
            start_input_index=len(config.sources) + sync_count + extra_count,
        )

        for mt in mapped_tracks:
            map_key = (
                int(mt.source_file_index),
                int(mt.stream_index),
                str(mt.track.track_type),
                int(mt.out_type_index),
            )
            remapped = offset_remap.get(map_key)
            if remapped is None:
                cmd.extend(["-map", f"{mt.source_input_idx}:{mt.stream_index}"])
            else:
                cmd.extend(["-map", f"{remapped[0]}:{remapped[1]}"])

        cmd.extend(["-c", "copy", "-default_mode", "passthrough"])
        if needs_strict_interleave:
            # Multi-source + sous-titres clairsemés: sans entrelacement strict,
            # l'audio importé d'une autre source peut être écrit très loin de la
            # vidéo, ce qui dégrade fortement le seeking côté Plex.
            cmd.extend(["-max_interleave_delta", "0"])
            cmd.extend(["-max_muxing_queue_size", "9999"])
        cmd.extend(["-map_metadata", self._metadata_map_value(config, chapter_input_index)])
        cmd.extend(["-map_chapters", self._chapter_map_value(config, chapter_input_index)])

        # Suppression des balises ENCODER et CREATION_TIME transportées depuis la source.
        cmd.extend(["-metadata", "encoder=", "-metadata", "creation_time="])

        for key, value in self._resolved_global_tags(config).items():
            cmd.extend(["-metadata", f"{key}={value}"])

        for mt in mapped_tracks:
            stream_spec = _STREAM_SPEC_BY_TYPE[mt.track.track_type]
            out_idx = mt.out_type_index

            lang_value = self._normalized_language_value(mt.track)
            cmd.extend([f"-metadata:s:{stream_spec}:{out_idx}", f"language={lang_value}"])
            # Purge l'ancien champ IETF pour éviter un doublon ISO + IETF incohérent.
            cmd.extend([f"-metadata:s:{stream_spec}:{out_idx}", "language-ietf="])

            cmd.extend([f"-metadata:s:{stream_spec}:{out_idx}", f"title={mt.track.title or ''}"])
            cmd.extend([f"-disposition:{stream_spec}:{out_idx}", self._disposition_value(mt.track)])

        final_attachments = attachments if attachments is not None else self._preview_attachments(config)
        for idx, att in enumerate(final_attachments):
            cmd.extend(["-attach", _cli_path(att.path)])
            cmd.extend([f"-metadata:s:t:{idx}", f"mimetype={att.mimetype}"])
            cmd.extend([f"-metadata:s:t:{idx}", f"filename={att.filename}"])

        cmd.append(_cli_path(config.output))
        return cmd

    def preview_command(self, config: RemuxConfig) -> str:
        extra_inputs: list[Path | str] = []
        chapter_input_index: int | None = None
        if config.chapter_overrides:
            extra_inputs.append(Path("<chapitres.ffmetadata>"))
            chapter_input_index = len(config.sources)

        parts = self.build_command(
            config,
            extra_inputs=extra_inputs,
            chapter_input_index=chapter_input_index,
            attachments=self._preview_attachments(config),
        )
        if not parts:
            return ""

        lines: list[str] = [parts[0]]
        i = 1
        while i < len(parts):
            p = parts[i]
            if p.startswith("-") and i + 1 < len(parts) and not parts[i + 1].startswith("-"):
                lines.append(f"    {p} {parts[i + 1]}")
                i += 2
            else:
                lines.append(f"    {p}")
                i += 1

        return " \\\n".join(lines)

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

        self.log_message.emit("INFO", f"Remuxage (FFmpeg) → {config.output.name}")
        self._log_step(2, "Préparation workspace et attachments")
        work_root = config.work_dir or Path(tempfile.gettempdir())
        process_work_dir = prepare_process_work_dir(
            work_root,
            output_path=config.output,
            fallback_name="remux_ffmpeg_job",
        )
        relocated_attachments = relocate_tmdb_covers_to_process_dir(
            [Path(p) for p in config.extra_attachments],
            work_root=work_root,
            process_dir=process_work_dir,
        )
        run_config = replace(config, extra_attachments=relocated_attachments)
        cwd = process_work_dir

        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def _task() -> None:
            tmp_dir = process_work_dir
            chapter_meta_file: Path | None = None
            live_sync_session: LiveSyncSession | None = None
            sync_inputs: list[Path | str] = []
            sync_cleanup_paths: list[Path] = []
            try:
                self._log_step(3, "Préparation des attachments (source + externes)")
                prepared_attachments = self._materialize_attachments(run_config, tmp_dir, signals)

                extra_inputs: list[Path | str] = []
                self._log_step(4, "Analyse du mapping pistes + pré-scan de risque")
                mapped_tracks = self._resolve_mapped_tracks(run_config)
                strict_interleave = self._decide_strict_interleave_with_prescan(run_config)

                if strict_interleave:
                    self._log_step(5, "Synchronisation timeline multi-source (live/fallback)")
                    allow_live_sync = True
                    if self._requires_file_sync_fallback_for_offsets(mapped_tracks):
                        allow_live_sync = False
                        self.log_message.emit(
                            "INFO",
                            "Décalage sur piste étrangère détecté : sync live désactivé, fallback fichier forcé.",
                        )
                    mapped_tracks, sync_inputs, live_sync_session = self._prepare_mkvmerge_like_sync_inputs(
                        run_config,
                        mapped_tracks,
                        tmp_dir,
                        signals,
                        allow_live=allow_live_sync,
                    )
                    sync_cleanup_paths = [p for p in sync_inputs if isinstance(p, Path)]
                else:
                    self._log_step(5, "Synchronisation timeline multi-source (non requise)")

                chapter_input_index: int | None = None
                if run_config.chapter_overrides:
                    self._log_step(6, "Matérialisation des chapitres FFMetadata")
                    duration_s = self._probe_duration_seconds(run_config.sources[0].path)
                    chapter_meta_file = self._write_ffmetadata_chapters(
                        entries=run_config.chapter_overrides,
                        out_dir=tmp_dir,
                        duration_s=duration_s,
                    )
                    extra_inputs.append(chapter_meta_file)
                    chapter_input_index = len(run_config.sources) + len(sync_inputs) + len(extra_inputs) - 1
                else:
                    self._log_step(6, "Chapitres: copie source ou désactivé (pas d'override)")

                self._log_step(7, "Construction de la commande ffmpeg remux")
                cmd = self.build_command(
                    run_config,
                    sync_inputs=sync_inputs,
                    extra_inputs=extra_inputs,
                    chapter_input_index=chapter_input_index,
                    attachments=prepared_attachments,
                    strict_interleave_override=strict_interleave,
                    mapped_tracks_override=mapped_tracks,
                )
                self.log_message.emit("INFO", "$ " + " ".join(str(c) for c in cmd))

                self._log_step(8, "Exécution du remux ffmpeg")
                output = self._runner._run_cmd(
                    cmd,
                    cwd=cwd,
                    label="ffmpeg-remux",
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
                shutil.rmtree(tmp_dir, ignore_errors=True)
                executor.shutdown(wait=False)

        executor.submit(_task)
        return signals

    # ------------------------------------------------------------------
    # Helpers: mapping pistes / tags / langues / flags
    # ------------------------------------------------------------------

    @staticmethod
    def _needs_strict_interleave(mapped_tracks: list[_MappedTrack]) -> bool:
        """
        Détecte le pattern réellement à risque observé côté Plex:
        - remux multi-source effectif (pas juste `len(config.sources) > 1`),
        - sous-titres en sortie (flux clairsemés),
        - au moins une piste audio venant d'une autre source que la vidéo de référence.
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

    @staticmethod
    def _requires_file_sync_fallback_for_offsets(mapped_tracks: list[_MappedTrack]) -> bool:
        """
        Le mode live (FIFO/pipe) n'est pas robuste pour les offsets piste-par-piste
        sur des flux étrangers. On force alors un fallback fichier explicite.
        """
        primary_video = next((mt for mt in mapped_tracks if mt.track.track_type == "video"), None)
        if primary_video is None:
            return False

        return any(
            mt.track.track_type in {"audio", "subtitle"}
            and mt.source_file_index != primary_video.source_file_index
            and int(getattr(mt.track, "time_shift_ms", 0) or 0) != 0
            for mt in mapped_tracks
        )

    def _decide_strict_interleave_with_prescan(self, config: RemuxConfig) -> bool:
        mapped_tracks = self._resolve_mapped_tracks(config)
        if self._requires_file_sync_fallback_for_offsets(mapped_tracks):
            self.log_message.emit(
                "INFO",
                "Décalage sur piste étrangère détecté : sync timeline activé.",
            )
            return True

        base_risk = self._needs_strict_interleave(mapped_tracks)
        if not base_risk:
            return False

        source_by_index = {src.file_index: src for src in config.sources}
        subtitle_tracks = [
            mt for mt in mapped_tracks
            if mt.track.track_type == "subtitle"
        ]
        if not subtitle_tracks:
            return False

        sparse_hits = 0
        scanned = 0

        for mt in subtitle_tracks:
            src = source_by_index.get(mt.source_file_index)
            if src is None:
                continue
            scanned += 1
            is_sparse = self._is_sparse_subtitle_stream(src, mt.stream_index)
            if is_sparse is None:
                self.log_message.emit(
                    "WARNING",
                    "Pré-scan sous-titres indisponible; activation du mode interleave strict par sécurité.",
                )
                return True
            if is_sparse:
                sparse_hits += 1

        decision = sparse_hits > 0
        if decision:
            self.log_message.emit(
                "INFO",
                f"Pré-scan ffprobe: {sparse_hits}/{max(scanned, 1)} piste(s) sous-titres clairsemée(s) -> interleave strict activé.",
            )
        else:
            self.log_message.emit(
                "INFO",
                "Pré-scan ffprobe: sous-titres denses -> interleave strict non activé.",
            )
        return decision

    def _is_sparse_subtitle_stream(self, source, stream_index: int) -> bool | None:
        subtitle_ids = sorted(t.mkv_tid for t in source.tracks if t.track_type == "subtitle")
        if stream_index not in subtitle_ids:
            return None
        subtitle_ordinal = subtitle_ids.index(stream_index)

        cmd = [
            self._ffprobe,
            "-v", "quiet",
            "-select_streams", f"s:{subtitle_ordinal}",
            "-show_packets",
            "-show_entries", "packet=pts_time",
            "-of", "csv=p=0",
            str(source.path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=40,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            return None

        if result.returncode != 0:
            return None

        pts_values: list[float] = []
        for raw_line in (result.stdout or "").splitlines():
            token = raw_line.strip().split(",", 1)[0].strip()
            if not token or token == "N/A":
                continue
            try:
                pts_values.append(float(token))
            except ValueError:
                continue

        if len(pts_values) < 2:
            return True

        gaps = [
            b - a
            for a, b in zip(pts_values, pts_values[1:])
            if b > a
        ]
        if not gaps:
            return True

        avg_gap = sum(gaps) / len(gaps)
        sorted_gaps = sorted(gaps)
        p95_gap = sorted_gaps[min(len(sorted_gaps) - 1, int(len(sorted_gaps) * 0.95))]
        max_gap = sorted_gaps[-1]

        span = max(1.0, pts_values[-1] - pts_values[0])
        cue_rate = len(pts_values) / span  # cues/s

        # Heuristique orientée "flux texte clairsemés" (SRT/ASS classiques).
        return (
            avg_gap >= 1.5
            or p95_gap >= 4.0
            or max_gap >= 10.0
            or cue_rate < 0.8
        )

    def _prepare_mkvmerge_like_sync_inputs(
        self,
        config: RemuxConfig,
        mapped_tracks: list[_MappedTrack],
        tmp_dir: Path,
        signals: TaskSignals,
        *,
        allow_live: bool = True,
    ) -> tuple[list[_MappedTrack], list[Path | str], LiveSyncSession | None]:
        """
        Délègue la normalisation des flux étrangers à un utilitaire dédié afin de
        conserver une logique testable et réutilisable hors workflow.
        """
        syncer = MkvmergeLikeTimelineSync(
            ffmpeg_bin=self._ffmpeg,
            ffmpeg_thread_args=self._ffmpeg_thread_args(),
            log_cb=lambda msg: self.log_message.emit("INFO", msg),
        )
        prepared_result = TimelineSyncFallbackHelper(
            syncer=syncer,
            work_dir=tmp_dir,
            ram_dir=TimelineSyncFallbackHelper.default_ram_dir(),
            log_cb=lambda msg: self.log_message.emit("INFO", msg),
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
        remapped: list[_MappedTrack] = []
        for mt in mapped_tracks:
            key = (mt.source_file_index, mt.stream_index, mt.track.track_type)
            hit = remap.get(key)
            if hit is None:
                remapped.append(mt)
                continue
            remapped.append(replace(
                mt,
                source_input_idx=hit.input_idx,
                source_path=hit.path,
                stream_index=0,
            ))

        return remapped, [item.path for item in prepared], live_session

    def _resolve_mapped_tracks(self, config: RemuxConfig) -> list[_MappedTrack]:
        file_index_to_input_idx = {
            src.file_index: i
            for i, src in enumerate(config.sources)
        }

        track_map = {
            (src.file_index, t.mkv_tid): (src.path, t)
            for src in config.sources
            for t in src.tracks
        }

        type_counters: dict[str, int] = {"video": 0, "audio": 0, "subtitle": 0}
        mapped: list[_MappedTrack] = []

        for file_index, mkv_tid in config.track_order:
            input_idx = file_index_to_input_idx.get(file_index)
            if input_idx is None:
                raise RemuxError(f"Source inconnue dans track_order : file_index={file_index}")

            found = track_map.get((file_index, mkv_tid))
            if found is None:
                raise RemuxError(
                    "Piste introuvable dans track_order : "
                    f"file_index={file_index}, stream={mkv_tid}"
                )
            src_path, track = found
            if track.track_type not in _STREAM_SPEC_BY_TYPE:
                raise RemuxError(
                    "Type de piste non supporté en remux FFmpeg : "
                    f"{track.track_type} (file_index={file_index}, stream={mkv_tid})"
                )

            out_type_index = type_counters[track.track_type]
            type_counters[track.track_type] += 1
            mapped.append(_MappedTrack(
                source_input_idx=input_idx,
                source_file_index=file_index,
                source_path=src_path,
                stream_index=mkv_tid,
                track=track,
                out_type_index=out_type_index,
            ))

        return mapped

    @staticmethod
    def _chapter_map_value(config: RemuxConfig, chapter_input_index: int | None) -> str:
        if config.chapter_overrides is not None:
            if config.chapter_overrides and chapter_input_index is not None:
                return str(chapter_input_index)
            return "-1"
        return "0" if config.keep_chapters else "-1"

    @staticmethod
    def _metadata_map_value(config: RemuxConfig, chapter_input_index: int | None) -> str:
        # tag_overrides explicite (y compris dict vide) => aucune recopie automatique.
        if config.tag_overrides is not None:
            if config.chapter_overrides and chapter_input_index is not None:
                return str(chapter_input_index)
            return "-1"
        # Sinon, reproduit la sémantique historique "copy_tags" quand disponible.
        for input_idx, src in enumerate(config.sources):
            if src.copy_tags:
                return str(input_idx)
        return "-1"

    def _resolved_global_tags(self, config: RemuxConfig) -> dict[str, str]:
        tags: dict[str, str] = {}

        if config.tag_overrides is not None:
            for key, value in config.tag_overrides.items():
                key_s = str(key).strip()
                value_s = str(value).strip()
                if not key_s or not value_s:
                    continue
                tags[key_s] = value_s

        if config.file_title.strip():
            tags["title"] = config.file_title.strip()
        if self._writing_application:
            # Champ segment Matroska "MuxingApp" (équivalent mkvpropedit: muxing-application).
            tags["muxing_application"] = self._writing_application

        return tags

    @staticmethod
    def _normalized_language_value(track: TrackEntry) -> str:
        raw = (track.language or "").strip() or "und"
        canonical = LangTags.normalize(raw) or raw

        if canonical.lower() == "und":
            return "und"

        regional = LangTags.regionalize_track_language(canonical, track.title) or canonical
        if LangTags.is_valid(regional) and regional.lower() != "und":
            return regional
        if LangTags.is_valid(canonical) and canonical.lower() != "und":
            return canonical
        return "und"

    @staticmethod
    def _disposition_value(track: TrackEntry) -> str:
        flags: list[str] = []
        if track.flag_default:
            flags.append("default")
        if track.flag_forced:
            flags.append("forced")
        if track.flag_hearing_impaired:
            flags.append("hearing_impaired")
        if track.flag_visual_impaired:
            flags.append("visual_impaired")
        if track.flag_original:
            flags.append("original")
        if track.flag_commentary:
            flags.append("comment")
        return "+".join(flags) if flags else "0"

    # ------------------------------------------------------------------
    # Helpers: chapitres (ffmetadata)
    # ------------------------------------------------------------------

    def _probe_duration_seconds(self, source: Path) -> float | None:
        cmd = [
            self._ffprobe,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
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
            payload = json.loads(result.stdout or "{}")
            raw = (payload.get("format") or {}).get("duration")
            if raw is None:
                return None
            value = float(raw)
            return value if value > 0 else None
        except Exception:
            return None

    def _write_ffmetadata_chapters(
        self,
        entries: list,
        out_dir: Path,
        duration_s: float | None,
    ) -> Path:
        sorted_entries = sorted(entries, key=lambda e: float(getattr(e, "timecode_s", 0.0)))

        if duration_s is None:
            duration_s = max((float(getattr(e, "timecode_s", 0.0)) for e in sorted_entries), default=0.0) + 1.0
        total_ms = max(1, int(round(duration_s * 1000.0)))

        lines: list[str] = [";FFMETADATA1"]
        for idx, chapter in enumerate(sorted_entries):
            start_ms = max(0, int(round(float(getattr(chapter, "timecode_s", 0.0)) * 1000.0)))
            if idx + 1 < len(sorted_entries):
                end_ms = max(start_ms + 1, int(round(float(getattr(sorted_entries[idx + 1], "timecode_s", 0.0)) * 1000.0)))
            else:
                end_ms = max(start_ms + 1, total_ms)

            lines.extend([
                "",
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={start_ms}",
                f"END={end_ms}",
                f"title={_ffmeta_escape(str(getattr(chapter, 'name', '') or ''))}",
            ])

        ffmeta_path = out_dir / "chapters.ffmetadata"
        ffmeta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ffmeta_path

    # ------------------------------------------------------------------
    # Helpers: attachements
    # ------------------------------------------------------------------

    def _preview_attachments(self, config: RemuxConfig) -> list[_AttachmentSpec]:
        specs: list[_AttachmentSpec] = []

        for src in config.sources:
            for att in sorted(src.selected_attachments, key=lambda a: a.local_index):
                meta_name, _extract_name = self._attachment_names(att)
                mimetype = (att.mimetype or "").strip() or _mime_for(Path(meta_name))
                specs.append(_AttachmentSpec(
                    path=Path(f"<extract:{src.path.name}:{att.index}>"),
                    filename=meta_name,
                    mimetype=mimetype,
                ))

        for att_path in config.extra_attachments:
            name = "cover" if att_path.stem.lower() == "cover" else att_path.name
            specs.append(_AttachmentSpec(
                path=att_path,
                filename=name,
                mimetype=_mime_for(att_path),
            ))

        return self._dedupe_attachment_filenames(specs)

    def _materialize_attachments(
        self,
        config: RemuxConfig,
        tmp_dir: Path,
        signals: TaskSignals,
    ) -> list[_AttachmentSpec]:
        specs: list[_AttachmentSpec] = []

        for src in config.sources:
            for att in sorted(src.selected_attachments, key=lambda a: a.local_index):
                if signals._cancel_event.is_set():
                    raise TaskCancelledError()

                meta_name, extract_name = self._attachment_names(att)
                out_path = self._unique_path(tmp_dir, extract_name)
                self._extract_attachment(src.path, att, out_path)
                mimetype = (att.mimetype or "").strip() or _mime_for(out_path)
                specs.append(_AttachmentSpec(
                    path=out_path,
                    filename=meta_name,
                    mimetype=mimetype,
                ))

        for att_path in config.extra_attachments:
            if signals._cancel_event.is_set():
                raise TaskCancelledError()
            name = "cover" if att_path.stem.lower() == "cover" else att_path.name
            specs.append(_AttachmentSpec(
                path=att_path,
                filename=name,
                mimetype=_mime_for(att_path),
            ))

        return self._dedupe_attachment_filenames(specs)

    def _extract_attachment(self, source: Path, att: AttachmentInfo, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)

        if att.is_attached_pic:
            decode_then_image = [
                self._ffmpeg,
                "-hide_banner", "-y",
                "-i", _cli_path(source),
                "-map", f"0:{att.index}",
                *self._ffmpeg_thread_args(),
                "-frames:v", "1",
                _cli_path(destination),
            ]
            try:
                self._run_extract_cmd(decode_then_image, destination, att, source)
            except RemuxError:
                # Fallback conservateur : certains flux attached_pic se copient tels quels.
                copy_mode = [
                    self._ffmpeg,
                    "-hide_banner", "-y",
                    "-i", _cli_path(source),
                    "-map", f"0:{att.index}",
                    *self._ffmpeg_thread_args(),
                    "-c", "copy",
                    "-frames:v", "1",
                    _cli_path(destination),
                ]
                self._run_extract_cmd(copy_mode, destination, att, source)
            return

        dump_cmd = [
            self._ffmpeg,
            "-hide_banner", "-y",
            *self._ffmpeg_thread_args(),
            f"-dump_attachment:{att.index}", _cli_path(destination),
            "-i", _cli_path(source),
            "-f", "null", "-",
        ]
        try:
            self._run_extract_cmd(dump_cmd, destination, att, source)
        except RemuxError:
            # Fallback conservateur : certains fichiers marquent mal attached_pic.
            fallback = [
                self._ffmpeg,
                "-hide_banner", "-y",
                "-i", _cli_path(source),
                "-map", f"0:{att.index}",
                *self._ffmpeg_thread_args(),
                "-c", "copy",
                "-frames:v", "1",
                _cli_path(destination),
            ]
            self._run_extract_cmd(fallback, destination, att, source)

    def _run_extract_cmd(
        self,
        cmd: list[str],
        destination: Path,
        att: AttachmentInfo,
        source: Path,
    ) -> None:
        self.log_message.emit("INFO", "$ " + " ".join(str(c) for c in cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=120,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0 or not destination.exists() or destination.stat().st_size == 0:
            stderr = (result.stderr or "").strip()
            raise RemuxError(
                "Extraction attachment échouée "
                f"(source={source.name}, stream={att.index}): {stderr}"
            )

    def _attachment_names(self, att: AttachmentInfo) -> tuple[str, str]:
        raw_name = _sanitize_filename(att.filename, f"attachment_{att.index}")
        source_suffix = Path(raw_name).suffix.lower()
        if not source_suffix:
            mime = (att.mimetype or "").strip().lower()
            source_suffix = _EXT_BY_MIME.get(mime, ".bin")
        base_name = Path(raw_name).stem or f"attachment_{att.index}"

        # Convention historique : cover.* devient filename="cover" dans le conteneur.
        meta_name = "cover" if base_name.lower() == "cover" else f"{base_name}{source_suffix}"
        extract_name = f"{base_name}{source_suffix}"
        return meta_name, extract_name

    @staticmethod
    def _unique_path(directory: Path, filename: str) -> Path:
        candidate = directory / filename
        if not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        for i in range(1, 10_000):
            alt = directory / f"{stem}_{i}{suffix}"
            if not alt.exists():
                return alt
        return directory / f"{stem}_x{suffix}"

    @staticmethod
    def _dedupe_attachment_filenames(specs: list[_AttachmentSpec]) -> list[_AttachmentSpec]:
        seen: dict[str, int] = {}
        out: list[_AttachmentSpec] = []

        for spec in specs:
            raw = spec.filename
            key = raw.lower()
            count = seen.get(key, 0)
            if count == 0:
                seen[key] = 1
                out.append(spec)
                continue

            stem = Path(raw).stem
            suffix = Path(raw).suffix
            while True:
                candidate = f"{stem}_{count}{suffix}"
                ckey = candidate.lower()
                count += 1
                if ckey not in seen:
                    seen[key] = count
                    seen[ckey] = 1
                    out.append(_AttachmentSpec(
                        path=spec.path,
                        filename=candidate,
                        mimetype=spec.mimetype,
                    ))
                    break

        return out
