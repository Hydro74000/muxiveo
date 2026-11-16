"""Exécution d'un encodage résolu (CLI, couche de service) : workflow, géométrie DV NVENC, aperçu, lancement.

Mêmes réglages d'exécution que le panneau Encodage (``refresh_runtime_settings``) ; aucune décision
interactive : le recadrage imposé par Dolby Vision + NVENC n'est appliqué que sur acceptation explicite.
"""

from __future__ import annotations

import threading
import time

import dataclasses
from contextlib import contextmanager
from collections.abc import Callable, Iterator
from typing import Any

from core.jobs.constants import EXIT_EXISTS, EXIT_VALIDATION
from core.jobs.errors import JobError
from core.jobs.logging import JobLogger
from core.jobs.options import CommonOptions
from core.service.context import CancelToken
from core.service.errors import ApiError, ErrorCode, ErrorDetail, error_code
from core.service.jobs import await_task, relay
from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings
from core.workflows.encode.progress_tracking import (
    TOOL_PERCENT_PREFIX,
    EncodeProgressTracker,
    ProgressSnapshot,
    parse_encode_internal_progress,
    progress_targets,
    stage_percent,
)
from core.workflows.encode.workflow import EncodeWorkflow

#: Encodeurs Dolby Vision sans débordement du canevas UHD (alternatives proposées).
DOVI_FULL_FRAME_ENCODERS = ("libx265", "hevc_qsv", "hevc_vaapi", "hevc_amf")


def encode_workflow(config: Any, options: CommonOptions) -> EncodeWorkflow:
    """Workflow d'encodage réglé comme le panneau Encodage (outils, threads, NFO, synchro, statistiques)."""
    ffmpeg = str(options.ffmpeg or config.tool_ffmpeg)
    wf = EncodeWorkflow(
        ffmpeg_bin=ffmpeg,
        ffprobe_bin=str(options.ffprobe or getattr(config, "tool_ffprobe", "") or "") or None,
        dovi_tool_bin=config.tool_dovi_tool,
        hdr10plus_bin=config.tool_hdr10plus,
        mediainfo_bin=str(options.mediainfo or config.tool_mediainfo),
        ram_buffer_enabled=config.ram_buffer_enabled,
        ram_buffer_threshold_pct=config.ram_buffer_threshold_pct,
        ffmpeg_threads=options.threads if options.threads is not None else config.ffmpeg_threads,
        max_parallel_video_encodes=config.max_parallel_video_encodes,
        x265_frame_threads=getattr(config, "x265_frame_threads", 0),
        writing_application=str(options.writing_application or ""),
        generate_nfo=config.generate_nfo if options.nfo is None else bool(options.nfo),
        nvencc_bin=getattr(config, "tool_nvencc", None) or None,
        rife_bin=getattr(config, "tool_muxiveo_rife", None) or None,
        sync_rewrite_enabled=config.sync_rewrite_enabled,
        aac_bitrate_per_channel_kbps=config.aac_bitrate_per_channel_kbps,
        eac3_bitrate_per_channel_kbps=config.eac3_bitrate_per_channel_kbps,
        regenerate_statistics=getattr(config, "matroska_regenerate_statistics", True),
    )
    return wf


@contextmanager
def probe_cancel_scope(wf: Any, cancel_event: threading.Event | None) -> Iterator[None]:
    """Jeton des sondes internes du workflow (bandes L5) pour la durée d'une opération, puis l'ancien.

    Un workflow réutilisé ne garde jamais le jeton (peut-être annulé) d'une opération terminée.
    """
    setter = getattr(wf, "set_probe_cancel_event", None)  # doubles de test : sans sondes internes
    if setter is None or cancel_event is None:
        yield
        return
    previous = getattr(wf, "_probe_cancel_event", None)
    setter(cancel_event)
    try:
        yield
    finally:
        setter(previous)


def prepare_vulkan(wf: EncodeWorkflow, encode_config: EncodeConfig, ffmpeg: str,
                   cancel_event: threading.Event | None = None) -> None:
    """NLMeans demandé : sonde Vulkan (GPU dédié) transmise au workflow, comme le tableau de bord ; annulable."""
    if not any(video.filters.nlmeans_enabled for video in encode_config.video_tracks):
        return
    from core.workflows.encode.vulkan import detect_vulkan

    wf.set_vulkan_capability(ffmpeg, detect_vulkan(ffmpeg, cancel_event))


