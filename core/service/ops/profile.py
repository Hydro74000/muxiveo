"""Opérations des profils décisionnels v1 : validation, aperçu sur des sources, application."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.jobs.encode import resolve_from_job
from core.jobs.encode_runtime import checked_encode, encode_workflow, preview_encode, run_encode
from core.jobs.logging import EmitterLogger
from core.jobs.profile import build_profile_remux_config, load_decision_profile, profile_summary
from core.jobs.runtime import export_exact_job, remux_workflow, run_remux
from core.jobs.serializers import serialize_remux_config, serialize_track_preview
from core.workflows.preparation_cache import PreparationContext
from core.service.context import ExecutionContext
from core.service.errors import ErrorCode
from core.service.models import Model, field
from core.service.operations import operation
from core.service.ops.media import JobOptions, MediaRunResult, encode_plan_payload


@dataclass(frozen=True)
class ProfileRequest(Model):
    """Profil décisionnel à valider."""

    profile: str = field(description="Profil : chemin JSON ou nom d'un profil enregistré.")


@dataclass(frozen=True)
class ProfileValidateResult(Model):
    valid: bool
    kind: str | None = None
    version: Any = None
    name: str = ""
    rules: int = 0


@dataclass(frozen=True)
class ProfileJobRequest(Model):
    """Profil appliqué à des sources."""

    profile: str = field(description="Profil : chemin JSON ou nom d'un profil enregistré.")
    inputs: tuple[Path, ...] = field(min_items=1, description="Sources du job.")
    output: str | None = field(default=None, description="Sortie : fichier, ou dossier (avec / final).")
    options: JobOptions = field(default_factory=JobOptions)
    metadata: dict[str, Any] = field(default_factory=dict,
                                     description="Métadonnées du job (tmdb, output_template, mux_backend…).")
    encode: dict[str, Any] | None = field(default=None, description="Bloc `encode` (voir le schéma des jobs).")

    @classmethod
    def document_issues(cls, values: dict[str, Any]) -> list[tuple[str, str]]:
        """Clés inconnues du bloc ``encode`` (refusées en requête, signalées en CLI)."""
        from core.service.schemas.encode import encode_block_issues

        return encode_block_issues(values.get("encode"), "encode")

    @classmethod
    def document_errors(cls, values: dict[str, Any]) -> list[tuple[str, str]]:
        """Valeurs du bloc ``encode`` refusées dès la lecture d'une requête API."""
        from core.service.schemas.encode import encode_block_errors

        return encode_block_errors(values.get("encode"), "encode")

    @classmethod
    def resolve_relative_paths(cls, values: dict[str, Any], base_dir: Path) -> dict[str, Any]:
        """Preset du bloc ``encode`` désigné par un fichier : depuis le dossier de la requête."""
        from core.json_documents import resolve_encode_paths

        if isinstance(values.get("encode"), dict):
            values = {**values, "encode": resolve_encode_paths(values["encode"], base_dir)}
        return values


@dataclass(frozen=True)
class ProfilePreviewRequest(ProfileJobRequest):
    include_command: bool = field(default=True, description="Inclure les commandes prévues.")


@dataclass(frozen=True)
class ProfileApplyRequest(ProfileJobRequest):
    force: bool = field(default=False, description="Remplacer une sortie existante.")
    export_workflow: Path | None = field(
        default=None, description="Enregistrer le job exact à cet emplacement au lieu d'exécuter.")


@dataclass(frozen=True)
class ProfilePreviewResult(Model):
    """Job résolu par le profil, rapport de décision et commandes prévues."""

    valid: bool
    errors: tuple[str, ...] = ()
    profile_report: Any = field(default=None, description="Décisions du profil (règles appliquées, choix).")
    tracks: tuple[dict[str, Any], ...] = ()
    config: dict[str, Any] = field(default_factory=dict)
    command: Any = None
    command_text: str | None = None
    execution: dict[str, Any] | None = None
    encode: dict[str, Any] | None = None


@operation("profile.validate", request=ProfileRequest, result=ProfileValidateResult,
           summary="Valider un profil décisionnel v1.", errors=(ErrorCode.VALIDATION,))
def profile_validate(request: ProfileRequest, ctx: ExecutionContext) -> ProfileValidateResult:
    summary = profile_summary(load_decision_profile(request.profile, ctx.config))
    return ProfileValidateResult(**summary)


