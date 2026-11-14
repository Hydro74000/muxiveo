"""Opérations de lot : ``media.batch`` (template) et ``profile.batch`` (profil décisionnel).

Chaque job du lot est un job enfant (événements ``parent`` = lot) ; le lot se termine en
``succeeded``, ``partial`` (au moins un succès et un échec ou une annulation), ``failed`` ou
``cancelled``. Les sorties déjà publiées restent listées dans le résultat.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.jobs.batch import BatchOutcome, BatchSteps, discover_direct_batch_jobs, run_batch
from core.jobs.constants import EXIT_OK, EXIT_VALIDATION, EXIT_WORKFLOW
from core.jobs.errors import exit_code_for
from core.jobs.encode import resolve_from_job
from core.jobs.encode_runtime import checked_encode, encode_workflow, preview_encode, run_encode
from core.jobs.logging import EmitterLogger
from core.jobs.profile import ProfileBatchSteps, build_profile_remux_config, load_decision_profile, run_profile_batch
from core.jobs.remux_config import build_remux_config
from core.jobs.runtime import remux_workflow, run_remux
from core.workflows.preparation_cache import PreparationContext
from core.service.context import ExecutionContext
from core.service.errors import ApiError, ErrorCode, error_code
from core.service.events import JobState
from core.service.models import Model, field
from core.service.operations import operation
from core.service.ops.media import JobOptions


@dataclass(frozen=True)
class BatchSelection(Model):
    """Sources d'un lot : fichiers et dossiers (avec filtres)."""

    inputs: tuple[Path, ...] = field(default=(), description="Fichiers sources, un job par fichier.")
    input_dirs: tuple[Path, ...] = field(default=(), description="Dossiers à parcourir, un job par vidéo.")
    recursive: bool = field(default=False, description="Parcourir les sous-dossiers.")
    include: tuple[str, ...] = field(default=(), description="Motifs de fichiers à inclure (glob).")
    exclude: tuple[str, ...] = field(default=(), description="Motifs de fichiers à exclure (glob).")


@dataclass(frozen=True)
class BatchMetadata(Model):
    """Métadonnées et nommage communs aux jobs du lot."""

    auto_tmdb: bool = field(default=False, description="Fiche TMDB automatique (premier résultat).")
    tmdb_id: int | None = field(default=None, description="ID TMDB explicite.")
    tmdb_apikey: str = field(default="", description="Clé API TMDB v3 (surcharge la configuration).")
    no_cover: bool = field(default=False, description="Pas de couverture TMDB.")
    no_attach: bool = field(default=False, description="Ni pièces jointes ni couverture.")
    output_template: str = field(default="", description="Gabarit du nom de sortie.")
    output_all: bool = field(default=False, description="Mots-clés de pistes du gabarit en mode « all ».")


@dataclass(frozen=True)
class BatchRequest(Model):
    """Lot de jobs construit d'un template (et d'un fichier batch, ou de sources)."""

    template: Path = field(description="Template ou exact-job JSON.")
    batch: Path | None = field(default=None, description="Fichier batch JSON (`jobs` ou `inputs`).")
    sources: BatchSelection = field(default_factory=BatchSelection, description="Sources (sans fichier batch).")
    output_dir: Path | None = field(default=None, description="Dossier des sorties générées.")
    metadata: BatchMetadata = field(default_factory=BatchMetadata)
    mux_backend: Literal["auto", "native", "ffmpeg"] | None = None
    options: JobOptions = field(default_factory=JobOptions)
    encode: dict[str, Any] | None = field(default=None, description="Bloc `encode` fusionné dans chaque job (prime).")
    dry_run: bool = field(default=False, description="Aperçu de chaque job, sans écriture.")
    force: bool = field(default=False, description="Remplacer les sorties existantes.")
    continue_on_error: bool = field(default=False, description="Poursuivre après un job en échec.")
    summary: Path | None = field(default=None, description="Rapport JSON final.")

    @classmethod
    def document_issues(cls, values: dict[str, Any]) -> list[tuple[str, str]]:
        """Clés inconnues du bloc ``encode`` (refusées en requête, signalées en CLI)."""
        from core.service.schemas.encode import encode_block_issues

        return encode_block_issues(values.get("encode"), "encode")

    @classmethod
    def resolve_relative_paths(cls, values: dict[str, Any], base_dir: Path) -> dict[str, Any]:
        """Preset du bloc ``encode`` désigné par un fichier : depuis le dossier de la requête."""
        from core.json_documents import resolve_encode_paths

        if isinstance(values.get("encode"), dict):
            values = {**values, "encode": resolve_encode_paths(values["encode"], base_dir)}
        return values


