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
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.jobs.constants import EXIT_ARGS, EXIT_VALIDATION
from core.jobs.errors import JobError
from core.service.errors import ApiError, ErrorCode
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
                 audio_scanner: Any = AudioSyncScanner,
                 cancel_event: threading.Event | None = None) -> SyncCalibration | None:
    """Mesure du calage de ``target_track`` face à ``reference_track`` (corrélation acoustique ou sous-titres).

    ``cancel_event`` arrête l'analyse en cours (processus FFmpeg tués) : erreur publique ``cancelled``.
    """
    try:
        return _measure_pair(reference, reference_track, target, target_track, kind=kind, mode=mode, config=config,
                             options=options, logger=logger, audio_scanner=audio_scanner, cancel_event=cancel_event)
    except Exception as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise ApiError(ErrorCode.CANCELLED, "Analyse de synchro annulée.") from exc
        raise


def _measure_pair(reference: SourceInput, reference_track: TrackEntry, target: SourceInput, target_track: TrackEntry,
                  *, kind: str, mode: str, config: Any, options: Any, logger: Any, audio_scanner: Any,
                  cancel_event: threading.Event | None) -> SyncCalibration | None:
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
        scanner = audio_scanner(ffmpeg, ffprobe, quick_check=quick, accept_coherent=coherent_sync_accepted(options),
                                cancel_event=cancel_event)
        return scanner.scan(
            AudioSyncTrack(reference.path, reference_track.mkv_tid),
            AudioSyncTrack(target.path, target_track.mkv_tid),
            detect_cuts=detect_cuts,
            drift_threshold_ms=getattr(options, "drift_threshold_ms", 25) or 25,
            cadence_mismatch=cadence_mismatch,
            cadence_audio_method=cadence_method,
            log=lambda msg: logger.emit("info", msg),
            warn=lambda msg, **fields: logger.emit("warning", msg, **fields),
        )
    from core.workflows.subtitle_sync_scan import SubtitleSyncScanner

    is_ref_sub = reference_track.is_subtitle
    logger.emit("info", ("Synchronisation sous-titres dynamique : " if is_ref_sub else
                         "Synchronisation audio vs sous-titre dynamique : ")
                + f"{reference.path.name} #{reference_track.mkv_tid} vs {target.path.name} #{target_track.mkv_tid}…")
    return SubtitleSyncScanner(ffmpeg, ffprobe, cancel_event=cancel_event).scan(
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
              target_track: TrackEntry, *, config: Any, options: Any, logger: Any,
              cancel_event: threading.Event | None = None) -> SyncCalibration | None:
    given = _given_calibration(spec)
    if given is not None:
        return given
    return measure_pair(reference, reference_track, target, target_track, kind=str(spec.get("type") or "auto"),
                        mode=str(spec.get("mode") or "auto"), config=config, options=options, logger=logger,
                        cancel_event=cancel_event)


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


@dataclass
class _SyncNode:
    """Synchro à résoudre : une source entière (``sources[i].sync``) ou une piste seule (``tracks[i].sync``)."""

    key: str
    context: str
    spec: Mapping[str, Any]
    target_index: int
    reference_index: int
    reference_track: TrackEntry
    track: TrackEntry | None = None  # piste seule (synchro propre)


def _source_index_of(sources: list[SourceInput], track: TrackEntry) -> int | None:
    return next((index for index, source in enumerate(sources) if any(t is track for t in source.tracks)), None)


def _sync_nodes(sources: list[SourceInput], source_specs: Mapping[int, Mapping[str, Any]],
                track_pairs: list[tuple[dict, TrackEntry]]) -> dict[str, _SyncNode]:
    nodes: dict[str, _SyncNode] = {}
    for index, spec in source_specs.items():
        reference_index = int(spec.get("reference", 0))
        context = f"sources[{index}].sync"
        if index >= len(sources) or reference_index >= len(sources):
            raise JobError(f"{context}.reference : source {reference_index} absente.", EXIT_ARGS)
        if reference_index == index:
            raise JobError(f"{context}.reference : une source ne se synchronise pas sur elle-même.", EXIT_ARGS)
        kind = "subtitle" if spec.get("type") == "subtitle" else None
        reference_track = pick_track(sources[reference_index], spec.get("reference_track"), kind=kind, context=context)
        nodes[f"s{index}"] = _SyncNode(f"s{index}", context, spec, index, reference_index, reference_track)
    for position, (spec, track) in enumerate(track_pairs):
        sync = spec.get("sync")
        if not isinstance(sync, dict):
            continue
        context = f"tracks[{position}].sync"
        if track.track_type not in ("audio", "subtitle"):
            raise JobError(f"{context} : synchro réservée aux pistes audio et sous-titres.", EXIT_VALIDATION)
        source_index = _source_index_of(sources, track)
        reference_index = int(sync.get("reference", 0))
        if source_index is None or reference_index >= len(sources):
            raise JobError(f"{context}.reference : source {reference_index} absente.", EXIT_ARGS)
        kind = "subtitle" if sync.get("type") == "subtitle" else None
        reference_track = pick_track(sources[reference_index], sync.get("reference_track"), kind=kind, context=context)
        if reference_track is track:
            raise JobError(f"{context} : la piste ne se synchronise pas sur elle-même.", EXIT_ARGS)
        nodes[f"t{position}"] = _SyncNode(f"t{position}", context, sync, source_index, reference_index,
                                          reference_track, track)
    return nodes


