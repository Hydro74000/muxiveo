"""Opérations média : inspection des sources, validation, aperçu et exécution d'un job de remux."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.inspector import FileInspector
from core.jobs.encode import ResolvedEncode, resolve_from_job
from core.jobs.encode_runtime import encode_workflow, prepare_vulkan, preview_encode, run_encode
from core.jobs.inspection import config_template_from_info
from core.jobs.logging import EmitterLogger
from core.jobs.options import CommonOptions
from core.jobs.remux_config import build_remux_config
from core.jobs.runtime import export_exact_job, remux_workflow, run_remux
from core.jobs.serializers import serialize_file_info, serialize_remux_config
from core.json_documents import resolve_document_paths
from core.service.context import ExecutionContext, Overrides
from core.service.errors import ErrorCode
from core.service.models import Model, field
from core.service.operations import operation
from core.workflows.preparation_cache import PreparationCache, PreparationContext
from core.workflows.remux_models import RemuxConfig

# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InspectRequest(Model):
    """Inspection rapide (ffprobe + mediainfo) d'une ou plusieurs sources."""

    inputs: tuple[Path, ...] = field(min_items=1, description="Fichiers à inspecter.")
    config_template: bool = field(
        default=False, description="Renvoyer un exact-job minimal de la première source au lieu des fiches.")
    output: str | None = field(
        default=None, description="Sortie inscrite dans le modèle de job (avec config_template).")


@dataclass(frozen=True)
class InspectResult(Model):
    """Fiches des sources, ou modèle de job."""

    files: tuple[dict[str, Any], ...] = field(default=(), description="Une fiche par source (pistes, HDR, chapitres…).")
    template: dict[str, Any] | None = field(default=None, description="Exact-job minimal (config_template).")


@operation(
    "media.inspect",
    request=InspectRequest,
    result=InspectResult,
    summary="Inspecter des sources : pistes, HDR, chapitres, pièces jointes ; ou générer un exact-job minimal.",
    errors=(ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED),
)
def media_inspect(request: InspectRequest, ctx: ExecutionContext) -> InspectResult:
    inspector = FileInspector(ffprobe_bin=ctx.tool("ffprobe"), mediainfo_bin=ctx.tool("mediainfo"))
    infos = []
    for path in request.inputs:
        ctx.cancel.raise_if_cancelled()
        infos.append(inspector.inspect(path))
    if request.config_template:
        return InspectResult(template=config_template_from_info(infos[0], output=request.output or ""))
    return InspectResult(files=tuple(serialize_file_info(info) for info in infos))


# ---------------------------------------------------------------------------
# Jobs de remux
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobOptions(Model):
    """Options d'un job (équivalent des options communes du CLI ; outils : surcharges de l'appel)."""

    verbose: bool = field(default=False, description="Publier la sortie brute des outils (ffmpeg…) au journal.")
    nfo: bool | None = field(default=None, description="Écrire le NFO ; défaut : réglage [metadata] generate_nfo.")
    writing_application: str = field(default="", description="Balise WritingApp de la sortie.")
    auto_forced_subs: bool = field(default=False, description="Repérer les sous-titres forcés.")
    forced_threshold: int = field(default=50, description="Seuil (nombre de sous-titres) des pistes forcées.")
    auto_sdh: bool = field(default=False, description="Repérer les sous-titres SDH.")
    remove_closed_captions: bool | None = field(
        default=None, description="Retirer les sous-titres EIA-608/708 embarqués dans la vidéo.")
    sync_mode: Literal["physical", "container"] | None = None
    sync_subtitles: Literal["mirror", "none"] | None = None
    clean_nfo: bool | None = None
    crossfade_ms: int | None = field(default=None, description="Fondu aux raccords de synchro (ms).")
    auto_sync: bool = field(default=False, description="Analyser la synchro de la source 1 face à la source 0.")
    calibration: Path | None = field(default=None, description="Calibration de synchro explicite (JSON).")
    detect_cuts: bool = field(default=False, description="Détecter coupures et ruptures (multi-segments).")
    quick_sync_check: bool | None = field(default=None, description="Synchro rapide ; défaut : [sync] quick_audio_check.")
    reject_coherent_sync: bool = field(
        default=False, description="Refuser une synchro cohérente en partie non vérifiée.")
    drift_threshold_ms: int = field(default=25, description="Seuil de dérive d'une coupure (ms).")
    cadence_auto: bool = field(default=True, description="Conversion de cadence PAL ↔ cinéma automatique.")
    cadence_method: Literal["auto", "atempo", "asetrate"] = "auto"

    @classmethod
    def from_common(cls, options: CommonOptions) -> dict[str, Any]:
        """Charge utile JSON des options d'un ``CommonOptions`` (adaptateur CLI)."""
        names = {item for item in cls.__dataclass_fields__}
        payload = {name: getattr(options, name) for name in names}
        return {key: value for key, value in payload.items() if value is not None or key in _NULLABLE}

    def to_common(self, overrides: Overrides, **extra: Any) -> CommonOptions:
        values = self.to_dict()
        values["calibration"] = str(self.calibration) if self.calibration is not None else None
        return CommonOptions(
            ffmpeg=overrides.ffmpeg,
            ffprobe=overrides.ffprobe,
            mediainfo=overrides.mediainfo,
            threads=overrides.threads,
            work_dir=str(overrides.work_dir) if overrides.work_dir is not None else None,
            **values,
            **extra,
        )


