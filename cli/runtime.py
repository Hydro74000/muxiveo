"""Exécution d'un remux depuis le CLI (implémentation partagée : ``core.jobs.runtime``)."""

from __future__ import annotations

from core.config import AppConfig
from core.jobs.errors import exit_code_for
from core.jobs.logging import JobLogger
from core.jobs.encode import resolve_from_job
from core.jobs.encode_runtime import encode_workflow, prepare_vulkan, preview_encode, run_encode
from core.jobs.runtime import export_exact_job, remux_workflow, run_remux
from core.service.context import CancelToken
from core.service.errors import ApiError
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig

from cli.constants import EXIT_OK, EXIT_VALIDATION
from cli.interrupts import interrupt_guard
from cli.options import CommonOptions


def workflow(config: AppConfig, options: CommonOptions, logger: JobLogger) -> RemuxWorkflow:
    return remux_workflow(config, options)


def preview_remux_config(config: AppConfig, options: CommonOptions, logger: JobLogger, remux_config: RemuxConfig) -> int:
    errors = workflow(config, options, logger).validate(remux_config)
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    print(workflow(config, options, logger).preview_command(remux_config))
    return EXIT_OK


def run_remux_config(
    config: AppConfig,
    options: CommonOptions,
    logger: JobLogger,
    remux_config: RemuxConfig,
    *,
    force: bool = False,
    cancel: CancelToken | None = None,
) -> int:
    """Remuxe ``remux_config`` ; Ctrl+C annule proprement. Renvoie le code de sortie.

    ``cancel`` : jeton d'un appelant qui gère lui-même Ctrl+C (lot) ; sinon une garde est posée.
    """
    if options.export_workflow:
        export_exact_job(remux_config, options)
        return EXIT_OK
    if cancel is not None:
        return _run(remux_config, config, options, logger, cancel, force)
    with interrupt_guard(CancelToken()) as token:
        return _run(remux_config, config, options, logger, token, force)


def _run(remux_config: RemuxConfig, config: AppConfig, options: CommonOptions, logger: JobLogger, cancel: CancelToken,
         force: bool) -> int:
    try:
        run_remux(remux_config, config=config, options=options, logger=logger, cancel=cancel, force=force,
                  workflow_factory=lambda: workflow(config, options, logger))
    except ApiError as error:
        if not error.logged:
            raise
        return exit_code_for(error)
    return EXIT_OK


def resolve_job_encode_config(job: dict, remux_config: RemuxConfig, config: AppConfig, options: CommonOptions,
                              logger: JobLogger):
    """Encodage d'un job (bloc ``encode``) ; ``None`` sans bloc ou en copie pure (le job reste un remux)."""
    resolved = resolve_from_job(job.get("encode"), remux_config, config=config, options=options, logger=logger)
    return None if resolved is None else resolved.config


def preview_encode_config(config: AppConfig, options: CommonOptions, logger: JobLogger, encode_config) -> int:
    wf = encode_workflow(config, options)
    prepare_vulkan(wf, encode_config, str(options.ffmpeg or config.tool_ffmpeg))
    errors = wf.validate(encode_config)
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    print(preview_encode(encode_config, wf))
    return EXIT_OK


def run_encode_config(config: AppConfig, options: CommonOptions, logger: JobLogger, encode_config, *,
                      force: bool = False, accept_dovi_nvenc_crop: bool = False) -> int:
    """Encode ; Ctrl+C annule proprement (garde imbriquée dans celle d'un lot). Renvoie le code de sortie."""
    with interrupt_guard(CancelToken()) as cancel:
        try:
            run_encode(encode_config, config=config, options=options, logger=logger, cancel=cancel, force=force,
                       accept_dovi_nvenc_crop=accept_dovi_nvenc_crop)
        except ApiError as error:
            if not error.logged:
                raise
            return exit_code_for(error)
    return EXIT_OK