def sync_order(nodes: Mapping[str, _SyncNode]) -> list[str]:
    """Ordre de résolution : chaque synchro après celle de sa piste de référence (synchro propre), sinon de la
    source de référence ; boucle refusée."""
    track_nodes = {id(node.track): key for key, node in nodes.items() if node.track is not None}
    links: dict[str, SyncLink] = {}
    for key, node in nodes.items():
        dependency = track_nodes.get(id(node.reference_track))
        if dependency is None and f"s{node.reference_index}" in nodes:
            dependency = f"s{node.reference_index}"
        if dependency is not None and dependency != key:
            links[key] = SyncLink(dependency, "", SyncCalibration.linear(0))
    try:
        linked = dependency_order(links)
    except SyncLoopError as exc:
        raise JobError(f"{exc} (s = source, t = entrée tracks[]).", EXIT_ARGS) from exc
    return [key for key in nodes if key not in links] + linked


def resolve_syncs(job: dict[str, Any], sources: list[SourceInput], source_specs: Mapping[int, Mapping[str, Any]],
                  track_pairs: list[tuple[dict, TrackEntry]], *, config: Any, options: Any, logger: Any,
                  apply: Any, cancel_event: threading.Event | None = None) -> None:
    """Résout les synchros des sources et des pistes dans l'ordre de leurs dépendances.

    Source : ``apply(calibration, source_index)`` (``apply_donor_calibration`` : toute la source, met à jour
    ``job["sync_mode"]`` / ``job["sync_calibrations"]``). Piste : calibration propre, prioritaire à l'exécution.
    Chaque synchro suit la position de sa piste de référence dans la sortie, déjà résolue.
    """
    nodes = _sync_nodes(sources, source_specs, track_pairs)
    linked_sources = {node.target_index for node in nodes.values() if node.track is None}
    for key in sync_order(nodes):
        node = nodes[key]
        reference = sources[node.reference_index]
        target = sources[node.target_index]
        if node.track is None:
            kind = "subtitle" if node.spec.get("type") == "subtitle" else None
            target_track = pick_track(target, node.spec.get("track"), kind=kind, context=node.context)
            if node.spec.get("track") is None and target_track.is_audio and node.reference_track.is_audio:
                target_track = measure_track(target_track, node.reference_track, target.tracks)
        else:
            target_track = node.track
        relative = _relative(node.spec, reference, node.reference_track, target, target_track, config=config,
                             options=options, logger=logger, cancel_event=cancel_event)
        if relative is None:
            what = f"piste #{target_track.mkv_tid}" if node.track is not None else "source"
            logger.emit("warning", f"{node.context} : aucune mesure exploitable, {what} de {target.path.name} "
                                   "non déplacée.")
            continue
        position = _track_position(node.reference_track, node.reference_index, job)
        if position.segments[0].shift_ms or len(position.segments) > 1 or position.converts_audio:
            logger.emit("info", f"La référence {reference.path.name} #{node.reference_track.mkv_tid} est elle-même "
                                f"synchronisée : {target.path.name} suit sa position dans la sortie.")
        calibration = compose_calibrations(relative, position)
        if node.track is not None:
            calibration = calibration.for_track(node.track.track_type)
            # Synchro propre : calibration de la piste (prioritaire à l'exécution), même constante.
            node.track.sync_calibration = calibration.to_dict()
            node.track.time_shift_ms = round(calibration.segments[0].shift_ms)
            note = (" (sa source est aussi synchronisée : la piste garde sa synchro propre)"
                    if node.target_index in linked_sources else "")
            logger.emit("info", f"Synchro propre de la piste #{node.track.mkv_tid} de {target.path.name} : "
                                f"{node.track.time_shift_ms:+d} ms{note}.")
            continue
        if is_negligible(calibration):
            logger.emit("info", f"Synchro de {target.path.name} : décalage {round(calibration.segments[0].shift_ms):+d} "
                                "ms négligeable, non appliqué.")
            apply(SyncCalibration.linear(0), node.target_index)  # le bloc `sync` définit la synchro de la source
            continue
        logger.emit("info", f"Synchro de {target.path.name} : {round(calibration.segments[0].shift_ms):+d} ms "
                            f"(face à {reference.path.name} #{node.reference_track.mkv_tid}).")
        apply(calibration, node.target_index)