_NULLABLE = {"nfo", "remove_closed_captions", "sync_mode", "sync_subtitles", "clean_nfo", "crossfade_ms",
             "calibration", "quick_sync_check"}


@dataclass(frozen=True)
class MediaJobRequest(Model):
    """Job de remux : document (contrat v1 / exact-job) et options."""

    job: dict[str, Any] = field(description="Document de job (sources, pistes, sortie…), contrat `schema --version 1`.")
    options: JobOptions = field(default_factory=JobOptions, description="Options du job.")

    @classmethod
    def resolve_relative_paths(cls, values: dict[str, Any], base_dir: Path) -> dict[str, Any]:
        """Chemins relatifs du document : depuis le dossier de la requête, comme un fichier ``--config``."""
        if isinstance(values.get("job"), dict):
            values = {**values, "job": resolve_document_paths(values["job"], base_dir)}
        return values


@dataclass(frozen=True)
class MediaRunRequest(MediaJobRequest):
    """Exécution d'un job de remux."""

    force: bool = field(default=False, description="Remplacer une sortie existante.")
    export_workflow: Path | None = field(
        default=None, description="Enregistrer le job exact à cet emplacement au lieu d'exécuter.")
    export_directory: bool = field(default=False, description="export_workflow désigne un dossier.")


@dataclass(frozen=True)
class MediaValidateResult(Model):
    """Job résolu et écarts de validation."""

    valid: bool
    errors: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict, description="Job résolu (sources, pistes, sortie…).")
    encode: dict[str, Any] | None = field(default=None,
                                          description="Encodage résolu (bloc `encode` du job) : pistes, avertissements.")


@dataclass(frozen=True)
class MediaPreviewResult(Model):
    """Job résolu, écarts de validation et commandes prévues."""

    valid: bool
    errors: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict, description="Job résolu (sources, pistes, sortie…).")
    command: Any = field(default=None, description="Commande FFmpeg prévue (arguments).")
    command_text: str | None = field(default=None, description="Commande(s) prévue(s), copiables.")
    execution: dict[str, Any] | None = field(default=None, description="Plan d'exécution (backend, préparations…).")
    encode: dict[str, Any] | None = field(default=None, description="Encodage résolu (bloc `encode` du job).")


@dataclass(frozen=True)
class MediaRunResult(Model):
    """Fin d'un job."""

    status: Literal["completed", "exported"]
    output: str = field(description="Fichier produit (ou job exact enregistré).")
    message: str = ""


def _build(request: MediaJobRequest, ctx: ExecutionContext) -> tuple[RemuxConfig, CommonOptions]:
    remux_config, options, _cache = _build_cached(request, ctx)
    return remux_config, options


def _build_cached(request: MediaJobRequest, ctx: ExecutionContext,
                  **extra: Any) -> tuple[RemuxConfig, CommonOptions, PreparationCache]:
    """Job de remux construit avec un cache d'inspection réutilisé par la résolution de l'encodage."""
    options = request.options.to_common(ctx.overrides, **extra)
    cache = PreparationCache()
    remux_config = build_remux_config(copy.deepcopy(dict(request.job)), ctx.config, options,
                                      EmitterLogger(ctx.events), context=PreparationContext(cache=cache))
    return remux_config, options, cache


def resolve_job_encode(request: MediaJobRequest, remux_config: RemuxConfig, options: CommonOptions,
                       cache: PreparationCache, ctx: ExecutionContext) -> ResolvedEncode | None:
    """Bloc ``encode`` du job résolu (règles du panneau Encodage) ; ``None`` sans bloc."""
    return resolve_from_job(request.job.get("encode"), remux_config, config=ctx.config, options=options,
                            logger=EmitterLogger(ctx.events), cache=cache)


def _encode_plan(resolved: ResolvedEncode | None) -> dict[str, Any] | None:
    if resolved is None:
        return None
    return {"pure_copy": resolved.pure_copy, "video": list(resolved.video), "audio": list(resolved.audio),
            "warnings": list(resolved.warnings)}


