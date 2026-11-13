"""Commande ``batch`` du CLI : lots de jobs (implémentation partagée : ``core.jobs.batch``)."""

from __future__ import annotations

from typing import Any

from core.jobs.batch import (  # noqa: F401 - réexports historiques (découverte, planification, résumé)
    BatchDiscovery,
    BatchOutcome,
    BatchSteps,
    PlannedBatchJob,
    assert_unique_batch_outputs,
    batch_jobs,
    discover_direct_batch_jobs,
    job_primary_input,
    log_batch_failures,
    write_batch_summary,
)
from core.jobs.batch import run_batch as _run_batch
from core.service.context import CancelToken

from cli.interrupts import interrupt_guard
from cli.remux_config import build_remux_config
from cli.runtime import preview_remux_config, run_remux_config


def cli_batch_steps() -> BatchSteps:
    """Étapes historiques du CLI, résolues à l'appel (fonctions remplaçables du module)."""
    return BatchSteps(
        build=lambda job, config, options, logger: build_remux_config(job, config, options, logger),
        preview=lambda config, options, logger, remux_config: preview_remux_config(config, options, logger,
                                                                                    remux_config),
        # Ctrl+C : la garde du job annule le job puis prévient celle du lot (gardes imbriquées).
        run=lambda config, options, logger, remux_config, *, force: run_remux_config(
            config, options, logger, remux_config, force=force),
    )


def run_batch(**kwargs: Any) -> int:
    """Lot de jobs ; Ctrl+C annule le job en cours et arrête le lot. Renvoie le code de sortie."""
    with interrupt_guard(CancelToken()) as cancel:
        return _run_batch(**kwargs, steps=cli_batch_steps(), cancel=cancel).exit_code
