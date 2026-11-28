"""Exécution d'un remux construit (CLI, couche de service) : workflow, aperçu, lancement, annulation."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.jobs.constants import EXIT_EXISTS
from core.jobs.errors import JobError
from core.jobs.logging import JobLogger
from core.jobs.options import CommonOptions, call_supported
from core.output_commit import ExistingPolicy, OutputRefusedError
from core.service.context import CancelToken
from core.service.errors import ApiError, ErrorCode, ErrorDetail, error_code
from core.service.jobs import await_task, relay
from core.sidecar_output import SidecarGuard, remux_input_paths
from core.workflows.encode.progress_tracking import TimelineProgress
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, RemuxValidationError


def remux_workflow(config: AppConfig, options: CommonOptions) -> RemuxWorkflow:
    """Workflow de remux réglé d'après la configuration et les options du job."""
    return RemuxWorkflow(
        ffmpeg_bin=str(options.ffmpeg or config.tool_ffmpeg),
        ffprobe_bin=str(options.ffprobe or config.tool_ffprobe),
        ffmpeg_threads=options.threads if options.threads is not None else config.ffmpeg_threads,
        writing_application=str(options.writing_application or ""),
        generate_nfo=config.generate_nfo if options.nfo is None else bool(options.nfo),
        mediainfo_bin=str(options.mediainfo or config.tool_mediainfo),
        sync_rewrite_enabled=config.sync_rewrite_enabled,
        sync_advanced_audio_rewrite_enabled=config.sync_advanced_audio_rewrite_enabled,
        aac_bitrate_per_channel_kbps=config.aac_bitrate_per_channel_kbps,
        eac3_bitrate_per_channel_kbps=config.eac3_bitrate_per_channel_kbps,
    )


def export_exact_job(remux_config: RemuxConfig, options: CommonOptions, *, encode: Any = None) -> Path:
    """Enregistre le job exact (``--export-workflow``) au lieu de l'exécuter ; renvoie le fichier écrit.

    ``encode`` : bloc d'encodage du job, conservé tel quel dans le job exact.
    """
    from core.profiles.selectors import remux_config_to_exact_job
    from core.workflows.workflow_store import workflow_text

    destination = exact_job_destination(remux_config, options)
    guard = remux_sidecar_guard(remux_config, options)
    guard.claim(destination, "exact-job (--export-workflow)")
    job = remux_config_to_exact_job(remux_config)
    if encode is not None:
        from core.json_documents import portable_encode_block

        job = {**job, "encode": portable_encode_block(encode, options.base_dir)}
    text = workflow_text(job)
    if options.export_directory:
        destination.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text(destination, text)
    return destination


def exact_job_destination(remux_config: RemuxConfig, options: CommonOptions) -> Path:
    """Fichier de ``--export-workflow`` : le chemin donné, ou ``<dossier>/<sortie>.exact-job.json`` en lot."""
    destination = Path(str(options.export_workflow))
    if options.export_directory:
        destination /= remux_config.output.stem + ".exact-job.json"
    return destination


def remux_sidecar_guard(remux_config: RemuxConfig, options: CommonOptions) -> SidecarGuard:
    """Fichiers protégés d'un job : documents du transport, sources, pièces jointes, sortie planifiée."""
    guard = SidecarGuard()
    guard.protect_many(getattr(options, "protected_paths", ()) or (), "document de la requête")
    guard.protect_many(remux_input_paths(remux_config), "source")
    guard.protect(remux_config.output, "sortie")
    return guard


def run_remux(
    remux_config: RemuxConfig,
    *,
    config: AppConfig,
    options: CommonOptions,
    logger: JobLogger,
    cancel: CancelToken,
    force: bool = False,
    workflow_factory: Callable[[], RemuxWorkflow] | None = None,
    progress: Callable[..., None] | None = None,
) -> str:
    """Remuxe ``remux_config`` jusqu'au nettoyage complet ; renvoie le message de fin du workflow.

    ``progress(pct=…, eta_s=…, stage=…)`` : pourcentage du muxer natif ou temps écrit / durée de la source
    principale (en-tête FFmpeg), comme la barre de la GUI.

    Échec ou annulation : message publié au journal (forme historique du CLI), puis
    :class:`ApiError` marquée ``logged``.
    """
    if remux_config.output.exists() and not force:
        raise JobError(f"Sortie déjà existante : {remux_config.output} (utiliser --force)", EXIT_EXISTS)
    existing = ExistingPolicy.from_force(force)  # refus revérifié sous la réservation du workflow
    wf = workflow_factory() if workflow_factory is not None else remux_workflow(config, options)
    relay(wf.log_message, lambda level, message: logger.emit(level, message))
    relay(wf.backend_event,
          lambda report: logger.emit("info", "Sélection du backend de remuxage.", event="mux_backend", **dict(report)))
    try:
        signals = call_supported(wf.run, remux_config, existing=existing)
    except OutputRefusedError as exc:  # sortie apparue depuis le contrôle : jamais remplacée sans force
        raise JobError(str(exc), EXIT_EXISTS) from exc
    except RemuxValidationError as exc:  # refus avant tout lancement : code public « validation », un détail par écart
        raise ApiError(ErrorCode.VALIDATION, str(exc), details=[ErrorDetail("", error) for error in exc.errors]) from exc
    timeline = TimelineProgress()
    lock = threading.Lock()
    last: dict[str, Any] = {}

    def publish(fields: dict[str, Any]) -> None:  # sous ``lock`` : rien de vide ni de répété
        if progress is not None and fields and fields != last:
            last.clear()
            last.update(fields)
            progress(**fields)

    def on_line(line: str) -> None:
        if options.verbose:
            logger.emit("info", line)
        if progress is None:
            return
        with lock:
            snapshot = timeline.on_line(line, time.monotonic())
            if snapshot is not None:
                publish({key: value for key, value in (("pct", snapshot.pct), ("eta_s", snapshot.eta_s),
                                                       ("stage", snapshot.stage)) if value is not None})

    def on_pct(pct: int) -> None:
        with lock:
            publish({"pct": int(pct)})

    outcome = await_task(signals, cancel, on_progress=on_line, on_progress_pct=on_pct)
    if outcome.status == "finished":
        if outcome.result:
            logger.emit("info", outcome.result)
        if cancel.cancelled:
            # Course annulation / publication : la sortie est déjà publiée, l'opération a réussi.
            logger.emit("warning", "Annulation trop tardive : la sortie était déjà publiée.", code="cancel_too_late")
        return outcome.result
    if outcome.status == "cancelled":
        logger.emit("error", "Opération annulée.")
        raise ApiError(ErrorCode.CANCELLED, "Opération annulée.", logged=True)
    logger.emit("error", outcome.message, exception=repr(outcome.exception))
    # Échec après la levée du jeton : annulation (règle des lots, ``_Children``), message d'origine au journal.
    code = ErrorCode.CANCELLED if cancel.cancelled else error_code(outcome.exception)
    raise ApiError(code, outcome.message, logged=True)