_JOB_ERRORS = (ErrorCode.VALIDATION, ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED)


@operation(
    "media.validate",
    request=MediaJobRequest,
    result=MediaValidateResult,
    summary="Résoudre et valider un job de remux sans l'exécuter.",
    errors=_JOB_ERRORS,
)
def media_validate(request: MediaJobRequest, ctx: ExecutionContext) -> MediaValidateResult:
    remux_config, options, cache = _build_cached(request, ctx)
    resolved = resolve_job_encode(request, remux_config, options, cache, ctx)
    errors = remux_workflow(ctx.config, options).validate(remux_config)
    return MediaValidateResult(valid=not errors, errors=tuple(errors), config=serialize_remux_config(remux_config),
                               encode=_encode_plan(resolved))


@operation(
    "media.preview",
    request=MediaJobRequest,
    result=MediaPreviewResult,
    summary="Résoudre un job de remux et afficher les commandes prévues.",
    errors=_JOB_ERRORS,
)
def media_preview(request: MediaJobRequest, ctx: ExecutionContext) -> MediaPreviewResult:
    remux_config, options, cache = _build_cached(request, ctx)
    resolved = resolve_job_encode(request, remux_config, options, cache, ctx)
    plan = _encode_plan(resolved)
    if resolved is not None and resolved.config is not None:
        # Encodage : commandes de l'EncodeWorkflow (préparation « à blanc »), comme « Copier » du panneau.
        encode_wf = encode_workflow(ctx.config, options)
        prepare_vulkan(encode_wf, resolved.config, str(options.ffmpeg or ctx.config.tool_ffmpeg))
        errors = encode_wf.validate(resolved.config)
        config = serialize_remux_config(remux_config)
        if errors:
            return MediaPreviewResult(valid=False, errors=tuple(errors), config=config, encode=plan)
        return MediaPreviewResult(
            valid=True, config=config, command_text=preview_encode(resolved.config, encode_wf), encode=plan,
            execution={"kind": "encode", "selected_backend": "encode", "plan_version": 1,
                       "preparation_commands": [], "native_diagnostics": []},
        )
    wf = remux_workflow(ctx.config, options)
    errors = wf.validate(remux_config)
    config = serialize_remux_config(remux_config)
    if errors:
        return MediaPreviewResult(valid=False, errors=tuple(errors), config=config, encode=plan)
    return MediaPreviewResult(
        valid=True,
        config=config,
        command=wf.build_command(remux_config),
        command_text=wf.preview_command(remux_config),
        execution=dict(wf.execution_preview(remux_config)),
        encode=plan,
    )


@operation(
    "media.run",
    request=MediaRunRequest,
    result=MediaRunResult,
    summary="Exécuter un job de remux (ou enregistrer son job exact).",
    long_running=True,
    mutating=True,
    errors=(*_JOB_ERRORS, ErrorCode.OUTPUT_EXISTS, ErrorCode.BUSY),
)
def media_run(request: MediaRunRequest, ctx: ExecutionContext) -> MediaRunResult:
    options = request.options.to_common(
        ctx.overrides,
        export_workflow=str(request.export_workflow) if request.export_workflow is not None else None,
        export_directory=request.export_directory,
    )
    cache = PreparationCache()
    remux_config = build_remux_config(copy.deepcopy(dict(request.job)), ctx.config, options,
                                      EmitterLogger(ctx.events), context=PreparationContext(cache=cache))
    if options.export_workflow:
        destination = export_exact_job(remux_config, options, encode=request.job.get("encode"))
        ctx.events.artifact("exact-job", str(destination))
        return MediaRunResult(status="exported", output=str(destination))
    resolved = resolve_job_encode(request, remux_config, options, cache, ctx)
    if resolved is not None and resolved.config is not None:
        encode = request.job.get("encode") or {}
        message = run_encode(
            resolved.config, config=ctx.config, options=options, logger=EmitterLogger(ctx.events),
            cancel=ctx.cancel, force=request.force,
            accept_dovi_nvenc_crop=bool(encode.get("accept_dovi_nvenc_crop")),
            on_progress=lambda event: ctx.events.progress(
                pct=event.percent, stage=event.stage_label, fps=event.fps, frame=event.frame,
                eta_s=event.eta_seconds),
        )
        ctx.events.artifact("output", str(resolved.config.output))
        return MediaRunResult(status="completed", output=str(resolved.config.output), message=message)
    message = run_remux(remux_config, config=ctx.config, options=options, logger=EmitterLogger(ctx.events),
                        cancel=ctx.cancel, force=request.force)
    ctx.events.artifact("output", str(remux_config.output))
    return MediaRunResult(status="completed", output=str(remux_config.output), message=message)