@dataclass(frozen=True)
class ProfileBatchRequest(Model):
    """Lot de jobs : un profil décisionnel appliqué à chaque source."""

    profile: str = field(description="Profil : chemin JSON ou nom d'un profil enregistré.")
    output_dir: Path = field(description="Dossier des sorties.")
    sources: BatchSelection = field(default_factory=BatchSelection)
    metadata: BatchMetadata = field(default_factory=BatchMetadata)
    options: JobOptions = field(default_factory=JobOptions)
    encode: dict[str, Any] | None = field(default=None, description="Bloc `encode` appliqué à chaque job.")
    dry_run: bool = False
    force: bool = False
    continue_on_error: bool = False
    summary: Path | None = None

    @classmethod
    def document_issues(cls, values: dict[str, Any]) -> list[tuple[str, str]]:
        """Clés inconnues du bloc ``encode`` (refusées en requête, signalées en CLI)."""
        from core.service.schemas.encode import encode_block_issues

        return encode_block_issues(values.get("encode"), "encode")

    @classmethod
    def resolve_relative_paths(cls, values: dict[str, Any], base_dir: Path) -> dict[str, Any]:
        """Preset du bloc ``encode`` désigné par un fichier : depuis le dossier de la requête."""
        from core.json_documents import resolve_encode_paths

        if isinstance(values.get("encode"), dict):
            values = {**values, "encode": resolve_encode_paths(values["encode"], base_dir)}
        return values


@dataclass(frozen=True)
class BatchResult(Model):
    """Bilan du lot ; un élément par job lancé (état, entrée, sortie, erreur)."""

    status: Literal["succeeded", "partial", "failed", "cancelled"]
    total: int
    successes: int
    failures: int
    jobs: tuple[dict[str, Any], ...] = ()


def _status(outcome: BatchOutcome) -> Literal["succeeded", "partial", "failed", "cancelled"]:
    successes = int(outcome.summary.get("successes", 0))
    failures = int(outcome.summary.get("failures", 0))
    if outcome.cancelled:
        return "partial" if successes else "cancelled"
    if not failures:
        return "succeeded"
    return "partial" if successes else "failed"


def _result(outcome: BatchOutcome) -> BatchResult:
    summary = outcome.summary
    return BatchResult(status=_status(outcome), total=int(summary.get("total", 0)),
                       successes=int(summary.get("successes", 0)), failures=int(summary.get("failures", 0)),
                       jobs=tuple(summary.get("jobs", ())))


