"""Opérations de lot : ``media.batch`` (template) et ``profile.batch`` (profil décisionnel).

Chaque job du lot est un job enfant (événements ``parent`` = lot) ; le lot se termine en
``succeeded``, ``partial`` (au moins un succès et un échec ou une annulation), ``failed`` ou
``cancelled``. Les sorties déjà publiées restent listées dans le résultat.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

from core.jobs.batch import BatchOutcome, BatchSteps, discover_direct_batch_jobs, run_batch
from core.jobs.constants import EXIT_OK, EXIT_VALIDATION, EXIT_WORKFLOW
from core.jobs.errors import exit_code_for
from core.jobs.encode import resolve_from_job
from core.workflows.preparation_cache import PreparationCache
from core.jobs.encode_runtime import checked_encode, encode_workflow, preview_encode, run_encode
from core.jobs.logging import EmitterLogger
from core.jobs.profile import ProfileBatchSteps, build_profile_remux_config, load_decision_profile, run_profile_batch
from core.jobs.remux_config import build_remux_config
from core.jobs.runtime import remux_workflow, run_remux
from core.workflows.preparation_cache import PreparationContext
from core.service.context import ExecutionContext
from core.service.errors import ApiError, ErrorCode, error_code
from core.service.events import JobState
from core.service.models import Model, field, list_of
from core.service.schemas.documents import ENCODE_BLOCK_REF
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
    encode: dict[str, Any] | None = field(default=None, description="Bloc `encode` fusionné dans chaque job (prime).",
                                          schema=ENCODE_BLOCK_REF)
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
    def document_errors(cls, values: dict[str, Any]) -> list[tuple[str, str]]:
        """Valeurs du bloc ``encode`` refusées dès la lecture d'une requête API."""
        from core.service.schemas.encode import encode_block_errors

        return encode_block_errors(values.get("encode"), "encode")

    @classmethod
    def document_path_errors(cls, values: dict[str, Any], base_dir: Path | None) -> list[tuple[str, str]]:
        """Requête de l'API : preset fichier du bloc ``encode`` et profil fichier utilisables ici."""
        from core.json_documents import encode_path_errors, reference_path_errors

        return (encode_path_errors(values.get("encode"), base_dir)
                + reference_path_errors("profile", values.get("profile"), base_dir, name_allowed=True))

    @classmethod
    def resolve_relative_paths(cls, values: dict[str, Any], base_dir: Path) -> dict[str, Any]:
        """Depuis le dossier de la requête : profil fichier, preset fichier du bloc ``encode``."""
        from core.json_documents import resolve_encode_paths, resolve_profile_reference

        if isinstance(values.get("profile"), str):
            values = {**values, "profile": resolve_profile_reference(values["profile"], base_dir)}
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
    encode: dict[str, Any] | None = field(default=None, description="Bloc `encode` appliqué à chaque job.",
                                          schema=ENCODE_BLOCK_REF)
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
    def document_errors(cls, values: dict[str, Any]) -> list[tuple[str, str]]:
        """Valeurs du bloc ``encode`` refusées dès la lecture d'une requête API."""
        from core.service.schemas.encode import encode_block_errors

        return encode_block_errors(values.get("encode"), "encode")

    @classmethod
    def document_path_errors(cls, values: dict[str, Any], base_dir: Path | None) -> list[tuple[str, str]]:
        """Requête de l'API : preset fichier du bloc ``encode`` et profil fichier utilisables ici."""
        from core.json_documents import encode_path_errors, reference_path_errors

        return (encode_path_errors(values.get("encode"), base_dir)
                + reference_path_errors("profile", values.get("profile"), base_dir, name_allowed=True))

    @classmethod
    def resolve_relative_paths(cls, values: dict[str, Any], base_dir: Path) -> dict[str, Any]:
        """Depuis le dossier de la requête : profil fichier, preset fichier du bloc ``encode``."""
        from core.json_documents import resolve_encode_paths, resolve_profile_reference

        if isinstance(values.get("profile"), str):
            values = {**values, "profile": resolve_profile_reference(values["profile"], base_dir)}
        if isinstance(values.get("encode"), dict):
            values = {**values, "encode": resolve_encode_paths(values["encode"], base_dir)}
        return values


