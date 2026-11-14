"""Exécution d'un encodage résolu (CLI, couche de service) : workflow, géométrie DV NVENC, aperçu, lancement.

Mêmes réglages d'exécution que le panneau Encodage (``refresh_runtime_settings``) ; aucune décision
interactive : le recadrage imposé par Dolby Vision + NVENC n'est appliqué que sur acceptation explicite.
"""

from __future__ import annotations

import threading

import dataclasses
from collections.abc import Callable
from typing import Any

from core.jobs.constants import EXIT_EXISTS, EXIT_VALIDATION
from core.jobs.errors import JobError
from core.jobs.logging import JobLogger
from core.jobs.options import CommonOptions
from core.service.context import CancelToken
from core.service.errors import ApiError, ErrorCode, error_code
from core.service.jobs import await_task
from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings
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


def prepare_vulkan(wf: EncodeWorkflow, encode_config: EncodeConfig, ffmpeg: str,
                   cancel_event: threading.Event | None = None) -> None:
    """NLMeans demandé : sonde Vulkan (GPU dédié) transmise au workflow, comme le tableau de bord.

    ``cancel_event`` : posé aussi sur le workflow (sondes internes : bandes L5 du RPU).
    """
    setter = getattr(wf, "set_probe_cancel_event", None)  # doubles de test : sans sondes internes
    if cancel_event is not None and setter is not None:
        setter(cancel_event)
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
    on_progress: Callable[[Any], None] | None = None,
) -> str:
    """Encode jusqu'au nettoyage complet ; renvoie le message de fin.

    Échec ou annulation : message publié au journal, puis :class:`ApiError` marquée ``logged``.
    """
    if encode_config.output.exists() and not force:
        raise JobError(f"Sortie déjà existante : {encode_config.output} (utiliser --force)", EXIT_EXISTS)
    wf = workflow_factory() if workflow_factory is not None else encode_workflow(config, options)
    wf.log_message.connect(lambda level, message: logger.emit(level, message))
    prepare_vulkan(wf, encode_config, str(options.ffmpeg or config.tool_ffmpeg), cancel.event)
    encode_config = align_dovi_nvenc(encode_config, wf, accept=accept_dovi_nvenc_crop, logger=logger,
                                     cancel_event=cancel.event)
    errors = wf.validate(encode_config)
    if errors:
        raise JobError("Encodage refusé :\n" + "\n".join(f"- {error}" for error in errors), EXIT_VALIDATION)

    def progress(line: str) -> None:
        event = wf.parse_progress(encode_config, line)
        if options.verbose:
            logger.emit("info", line)
        if event is not None and on_progress is not None:
            on_progress(event)

    keep = bool(getattr(options, "keep_failed_work_dir", False))
    retained: list[Any] = []
    if keep:
        wf.failed_work_dir_retained.connect(retained.append)
    signals = wf.run(encode_config, validate=False, retain_failed_work_dir=keep)
    outcome = await_task(signals, cancel, on_progress=progress)
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
    raise ApiError(error_code(outcome.exception), outcome.message, logged=True)


def preview_encode(encode_config: EncodeConfig, wf: EncodeWorkflow) -> str:
    """Commande(s) prévue(s) de l'encodage (préparation « à blanc », comme « Copier » du panneau)."""
    return wf.preview_command(encode_config)
