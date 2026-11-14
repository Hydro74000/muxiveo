"""Analyse de synchro automatique d'une source donneuse face à la référence (audio, sinon sous-titres)."""

from __future__ import annotations

from typing import Any

from core.jobs.source_sync import measure_pair
from core.workflows.audio_sync_scan import AudioSyncScanner
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
        # Analyseur lu dans ce module à chaque appel (remplaçable par les tests).
        return measure_pair(ref_source, ref_audio, donor_source, sync_audio, kind="audio", mode="auto",
                            config=config, options=options, logger=logger, audio_scanner=AudioSyncScanner)

    # 2. Sous-titre donneur si pas d'audio
    if target_sub:
        reference_track = ref_sub if ref_sub is not None else ref_audio
        if reference_track is not None:
            return measure_pair(ref_source, reference_track, donor_source, target_sub, kind="subtitle", mode="auto",
                                config=config, options=options, logger=logger)

    logger.emit("warning", f"Aucune paire de pistes compatible trouvée pour la synchronisation automatique de {donor_source.path.name}.")
    return None
