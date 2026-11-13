"""Commandes CLI des profils décisionnels v1 (construction : ``core.jobs.profile``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.profiles.decision import (
    profile_output_template,
)

from cli.batch import (
    PlannedBatchJob,
    assert_unique_batch_outputs,
    discover_direct_batch_jobs,
    job_primary_input,
    log_batch_failures,
    write_batch_summary,
)
from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_PARTIAL, EXIT_VALIDATION, EXIT_WORKFLOW
from cli.errors import CliError
from cli.inspection import source_path_items
from cli.jobs import apply_metadata_overrides
from cli.json_io import json_default
from cli.logging import Logger
from cli.options import CommonOptions
from cli.runtime import preview_remux_config, run_remux_config, workflow
from cli.serializers import serialize_remux_config, serialize_track_preview


from core.jobs.profile import (  # fonctions de construction partagées (core.jobs.profile)
    build_profile_remux_config,
    load_decision_profile,
    resolve_decision_profile_path as resolve_decision_profile_path,
    snapshot_decision_profile as snapshot_decision_profile,
)


def profile_validate(profile_path: str, *, json_output: bool = False, config: AppConfig | None = None) -> int:
    profile = load_decision_profile(profile_path, config)
    payload = {
        "valid": True,
        "kind": profile.get("kind"),
        "version": profile.get("version"),
        "name": profile.get("name", ""),
        "rules": len(profile.get("rules", []) if isinstance(profile.get("rules"), list) else []),
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    return EXIT_OK


def profile_preview(
    profile_path: str,
    *,
    inputs: list[str],
    output: str | None,
    json_output: bool,
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    include_command: bool = True,
    metadata_job: dict[str, Any] | None = None,
) -> int:
    profile = load_decision_profile(profile_path, config)
    try:
        remux_config, report = build_profile_remux_config(
            profile,
            cli_inputs=inputs,
            cli_output=output,
            config=config,
            options=options,
            logger=logger,
            preview=True,
            metadata_job=metadata_job,
        )
    except CliError as exc:
        if json_output and exc.exit_code == EXIT_VALIDATION:
            try:
                payload = json.loads(str(exc))
            except json.JSONDecodeError:
                raise
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
            return EXIT_VALIDATION
        raise
    wf = workflow(config, options, logger)
    errors = wf.validate(remux_config)
    payload = {
        "valid": not errors,
        "errors": errors,
        "profile_report": report,
        "tracks": [serialize_track_preview(track) for source in remux_config.sources for track in source.tracks],
        **serialize_remux_config(remux_config),
    }
    if not errors and include_command:
        payload["command"] = wf.build_command(remux_config)
        payload["command_text"] = wf.preview_command(remux_config)
        execution = wf.execution_preview(remux_config)
        payload.update({
            "selected_backend": execution["selected_backend"],
            "plan_version": execution["plan_version"],
            "preparation_commands": execution["preparation_commands"],
            "native_diagnostics": execution["native_diagnostics"],
            "execution_preview": execution,
        })
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    else:
        if errors:
            for error in errors:
                logger.emit("error", error)
            return EXIT_VALIDATION
        if include_command:
            print(payload.get("command_text", ""))
        else:
            logger.emit("info", "Configuration profil valide.")
    return EXIT_OK if not errors else EXIT_VALIDATION


def profile_apply(
    profile_path: str,
    *,
    inputs: list[str],
    output: str | None,
    force: bool,
    dry_run: bool,
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    metadata_job: dict[str, Any] | None = None,
) -> int:
    profile = load_decision_profile(profile_path, config)
    remux_config, _report = build_profile_remux_config(
        profile,
        cli_inputs=inputs,
        cli_output=output,
        config=config,
        options=options,
        logger=logger,
        preview=dry_run,
        metadata_job=metadata_job,
    )
    if dry_run:
        return preview_remux_config(config, options, logger, remux_config)
    return run_remux_config(config, options, logger, remux_config, force=force)


def profile_batch(
    profile_path: str,
    *,
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
    auto_tmdb: bool = False,
    tmdb: bool = False,
    tmdb_id: int | None = None,
    tmdb_apikey: str = "",
    output_template: str = "",
    output_all: bool = False,
    no_cover: bool = False,
    no_attach: bool = False,
) -> int:
    if not output_dir:
        raise CliError("`profile batch` requiert `--output-dir`.", EXIT_ARGS)
    profile = load_decision_profile(profile_path, config)
    output_template = output_template or profile_output_template(profile)
    metadata_template: dict[str, Any] = {}
    apply_metadata_overrides(
        metadata_template,
        auto_tmdb=auto_tmdb,
        tmdb=tmdb,
        tmdb_id=tmdb_id,
        tmdb_apikey=tmdb_apikey,
        no_cover=no_cover,
        no_attach=no_attach,
    )
    if output_template:
        metadata_template["output_template"] = output_template
        if output_all:
            metadata_template["output_all"] = True
        metadata_template["_batch_output_dir"] = str(Path(output_dir).expanduser())
    elif output_all:
        metadata_template["output_all"] = True
    discovery = discover_direct_batch_jobs(
        cli_inputs=cli_inputs,
        input_dirs=input_dirs,
        output_dir=output_dir,
        recursive=recursive,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        output_template=output_template,
        output_all=output_all,
    )
    if not discovery.jobs:
        raise CliError("Aucun fichier vidéo compatible trouvé pour le batch profil.", EXIT_ARGS)
    logger.emit(
        "info",
        f"Découverte batch profil : {discovery.selected}/{discovery.scanned} fichier(s) sélectionné(s).",
        event="profile_batch_discovery",
        scanned=discovery.scanned,
        selected=discovery.selected,
        roots=discovery.roots,
        recursive=recursive,
    )
    # Phase 1 — préparation de tous les jobs sans écriture ; collisions de
    # sorties refusées avant le premier traitement, même avec --force.
    planned: list[PlannedBatchJob] = []
    for job_index, job in enumerate(discovery.jobs):
        entry = PlannedBatchJob(job_index, job_primary_input(job), str(job.get("output") or ""))
        entry.template = output_template
        entry.generated_output = True
        planned.append(entry)
        try:
            inputs = [item["path"] for item in source_path_items(job)]
            remux_config, _report = build_profile_remux_config(
                profile,
                cli_inputs=inputs,
                cli_output=entry.output_label,
                config=config,
                options=options,
                logger=logger,
                preview=dry_run,
                metadata_job=metadata_template,
            )
            entry.remux_config = remux_config
            entry.output_label = str(remux_config.output)
        except Exception as exc:
            entry.error = exc
            if not continue_on_error:
                break
    assert_unique_batch_outputs(planned)

    # Phase 2 — exécution dans l'ordre.
    failures = 0
    summary_jobs: list[dict[str, Any]] = []
    for entry in planned:
        job_index = entry.index
        input_label = entry.input_label
        output_label = entry.output_label
        try:
            if entry.error is not None:
                raise entry.error
            remux_config = entry.remux_config
            if not dry_run and output_label:
                Path(output_label).expanduser().parent.mkdir(parents=True, exist_ok=True)
            rc = preview_remux_config(config, options, logger, remux_config) if dry_run else run_remux_config(
                config,
                options,
                logger,
                remux_config,
                force=force,
            )
            status = "success" if rc == EXIT_OK else "failed"
            failures += 0 if rc == EXIT_OK else 1
            summary_jobs.append({"job_index": job_index, "input": input_label, "output": output_label, "status": status, "exit_code": rc})
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
            logger.emit("error", f"Job profil batch échoué : {exc}", event="profile_batch_job", job_index=job_index, status="failed")
            if not continue_on_error:
                break
    total = len(summary_jobs)
    exit_code = EXIT_OK if failures == 0 else EXIT_PARTIAL
    write_batch_summary(
        summary_path,
        {
            "total": total,
            "successes": total - failures,
            "failures": failures,
            "exit_code": exit_code,
            "jobs": summary_jobs,
        },
    )
    log_batch_failures(logger, summary_jobs, event="profile_batch_failure")
    logger.emit("info", f"Batch profil terminé : {total - failures}/{total} succès.", event="profile_batch_summary", total=total, failures=failures)
    return exit_code
