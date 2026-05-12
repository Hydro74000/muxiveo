"""CLI support for decision-profile v1."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.profiles.decision import (
    DECISION_PROFILE_KIND,
    DecisionProfileError,
    apply_decision_profile,
    validate_decision_profile,
)
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry

from cli.batch import discover_direct_batch_jobs, job_primary_input, write_batch_summary
from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_PARTIAL, EXIT_VALIDATION, EXIT_WORKFLOW
from cli.errors import CliError
from cli.inspection import inspect_sources, source_path_items
from cli.json_io import json_default, load_json
from cli.logging import Logger
from cli.options import CommonOptions
from cli.runtime import preview_remux_config, run_remux_config, workflow
from cli.serializers import serialize_remux_config, serialize_track_preview


def load_decision_profile(path: str | Path) -> dict[str, Any]:
    profile = load_json(Path(path).expanduser())
    try:
        validate_decision_profile(profile)
    except DecisionProfileError as exc:
        raise CliError(str(exc), EXIT_ARGS) from exc
    if profile.get("kind") != DECISION_PROFILE_KIND:
        raise CliError("Le fichier n'est pas un decision-profile v1.", EXIT_ARGS)
    return profile


def _output_path(raw_output: str | None, sources: list[SourceInput], *, preview: bool = False) -> Path:
    if raw_output:
        return Path(str(raw_output)).expanduser()
    if preview and sources:
        return sources[0].path.with_suffix(".profile-preview.mkv")
    raise CliError("Une sortie est requise (`-o/--output`).", EXIT_ARGS)


def _refresh_sources_tracks(sources: list[SourceInput], tracks: list[TrackEntry]) -> None:
    by_file_id: dict[str, list[TrackEntry]] = {}
    for track in tracks:
        by_file_id.setdefault(track.file_id, []).append(track)
    for source in sources:
        source.tracks = by_file_id.get(f"src{source.file_index}", source.tracks)


def _track_order(tracks: list[TrackEntry]) -> list[tuple[int, int, str]]:
    order: list[tuple[int, int, str]] = []
    for track in tracks:
        if not track.enabled:
            continue
        source_index = 0
        if str(track.file_id).startswith("src"):
            try:
                source_index = int(str(track.file_id)[3:])
            except ValueError:
                source_index = 0
        order.append((source_index, int(track.mkv_tid), track.entry_id))
    return order


def build_profile_remux_config(
    profile: dict[str, Any],
    *,
    cli_inputs: list[str],
    cli_output: str | None,
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    preview: bool = False,
) -> tuple[RemuxConfig, dict[str, Any]]:
    if not cli_inputs:
        raise CliError("Au moins une entrée est requise avec `--profile` (`-i/--input`).", EXIT_ARGS)
    job = {"sources": [{"path": value} for value in cli_inputs]}
    sources, _infos, tracks = inspect_sources(job, config, options, logger)
    source_index_by_file_id = {f"src{source.file_index}": source.file_index for source in sources}
    result = apply_decision_profile(
        profile,
        tracks,
        source_index_by_file_id=source_index_by_file_id,
    )
    if not result.report.get("valid", True):
        raise CliError(json.dumps(result.report, ensure_ascii=False, default=json_default), EXIT_VALIDATION)
    _refresh_sources_tracks(sources, result.tracks)
    output = _output_path(cli_output, sources, preview=preview)
    remux_config = RemuxConfig(
        sources=sources,
        output=output,
        track_order=_track_order(result.tracks),
        keep_chapters=True,
        work_dir=Path(str(options.work_dir or config.work_dir)).expanduser(),
        allow_missing_output_dir=preview,
    )
    return remux_config, result.report


def profile_validate(profile_path: str, *, json_output: bool = False) -> int:
    profile = load_decision_profile(profile_path)
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
) -> int:
    profile = load_decision_profile(profile_path)
    try:
        remux_config, report = build_profile_remux_config(
            profile,
            cli_inputs=inputs,
            cli_output=output,
            config=config,
            options=options,
            logger=logger,
            preview=True,
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
) -> int:
    profile = load_decision_profile(profile_path)
    remux_config, _report = build_profile_remux_config(
        profile,
        cli_inputs=inputs,
        cli_output=output,
        config=config,
        options=options,
        logger=logger,
        preview=dry_run,
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
) -> int:
    if not output_dir:
        raise CliError("`profile batch` requiert `--output-dir`.", EXIT_ARGS)
    profile = load_decision_profile(profile_path)
    discovery = discover_direct_batch_jobs(
        cli_inputs=cli_inputs,
        input_dirs=input_dirs,
        output_dir=output_dir,
        recursive=recursive,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
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
    failures = 0
    summary_jobs: list[dict[str, Any]] = []
    for job_index, job in enumerate(discovery.jobs):
        input_label = job_primary_input(job)
        output_label = str(job.get("output") or "")
        try:
            inputs = [item["path"] for item in source_path_items(job)]
            if not dry_run and output_label:
                Path(output_label).expanduser().parent.mkdir(parents=True, exist_ok=True)
            remux_config, _report = build_profile_remux_config(
                profile,
                cli_inputs=inputs,
                cli_output=output_label,
                config=config,
                options=options,
                logger=logger,
                preview=dry_run,
            )
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
    logger.emit("info", f"Batch profil terminé : {total - failures}/{total} succès.", event="profile_batch_summary", total=total, failures=failures)
    return exit_code
