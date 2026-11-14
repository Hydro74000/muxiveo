"""Commandes CLI des profils décisionnels v1 (opérations ``profile.*`` ; construction : ``core.jobs.profile``)."""

from __future__ import annotations

import json
from typing import Any

from core.config import AppConfig
from core.jobs.errors import exit_code_for
from core.jobs.profile import (  # fonctions de construction partagées (core.jobs.profile)
    ProfileBatchSteps,
    build_profile_remux_config,
    load_decision_profile,
    resolve_decision_profile_path as resolve_decision_profile_path,
    run_profile_batch,
    snapshot_decision_profile as snapshot_decision_profile,
)
from core.jobs.options import call_cancellable, call_supported
from core.service.context import CancelToken
from core.service.errors import ApiError
from core.service.ops.media import JobOptions
from core.service.ops.profile import ProfilePreviewResult, ProfileValidateResult

from cli.batch import discover_direct_batch_jobs
from cli.constants import EXIT_OK, EXIT_VALIDATION
from cli.errors import CliError
from cli.interrupts import interrupt_guard
from cli.json_io import json_default
from cli.logging import Logger
from cli.options import CommonOptions
from cli.runtime import (
    preview_encode_config,
    preview_remux_config,
    resolve_job_encode_config,
    run_encode_config,
    run_remux_config,
)
from cli.service_adapter import envelope, overrides_from_options, print_json, run_operation


def _job_payload(profile_path: str, inputs: list[str], output: str | None, options: CommonOptions,
                 metadata_job: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"profile": str(profile_path), "inputs": list(inputs),
                               "options": JobOptions.from_common(options), "metadata": dict(metadata_job or {})}
    if output:
        payload["output"] = str(output)
    return payload


def _overrides(options: CommonOptions):
    return overrides_from_options(options)


def profile_validate(profile_path: str, *, json_output: bool = False, config: AppConfig | None = None,
                     logger: Logger | None = None) -> int:
    log = logger or Logger()
    result, ctx = run_operation("profile.validate", {"profile": str(profile_path)}, config=config, logger=log)
    assert isinstance(result, ProfileValidateResult)
    if log.fmt == "events":
        print_json(envelope("profile.validate", ctx.events.job, result=result))
    elif json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=json_default))
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
    encode_block: dict[str, Any] | None = None,
) -> int:
    payload = {**_job_payload(profile_path, inputs, output, options, metadata_job), "include_command": include_command}
    if encode_block:
        payload["encode"] = encode_block
    try:
        result, ctx = run_operation("profile.preview", payload, config=config, logger=logger,
                                    overrides=_overrides(options))
    except CliError as exc:
        # Contrat du profil non satisfait : rapport JSON porté par le message (forme historique).
        if json_output and exc.exit_code == EXIT_VALIDATION:
            try:
                report = json.loads(str(exc))
            except json.JSONDecodeError:
                raise exc from None
            print(json.dumps(report, ensure_ascii=False, indent=2, default=json_default))
            return EXIT_VALIDATION
        raise
    assert isinstance(result, ProfilePreviewResult)
    exit_code = EXIT_OK if result.valid else EXIT_VALIDATION
    if logger.fmt == "events":
        print_json(envelope("profile.preview", ctx.events.job, result=result))
        return exit_code
    rendered: dict[str, Any] = {"valid": result.valid, "errors": list(result.errors),
                                "profile_report": result.profile_report, "tracks": list(result.tracks),
                                **result.config}
    if result.valid and include_command:
        execution = result.execution or {}
        rendered["command"] = result.command
        rendered["command_text"] = result.command_text
        rendered.update({
            "selected_backend": execution["selected_backend"],
            "plan_version": execution["plan_version"],
            "preparation_commands": execution["preparation_commands"],
            "native_diagnostics": execution["native_diagnostics"],
            "execution_preview": execution,
        })
    if result.encode is not None:
        rendered["encode"] = result.encode
    if json_output:
        print(json.dumps(rendered, ensure_ascii=False, indent=2, default=json_default))
        return exit_code
    if result.errors:
        for error in result.errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    if include_command:
        print(rendered.get("command_text", ""))
    else:
        logger.emit("info", "Configuration profil valide.")
    return EXIT_OK


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
    encode_block: dict[str, Any] | None = None,
) -> int:
    if dry_run:
        return profile_preview(profile_path, inputs=inputs, output=output, json_output=False, config=config,
                               options=options, logger=logger, metadata_job=metadata_job, encode_block=encode_block)
    payload = {**_job_payload(profile_path, inputs, output, options, metadata_job), "force": bool(force)}
    if encode_block:
        payload["encode"] = encode_block
    if options.export_workflow:  # --export-workflow : job exact enregistré, rien n'est exécuté
        payload["export_workflow"] = str(options.export_workflow)
    with interrupt_guard(CancelToken()) as cancel:
        try:
            result, ctx = run_operation("profile.apply", payload, config=config, logger=logger,
                                        overrides=_overrides(options), cancel=cancel)
        except ApiError as error:
            if not error.logged:
                raise
            return exit_code_for(error)
    if logger.fmt == "events":
        print_json(envelope("profile.apply", ctx.events.job, result=result))
    return EXIT_OK


def cli_profile_batch_steps(cancel: CancelToken | None = None) -> ProfileBatchSteps:
    """Étapes historiques du CLI, résolues à l'appel (fonctions remplaçables du module).

    ``cancel`` : jeton du lot, transmis à la préparation des constructeurs qui l'acceptent.
    """
    return ProfileBatchSteps(
        load=lambda path, config: load_decision_profile(path, config),
        discover=lambda **kwargs: discover_direct_batch_jobs(**kwargs),
        build=lambda profile, **kwargs: call_cancellable(build_profile_remux_config, cancel, profile, **kwargs),
        preview=lambda config, options, logger, remux_config: preview_remux_config(config, options, logger,
                                                                                    remux_config),
        # Ctrl+C : la garde du job annule le job puis prévient celle du lot (gardes imbriquées).
        run=lambda config, options, logger, remux_config, *, force: run_remux_config(
            config, options, logger, remux_config, force=force),
        encode=lambda job, remux_config, config, options, logger: call_cancellable(
            resolve_job_encode_config, cancel, job, remux_config, config, options, logger,
            context_param="cancel_event"),
        preview_encode=lambda config, options, logger, encode_config, *, accept_dovi_nvenc_crop=False: (
            call_supported(preview_encode_config, config, options, logger, encode_config,
                           accept_dovi_nvenc_crop=accept_dovi_nvenc_crop,
                           cancel_event=cancel.event if cancel is not None else None)),
        run_encode=lambda config, options, logger, encode_config, *, force, accept_dovi_nvenc_crop: run_encode_config(
            config, options, logger, encode_config, force=force, accept_dovi_nvenc_crop=accept_dovi_nvenc_crop),
    )


def profile_batch(profile_path: str, **kwargs: Any) -> int:
    """Applique un profil à un lot ; Ctrl+C annule le job en cours et arrête le lot."""
    with interrupt_guard(CancelToken()) as cancel:
        return run_profile_batch(profile_path, **kwargs, steps=cli_profile_batch_steps(cancel),
                                 cancel=cancel).exit_code