def align_dovi_nvenc(encode_config: EncodeConfig, wf: EncodeWorkflow, *, accept: bool,
                     logger: JobLogger | None = None, cancel_event: threading.Event | None = None) -> EncodeConfig:
    """Géométrie Dolby Vision + NVENC (blocs de 32 lignes) : recadrage aligné appliqué si accepté, sinon refus."""
    from core.workflows.encode.runtime.dovi_geometry import align_nvenc_dovi_geometry
    from core.workflows.encode.catalog import is_nvenc_hevc

    tracks: list[VideoEncodeSettings] = []
    changed = False
    for video in encode_config.video_tracks:
        if not is_nvenc_hevc(video.codec) or not video.copy_dv:
            tracks.append(video)
            continue
        dimensions = _dimensions(wf, video, encode_config, cancel_event)
        if dimensions is None:
            tracks.append(video)
            continue
        l5 = wf._probe_dovi_l5_offsets(video.source_path or encode_config.source, stream_index=video.stream_index,
                                       cancel_event=cancel_event)  # facultative : None si indisponible
        result = align_nvenc_dovi_geometry(video, dimensions, l5_offsets=l5)
        required = result.video.crop
        current = video.crop
        needed = result.full_frame_crop or (result.crop_offsets is not None and (
            current.unit != "px" or current.auto or not current.enabled
            or (current.top, current.bottom, current.left, current.right)
            != (required.top, required.bottom, required.left, required.right)))
        if not needed:
            tracks.append(video)
            continue
        out_w = dimensions[0] - required.left - required.right
        out_h = dimensions[1] - required.top - required.bottom
        if not accept:
            alternatives = ", ".join(DOVI_FULL_FRAME_ENCODERS)
            raise JobError(
                f"Dolby Vision + NVENC : recadrage aligné sur 32 requis ({out_w} × {out_h} ; haut={required.top}, "
                f"bas={required.bottom}, gauche={required.left}, droite={required.right}). Accepter avec "
                f"accept_dovi_nvenc_crop (--accept-dovi-nvenc-crop) ou choisir un encodeur sans cette contrainte "
                f"({alternatives}).", EXIT_VALIDATION)
        if logger is not None:
            logger.emit("info", f"Dolby Vision + NVENC : recadrage aligné appliqué ({out_w} × {out_h}).")
        tracks.append(dataclasses.replace(video, crop=required))
        changed = True
    if not changed:
        return encode_config
    return dataclasses.replace(encode_config, video=tracks[0], video_tracks=tracks)


def _dimensions(wf: EncodeWorkflow, video: VideoEncodeSettings, config: EncodeConfig,
                cancel_event: threading.Event | None = None) -> tuple[int, int] | None:
    from core.inspector import FileInspector, InspectionCancelled

    try:
        info = FileInspector(ffprobe_bin=wf._ffprobe_bin_from_ffmpeg(wf._ffmpeg), cancel_event=cancel_event).inspect(
            video.source_path or config.source)
    except InspectionCancelled:
        raise
    except Exception:  # noqa: BLE001
        return None
    track = next((v for v in info.video_tracks if v.index == video.stream_index), info.primary_video)
    if track is None or not track.width or not track.height:
        return None
    return int(track.width), int(track.height)


def checked_encode(encode_config: EncodeConfig, wf: EncodeWorkflow, *, config: Any, options: CommonOptions,
                   accept_dovi_nvenc_crop: bool = False, logger: JobLogger | None = None,
                   cancel_event: threading.Event | None = None) -> tuple[EncodeConfig, list[str]]:
    """Configuration effective (géométrie DV NVENC) et refus du workflow, comme au lancement de :func:`run_encode`.

    ``cancel_event`` : inspection et sonde L5 annulables (l'annulation remonte, jamais un refus).
    """
    with probe_cancel_scope(wf, cancel_event):
        prepare_vulkan(wf, encode_config, str(options.ffmpeg or config.tool_ffmpeg), cancel_event)
        try:
            encode_config = align_dovi_nvenc(encode_config, wf, accept=accept_dovi_nvenc_crop, logger=logger,
                                             cancel_event=cancel_event)
        except JobError as exc:
            return encode_config, [exc.message]
        return encode_config, list(wf.validate(encode_config))


