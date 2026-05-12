"""Command handlers for the headless CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.config import AppConfig
from core.inspector import FileInspector

from cli.batch import run_batch
from cli.constants import EXIT_OK, EXIT_VALIDATION
from cli.inspection import config_template_from_info, inspect_sources
from cli.jobs import load_job
from cli.json_io import json_default, write_json
from cli.logging import Logger
from cli.options import JobOverrides, common_options
from cli.remux_config import apply_explicit_track_edits, build_remux_config, config_to_template
from cli.rules import apply_track_rules
from cli.runtime import preview_remux_config, run_remux_config, workflow
from cli.schema import build_cli_json_schema
from cli.serializers import serialize_file_info, serialize_remux_config, serialize_track_preview


def cmd_inspect(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    if getattr(args, "rules_preview", False):
        job = load_job(JobOverrides.from_namespace(args))
        _sources, infos, tracks = inspect_sources(job, config, options, logger, cli_inputs=args.input)
        tracks = apply_track_rules(tracks, job.get("rules", {}))
        apply_explicit_track_edits(job, tracks)
        payload = {
            "files": [serialize_file_info(info) for info in infos],
            "rules_preview": [serialize_track_preview(track) for track in tracks],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
        return EXIT_OK

    ffprobe = options.ffprobe or config.tool_ffprobe
    mediainfo = options.mediainfo or config.tool_mediainfo
    inspector = FileInspector(ffprobe_bin=str(ffprobe), mediainfo_bin=str(mediainfo))
    infos = [inspector.inspect(Path(value).expanduser()) for value in args.input]
    if args.config_template:
        payload = config_template_from_info(infos[0], output=args.output or "")
    else:
        payload = {"files": [serialize_file_info(info) for info in infos]}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    return EXIT_OK


def cmd_schema(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    payload = build_cli_json_schema()
    if args.output:
        write_json(Path(args.output).expanduser(), payload)
        logger.emit("info", f"Schéma JSON sauvegardé : {args.output}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default))
    return EXIT_OK


def cmd_validate(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
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
    job = load_job(JobOverrides.from_namespace(args))
    if args.save:
        write_json(Path(args.save).expanduser(), config_to_template(job))
        logger.emit("info", f"Template sauvegardé : {args.save}")
    remux_config = build_remux_config(job, config, options, logger)
    if getattr(args, "dry_run", False):
        return preview_remux_config(config, options, logger, remux_config)
    return run_remux_config(config, options, logger, remux_config, force=bool(args.force))


def cmd_batch(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    return run_batch(
        template_path=args.template,
        batch_path=args.batch,
        cli_inputs=args.input,
        output_dir=args.output_dir,
        dry_run=bool(args.dry_run),
        force=bool(args.force),
        continue_on_error=bool(args.continue_on_error),
        summary_path=args.summary,
        config=config,
        options=common_options(args),
        logger=logger,
    )
