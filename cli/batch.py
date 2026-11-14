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
from core.jobs.options import call_cancellable
from core.service.context import CancelToken

from cli.interrupts import interrupt_guard
from cli.remux_config import build_remux_config
from cli.runtime import (
    preview_encode_config,
    preview_remux_config,
    resolve_job_encode_config,
    run_encode_config,
    run_remux_config,
)


def cli_batch_steps(cancel: CancelToken | None = None) -> BatchSteps:
    """Étapes historiques du CLI, résolues à l'appel (fonctions remplaçables du module).

    ``cancel`` : jeton du lot, transmis à la préparation (inspections, synchros, encodage) des constructeurs.
    """
    return BatchSteps(
        build=lambda job, config, options, logger: call_cancellable(build_remux_config, cancel, job, config, options,
                                                                    logger),
        preview=lambda config, options, logger, remux_config: preview_remux_config(config, options, logger,
                                                                                    remux_config),
        # Ctrl+C : la garde du job annule le job puis prévient celle du lot (gardes imbriquées).
        run=lambda config, options, logger, remux_config, *, force: run_remux_config(
            config, options, logger, remux_config, force=force),
        encode=lambda job, remux_config, config, options, logger: call_cancellable(
            resolve_job_encode_config, cancel, job, remux_config, config, options, logger,
            context_param="cancel_event"),
        preview_encode=lambda config, options, logger, encode_config: preview_encode_config(
            config, options, logger, encode_config),
        run_encode=lambda config, options, logger, encode_config, *, force, accept_dovi_nvenc_crop: run_encode_config(
            config, options, logger, encode_config, force=force, accept_dovi_nvenc_crop=accept_dovi_nvenc_crop),
    )


def run_batch(**kwargs: Any) -> int:
    """Lot de jobs ; Ctrl+C annule le job en cours et arrête le lot. Renvoie le code de sortie."""
    with interrupt_guard(CancelToken()) as cancel:
        return _run_batch(**kwargs, steps=cli_batch_steps(cancel), cancel=cancel).exit_code