def run_encode(
    encode_config: EncodeConfig,
    *,
    config: Any,
    options: CommonOptions,
    logger: JobLogger,
    cancel: CancelToken,
    force: bool = False,
    accept_dovi_nvenc_crop: bool = False,
    workflow_factory: Callable[[], EncodeWorkflow] | None = None,
    progress: Callable[..., None] | None = None,
    cache: Any = None,
) -> str:
    """Encode jusqu'au nettoyage complet ; renvoie le message de fin.

    ``progress(**champs)`` : progression chiffrée (``pct``, ``fps``, ``frame``, ``eta_s``, ``track``…), calculée comme
    la GUI (:class:`EncodeProgressTracker`) ; ``cache`` : ``PreparationCache`` des inspections déjà faites (cibles).
    Échec ou annulation : message publié au journal, puis :class:`ApiError` marquée ``logged``.
    """
    if encode_config.output.exists() and not force:
        raise JobError(f"Sortie déjà existante : {encode_config.output} (utiliser --force)", EXIT_EXISTS)
    wf = workflow_factory() if workflow_factory is not None else encode_workflow(config, options)
    relay(wf.log_message, lambda level, message: logger.emit(level, message))
    with probe_cancel_scope(wf, cancel.event):  # sondes internes annulables jusqu'au nettoyage
        return _run_checked_encode(wf, encode_config, options=options, config=config, logger=logger, cancel=cancel,
                                   accept_dovi_nvenc_crop=accept_dovi_nvenc_crop, progress=progress, cache=cache)


def _run_checked_encode(wf: EncodeWorkflow, encode_config: EncodeConfig, *, options: CommonOptions, config: Any,
                        logger: JobLogger, cancel: CancelToken, accept_dovi_nvenc_crop: bool,
                        progress: Callable[..., None] | None, cache: Any) -> str:
    prepare_vulkan(wf, encode_config, str(options.ffmpeg or config.tool_ffmpeg), cancel.event)
    encode_config = align_dovi_nvenc(encode_config, wf, accept=accept_dovi_nvenc_crop, logger=logger,
                                     cancel_event=cancel.event)
    errors = wf.validate(encode_config)
    if errors:
        raise ApiError(ErrorCode.VALIDATION, "Encodage refusé :\n" + "\n".join(f"- {error}" for error in errors),
                       details=[ErrorDetail("", error) for error in errors])

    tracker = _progress_tracker(encode_config, config=config, options=options, cancel=cancel,
                                cache=cache) if progress is not None else None
    lock = threading.Lock()  # lignes de préparations concurrentes : lecture → calcul → publication, dans l'ordre
    last: dict[str, Any] = {}

    def publish(snapshot: ProgressSnapshot | None) -> None:  # sous ``lock``
        assert progress is not None
        fields = progress_fields(snapshot) if snapshot is not None else {}
        # Comme la barre de la GUI : rien à publier sans pourcentage, vitesse ni ETA, ni sans changement
        # (FFmpeg écrit une clé par ligne : ``out_time``, ``out_time_ms``, ``bitrate``…).
        if fields.keys() & {"pct", "fps", "eta_s"} and fields != last:
            last.clear()
            last.update(fields)
            progress(**fields)

    def on_line(line: str) -> None:
        if options.verbose:
            logger.emit("info", line)
        if tracker is None or progress is None:
            return
        with lock:
            publish(track_progress_line(tracker, line, time.monotonic(),
                                        parse=lambda text: wf.parse_progress(encode_config, text)))

    def on_pct(pct: int) -> None:  # outils lus sur PTY (dovi_tool, hdr10plus_tool) : pourcentage de l'étape
        if progress is not None:
            with lock:
                publish(stage_percent(f"{TOOL_PERCENT_PREFIX}{int(pct)}"))

    keep = bool(getattr(options, "keep_failed_work_dir", False))
    retained: list[Any] = []
    if keep:
        relay(wf.failed_work_dir_retained, retained.append)
    signals = wf.run(encode_config, validate=False, retain_failed_work_dir=keep)
    outcome = await_task(signals, cancel, on_progress=on_line, on_progress_pct=on_pct)
    for item in retained:  # échec avec fichiers produits : dossier gardé, proposé au prochain « Nettoyer »
        item.keep()
        logger.emit("info", f"Fichiers temporaires conservés : {item.path} (proposés au nettoyage au prochain "
                            "démarrage).", path=str(item.path))
    if outcome.status == "finished":
        # Le workflow rend la sortie brute de FFmpeg : publiée seulement en mode verbeux (comme la progression).
        if outcome.result and options.verbose:
            logger.emit("info", outcome.result)
        if cancel.cancelled:
            logger.emit("warning", "Annulation trop tardive : la sortie était déjà publiée.", code="cancel_too_late")
        lines = [line.strip() for line in (outcome.result or "").splitlines() if line.strip()]
        return lines[-1] if lines else ""
    if outcome.status == "cancelled":
        logger.emit("error", "Opération annulée.")
        raise ApiError(ErrorCode.CANCELLED, "Opération annulée.", logged=True)
    logger.emit("error", outcome.message, exception=repr(outcome.exception))
    # Échec après la levée du jeton : annulation (règle des lots, ``_Children``), message d'origine au journal.
    code = ErrorCode.CANCELLED if cancel.cancelled else error_code(outcome.exception)
    raise ApiError(code, outcome.message, logged=True)


