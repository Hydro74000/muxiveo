"""Command handlers for the headless CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.config import AppConfig
from core.service.catalog import CATALOG
from core.service.context import Overrides
from core.service.errors import ApiError
from core.service.ops.media import InspectResult
from core.service.ops.system import VersionResult
from core.update_check import query_latest_release

from cli.batch import run_batch
from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_UPDATE_AVAILABLE, EXIT_VALIDATION, EXIT_WORKFLOW
from cli.errors import CliError
from cli.jobs import apply_metadata_overrides
from cli.jobs import load_job
from cli.json_io import json_default, write_json
from cli.logging import Logger
from cli.options import JobOverrides, common_options
from cli.profile import profile_apply, profile_batch, profile_preview, profile_validate
from cli.remux_config import build_remux_config, config_to_template
from cli.runtime import preview_remux_config, run_remux_config, workflow
from cli.schema import (
    build_cli_json_schema,
    build_cli_json_schema_bundle,
    build_decision_profile_schema_v1,
    build_exact_job_schema_v1,
)
from cli.serializers import serialize_remux_config
from cli.service_adapter import envelope, print_json, render_api_error, run_operation


def cmd_tools(_args: argparse.Namespace, config: AppConfig, _logger: Logger) -> int:
    """Expose the exact commands resolved from Muxiveo configuration."""
    commands = config.tool_commands()
    print(json.dumps({"version": 1, "tools": commands}, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_version(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    """Affiche la version ; avec --check, compare à la dernière release du canal choisi (opération system.version)."""
    payload: dict = {"check": bool(args.check)}
    if args.channel:
        payload["channel"] = args.channel
    # Port réseau résolu à l'appel : remplaçable dans les tests (cli.commands.query_latest_release).
    ports = {"query_latest_release": lambda channel: query_latest_release(channel)}
    try:
        result, ctx = run_operation("system.version", payload, config=config, logger=logger, ports=ports)
    except ApiError as error:
        return render_api_error(logger, error)
    assert isinstance(result, VersionResult)
    exit_code = EXIT_OK
    if args.check:
        if result.check_failed:
            exit_code = EXIT_WORKFLOW
        elif result.update_available:
            exit_code = EXIT_UPDATE_AVAILABLE
    if args.log_format == "events":
        print_json(envelope("system.version", ctx.events.job, result=result))
        return exit_code
    legacy: dict = {"name": result.name, "version": result.version}
    if result.channel is not None:
        legacy["channel"] = result.channel
    if result.latest is not None:
        legacy.update(latest=result.latest, update_available=result.update_available, url=result.url)
    if args.log_format == "jsonl":
        print(json.dumps(legacy, ensure_ascii=False))
        return exit_code
    print(f"{result.name} {result.version}")
    if result.latest is not None:
        if result.update_available:
            print(f"Nouvelle version disponible ({result.channel}) : {result.latest} — {result.url}")
        else:
            print(f"À jour (dernière version {result.channel} : {result.latest}).")
    return exit_code


def _metadata_job_from_args(args: argparse.Namespace) -> dict:
    job: dict = {}
    output_template = str(getattr(args, "output_template", "") or "")
    if output_template:
        job["output_template"] = output_template
    if bool(getattr(args, "output_all", False)):
        job["output_all"] = True
    mux_backend = getattr(args, "mux_backend", None)
    if mux_backend:
        job["mux_backend"] = mux_backend
    for key in ("sync_mode", "sync_subtitles", "clean_nfo", "crossfade_ms"):
        value = getattr(args, key, None)
        if value is not None:
            job[key] = value
    apply_metadata_overrides(
        job,
        auto_tmdb=bool(getattr(args, "auto_tmdb", False)),
        tmdb=bool(getattr(args, "tmdb", False)),
        tmdb_id=getattr(args, "tmdb_id", None),
        tmdb_apikey=str(getattr(args, "tmdb_apikey", "") or ""),
        no_cover=bool(getattr(args, "no_cover", False)),
        no_attach=bool(getattr(args, "no_attach", False)),
    )
    return job


def cmd_inspect(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    if not getattr(args, "hdr_fel", False) and (getattr(args, "hdr_fel_survival", None)
                                                or getattr(args, "hdr_fel_min", None)):
        raise CliError("--hdr-fel-survival et --hdr-fel-min s'utilisent avec --hdr-fel.", EXIT_ARGS)
    if getattr(args, "hdr", None) or getattr(args, "hdr_fel", False):
        from cli.hdr import cmd_inspect_hdr

        return cmd_inspect_hdr(args, config, logger)
    options = common_options(args)
    payload: dict = {"inputs": list(args.input), "config_template": bool(args.config_template)}
    if args.output:
        payload["output"] = args.output
    overrides = Overrides(ffprobe=options.ffprobe or None, mediainfo=options.mediainfo or None)
    try:
        result, ctx = run_operation("media.inspect", payload, config=config, logger=logger, overrides=overrides)
    except ApiError as error:
        return render_api_error(logger, error)
    assert isinstance(result, InspectResult)
    if args.log_format == "events":
        print_json(envelope("media.inspect", ctx.events.job, result=result))
        return EXIT_OK
    output = result.template if args.config_template else {"files": list(result.files)}
    print(json.dumps(output, ensure_ascii=False, indent=2, default=json_default))
    return EXIT_OK


def cmd_schema(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    version = str(getattr(args, "schema_version", "1") or "1")
    if version == "exact-job":
        payload = build_exact_job_schema_v1()
    elif version == "decision-profile":
        payload = build_decision_profile_schema_v1()
    elif version == "all":
        payload = build_cli_json_schema_bundle()
    elif version == "api":
        payload = CATALOG.describe()
    else:
        payload = build_cli_json_schema()
    if args.output:
        write_json(Path(args.output).expanduser(), payload)
        logger.emit("info", f"Schéma JSON sauvegardé : {args.output}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    return EXIT_OK


def cmd_validate(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "config", None):
            raise CliError("`--profile` ne se combine pas avec `--config`.", EXIT_VALIDATION)
        if not getattr(args, "input", None):
            rc = profile_validate(profile_path, json_output=bool(getattr(args, "json_output", False)), config=config)
            if rc == EXIT_OK and not getattr(args, "json_output", False):
                logger.emit("info", "Profil décisionnel valide.")
            return rc
        return profile_preview(
            profile_path,
            inputs=args.input or [],
            output=args.output,
            json_output=bool(getattr(args, "json_output", False)),
            config=config,
            options=options,
            logger=logger,
            include_command=False,
            metadata_job=_metadata_job_from_args(args),
        )
    job = load_job(JobOverrides.from_namespace(args))
    remux_config = build_remux_config(job, config, options, logger)
    errors = workflow(config, options, logger).validate(remux_config)
    if getattr(args, "json_output", False):
        payload = {
            "valid": not errors,
            "errors": errors,
            **serialize_remux_config(remux_config),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
        return EXIT_OK if not errors else EXIT_VALIDATION
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    logger.emit("info", "Configuration valide.")
    return EXIT_OK


def cmd_preview(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "config", None):
            raise CliError("`--profile` ne se combine pas avec `--config`.", EXIT_VALIDATION)
        return profile_preview(
            profile_path,
            inputs=args.input or [],
            output=args.output,
            json_output=bool(getattr(args, "json_output", False)),
            config=config,
            options=options,
            logger=logger,
            metadata_job=_metadata_job_from_args(args),
        )
    job = load_job(JobOverrides.from_namespace(args))
    remux_config = build_remux_config(job, config, options, logger)
    wf = workflow(config, options, logger)
    errors = wf.validate(remux_config)
    if getattr(args, "json_output", False):
        payload = {
            "valid": not errors,
            "errors": errors,
            **serialize_remux_config(remux_config),
        }
        if not errors:
            command = wf.build_command(remux_config)
            payload["command"] = command
            payload["command_text"] = wf.preview_command(remux_config)
            execution = wf.execution_preview(remux_config)
            payload.update({
                "selected_backend": execution["selected_backend"],
                "plan_version": execution["plan_version"],
                "preparation_commands": execution["preparation_commands"],
                "native_diagnostics": execution["native_diagnostics"],
                "execution_preview": execution,
            })
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
        return EXIT_OK if not errors else EXIT_VALIDATION
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    print(wf.preview_command(remux_config))
    return EXIT_OK


def cmd_remux(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "config", None):
            raise CliError("`--profile` ne se combine pas avec `--config`.", EXIT_VALIDATION)
        return profile_apply(
            profile_path,
            inputs=args.input or [],
            output=args.output,
            force=bool(getattr(args, "force", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            config=config,
            options=options,
            logger=logger,
            metadata_job=_metadata_job_from_args(args),
        )
    job = load_job(JobOverrides.from_namespace(args))
    if args.save:
        write_json(Path(args.save).expanduser(), config_to_template(job))
        logger.emit("info", f"Template sauvegardé : {args.save}")
    remux_config = build_remux_config(job, config, options, logger)
    if getattr(args, "dry_run", False):
        return preview_remux_config(config, options, logger, remux_config)
    return run_remux_config(config, options, logger, remux_config, force=bool(args.force))


def cmd_batch(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "template", None) or getattr(args, "batch", None):
            raise CliError("`--profile` ne se combine pas avec `--template` ou `--batch`.", EXIT_ARGS)
        return profile_batch(
            profile_path,
            cli_inputs=args.input,
            input_dirs=args.input_dir,
            recursive=bool(args.recursive),
            include_patterns=args.include,
            exclude_patterns=args.exclude,
            output_dir=args.output_dir,
            dry_run=bool(args.dry_run),
            force=bool(args.force),
            continue_on_error=bool(args.continue_on_error),
            summary_path=args.summary,
            auto_tmdb=bool(getattr(args, "auto_tmdb", False)),
            tmdb=bool(getattr(args, "tmdb", False)),
            tmdb_id=getattr(args, "tmdb_id", None),
            tmdb_apikey=str(getattr(args, "tmdb_apikey", "") or ""),
            output_template=str(getattr(args, "output_template", "") or ""),
            output_all=bool(getattr(args, "output_all", False)),
            no_cover=bool(getattr(args, "no_cover", False)),
            no_attach=bool(getattr(args, "no_attach", False)),
            config=config,
            options=common_options(args),
            logger=logger,
        )
    if not args.template:
        raise CliError("`batch` requiert `--template` ou `--profile`.", EXIT_ARGS)
    return run_batch(
        template_path=args.template,
        batch_path=args.batch,
        cli_inputs=args.input,
        input_dirs=args.input_dir,
        recursive=bool(args.recursive),
        include_patterns=args.include,
        exclude_patterns=args.exclude,
        output_dir=args.output_dir,
        dry_run=bool(args.dry_run),
        force=bool(args.force),
        continue_on_error=bool(args.continue_on_error),
        summary_path=args.summary,
        auto_tmdb=bool(getattr(args, "auto_tmdb", False)),
        tmdb=bool(getattr(args, "tmdb", False)),
        tmdb_id=getattr(args, "tmdb_id", None),
        tmdb_apikey=str(getattr(args, "tmdb_apikey", "") or ""),
        output_template=str(getattr(args, "output_template", "") or ""),
        output_all=bool(getattr(args, "output_all", False)),
        no_cover=bool(getattr(args, "no_cover", False)),
        no_attach=bool(getattr(args, "no_attach", False)),
        mux_backend=getattr(args, "mux_backend", None),
        config=config,
        options=common_options(args),
        logger=logger,
    )


def cmd_profile(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    command = str(getattr(args, "profile_command", "") or "")
    if command == "validate":
        rc = profile_validate(args.profile, json_output=bool(getattr(args, "json_output", False)), config=config)
        if rc == EXIT_OK and not getattr(args, "json_output", False):
            logger.emit("info", "Profil décisionnel valide.")
        return rc
    if command == "preview":
        return profile_preview(
            args.profile,
            inputs=args.input or [],
            output=args.output,
            json_output=bool(getattr(args, "json_output", False)),
            config=config,
            options=common_options(args),
            logger=logger,
            metadata_job=_metadata_job_from_args(args),
        )
    if command == "apply":
        return profile_apply(
            args.profile,
            inputs=args.input or [],
            output=args.output,
            force=bool(getattr(args, "force", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            config=config,
            options=common_options(args),
            logger=logger,
            metadata_job=_metadata_job_from_args(args),
        )
    if command == "batch":
        return profile_batch(
            args.profile,
            cli_inputs=args.input,
            input_dirs=args.input_dir,
            recursive=bool(args.recursive),
            include_patterns=args.include,
            exclude_patterns=args.exclude,
            output_dir=args.output_dir,
            dry_run=bool(args.dry_run),
            force=bool(args.force),
            continue_on_error=bool(args.continue_on_error),
            summary_path=args.summary,
            auto_tmdb=bool(getattr(args, "auto_tmdb", False)),
            tmdb=bool(getattr(args, "tmdb", False)),
            tmdb_id=getattr(args, "tmdb_id", None),
            tmdb_apikey=str(getattr(args, "tmdb_apikey", "") or ""),
            output_template=str(getattr(args, "output_template", "") or ""),
            output_all=bool(getattr(args, "output_all", False)),
            no_cover=bool(getattr(args, "no_cover", False)),
            no_attach=bool(getattr(args, "no_attach", False)),
            config=config,
            options=common_options(args),
            logger=logger,
        )
    raise CliError("Sous-commande profile inconnue.", EXIT_VALIDATION)


def cmd_run(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "config", None):
            raise CliError("`--profile` ne se combine pas avec `--config`.", EXIT_VALIDATION)
        return profile_apply(
            profile_path,
            inputs=args.input or [],
            output=args.output,
            force=bool(getattr(args, "force", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            config=config,
            options=options,
            logger=logger,
            metadata_job=_metadata_job_from_args(args),
        )
    job = load_job(JobOverrides.from_namespace(args))
    remux_config = build_remux_config(job, config, options, logger)
    if getattr(args, "dry_run", False):
        return preview_remux_config(config, options, logger, remux_config)
    return run_remux_config(config, options, logger, remux_config, force=bool(args.force))
