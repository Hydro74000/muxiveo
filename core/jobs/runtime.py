"""Exécution d'un remux construit (CLI, couche de service) : workflow, aperçu, lancement, annulation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.jobs.constants import EXIT_EXISTS
from core.jobs.errors import JobError
from core.jobs.logging import JobLogger
from core.jobs.options import CommonOptions
from core.service.context import CancelToken
from core.service.errors import ApiError, ErrorCode, error_code
from core.service.jobs import await_task
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig


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
    from core.workflows.workflow_store import save_workflow

    destination = Path(str(options.export_workflow))
    if options.export_directory:
        destination.mkdir(parents=True, exist_ok=True)
        destination /= remux_config.output.stem + ".exact-job.json"
    job = remux_config_to_exact_job(remux_config)
    if encode is not None:
        from core.json_documents import portable_encode_block

        job = {**job, "encode": portable_encode_block(encode)}
    save_workflow(destination, job)
    return destination


def run_remux(
    remux_config: RemuxConfig,
    *,
    config: AppConfig,
    options: CommonOptions,
    logger: JobLogger,
    cancel: CancelToken,
    force: bool = False,
    workflow_factory: Callable[[], RemuxWorkflow] | None = None,
) -> str:
    """Remuxe ``remux_config`` jusqu'au nettoyage complet ; renvoie le message de fin du workflow.

    Échec ou annulation : message publié au journal (forme historique du CLI), puis
    :class:`ApiError` marquée ``logged``.
    """
    if remux_config.output.exists() and not force:
        raise JobError(f"Sortie déjà existante : {remux_config.output} (utiliser --force)", EXIT_EXISTS)
    wf = workflow_factory() if workflow_factory is not None else remux_workflow(config, options)
    wf.log_message.connect(lambda level, message: logger.emit(level, message))
    wf.backend_event.connect(
        lambda report: logger.emit("info", "Sélection du backend de remuxage.", event="mux_backend", **dict(report))
    )
    signals = wf.run(remux_config)
    progress: Any = (lambda line: logger.emit("info", line)) if options.verbose else None
    outcome = await_task(signals, cancel, on_progress=progress)
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
    raise ApiError(error_code(outcome.exception), outcome.message, logged=True)
