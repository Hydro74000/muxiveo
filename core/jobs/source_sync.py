"""Synchro des sources et des pistes d'un job (liens en chaîne du panneau Conteneur, en headless).

``sources[i].sync`` : la source ``i`` est mesurée (ou calée par une calibration / un décalage donnés)
face à une piste de la source ``reference`` ; la mesure vaut face à l'audio d'origine de la référence
et la synchro appliquée suit la position de la référence dans la sortie
(``compose_calibrations(relative, position)``, ``core.workflows.sync_links``). Résolution dans l'ordre
des dépendances, boucle refusée. ``tracks[].sync`` : synchro propre à une piste (elle seule bouge,
comme « Synchroniser cette piste »), jamais écrasée par la synchro de sa source.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.jobs.constants import EXIT_ARGS, EXIT_VALIDATION
from core.jobs.errors import JobError
from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner, coherent_sync_accepted, quick_check_enabled
from core.workflows.remux_models import SourceInput, TrackEntry
from core.workflows.sync_calibration import SyncCalibration, compose_calibrations, projected_calibration
from core.workflows.sync_links import SyncLink, SyncLoopError, dependency_order, is_negligible, measure_track

SYNC_MODES = ("auto", "quick", "deep")
SYNC_KINDS = ("auto", "audio", "subtitle")
SYNC_KEYS = {"reference", "reference_track", "track", "mode", "type", "calibration", "offset_ms"}
_LETTERS = {"a": "audio", "s": "subtitle", "v": "video"}


def validate_sync_spec(errors: list[str], path: str, value: Any, *, track_level: bool = False) -> None:
    """Contrat de ``sources[i].sync`` / ``tracks[i].sync``."""
    if not isinstance(value, dict):
        errors.append(f"{path}: attendu object")
        return
    allowed = SYNC_KEYS - ({"track"} if track_level else set())
    for key in sorted(set(value) - allowed):
        errors.append(f"{path}.{key}: champ inconnu")
    if "reference" in value and (not isinstance(value["reference"], int) or isinstance(value["reference"], bool)
                                 or value["reference"] < 0):
        errors.append(f"{path}.reference: attendu index de source (entier ≥ 0)")
    for key in ("reference_track", "track"):
        if key in value and (not isinstance(value[key], (str, int)) or isinstance(value[key], bool)):
            errors.append(f"{path}.{key}: attendu mkv_tid (entier) ou « a:N » / « s:N »")
    if "mode" in value and value["mode"] not in SYNC_MODES:
        errors.append(f"{path}.mode: attendu {SYNC_MODES}")
    if "type" in value and value["type"] not in SYNC_KINDS:
        errors.append(f"{path}.type: attendu {SYNC_KINDS}")
    if "offset_ms" in value and (not isinstance(value["offset_ms"], int) or isinstance(value["offset_ms"], bool)):
        errors.append(f"{path}.offset_ms: attendu entier (ms)")
    if "calibration" in value and "offset_ms" in value:
        errors.append(f"{path}: calibration et offset_ms s'excluent")
    calibration = value.get("calibration")
    if isinstance(calibration, dict):
        try:
            SyncCalibration.from_dict(calibration)
        except (ValueError, TypeError) as exc:
            errors.append(f"{path}.calibration: {exc}")
    elif calibration is not None and not isinstance(calibration, str):
        errors.append(f"{path}.calibration: attendu object ou chemin de fichier")


def pick_track(source: SourceInput, selector: Any, *, kind: str | None = None, context: str = "") -> TrackEntry:
    """Piste d'une source : ``mkv_tid`` (entier), ``a:N`` / ``s:N`` (rang par nature), sinon la première de
    ``kind`` (audio par défaut, puis sous-titres)."""
    tracks = [track for track in source.tracks if not track.is_new]
    if selector is None:
        for track_type in ((kind,) if kind else ("audio", "subtitle")):
            found = next((track for track in tracks if track.track_type == track_type), None)
            if found is not None:
                return found
        raise JobError(f"{context} : aucune piste {kind or 'audio ou sous-titres'} dans {source.path.name}.",
                       EXIT_VALIDATION)
    text = str(selector).strip()
    if ":" in text:
        letter, _sep, rank = text.partition(":")
        wanted = _LETTERS.get(letter.lower())
        of_kind = [track for track in tracks if track.track_type == wanted]
        if wanted is None or not rank.isdigit() or int(rank) >= len(of_kind):
            raise JobError(f"{context} : piste {text!r} introuvable dans {source.path.name}.", EXIT_VALIDATION)
        return of_kind[int(rank)]
    found = next((track for track in tracks if text.isdigit() and track.mkv_tid == int(text)), None)
    if found is None:
        raise JobError(f"{context} : piste #{text} introuvable dans {source.path.name}.", EXIT_VALIDATION)
    return found


def _source_fps(source: SourceInput) -> Any:
    from core.workflows.hybrid_matrix import _extract_source_video_fps

    return _extract_source_video_fps(source)


def measure_pair(reference: SourceInput, reference_track: TrackEntry, target: SourceInput, target_track: TrackEntry,
                 *, kind: str, mode: str, config: Any, options: Any, logger: Any,
                 audio_scanner: Any = AudioSyncScanner) -> SyncCalibration | None:
    """Mesure du calage de ``target_track`` face à ``reference_track`` (corrélation acoustique ou sous-titres)."""
    ffmpeg = getattr(options, "ffmpeg", None) or config.tool_ffmpeg
    ffprobe = getattr(options, "ffprobe", None) or config.tool_ffprobe
    detect_cuts = bool(getattr(options, "detect_cuts", False))
    if kind == "auto":
        kind = "audio" if reference_track.is_audio and target_track.is_audio else "subtitle"
    if kind == "audio":
        cadence_mismatch = None
        cadence_auto = getattr(options, "cadence_auto", None)
        if cadence_auto is None:
            cadence_auto = getattr(config, "sync_cadence_auto_apply", True)
        cadence_method = (getattr(options, "cadence_method", None) or getattr(config, "sync_cadence_audio_method", "auto")
                          or "auto")
        if cadence_auto:
            from core.workflows.cadence import detect_cadence_from_metadata

            ref_fps, donor_fps = _source_fps(reference), _source_fps(target)
            if ref_fps and donor_fps:
                cadence_mismatch = detect_cadence_from_metadata(ref_fps, donor_fps)
                if cadence_mismatch:
                    logger.emit("info", f"Différence de cadence vidéo détectée ({ref_fps} vs {donor_fps}) : "
                                        f"{cadence_mismatch.description} — méthode audio : {cadence_method}")
        logger.emit("info", f"Synchronisation audio dynamique : {reference.path.name} #{reference_track.mkv_tid} vs "
                            f"{target.path.name} #{target_track.mkv_tid}…")
        quick = {"quick": True, "deep": False}.get(mode, quick_check_enabled(options, config))
        scanner = audio_scanner(ffmpeg, ffprobe, quick_check=quick, accept_coherent=coherent_sync_accepted(options))
        return scanner.scan(
            AudioSyncTrack(reference.path, reference_track.mkv_tid),
            AudioSyncTrack(target.path, target_track.mkv_tid),
            detect_cuts=detect_cuts,
            drift_threshold_ms=getattr(options, "drift_threshold_ms", 25) or 25,
            cadence_mismatch=cadence_mismatch,
            cadence_audio_method=cadence_method,
            log=lambda msg: logger.emit("info", msg),
            warn=lambda msg: logger.emit("warning", msg),
        )
    from core.workflows.subtitle_sync_scan import SubtitleSyncScanner

    is_ref_sub = reference_track.is_subtitle
    logger.emit("info", ("Synchronisation sous-titres dynamique : " if is_ref_sub else
                         "Synchronisation audio vs sous-titre dynamique : ")
                + f"{reference.path.name} #{reference_track.mkv_tid} vs {target.path.name} #{target_track.mkv_tid}…")
    return SubtitleSyncScanner(ffmpeg, ffprobe).scan(
        reference.path, reference_track.mkv_tid, target.path, target_track.mkv_tid, is_ref_sub=is_ref_sub,
        detect_cuts=detect_cuts if is_ref_sub else False, log=lambda msg: logger.emit("info", msg))


def _given_calibration(spec: Mapping[str, Any]) -> SyncCalibration | None:
    if "offset_ms" in spec:
        return SyncCalibration.linear(int(spec["offset_ms"]))
    calibration = spec.get("calibration")
    if isinstance(calibration, dict):
        return SyncCalibration.from_dict(calibration)
    if isinstance(calibration, str):
        try:
            return SyncCalibration.from_dict(json.loads(Path(calibration).expanduser().read_text(encoding="utf-8-sig")))
        except (OSError, ValueError, TypeError) as exc:
            raise JobError(f"Calibration illisible ({calibration}) : {exc}", EXIT_ARGS) from exc
    return None


def _relative(spec: Mapping[str, Any], reference: SourceInput, reference_track: TrackEntry, target: SourceInput,
              target_track: TrackEntry, *, config: Any, options: Any, logger: Any) -> SyncCalibration | None:
    given = _given_calibration(spec)
    if given is not None:
        return given
    return measure_pair(reference, reference_track, target, target_track, kind=str(spec.get("type") or "auto"),
                        mode=str(spec.get("mode") or "auto"), config=config, options=options, logger=logger)


def sync_specs(job: Mapping[str, Any], *, auto_sync_all: bool = False) -> dict[int, dict[str, Any]]:
    """``sources[i].sync`` du job ; ``auto_sync_all`` : chaque source ≥ 1 sans bloc mesurée face à la source 0."""
    specs: dict[int, dict[str, Any]] = {}
    sources = job.get("sources")
    if isinstance(sources, list):
        for index, item in enumerate(sources):
            if isinstance(item, dict) and isinstance(item.get("sync"), dict):
                specs[index] = dict(item["sync"])
        if auto_sync_all:
            for index in range(1, len(sources)):
                specs.setdefault(index, {"reference": 0})
    return specs


def _track_position(track: TrackEntry, source_index: int, job: Mapping[str, Any]) -> SyncCalibration:
    """Position d'une piste dans la sortie : sa synchro propre, sinon celle de sa source (audio, sous-titres en
    miroir), sinon son décalage."""
    own = projected_calibration(track.sync_calibration, track.track_type)
    if own is not None:
        return own
    inherited = None
    if track.is_audio or (track.is_subtitle and job.get("sync_subtitles", "mirror") == "mirror"):
        inherited = projected_calibration((job.get("sync_calibrations") or {}).get(str(source_index)), track.track_type)
    return inherited or SyncCalibration.linear(int(track.time_shift_ms or 0))


def resolve_source_syncs(job: dict[str, Any], sources: list[SourceInput], specs: Mapping[int, Mapping[str, Any]], *,
                         config: Any, options: Any, logger: Any, apply: Any) -> None:
    """Résout les liens de synchro des sources (ordre des dépendances) et les applique par ``apply``.

    ``apply(calibration, source_index)`` : application d'une synchro à toute la source
    (``apply_donor_calibration``) ; met à jour ``job["sync_mode"]`` / ``job["sync_calibrations"]``.
    """
    links: dict[str, SyncLink] = {}
    for index, spec in specs.items():
        reference_index = int(spec.get("reference", 0))
        if index >= len(sources) or reference_index >= len(sources):
            raise JobError(f"sources[{index}].sync.reference : source {reference_index} absente.", EXIT_ARGS)
        if reference_index == index:
            raise JobError(f"sources[{index}].sync.reference : une source ne se synchronise pas sur elle-même.",
                           EXIT_ARGS)
        links[str(index)] = SyncLink(str(reference_index), "", SyncCalibration.linear(0))
    try:
        order = [int(file_id) for file_id in dependency_order(links)]
    except SyncLoopError as exc:
        raise JobError(f"{exc} (indices de sources).", EXIT_ARGS) from exc
    for index in order:
        spec = specs[index]
        reference = sources[int(spec.get("reference", 0))]
        target = sources[index]
        context = f"sources[{index}].sync"
        kind = str(spec.get("type") or "auto")
        reference_track = pick_track(reference, spec.get("reference_track"),
                                     kind="subtitle" if kind == "subtitle" else None, context=context)
        target_track = pick_track(target, spec.get("track"), kind="subtitle" if kind == "subtitle" else None,
                                  context=context)
        if spec.get("track") is None and target_track.is_audio and reference_track.is_audio:
            target_track = measure_track(target_track, reference_track, target.tracks)
        relative = _relative(spec, reference, reference_track, target, target_track, config=config, options=options,
                             logger=logger)
        if relative is None:
            logger.emit("warning", f"Synchro de {target.path.name} : aucune mesure exploitable, source non déplacée.")
            continue
        position = _track_position(reference_track, int(spec.get("reference", 0)), job)
        calibration = compose_calibrations(relative, position)
        if position.segments[0].shift_ms or len(position.segments) > 1 or position.converts_audio:
            logger.emit("info", f"La référence {reference.path.name} est elle-même synchronisée : "
                                f"{target.path.name} suit sa position dans la sortie.")
        if is_negligible(calibration):
            logger.emit("info", f"Synchro de {target.path.name} : décalage {round(calibration.segments[0].shift_ms):+d} ms "
                                "négligeable, non appliqué.")
            apply(SyncCalibration.linear(0), index)  # le bloc `sync` définit la synchro de la source
            continue
        logger.emit("info", f"Synchro de {target.path.name} : {round(calibration.segments[0].shift_ms):+d} ms "
                            f"(face à {reference.path.name} #{reference_track.mkv_tid}).")
        apply(calibration, index)


def resolve_track_syncs(job: dict[str, Any], sources: list[SourceInput], pairs: list[tuple[dict, TrackEntry]], *,
                        linked: set[int], config: Any, options: Any, logger: Any) -> None:
    """Synchro propre à une piste (``tracks[].sync``) : seule la piste bouge ; sa source ne la déplace plus."""
    by_track = {id(track): index for index, source in enumerate(sources) for track in source.tracks}
    for spec_index, (spec, track) in enumerate(pairs):
        sync = spec.get("sync")
        if not isinstance(sync, dict):
            continue
        context = f"tracks[{spec_index}].sync"
        if track.track_type not in ("audio", "subtitle"):
            raise JobError(f"{context} : synchro réservée aux pistes audio et sous-titres.", EXIT_VALIDATION)
        source_index = by_track.get(id(track))
        reference_index = int(sync.get("reference", 0))
        if source_index is None or reference_index >= len(sources):
            raise JobError(f"{context}.reference : source {reference_index} absente.", EXIT_ARGS)
        reference = sources[reference_index]
        kind = str(sync.get("type") or "auto")
        reference_track = pick_track(reference, sync.get("reference_track"),
                                     kind="subtitle" if kind == "subtitle" else None, context=context)
        if reference_track is track:
            raise JobError(f"{context} : la piste ne se synchronise pas sur elle-même.", EXIT_ARGS)
        relative = _relative(sync, reference, reference_track, sources[source_index], track, config=config,
                             options=options, logger=logger)
        if relative is None:
            logger.emit("warning", f"{context} : aucune mesure exploitable, piste #{track.mkv_tid} non déplacée.")
            continue
        position = _track_position(reference_track, reference_index, job)
        calibration = compose_calibrations(relative, position).for_track(track.track_type)
        # Synchro propre : calibration de la piste (prioritaire à l'exécution), même constante.
        track.sync_calibration = calibration.to_dict()
        track.time_shift_ms = round(calibration.segments[0].shift_ms)
        note = " (sa source est aussi synchronisée : la piste garde sa synchro propre)" if source_index in linked else ""
        logger.emit("info", f"Synchro propre de la piste #{track.mkv_tid} de {sources[source_index].path.name} : "
                            f"{track.time_shift_ms:+d} ms{note}.")