class _Children:
    """Journal de chaque job du lot : émetteur enfant, états ``running`` puis final."""

    def __init__(self, ctx: ExecutionContext) -> None:
        self.ctx = ctx

    def logger(self, index: int) -> EmitterLogger:
        child = self.ctx.events.child()
        child.state(JobState.RUNNING, index=index)
        return EmitterLogger(child)

    def _guarded(self, logger: Any, step: Any) -> int:
        """Étape d'un job enfant ; toute issue lui donne un état final (annulation : ``cancelled``, code 6)."""
        try:
            return int(step())
        except ApiError as error:
            if not error.logged:
                logger.emit("error", error.render())
            self._final(logger, JobState.CANCELLED if error.code is ErrorCode.CANCELLED else JobState.FAILED)
            return exit_code_for(error)
        except BaseException as exc:
            if error_code(exc) is ErrorCode.CANCELLED and isinstance(exc, Exception):
                logger.emit("error", "Opération annulée.")
                self._final(logger, JobState.CANCELLED)
                return EXIT_WORKFLOW
            self._final(logger, JobState.FAILED)
            raise

    def preview(self, config: Any, options: Any, logger: Any, remux_config: Any) -> int:
        def step() -> int:
            wf = remux_workflow(config, options)
            errors = wf.validate(remux_config)
            for error in errors:
                logger.emit("error", error)
            if not errors:
                logger.emit("info", wf.preview_command(remux_config), event="batch_preview")
            self._final(logger, JobState.SUCCEEDED if not errors else JobState.FAILED)
            return EXIT_OK if not errors else EXIT_VALIDATION

        return self._guarded(logger, step)

    def run(self, config: Any, options: Any, logger: Any, remux_config: Any, *, force: bool) -> int:
        try:
            run_remux(remux_config, config=config, options=options, logger=logger, cancel=self.ctx.cancel,
                      force=force)
        except ApiError as error:
            if not error.logged:
                logger.emit("error", error.render())
            self._final(logger, JobState.CANCELLED if error.code is ErrorCode.CANCELLED else JobState.FAILED)
            return exit_code_for(error)
        except BaseException as exc:
            self._final(logger, JobState.CANCELLED if error_code(exc) is ErrorCode.CANCELLED else JobState.FAILED)
            raise
        logger.events.artifact("output", str(remux_config.output))
        self._final(logger, JobState.SUCCEEDED)
        return EXIT_OK

    def encode(self, job: dict[str, Any], remux_config: Any, config: Any, options: Any, logger: Any) -> Any:
        resolved = resolve_from_job(job.get("encode"), remux_config, config=config, options=options, logger=logger,
                                    cancel_event=self.ctx.cancel.event)
        return None if resolved is None else resolved.config

    def preview_encode(self, config: Any, options: Any, logger: Any, encode_config: Any, *,
                       accept_dovi_nvenc_crop: bool = False) -> int:
        """Aperçu avec les refus du lancement (géométrie DV + NVENC comprise)."""
        def step() -> int:
            wf = encode_workflow(config, options)
            checked, errors = checked_encode(encode_config, wf, config=config, options=options,
                                             accept_dovi_nvenc_crop=accept_dovi_nvenc_crop,
                                             cancel_event=self.ctx.cancel.event)
            for error in errors:
                logger.emit("error", error)
            if not errors:
                logger.emit("info", preview_encode(checked, wf), event="batch_preview")
            self._final(logger, JobState.SUCCEEDED if not errors else JobState.FAILED)
            return EXIT_OK if not errors else EXIT_VALIDATION

        return self._guarded(logger, step)

    def run_encode(self, config: Any, options: Any, logger: Any, encode_config: Any, *, force: bool,
                   accept_dovi_nvenc_crop: bool = False) -> int:
        try:
            run_encode(encode_config, config=config, options=options, logger=logger, cancel=self.ctx.cancel,
                       force=force, accept_dovi_nvenc_crop=accept_dovi_nvenc_crop,
                       on_progress=lambda event: logger.events.progress(pct=event.percent, stage=event.stage_label,
                                                                        fps=event.fps, eta_s=event.eta_seconds))
        except ApiError as error:
            if not error.logged:
                logger.emit("error", error.render())
            self._final(logger, JobState.CANCELLED if error.code is ErrorCode.CANCELLED else JobState.FAILED)
            return exit_code_for(error)
        except BaseException as exc:
            self._final(logger, JobState.CANCELLED if error_code(exc) is ErrorCode.CANCELLED else JobState.FAILED)
            raise
        logger.events.artifact("output", str(encode_config.output))
        self._final(logger, JobState.SUCCEEDED)
        return EXIT_OK

    def steps(self, build: Any) -> BatchSteps:
        return BatchSteps(build=build, preview=self.preview, run=self.run, encode=self.encode,
                          preview_encode=self.preview_encode, run_encode=self.run_encode)

    @staticmethod
    def _final(logger: Any, state: JobState) -> None:
        events = getattr(logger, "events", None)
        if events is not None:
            events.state(state)


