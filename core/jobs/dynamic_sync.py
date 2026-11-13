"""Analyse de synchro automatique d'une source donneuse face à la référence (audio, sinon sous-titres)."""

from __future__ import annotations

from typing import Any

from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner, coherent_sync_accepted, quick_check_enabled
from core.workflows.sync_calibration import SyncCalibration


def perform_dynamic_sync(
    sources: list[Any],
    tracks: list[Any],
    config: Any,
    options: Any,
    logger: Any,
) -> SyncCalibration | None:
    """Analyse dynamiquement les sources pour déterminer le décalage (audio ou sous-titres)."""
    if len(sources) < 2:
        return None
    ref_source = sources[0]
    donor_source = sources[1]

    ref_audio = next((t for t in ref_source.tracks if t.track_type == "audio"), None)
    ref_sub = next((t for t in ref_source.tracks if t.track_type == "subtitle" and t.enabled), None)
    if ref_sub is None:
        ref_sub = next((t for t in ref_source.tracks if t.track_type == "subtitle"), None)

    donor_tracks = [t for t in donor_source.tracks if t.enabled]
    target_audio = next((t for t in donor_tracks if t.track_type == "audio"), None)
    sync_audio = None
    if ref_audio is not None and ref_audio.language:
        sync_audio = next((t for t in donor_source.tracks if t.track_type == "audio" and t.language == ref_audio.language), None)
    if sync_audio is None:
        sync_audio = target_audio
    target_sub = next((t for t in donor_tracks if t.track_type == "subtitle"), None)

    # 1. Audio vs Audio si disponible
    if sync_audio and ref_audio:
        cadence_mismatch = None
        cadence_auto = getattr(options, "cadence_auto", None)
        if cadence_auto is None:
            cadence_auto = getattr(config, "sync_cadence_auto_apply", True)
        cadence_method = getattr(options, "cadence_method", None) or getattr(config, "sync_cadence_audio_method", "auto") or "auto"

        if cadence_auto:
            from core.workflows.hybrid_matrix import _extract_source_video_fps
            from core.workflows.cadence import detect_cadence_from_metadata
            ref_fps = _extract_source_video_fps(ref_source)
            donor_fps = _extract_source_video_fps(donor_source)
            if ref_fps and donor_fps:
                cadence_mismatch = detect_cadence_from_metadata(ref_fps, donor_fps)
                if cadence_mismatch:
                    logger.emit(
                        "info",
                        f"Différence de cadence vidéo détectée ({ref_fps} vs {donor_fps}) : {cadence_mismatch.description} — méthode audio : {cadence_method}",
                    )

        logger.emit("info", f"Synchronisation audio dynamique : {ref_source.path.name} #{ref_audio.mkv_tid} vs {donor_source.path.name} #{sync_audio.mkv_tid}…")
        audio_scanner = AudioSyncScanner(
            getattr(options, "ffmpeg", None) or config.tool_ffmpeg,
            getattr(options, "ffprobe", None) or config.tool_ffprobe,
            quick_check=quick_check_enabled(options, config),
            accept_coherent=coherent_sync_accepted(options),
        )
        return audio_scanner.scan(
            AudioSyncTrack(ref_source.path, ref_audio.mkv_tid),
            AudioSyncTrack(donor_source.path, sync_audio.mkv_tid),
            detect_cuts=bool(getattr(options, "detect_cuts", False)),
            drift_threshold_ms=getattr(options, "drift_threshold_ms", 25) or 25,
            cadence_mismatch=cadence_mismatch,
            cadence_audio_method=cadence_method,
            log=lambda msg: logger.emit("info", msg),
            warn=lambda msg: logger.emit("warning", msg),
        )

    # 2. Sous-titre donneur si pas d'audio
    if target_sub:
        from core.workflows.subtitle_sync_scan import SubtitleSyncScanner
        sub_scanner = SubtitleSyncScanner(
            getattr(options, "ffmpeg", None) or config.tool_ffmpeg,
            getattr(options, "ffprobe", None) or config.tool_ffprobe,
        )
        if ref_sub is not None:
            logger.emit("info", f"Synchronisation sous-titres dynamique : {ref_source.path.name} #{ref_sub.mkv_tid} vs {donor_source.path.name} #{target_sub.mkv_tid}…")
            return sub_scanner.scan(
                ref_source.path, ref_sub.mkv_tid,
                donor_source.path, target_sub.mkv_tid,
                is_ref_sub=True,
                detect_cuts=bool(getattr(options, "detect_cuts", False)),
                log=lambda msg: logger.emit("info", msg),
            )
        elif ref_audio is not None:
            logger.emit("info", f"Synchronisation audio vs sous-titre dynamique : {ref_source.path.name} #{ref_audio.mkv_tid} vs {donor_source.path.name} #{target_sub.mkv_tid}…")
            return sub_scanner.scan(
                ref_source.path, ref_audio.mkv_tid,
                donor_source.path, target_sub.mkv_tid,
                is_ref_sub=False,
                detect_cuts=False,
                log=lambda msg: logger.emit("info", msg),
            )

    logger.emit("warning", f"Aucune paire de pistes compatible trouvée pour la synchronisation automatique de {donor_source.path.name}.")
    return None
