"""Command handlers for the headless CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.config import AppConfig
from core.service.context import Overrides
from core.service.errors import ApiError
from core.jobs.errors import exit_code_for
from core.service.context import CancelToken
from core.service.ops.media import InspectResult, JobOptions, MediaPreviewResult, MediaValidateResult
from core.service.ops.system import SchemaResult, VersionResult
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
from cli.remux_config import config_to_template
from cli.encode_options import apply_encode_options, encode_block_from_args
from cli.interrupts import interrupt_guard
from cli.service_adapter import (
    argv_documents,
    envelope,
    exit_code_for_result,
    overrides_from_args,
    overrides_from_options,
    print_json,
    render_api_error,
    run_operation,
)


def cmd_tools(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    """Commandes d'outils résolues (opération system.tools)."""
    payload = {"versions": True} if getattr(args, "versions", False) else {}
    result, ctx = run_operation("system.tools", payload, config=config, logger=logger)
    if getattr(args, "log_format", "text") == "events":
        print_json(envelope("system.tools", ctx.events.job, result=result))
    else:
        print(json.dumps(result.to_dict(omit_none=True), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_capabilities(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    """Capacités de la machine (opération system.capabilities), en JSON."""
    from core.service.context import CancelToken

    payload = {"sections": list(args.section or [])}
    with interrupt_guard(CancelToken()) as cancel:  # Ctrl+C : sondes tuées, état « cancelled » (code 6)
        result, ctx = run_operation("system.capabilities", payload, config=config, logger=logger,
                                    overrides=overrides_from_args(args), cancel=cancel)
    if args.log_format == "events":
        print_json(envelope("system.capabilities", ctx.events.job, result=result))
    else:
        print_json(result.to_dict(omit_none=True))
    return EXIT_OK


def cmd_version(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    """Affiche la version ; avec --check, compare à la dernière release du canal choisi (opération system.version) ;
    avec --install, installe la dernière version (opération app.update_install)."""
    if getattr(args, "install", False):
        return _install_update(args, config, logger)
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


def _install_update(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    from cli.interrupts import interrupt_guard
    from core.service.context import CancelToken

    payload: dict = {"confirm": bool(getattr(args, "yes", False))}
    if args.channel:
        payload["channel"] = args.channel
    ports = {"query_latest_release": lambda channel: query_latest_release(channel)}
    with interrupt_guard(CancelToken()) as cancel:
        result, ctx = run_operation("app.update_install", payload, config=config, logger=logger, ports=ports,
                                    cancel=cancel)
    if args.log_format == "events":
        print_json(envelope("app.update_install", ctx.events.job, result=result))
        return EXIT_OK
    for entry in getattr(result, "previous", ()):
        level = "info" if entry.get("ok") else "warning"
        logger.emit(level, f"Mise à jour précédente ({entry.get('version', '?')}) : "
                           + ("réussie." if entry.get("ok") else str(entry.get("error") or "échec.")))
    logger.emit("info", str(getattr(result, "message", "")))
    return EXIT_OK


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
    if not getattr(args, "hdr_fel", False) and getattr(args, "hdr_fel_moments", None):
        raise CliError("--hdr-fel-moments s'utilise avec --hdr-fel.", EXIT_ARGS)
    if getattr(args, "hdr_advice", False) and not getattr(args, "hdr", None):
        raise CliError("--hdr-advice s'utilise avec --hdr static|dynamic|measure.", EXIT_ARGS)
    if getattr(args, "hdr", None) or getattr(args, "hdr_fel", False):
        from cli.hdr import cmd_inspect_hdr

        return cmd_inspect_hdr(args, config, logger)
    options = common_options(args)
    payload: dict = {"inputs": list(args.input), "config_template": bool(args.config_template)}
    if getattr(args, "with_encode", False):
        payload["with_encode"] = True
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
    """Schémas JSON publiés (opération schema.get)."""
    version = str(getattr(args, "schema_version", "1") or "1")
    result, ctx = run_operation("schema.get", {"version": version}, config=config, logger=logger)
    assert isinstance(result, SchemaResult)
    if args.log_format == "events":
        print_json(envelope("schema.get", ctx.events.job, result=result))
    elif args.output:
        write_json(Path(args.output).expanduser(), result.schema)
        logger.emit("info", f"Schéma JSON sauvegardé : {args.output}")
    else:
        print(json.dumps(result.schema, ensure_ascii=False, indent=2, default=json_default))
    return EXIT_OK


def cmd_validate(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "config", None):
            raise CliError("`--profile` ne se combine pas avec `--config`.", EXIT_VALIDATION)
        if not getattr(args, "input", None):
            rc = profile_validate(profile_path, json_output=bool(getattr(args, "json_output", False)), config=config,
                                  logger=logger)
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
            encode_block=encode_block_from_args(args),
        )
    job = apply_encode_options(load_job(JobOverrides.from_namespace(args)), args)
    result, ctx = run_operation("media.validate", _job_payload(job, options), config=config, logger=logger,
                                overrides=_job_overrides(options))
    assert isinstance(result, MediaValidateResult)
    exit_code = EXIT_OK if result.valid else EXIT_VALIDATION
    if args.log_format == "events":
        print_json(envelope("media.validate", ctx.events.job, result=result))
        return exit_code
    if getattr(args, "json_output", False):
        payload = {"valid": result.valid, "errors": list(result.errors), **result.config}
        if result.encode is not None:
            payload["encode"] = result.encode
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
        return exit_code
    if result.errors:
        for error in result.errors:
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
            encode_block=encode_block_from_args(args),
        )
    job = apply_encode_options(load_job(JobOverrides.from_namespace(args)), args)
    return _render_preview(_preview_job(job, options, config, logger), args, logger, json_output=bool(
        getattr(args, "json_output", False)))


def _preview_job(job: dict, options, config: AppConfig, logger: Logger) -> tuple[MediaPreviewResult, str]:
    result, ctx = run_operation("media.preview", _job_payload(job, options), config=config, logger=logger,
                                overrides=_job_overrides(options))
    assert isinstance(result, MediaPreviewResult)
    return result, ctx.events.job


def _render_preview(preview: tuple[MediaPreviewResult, str], args: argparse.Namespace, logger: Logger, *,
                    json_output: bool) -> int:
    """Rendu historique de ``preview`` (JSON ou commande), ou réponse publique en ``--log-format events``."""
    result, job = preview
    exit_code = EXIT_OK if result.valid else EXIT_VALIDATION
    if args.log_format == "events":
        print_json(envelope("media.preview", job, result=result))
        return exit_code
    if json_output:
        payload: dict = {"valid": result.valid, "errors": list(result.errors), **result.config}
        if result.valid:
            execution = result.execution or {}
            payload["command"] = result.command
            payload["command_text"] = result.command_text
            payload.update({
                "selected_backend": execution["selected_backend"],
                "plan_version": execution["plan_version"],
                "preparation_commands": execution["preparation_commands"],
                "native_diagnostics": execution["native_diagnostics"],
                "execution_preview": execution,
            })
        if result.encode is not None:
            payload["encode"] = result.encode
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
        return exit_code
    if result.errors:
        for error in result.errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    print(result.command_text)
    return EXIT_OK


def cmd_remux(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path:
        if getattr(args, "config", None):
            raise CliError("`--profile` ne se combine pas avec `--config`.", EXIT_VALIDATION)
        if getattr(args, "preview_image", None) or getattr(args, "preview_video", None):
            # Sinon le profil serait appliqué et le film entier encodé.
            raise CliError("Les aperçus (--preview-image / --preview-video) ne se combinent pas avec `--profile` : "
                           "exporter le job (--export-workflow) puis `encode --config job.json --preview-…`.",
                           EXIT_ARGS)
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
            encode_block=encode_block_from_args(args),
        )
    job = apply_encode_options(load_job(JobOverrides.from_namespace(args)), args)
    if args.save:
        # Template jamais écrit sur un fichier lu ou produit par le job (sources, documents, sortie).
        from core.sidecar_output import SidecarGuard, job_document_paths

        guard = SidecarGuard(job_document_paths(job))
        guard.protect_many(argv_documents(args), "document de la requête")
        guard.claim(args.save, "template (--save)")
        guard.write_json(Path(args.save).expanduser(), config_to_template(job))
        logger.emit("info", f"Template sauvegardé : {args.save}")
    return _run_job(job, args, config, options, logger)


def _run_job(job: dict, args: argparse.Namespace, config: AppConfig, options, logger: Logger) -> int:
    """``remux`` / ``run`` d'un job : aperçu (``--dry-run``) ou exécution (opération media.run, Ctrl+C = annulation)."""
    if getattr(args, "preview_image", None) or getattr(args, "preview_video", None):
        from cli.encode_commands import run_previews

        return run_previews(job, args, config, options, logger)
    if getattr(args, "dry_run", False):
        return _render_preview(_preview_job(job, options, config, logger), args, logger, json_output=False)
    payload = {**_job_payload(job, options), "force": bool(getattr(args, "force", False))}
    if options.export_workflow:
        payload.update(export_workflow=str(options.export_workflow), export_directory=options.export_directory)
    with interrupt_guard(CancelToken()) as cancel:
        try:
            result, ctx = run_operation("media.run", payload, config=config, logger=logger,
                                        overrides=_job_overrides(options), cancel=cancel,
                                        documents=argv_documents(args))
        except ApiError as error:
            if not error.logged:
                raise
            return exit_code_for(error)
    if args.log_format == "events":
        print_json(envelope("media.run", ctx.events.job, result=result))
    return EXIT_OK


def _job_payload(job: dict, options) -> dict:
    return {"job": job, "options": JobOptions.from_common(options)}


def _job_overrides(options) -> Overrides:
    return overrides_from_options(options)


def _batch_via_operation(args: argparse.Namespace, config: AppConfig, logger: Logger, profile_path: str) -> int:
    """``--log-format events`` : lot exécuté par l'opération (jobs enfants, réponse publique sur stdout)."""
    options = common_options(args)
    payload: dict = {
        "sources": {
            "inputs": list(args.input or []),
            "input_dirs": list(args.input_dir or []),
            "recursive": bool(args.recursive),
            "include": list(args.include or []),
            "exclude": list(args.exclude or []),
        },
        "metadata": {
            "auto_tmdb": bool(getattr(args, "auto_tmdb", False) or getattr(args, "tmdb", False)),
            "tmdb_apikey": str(getattr(args, "tmdb_apikey", "") or ""),
            "no_cover": bool(getattr(args, "no_cover", False)),
            "no_attach": bool(getattr(args, "no_attach", False)),
            "output_template": str(getattr(args, "output_template", "") or ""),
            "output_all": bool(getattr(args, "output_all", False)),
        },
        "options": JobOptions.from_common(options),
        "encode": encode_block_from_args(args),
        "dry_run": bool(args.dry_run),
        "force": bool(args.force),
        "continue_on_error": bool(args.continue_on_error),
    }
    if getattr(args, "tmdb_id", None) is not None:
        payload["metadata"]["tmdb_id"] = args.tmdb_id
    if args.summary:
        payload["summary"] = args.summary
    if profile_path:
        if not args.output_dir:
            raise CliError("`profile batch` requiert `--output-dir`.", EXIT_ARGS)
        name = "profile.batch"
        payload.update(profile=profile_path, output_dir=args.output_dir)
    else:
        if not getattr(args, "template", None):
            raise CliError("`batch` requiert `--template` ou `--profile`.", EXIT_ARGS)
        name = "media.batch"
        payload["template"] = args.template
        if getattr(args, "batch", None):
            payload["batch"] = args.batch
        if args.output_dir:
            payload["output_dir"] = args.output_dir
        if getattr(args, "mux_backend", None):
            payload["mux_backend"] = args.mux_backend
    with interrupt_guard(CancelToken()) as cancel:
        result, ctx = run_operation(name, payload, config=config, logger=logger,
                                    overrides=overrides_from_options(options), cancel=cancel,
                                    documents=argv_documents(args))
    print_json(envelope(name, ctx.events.job, result=result))
    return exit_code_for_result(result)


def cmd_batch(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    profile_path = str(getattr(args, "profile", "") or "")
    if profile_path and (getattr(args, "template", None) or getattr(args, "batch", None)):
        raise CliError("`--profile` ne se combine pas avec `--template` ou `--batch`.", EXIT_ARGS)
    if args.log_format == "events":
        return _batch_via_operation(args, config, logger, profile_path)
    if profile_path:
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
            encode_block=encode_block_from_args(args),
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
        encode_overrides=encode_block_from_args(args),
        config=config,
        options=common_options(args),
        logger=logger,
    )


def cmd_profile(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    command = str(getattr(args, "profile_command", "") or "")
    if command == "validate":
        rc = profile_validate(args.profile, json_output=bool(getattr(args, "json_output", False)), config=config,
                              logger=logger)
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
            encode_block=encode_block_from_args(args),
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
            encode_block=encode_block_from_args(args),
        )
    if command == "batch":
        if args.log_format == "events":
            return _batch_via_operation(args, config, logger, str(args.profile))
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
            encode_block=encode_block_from_args(args),
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
        if getattr(args, "preview_image", None) or getattr(args, "preview_video", None):
            # Sinon le profil serait appliqué et le film entier encodé.
            raise CliError("Les aperçus (--preview-image / --preview-video) ne se combinent pas avec `--profile` : "
                           "exporter le job (--export-workflow) puis `encode --config job.json --preview-…`.",
                           EXIT_ARGS)
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
            encode_block=encode_block_from_args(args),
        )
    job = apply_encode_options(load_job(JobOverrides.from_namespace(args)), args)
    return _run_job(job, args, config, options, logger)
