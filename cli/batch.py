"""Batch processing helpers for CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.config import AppConfig

from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_PARTIAL, EXIT_WORKFLOW
from cli.contract import validate_batch_contract, validate_job_contract
from cli.errors import CliError
from cli.inspection import source_path_items
from cli.json_io import deep_merge, load_json, write_json
from cli.logging import Logger
from cli.options import CommonOptions
from cli.remux_config import build_remux_config
from cli.runtime import preview_remux_config, run_remux_config


def batch_jobs(batch: dict[str, Any]) -> list[dict[str, Any]]:
    raw_jobs = batch.get("jobs", batch.get("inputs", []))
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise CliError("Le batch doit contenir `jobs` ou `inputs`.", EXIT_ARGS)
    jobs: list[dict[str, Any]] = []
    for item in raw_jobs:
        if isinstance(item, dict):
            jobs.append(item)
        else:
            jobs.append({"sources": [str(item)]})
    return jobs


def job_primary_input(job: dict[str, Any]) -> str:
    try:
        first = source_path_items(job)[0]["path"]
        return str(first)
    except Exception:
        return ""


def write_batch_summary(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    write_json(Path(path).expanduser(), payload)


def run_batch(
    *,
    template_path: str,
    batch_path: str | None,
    cli_inputs: list[str] | None,
    output_dir: str | None,
    dry_run: bool,
    force: bool,
    continue_on_error: bool,
    summary_path: str | None,
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
) -> int:
    template = load_json(Path(template_path).expanduser())
    validate_job_contract(template, require_version=True)
    batch = load_json(Path(batch_path).expanduser()) if batch_path else {"inputs": cli_inputs or []}
    validate_batch_contract(batch)
    failures = 0
    total = 0
    summary_jobs: list[dict[str, Any]] = []
    for item in batch_jobs(batch):
        job_index = total
        total += 1
        job = deep_merge(template, item)
        input_label = job_primary_input(job)
        output_label = str(job.get("output") or "")
        logger.emit(
            "info",
            f"Batch job {job_index + 1} démarré",
            event="batch_job",
            job_index=job_index,
            input=input_label,
            output=output_label,
            status="started",
        )
        try:
            if "output" not in job and output_dir:
                first = source_path_items(job)[0]["path"]
                job["output"] = str(Path(output_dir).expanduser() / (Path(str(first)).stem + ".mkv"))
                output_label = str(job["output"])
            validate_job_contract(job, path=f"jobs[{total - 1}]", require_version=True)
            remux_config = build_remux_config(job, config, options, logger)
            output_label = str(remux_config.output)
            if dry_run:
                rc = preview_remux_config(config, options, logger, remux_config)
            else:
                rc = run_remux_config(config, options, logger, remux_config, force=force)
            if rc != EXIT_OK:
                failures += 1
                summary_jobs.append(
                    {"job_index": job_index, "input": input_label, "output": output_label, "status": "failed", "exit_code": rc}
                )
                logger.emit(
                    "error",
                    f"Batch job {job_index + 1} échoué",
                    event="batch_job",
                    job_index=job_index,
                    input=input_label,
                    output=output_label,
                    status="failed",
                    exit_code=rc,
                )
            else:
                summary_jobs.append(
                    {"job_index": job_index, "input": input_label, "output": output_label, "status": "success", "exit_code": EXIT_OK}
                )
                logger.emit(
                    "info",
                    f"Batch job {job_index + 1} terminé",
                    event="batch_job",
                    job_index=job_index,
                    input=input_label,
                    output=output_label,
                    status="success",
                )
        except Exception as exc:
            failures += 1
            summary_jobs.append(
                {
                    "job_index": job_index,
                    "input": input_label,
                    "output": output_label,
                    "status": "failed",
                    "exit_code": getattr(exc, "exit_code", EXIT_WORKFLOW),
                    "error": str(exc),
                }
            )
            logger.emit(
                "error",
                f"Job batch échoué : {exc}",
                event="batch_job",
                job_index=job_index,
                input=input_label,
                output=output_label,
                status="failed",
                exception=repr(exc),
            )
            if not continue_on_error:
                break
    exit_code = EXIT_OK if failures == 0 else EXIT_PARTIAL
    summary = {
        "total": total,
        "successes": total - failures,
        "failures": failures,
        "exit_code": exit_code,
        "jobs": summary_jobs,
    }
    write_batch_summary(summary_path, summary)
    logger.emit("info", f"Batch terminé : {total - failures}/{total} succès.", event="batch_summary", total=total, failures=failures)
    return exit_code
