"""Batch processing helpers for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.file_types import is_accepted

from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_PARTIAL, EXIT_WORKFLOW
from cli.contract import validate_batch_contract, validate_job_contract
from cli.errors import CliError
from cli.hybrid import is_v2_job, preview_hybrid_job, run_hybrid_job
from cli.inspection import source_path_items
from cli.json_io import deep_merge, load_json, write_json
from cli.logging import Logger
from cli.options import CommonOptions
from cli.remux_config import build_remux_config
from cli.runtime import preview_remux_config, run_remux_config


@dataclass(frozen=True)
class BatchDiscovery:
    jobs: list[dict[str, Any]]
    scanned: int
    selected: int
    explicit_inputs: int
    roots: list[str]


def _matches_any(path: Path, patterns: list[str] | None) -> bool:
    if not patterns:
        return False
    rel = path.as_posix()
    name = path.name
    return any(fnmatch(rel, pattern) or fnmatch(name, pattern) for pattern in patterns)


def _generated_output_path(output_dir: str | None, relative_path: Path) -> str | None:
    if not output_dir:
        return None
    return str(Path(output_dir).expanduser() / relative_path.with_suffix(".mkv"))


def _job_for_source(path: Path, *, output: str | None = None) -> dict[str, Any]:
    job: dict[str, Any] = {"sources": [{"path": str(path)}]}
    if output:
        job["output"] = output
        job["_batch_generated_output"] = True
    return job


def _assert_unique_generated_outputs(jobs: list[dict[str, Any]]) -> None:
    seen: dict[str, str] = {}
    for job in jobs:
        if not job.get("_batch_generated_output"):
            continue
        output = str(job.get("output") or "")
        if not output:
            continue
        input_path = job_primary_input(job)
        previous = seen.get(output)
        if previous is not None:
            raise CliError(
                "Sortie générée en double : "
                f"{output} pour {previous} et {input_path}. "
                "Utilisez des dossiers distincts ou un batch JSON avec sorties explicites.",
                EXIT_ARGS,
            )
        seen[output] = input_path


def discover_direct_batch_jobs(
    *,
    cli_inputs: list[str] | None = None,
    input_dirs: list[str] | None = None,
    output_dir: str | None = None,
    recursive: bool = False,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> BatchDiscovery:
    jobs: list[dict[str, Any]] = []
    scanned = 0
    roots: list[str] = []

    for raw in cli_inputs or []:
        path = Path(str(raw)).expanduser()
        scanned += 1
        output = _generated_output_path(output_dir, Path(path.name))
        jobs.append(_job_for_source(path, output=output))

    for raw_dir in input_dirs or []:
        root = Path(str(raw_dir)).expanduser()
        if not root.exists():
            raise CliError(f"Dossier batch introuvable : {root}", EXIT_ARGS)
        if not root.is_dir():
            raise CliError(f"Chemin --input-dir invalide, attendu dossier : {root}", EXIT_ARGS)
        roots.append(str(root))
        iterator = root.rglob("*") if recursive else root.iterdir()
        candidates = sorted(
            (path for path in iterator if path.is_file()),
            key=lambda path: path.relative_to(root).as_posix().lower(),
        )
        for path in candidates:
            scanned += 1
            relative = path.relative_to(root)
            if not is_accepted(path, video_only=True):
                continue
            if include_patterns and not _matches_any(relative, include_patterns):
                continue
            if _matches_any(relative, exclude_patterns):
                continue
            output = _generated_output_path(output_dir, relative)
            jobs.append(_job_for_source(path, output=output))

    _assert_unique_generated_outputs(jobs)
    return BatchDiscovery(
        jobs=jobs,
        scanned=scanned,
        selected=len(jobs),
        explicit_inputs=len(cli_inputs or []),
        roots=roots,
    )


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
    input_dirs: list[str] | None,
    recursive: bool,
    include_patterns: list[str] | None,
    exclude_patterns: list[str] | None,
    output_dir: str | None,
    dry_run: bool,
    force: bool,
    continue_on_error: bool,
    summary_path: str | None,
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
) -> int:
    direct_mode = bool(cli_inputs or input_dirs or recursive or include_patterns or exclude_patterns)
    if batch_path and direct_mode:
        raise CliError(
            "`--batch` ne peut pas être combiné avec `-i/--input`, `--input-dir`, "
            "`--recursive`, `--include` ou `--exclude`.",
            EXIT_ARGS,
        )

    template = load_json(Path(template_path).expanduser())
    validate_job_contract(template, require_version=True)

    discovery: BatchDiscovery | None = None
    if batch_path:
        batch = load_json(Path(batch_path).expanduser())
    else:
        discovery = discover_direct_batch_jobs(
            cli_inputs=cli_inputs,
            input_dirs=input_dirs,
            output_dir=output_dir,
            recursive=recursive,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        if not discovery.jobs:
            raise CliError("Aucun fichier vidéo compatible trouvé pour le batch.", EXIT_ARGS)
        logger.emit(
            "info",
            f"Découverte batch : {discovery.selected}/{discovery.scanned} fichier(s) sélectionné(s).",
            event="batch_discovery",
            scanned=discovery.scanned,
            selected=discovery.selected,
            explicit_inputs=discovery.explicit_inputs,
            roots=discovery.roots,
            recursive=recursive,
        )
        batch = {"jobs": discovery.jobs}

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
                job["_batch_generated_output"] = True
                output_label = str(job["output"])
            if dry_run and job.get("_batch_generated_output"):
                job["_allow_missing_output_dir"] = True
            if not dry_run and job.get("_batch_generated_output") and job.get("output"):
                Path(str(job["output"])).expanduser().parent.mkdir(parents=True, exist_ok=True)
            validate_job_contract(job, path=f"jobs[{total - 1}]", require_version=True)
            if is_v2_job(job):
                output_label = str(job.get("output") or output_label)
                if dry_run:
                    rc = preview_hybrid_job(config, options, logger, job)
                else:
                    rc = run_hybrid_job(config, options, logger, job, force=force)
            else:
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
