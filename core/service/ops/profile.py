"""Opérations des profils décisionnels v1 : validation, aperçu sur des sources, application."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.jobs.logging import EmitterLogger
from core.jobs.profile import build_profile_remux_config, load_decision_profile, profile_summary
from core.jobs.runtime import remux_workflow, run_remux
from core.jobs.serializers import serialize_remux_config, serialize_track_preview
from core.service.context import ExecutionContext
from core.service.errors import ErrorCode
from core.service.models import Model, field
from core.service.operations import operation
from core.service.ops.media import JobOptions, MediaRunResult


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


@dataclass(frozen=True)
class ProfilePreviewRequest(ProfileJobRequest):
    include_command: bool = field(default=True, description="Inclure les commandes prévues.")


@dataclass(frozen=True)
class ProfileApplyRequest(ProfileJobRequest):
    force: bool = field(default=False, description="Remplacer une sortie existante.")


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
    )
    return remux_config, report, options


@operation("profile.preview", request=ProfilePreviewRequest, result=ProfilePreviewResult,
           summary="Appliquer un profil à des sources sans exécuter : pistes retenues, décisions, commandes.",
           errors=(ErrorCode.VALIDATION, ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED))
def profile_preview(request: ProfilePreviewRequest, ctx: ExecutionContext) -> ProfilePreviewResult:
    remux_config, report, options = _build(request, ctx, preview=True)
    wf = remux_workflow(ctx.config, options)
    errors = wf.validate(remux_config)
    base: dict[str, Any] = dict(
        valid=not errors,
        errors=tuple(errors),
        profile_report=report,
        tracks=tuple(serialize_track_preview(track) for source in remux_config.sources for track in source.tracks),
        config=serialize_remux_config(remux_config),
    )
    if not errors and request.include_command:
        base.update(command=wf.build_command(remux_config), command_text=wf.preview_command(remux_config),
                    execution=dict(wf.execution_preview(remux_config)))
    return ProfilePreviewResult(**base)


@operation("profile.apply", request=ProfileApplyRequest, result=MediaRunResult,
           summary="Appliquer un profil à des sources et remuxer.", long_running=True, mutating=True,
           errors=(ErrorCode.VALIDATION, ErrorCode.OUTPUT_EXISTS, ErrorCode.BUSY, ErrorCode.TOOL_MISSING,
                   ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED))
def profile_apply(request: ProfileApplyRequest, ctx: ExecutionContext) -> MediaRunResult:
    remux_config, _report, options = _build(request, ctx, preview=False)
    message = run_remux(remux_config, config=ctx.config, options=options, logger=EmitterLogger(ctx.events),
                        cancel=ctx.cancel, force=request.force)
    ctx.events.artifact("output", str(remux_config.output))
    return MediaRunResult(status="completed", output=str(remux_config.output), message=message)