def _strs(paths: tuple[Path, ...]) -> list[str] | None:
    return [str(path) for path in paths] or None


_BATCH_ERRORS = (ErrorCode.VALIDATION, ErrorCode.OUTPUT_EXISTS, ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED,
                 ErrorCode.CANCELLED)


@operation(
    "media.batch",
    request=BatchRequest,
    result=BatchResult,
    summary="Exécuter un lot de jobs de remux (template + fichier batch ou sources) ; collisions refusées d'avance.",
    long_running=True,
    mutating=True,
    uses_gpu=True,
    errors=_BATCH_ERRORS,
)
def media_batch(request: BatchRequest, ctx: ExecutionContext) -> BatchResult:
    children = _Children(ctx)
    meta, selection = request.metadata, request.sources
    outcome = run_batch(
        template_path=str(request.template),
        batch_path=str(request.batch) if request.batch is not None else None,
        cli_inputs=_strs(selection.inputs),
        input_dirs=_strs(selection.input_dirs),
        recursive=selection.recursive,
        include_patterns=list(selection.include) or None,
        exclude_patterns=list(selection.exclude) or None,
        output_dir=str(request.output_dir) if request.output_dir is not None else None,
        dry_run=request.dry_run,
        force=request.force,
        continue_on_error=request.continue_on_error,
        summary_path=str(request.summary) if request.summary is not None else None,
        config=ctx.config,
        options=request.options.to_common(ctx.overrides, export_directory=True),
        logger=EmitterLogger(ctx.events),
        auto_tmdb=meta.auto_tmdb,
        tmdb_id=meta.tmdb_id,
        tmdb_apikey=meta.tmdb_apikey,
        output_template=meta.output_template,
        output_all=meta.output_all,
        no_cover=meta.no_cover,
        no_attach=meta.no_attach,
        mux_backend=request.mux_backend,
        encode_overrides=request.encode,
        steps=children.steps(lambda job, config, options, logger: build_remux_config(
            job, config, options, logger, context=PreparationContext(cancel_event=ctx.cancel.event))),
        cancel=ctx.cancel,
        job_logger=children.logger,
    )
    return _result(outcome)


@operation(
    "profile.batch",
    request=ProfileBatchRequest,
    result=BatchResult,
    summary="Appliquer un profil décisionnel à un lot de fichiers ; collisions refusées d'avance.",
    long_running=True,
    mutating=True,
    uses_gpu=True,
    errors=_BATCH_ERRORS,
)
def profile_batch(request: ProfileBatchRequest, ctx: ExecutionContext) -> BatchResult:
    children = _Children(ctx)
    meta, selection = request.metadata, request.sources
    outcome = run_profile_batch(
        request.profile,
        cli_inputs=_strs(selection.inputs),
        input_dirs=_strs(selection.input_dirs),
        recursive=selection.recursive,
        include_patterns=list(selection.include) or None,
        exclude_patterns=list(selection.exclude) or None,
        output_dir=str(request.output_dir),
        dry_run=request.dry_run,
        force=request.force,
        continue_on_error=request.continue_on_error,
        summary_path=str(request.summary) if request.summary is not None else None,
        config=ctx.config,
        options=request.options.to_common(ctx.overrides, export_directory=True),
        logger=EmitterLogger(ctx.events),
        auto_tmdb=meta.auto_tmdb,
        tmdb_id=meta.tmdb_id,
        tmdb_apikey=meta.tmdb_apikey,
        output_template=meta.output_template,
        output_all=meta.output_all,
        no_cover=meta.no_cover,
        no_attach=meta.no_attach,
        encode_block=request.encode,
        steps=ProfileBatchSteps(load=load_decision_profile, discover=discover_direct_batch_jobs,
                                build=lambda profile, **kwargs: build_profile_remux_config(
                                    profile, context=PreparationContext(cancel_event=ctx.cancel.event), **kwargs),
                                preview=children.preview, run=children.run,
                                encode=children.encode, preview_encode=children.preview_encode,
                                run_encode=children.run_encode),
        cancel=ctx.cancel,
        job_logger=children.logger,
    )
    return _result(outcome)