def _build(request: ProfileJobRequest, ctx: ExecutionContext, *, preview: bool):
    options = request.options.to_common(ctx.overrides)
    profile = load_decision_profile(request.profile, ctx.config)
    remux_config, report = build_profile_remux_config(
        profile,
        cli_inputs=[str(path) for path in request.inputs],
        cli_output=request.output,
        config=ctx.config,
        options=options,
        logger=EmitterLogger(ctx.events),
        preview=preview,
        metadata_job=dict(request.metadata),
        context=PreparationContext(cancel_event=ctx.cancel.event),
    )
    ctx.cancel.raise_if_cancelled()  # préparation annulée : rien n'est exporté ni lancé
    return remux_config, report, options


@operation("profile.preview", request=ProfilePreviewRequest, result=ProfilePreviewResult,
           summary="Appliquer un profil à des sources sans exécuter : pistes retenues, décisions, commandes.",
           uses_gpu=True, errors=(ErrorCode.VALIDATION, ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED))
def profile_preview(request: ProfilePreviewRequest, ctx: ExecutionContext) -> ProfilePreviewResult:
    remux_config, report, options = _build(request, ctx, preview=True)
    resolved = _resolve(request, remux_config, options, ctx)
    plan = encode_plan_payload(resolved)
    base: dict[str, Any] = dict(
        profile_report=report,
        tracks=tuple(serialize_track_preview(track) for source in remux_config.sources for track in source.tracks),
        config=serialize_remux_config(remux_config),
        encode=plan,
    )
    if resolved is not None and resolved.config is not None:
        # Mêmes refus qu'au lancement (géométrie DV + NVENC comprise), plan d'après la configuration contrôlée.
        encode_wf = encode_workflow(ctx.config, options)
        checked, errors = checked_encode(resolved.config, encode_wf, config=ctx.config, options=options,
                                         accept_dovi_nvenc_crop=bool((request.encode or {}).get("accept_dovi_nvenc_crop")),
                                         cancel_event=ctx.cancel.event)
        base.update(valid=not errors, errors=tuple(errors), encode=encode_plan_payload(resolved, checked))
        if not errors and request.include_command:
            base.update(command_text=preview_encode(checked, encode_wf, ctx.cancel.event),
                        execution={"kind": "encode", "selected_backend": "encode", "plan_version": 1,
                                   "preparation_commands": [], "native_diagnostics": []})
        return ProfilePreviewResult(**base)
    wf = remux_workflow(ctx.config, options)
    errors = wf.validate(remux_config)
    base.update(valid=not errors, errors=tuple(errors))
    if not errors and request.include_command:
        base.update(command=wf.build_command(remux_config), command_text=wf.preview_command(remux_config),
                    execution=dict(wf.execution_preview(remux_config)))
    return ProfilePreviewResult(**base)


def _resolve(request: ProfileJobRequest, remux_config: Any, options: Any, ctx: ExecutionContext):
    return resolve_from_job(request.encode, remux_config, config=ctx.config, options=options,
                            logger=EmitterLogger(ctx.events), report_unknown=False, cancel_event=ctx.cancel.event)


@operation("profile.apply", request=ProfileApplyRequest, result=MediaRunResult,
           summary="Appliquer un profil à des sources et remuxer (ou encoder).", long_running=True, mutating=True,
           uses_gpu=True,
           errors=(ErrorCode.VALIDATION, ErrorCode.OUTPUT_EXISTS, ErrorCode.BUSY, ErrorCode.TOOL_MISSING,
                   ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED))
def profile_apply(request: ProfileApplyRequest, ctx: ExecutionContext) -> MediaRunResult:
    remux_config, _report, options = _build(request, ctx, preview=False)
    if request.export_workflow is not None:
        destination = export_exact_job(remux_config, dataclasses.replace(options, export_workflow=str(
            request.export_workflow)), encode=request.encode)
        ctx.events.artifact("exact-job", str(destination))
        return MediaRunResult(status="exported", output=str(destination))
    resolved = _resolve(request, remux_config, options, ctx)
    if resolved is not None and resolved.config is not None:
        message = run_encode(resolved.config, config=ctx.config, options=options, logger=EmitterLogger(ctx.events),
                             cancel=ctx.cancel, force=request.force,
                             accept_dovi_nvenc_crop=bool((request.encode or {}).get("accept_dovi_nvenc_crop")),
                             on_progress=lambda event: ctx.events.progress(pct=event.percent, stage=event.stage_label,
                                                                           fps=event.fps, eta_s=event.eta_seconds))
        ctx.events.artifact("output", str(resolved.config.output))
        return MediaRunResult(status="completed", output=str(resolved.config.output), message=message)
    message = run_remux(remux_config, config=ctx.config, options=options, logger=EmitterLogger(ctx.events),
                        cancel=ctx.cancel, force=request.force)
    ctx.events.artifact("output", str(remux_config.output))
    return MediaRunResult(status="completed", output=str(remux_config.output), message=message)