@dataclass(frozen=True)
class BatchEntry(Model):
    """Job lancé par le lot."""

    job_index: int = field(description="Rang du job dans le lot (0 = premier).")
    input: str = field()
    output: str = field()
    status: Literal["success", "failed", "cancelled"] = field()
    exit_code: int = field(description="Code de sortie du CLI correspondant (0 : succès).")
    job: str | None = field(description="Identifiant du job enfant (événements) ; null sans job enfant.")
    error: str = field(default="", description="Erreur levée par la préparation ou l'exécution (absente sinon).")


@dataclass(frozen=True)
class BatchResult(Model):
    """Bilan du lot ; un élément par job lancé (état, entrée, sortie, erreur)."""

    status: Literal["succeeded", "partial", "failed", "cancelled"]
    total: int
    successes: int
    failures: int
    jobs: tuple[dict[str, Any], ...] = field(default=(), schema=list_of(BatchEntry))


def _status(outcome: BatchOutcome) -> Literal["succeeded", "partial", "failed", "cancelled"]:
    successes = int(outcome.summary.get("successes", 0))
    failures = int(outcome.summary.get("failures", 0))
    if outcome.cancelled:
        return "partial" if successes else "cancelled"
    if not failures:
        return "succeeded"
    return "partial" if successes else "failed"


def _result(outcome: BatchOutcome, children: _Children) -> BatchResult:
    """Résultat du lot ; chaque entrée reçoit ``job`` (identifiant du job enfant, ``null`` sans enfant) dans le
    résultat de service seulement — le résumé écrit sur disque reste inchangé."""
    summary = outcome.summary
    jobs = tuple({**entry, "job": children.job_ids.get(entry.get("job_index"))} for entry in summary.get("jobs", ()))
    return BatchResult(status=_status(outcome), total=int(summary.get("total", 0)),
                       successes=int(summary.get("successes", 0)), failures=int(summary.get("failures", 0)),
                       jobs=jobs)


