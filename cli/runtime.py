"""Exécution d'un remux depuis le CLI (implémentation partagée : ``core.jobs.runtime``)."""

from __future__ import annotations

from core.config import AppConfig
from core.jobs.errors import exit_code_for
from core.jobs.runtime import export_exact_job, remux_workflow, run_remux
from core.service.context import CancelToken
from core.service.errors import ApiError
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig

from cli.constants import EXIT_OK, EXIT_VALIDATION
from cli.interrupts import interrupt_guard
from cli.logging import Logger
from cli.options import CommonOptions


def workflow(config: AppConfig, options: CommonOptions, logger: Logger) -> RemuxWorkflow:
    return remux_workflow(config, options)


def preview_remux_config(config: AppConfig, options: CommonOptions, logger: Logger, remux_config: RemuxConfig) -> int:
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
    logger: Logger,
    remux_config: RemuxConfig,
    *,
    force: bool = False,
) -> int:
    """Remuxe ``remux_config`` ; Ctrl+C annule proprement. Renvoie le code de sortie."""
    if options.export_workflow:
        export_exact_job(remux_config, options)
        return EXIT_OK
    with interrupt_guard(CancelToken()) as cancel:
        try:
            run_remux(remux_config, config=config, options=options, logger=logger, cancel=cancel, force=force)
        except ApiError as error:
            if not error.logged:
                raise
            return exit_code_for(error)
    return EXIT_OK