def preview_encode(encode_config: EncodeConfig, wf: EncodeWorkflow,
                   cancel_event: threading.Event | None = None) -> str:
    """Commande(s) prévue(s) de l'encodage (préparation « à blanc », comme « Copier » du panneau).

    ``cancel_event`` : posé sur le workflow pendant la construction (la préparation à blanc peut relancer
    les sondes internes, bandes L5 du RPU).
    """
    with probe_cancel_scope(wf, cancel_event):
        return wf.preview_command(encode_config)


def track_progress_line(tracker: EncodeProgressTracker, line: str, now: float,
                        parse: Callable[[str], Any]) -> ProgressSnapshot | None:
    """Ligne de sortie de l'encodage → instantané de progression (``None`` : rien de nouveau), règles de la GUI.

    ``parse`` : normalisation du backend (``EncodeWorkflow.parse_progress``) ; lignes internes multi-pistes et
    lignes brutes FFmpeg lues par le suivi.
    """
    internal = parse_encode_internal_progress(line)
    if internal is not None:
        label = str(internal.get("label") or "").strip()
        if str(internal.get("kind") or "") != "encode_ffmpeg" or not label.startswith("ffmpeg-video-"):
            return None
        raw_line = str(internal.get("line") or "")
        tracker.state(label, now)
        if str(internal.get("event") or "").strip().lower() == "done":
            tracker.internal_done(label, now)
        elif raw_line.startswith("$ "):
            return None
        else:
            tracker.internal_line(label, raw_line, now)
        return tracker.multi_snapshot(now)
    if line.startswith("$ "):
        tracker.command_started()
        return None
    stage = stage_percent(line)  # assemblage Matroska, outils DV / HDR10+ : barre de l'étape, comme la GUI
    if stage is not None:
        return stage
    event = parse(line)
    if event is not None:
        return tracker.on_event(event, now)
    return tracker.on_line(line, now).snapshot


def progress_fields(snapshot: ProgressSnapshot) -> dict[str, Any]:
    """Champs d'un événement ``progress`` (inconnus omis ; vitesse nulle = inconnue, comme dans la GUI)."""
    fields = {
        "pct": snapshot.pct, "fps": snapshot.fps if snapshot.fps and snapshot.fps > 0 else None,
        "frame": snapshot.frame, "eta_s": snapshot.eta_s,
        "track": snapshot.track, "track_count": snapshot.track_count, "pass_index": snapshot.pass_index,
        "pass_count": snapshot.pass_count if snapshot.pass_index is not None else None,
        "stage": snapshot.stage,
    }
    return {key: value for key, value in fields.items() if value is not None}


def _progress_tracker(encode_config: EncodeConfig, *, config: Any, options: CommonOptions, cancel: CancelToken,
                      cache: Any) -> EncodeProgressTracker:
    """Suivi avec les cibles de chaque piste vidéo (durée, nombre d'images) lues dans les inspections."""
    from core.inspector import FileInspector
    from core.workflows.preparation_cache import PreparationCache

    inspector = FileInspector(ffprobe_bin=str(options.ffprobe or getattr(config, "tool_ffprobe", None) or "ffprobe"),
                              mediainfo_bin=str(options.mediainfo or getattr(config, "tool_mediainfo", None)
                                                or "mediainfo"),
                              cancel_event=cancel.event)
    store = cache if cache is not None else PreparationCache()

    def info_for(path: Any) -> Any:
        try:
            return store.inspect(inspector, path)
        except Exception:  # progression facultative : une inspection impossible ne bloque pas l'encodage
            return None

    targets = progress_targets(encode_config, info_for)
    first = targets.get(1, {})
    duration = first.get("duration_s")
    frames = first.get("total_frames")
    return EncodeProgressTracker(duration_s=duration if isinstance(duration, (int, float)) else None,
                                 total_frames=frames if isinstance(frames, int) else None, targets=targets)