class _Children:
    """Journal de chaque job du lot : émetteur enfant, états ``running`` puis final (toujours un seul)."""

    def __init__(self, ctx: ExecutionContext, cache: PreparationCache | None = None) -> None:
        self.ctx = ctx
        self._finished: set[str] = set()
        # Inspections partagées par la préparation, la résolution de l'encodage et sa progression (clé : chemin,
        # taille, date) ; Hybrid fournit celui de sa préparation.
        self._cache = cache if cache is not None else PreparationCache()
        #: Identifiant du job enfant de chaque élément (rang → job), pour relier résultat et événements.
        self.job_ids: dict[int, str] = {}

    def context(self) -> PreparationContext:
        """Contexte de préparation des jobs du lot : cache partagé, annulation."""
        return PreparationContext(cache=self._cache, cancel_event=self.ctx.cancel.event)

    def logger(self, index: int) -> EmitterLogger:
        child = self.ctx.events.child()
        self.job_ids[index] = child.job
        child.state(JobState.RUNNING, index=index)
        return EmitterLogger(child)

    def _guarded(self, logger: Any, step: Callable[[], int]) -> int:
        """Étape d'un job enfant ; toute issue lui donne un état final.

        Annulation (jeton, sonde, Ctrl+C) : job ``cancelled`` et jeton du lot levé, pour que le lot
        s'arrête et soit classé annulé quelle que soit l'origine de l'annulation.
        """
        try:
            return int(step())
        except ApiError as error:
            if not error.logged:
                logger.emit("error", error.render())
            self._stop(logger, cancelled=error.code is ErrorCode.CANCELLED or self.ctx.cancel.cancelled)
            return exit_code_for(error)
        except Exception as exc:
            if error_code(exc) is not ErrorCode.CANCELLED and not self.ctx.cancel.cancelled:
                self._final(logger, JobState.FAILED)
                raise
            if error_code(exc) is not ErrorCode.CANCELLED:  # erreur après la levée du jeton : job annulé
                self._final(logger, JobState.CANCELLED)
                raise
            logger.emit("error", "Opération annulée.")
            self._stop(logger, cancelled=True)
            return EXIT_WORKFLOW
        except BaseException as exc:  # Ctrl+C non intercepté, arrêt d'une tâche
            self._stop(logger, cancelled=error_code(exc) is ErrorCode.CANCELLED or self.ctx.cancel.cancelled)
            raise

    def _failure_state(self) -> JobState:
        """Issue non réussie d'un job : annulé si le jeton du lot est levé (règle du lot et de Hybrid), sinon échec."""
        return JobState.CANCELLED if self.ctx.cancel.cancelled else JobState.FAILED

    def _stop(self, logger: Any, *, cancelled: bool) -> None:
        self._final(logger, JobState.CANCELLED if cancelled else JobState.FAILED)
        if cancelled:
            self.ctx.cancel.cancel()

    def finish(self, logger: Any, status: str) -> None:
        """Fin d'un job que le lot n'a pas pu exécuter (préparation en échec, annulation)."""
        self._final(logger, JobState.CANCELLED if status == "cancelled" else JobState.FAILED)

    def preview(self, config: Any, options: Any, logger: Any, remux_config: Any) -> int:
        def step() -> int:
            wf = remux_workflow(config, options)
            errors = wf.validate(remux_config)
            for error in errors:
                logger.emit("error", error)
            if not errors:
                logger.emit("info", wf.preview_command(remux_config), event="batch_preview")
            self._final(logger, JobState.SUCCEEDED if not errors else self._failure_state())
            return EXIT_OK if not errors else EXIT_VALIDATION

        return self._guarded(logger, step)

    def run(self, config: Any, options: Any, logger: Any, remux_config: Any, *, force: bool) -> int:
        def step() -> int:
            run_remux(remux_config, config=config, options=options, logger=logger, cancel=self.ctx.cancel,
                      force=force, progress=logger.events.progress)
            logger.events.artifact("output", str(remux_config.output))
            self._final(logger, JobState.SUCCEEDED)
            return EXIT_OK

        return self._guarded(logger, step)

    def encode(self, job: dict[str, Any], remux_config: Any, config: Any, options: Any, logger: Any) -> Any:
        resolved = resolve_from_job(job.get("encode"), remux_config, config=config, options=options, logger=logger,
                                    cancel_event=self.ctx.cancel.event, cache=self._cache)
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
                logger.emit("info", preview_encode(checked, wf, self.ctx.cancel.event), event="batch_preview")
            self._final(logger, JobState.SUCCEEDED if not errors else self._failure_state())
            return EXIT_OK if not errors else EXIT_VALIDATION

        return self._guarded(logger, step)

    def run_encode(self, config: Any, options: Any, logger: Any, encode_config: Any, *, force: bool,
                   accept_dovi_nvenc_crop: bool = False) -> int:
        def step() -> int:
            run_encode(encode_config, config=config, options=options, logger=logger, cancel=self.ctx.cancel,
                       force=force, accept_dovi_nvenc_crop=accept_dovi_nvenc_crop,
                       progress=logger.events.progress, cache=self._cache)
            logger.events.artifact("output", str(encode_config.output))
            self._final(logger, JobState.SUCCEEDED)
            return EXIT_OK

        return self._guarded(logger, step)

    def steps(self, build: Any) -> BatchSteps:
        return BatchSteps(build=build, preview=self.preview, run=self.run, encode=self.encode,
                          preview_encode=self.preview_encode, run_encode=self.run_encode, finish=self.finish)

    def _final(self, logger: Any, state: JobState) -> None:
        events = getattr(logger, "events", None)
        if events is None or events.job in self._finished:
            return
        self._finished.add(events.job)
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
    long_running=True, progress=True,
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
        options=request.options.to_common(ctx, export_directory=True),
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
            job, config, options, logger, context=children.context())),
        cancel=ctx.cancel,
        job_logger=children.logger,
    )
    return _result(outcome, children)


@operation(
    "profile.batch",
    request=ProfileBatchRequest,
    result=BatchResult,
    summary="Appliquer un profil décisionnel à un lot de fichiers ; collisions refusées d'avance.",
    long_running=True, progress=True,
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
        options=request.options.to_common(ctx, export_directory=True),
        logger=EmitterLogger(ctx.events),
        auto_tmdb=meta.auto_tmdb,
        tmdb_id=meta.tmdb_id,
        tmdb_apikey=meta.tmdb_apikey,
        output_template=meta.output_template,
        output_all=meta.output_all,
        no_cover=meta.no_cover,
        no_attach=meta.no_attach,
        encode_block=request.encode,
        steps=ProfileBatchSteps(load=lambda path, conf: load_decision_profile(path, conf, base_dir=ctx.base_dir),
                                discover=discover_direct_batch_jobs,
                                build=lambda profile, **kwargs: build_profile_remux_config(
                                    profile, context=children.context(), **kwargs),
                                preview=children.preview, run=children.run,
                                encode=children.encode, preview_encode=children.preview_encode,
                                run_encode=children.run_encode, finish=children.finish),
        cancel=ctx.cancel,
        job_logger=children.logger,
    )
    return _result(outcome, children)
