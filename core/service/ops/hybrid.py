"""Opérations ``hybrid.plan`` et ``hybrid.run`` : hybridation sur le moteur du Hybrid Studio.

Sources à rôles (master, donneurs généraux / audio / sous-titres), appariement par épisode, similarité,
ordre ou paires manuelles, recette (fichier exporté par le Studio et options), préparations en parallèle,
méthodes de cadence « auto » tranchées. ``hybrid.run`` : un job enfant par épisode (remux, ou encodage
avec le bloc ``encode``) ; collisions de sorties refusées avant le premier épisode.
"""

from __future__ import annotations

import dataclasses

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.jobs.constants import EXIT_ARGS, EXIT_OK
from core.jobs.errors import JobError
from core.jobs.hybrid_studio import (
    MATCHING,
    ROLES,
    PreparedEpisode,
    StudioSpec,
    check_outputs,
    export_jobs,
    load_recipe,
    plan_episodes,
    prepare_episodes,
    recipe_json,
    recipe_with_overrides,
)
from core.jobs.logging import EmitterLogger
from core.service.context import ExecutionContext
from core.service.errors import ApiError, ErrorCode, error_code
from core.workflows.preparation_cache import PreparationCache
from core.service.models import Model, field
from core.service.operations import operation
from core.service.ops.batch import _Children
from core.service.ops.media import JobOptions
from core.workflows.hybrid_matrix import HybridRecipe

#: Réglages par défaut d'une hybridation (réécriture physique, sous-titres en miroir, fondu 80 ms).
_HYBRID_DEFAULTS = {"sync_mode": "physical", "sync_subtitles": "mirror", "clean_nfo": True, "crossfade_ms": 80}


@dataclass(frozen=True)
class HybridSourceRef(Model):
    role: Literal["master", "donor", "donor-audio", "donor-sub"]
    path: Path = field(description="Fichier ou dossier (épisodes).")


@dataclass(frozen=True)
class HybridPairRef(Model):
    master: Path
    donors: tuple[Path, ...] = field(min_items=1)


@dataclass(frozen=True)
class HybridRequest(Model):
    """Sources à rôles (ou ref/donor et ref_dir/donor_dir), appariement, recette, nommage et options."""

    output_dir: Path = field(description="Dossier des sorties.")
    sources: tuple[HybridSourceRef, ...] = field(default=(), description="Sources et leur rôle.")
    ref: Path | None = field(default=None, description="Alias : source master (fichier).")
    donor: Path | None = field(default=None, description="Alias : source donneuse (fichier).")
    ref_dir: Path | None = field(default=None, description="Alias : source master (dossier d'épisodes).")
    donor_dir: Path | None = field(default=None, description="Alias : source donneuse (dossier d'épisodes).")
    matching: Literal["episode", "fuzzy", "order"] = field(
        default="episode", description="Appariement : numéros d'épisode, similarité des titres, ou ordre.")
    pairs: tuple[HybridPairRef, ...] = field(default=(), description="Paires manuelles (exclut sources).")
    recipe: Path | None = field(default=None, description="Recette exportée par le Hybrid Studio (JSON).")
    keep_master_audio: tuple[str, ...] | None = field(default=None, description="Langues audio du master gardées.")
    donor_audio_langs: tuple[str, ...] | None = None
    donor_sub_langs: tuple[str, ...] | None = None
    exclude_langs: tuple[str, ...] | None = None
    sync: Literal["auto", "manual", "none"] | None = field(default=None, description="Synchro des donneurs.")
    shift_ms: int | None = field(default=None, description="Décalage manuel (sync manual).")
    profile: str = field(default="", description="Profil décisionnel (chemin ou nom).")
    auto_tmdb: int | None = field(default=None, description="TMDB : absent = sans ; 0 = recherche ; ID sinon.")
    tmdb_apikey: str = ""
    no_cover: bool = False
    tag: str = field(default="MVO", description="Groupe de release ({group} du gabarit).")
    output_template: str = ""
    output_all: bool = False
    options: JobOptions = field(default_factory=JobOptions,
                                description="Synchro et pistes ; défauts hybrides : physical, mirror, fondu 80 ms.")
    encode: dict[str, Any] | None = field(default=None, description="Bloc `encode` appliqué à chaque épisode.")
    export_workflow: Path | None = field(default=None, description="Enregistrer les exact-jobs au lieu d'exécuter.")
    dry_run: bool = field(default=False, description="Plan d'exécution de chaque épisode, sans écriture.")
    force: bool = False
    continue_on_error: bool = False
    report_json: Path | None = field(default=None, description="Rapport JSON des épisodes.")

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
class HybridPlanRequest(HybridRequest):
    """Plan d'hybridation (appariement, pistes, calibrations) sans exécution."""

    export_jobs: Path | None = field(default=None, description="Dossier où écrire les exact-jobs finalisés.")
    save_recipe: Path | None = field(default=None, description="Recette effective écrite dans ce fichier.")


@dataclass(frozen=True)
class HybridResult(Model):
    """Bilan : un élément par épisode (master, donneurs, sortie, calibration, état)."""

    status: Literal["succeeded", "partial", "failed", "cancelled"]
    episodes: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class HybridPlanResult(Model):
    status: Literal["succeeded", "partial", "failed"] = "succeeded"
    episodes: tuple[dict[str, Any], ...] = ()
    recipe: dict[str, Any] = field(default_factory=dict)
    exported: tuple[str, ...] = ()


def _spec(request: HybridRequest, ctx: ExecutionContext) -> StudioSpec:
    payload = request.options.to_dict()
    explicit = {key for key in (*_HYBRID_DEFAULTS, "cadence_auto", "cadence_method")
                if payload.get(key) is not None}  # options réellement fournies
    for key, value in _HYBRID_DEFAULTS.items():
        if payload.get(key) is None:
            payload[key] = value
    common = JobOptions.from_dict(payload, allow_relative=True).to_common(ctx)
    sources = [(ROLES[item.role], Path(item.path)) for item in request.sources]
    for path, role in ((request.ref, "master"), (request.ref_dir, "master"), (request.donor, "donor"),
                       (request.donor_dir, "donor")):
        if path is not None:
            sources.append((ROLES[role], Path(path)))
    if (request.ref is not None) + (request.ref_dir is not None) > 1 or \
            (request.donor is not None and request.donor_dir is not None):
        raise JobError("Choisir --ref/--donor ou --ref-dir/--donor-dir.", EXIT_ARGS)
    recipe = load_recipe(request.recipe) if request.recipe is not None else HybridRecipe()

    def chosen(name: str) -> Any:
        """Réglage des options : prioritaire s'il a été fourni (même égal au défaut) ou sans recette ; sinon celui
        de la recette."""
        value = getattr(common, name)
        return value if request.recipe is None or name in explicit else None

    recipe = recipe_with_overrides(
        recipe, keep_master_audio_langs=request.keep_master_audio, donor_audio_langs=request.donor_audio_langs,
        donor_sub_langs=request.donor_sub_langs, exclude_languages=request.exclude_langs,
        sync_strategy=request.sync, manual_shift_ms=request.shift_ms, sync_mode=chosen("sync_mode"),
        sync_subtitles=chosen("sync_subtitles"), crossfade_ms=chosen("crossfade_ms"),
        cadence_auto_apply=common.cadence_auto if request.recipe is None or "cadence_auto" in explicit else None,
        cadence_audio_method=(common.cadence_method if request.recipe is None or "cadence_method" in explicit
                              else None))
    if request.shift_ms is not None and recipe.sync_strategy != "manual":
        raise JobError("--shift-ms s'utilise avec --sync manual.", EXIT_ARGS)
    # Fondu effectif (recette, sauf option fournie) : le même pour la préparation, le résultat et --save-recipe ;
    # contrôlé après la recette (une valeur importée hors plage est refusée ici, avant tout traitement).
    common = dataclasses.replace(common, crossfade_ms=recipe.crossfade_ms)
    if common.forced_threshold <= 0 or common.drift_threshold_ms <= 0 or not 0 <= (common.crossfade_ms or 0) <= 1000:
        raise JobError("Seuils hybrides invalides.", EXIT_ARGS)
    return StudioSpec(
        sources=tuple(sources), output_dir=Path(request.output_dir).expanduser().resolve(),
        matching=MATCHING[request.matching],
        pairs=tuple((Path(pair.master), tuple(Path(donor) for donor in pair.donors)) for pair in request.pairs),
        recipe=recipe, common=common, profile=request.profile, auto_tmdb=request.auto_tmdb,
        tmdb_apikey=request.tmdb_apikey, no_cover=request.no_cover, tag=request.tag,
        output_template=request.output_template, output_all=request.output_all)


def _prepare(request: HybridRequest, ctx: ExecutionContext,
             cache: PreparationCache | None = None) -> tuple[StudioSpec, list[PreparedEpisode]]:
    spec = _spec(request, ctx)
    logger = EmitterLogger(ctx.events)
    episodes = plan_episodes(spec, logger)
    logger.emit("info", f"[HYBRID] {len(episodes)} épisode(s) apparié(s) ; préparation des plans…")
    prepared = prepare_episodes(spec, episodes, ctx.config, lambda _episode: logger, ctx.cancel, cache=cache)
    for item in prepared:
        if item.error is not None:
            logger.emit("error", f"[HYBRID] {item.episode.display_name} : {item.error}")
    return spec, prepared


@operation(
    "hybrid.plan",
    request=HybridPlanRequest,
    result=HybridPlanResult,
    summary="Préparer une hybridation sans l'exécuter : appariement, pistes retenues, calibrations, sorties ; "
            "exact-jobs finalisés et recette effective en option.",
    long_running=True,
    mutating=True,
    uses_gpu=True,
    errors=(ErrorCode.INVALID_REQUEST, ErrorCode.VALIDATION, ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED,
            ErrorCode.CANCELLED),
)
def hybrid_plan(request: HybridPlanRequest, ctx: ExecutionContext) -> HybridPlanResult:
    spec, prepared = _prepare(request, ctx)
    ctx.cancel.raise_if_cancelled()  # préparation annulée : aucun export
    episodes = [item.describe() for item in prepared]
    failures = sum(1 for item in prepared if item.error is not None)
    if request.encode is not None:
        # Bloc `encode` résolu et validé par épisode (règles du panneau Encodage), comme à l'exécution.
        from core.jobs.encode import resolve_from_job
        from core.jobs.encode_runtime import checked_encode, encode_workflow
        from core.service.ops.media import encode_plan_payload

        logger = EmitterLogger(ctx.events)
        accept = bool(request.encode.get("accept_dovi_nvenc_crop"))
        for item, entry in zip(prepared, episodes):
            ctx.cancel.raise_if_cancelled()
            if item.remux_config is None:
                continue
            try:
                resolved = resolve_from_job(request.encode, item.remux_config, config=ctx.config,
                                            options=spec.common, logger=logger, report_unknown=False,
                                            cancel_event=ctx.cancel.event)
                checked, errors = (checked_encode(resolved.config, encode_workflow(ctx.config, spec.common),
                                                  config=ctx.config, options=spec.common, accept_dovi_nvenc_crop=accept,
                                                  cancel_event=ctx.cancel.event)
                                   if resolved is not None and resolved.config is not None else (None, []))
            except ApiError as error:
                if error.code is ErrorCode.CANCELLED:
                    raise
                errors, checked, resolved = [error.render()], None, None
            if errors:
                entry["error"] = "Encodage refusé : " + " ; ".join(errors)
                failures += 1
                logger.emit("error", f"[HYBRID] {item.episode.display_name} : {entry['error']}")
                continue
            if resolved is not None:
                entry["encode"] = encode_plan_payload(resolved, checked)
    ctx.cancel.raise_if_cancelled()  # annulation pendant la validation de l'encodage : aucun export
    exported: list[Path] = []
    if request.export_jobs is not None:
        check_outputs(prepared, spec.output_template)
        valid = [item for item, entry in zip(prepared, episodes) if "error" not in entry]  # encodage refusé : non exporté
        exported = export_jobs(valid, Path(request.export_jobs), encode=request.encode, base_dir=ctx.base_dir)
        for path in exported:
            ctx.events.artifact("exact-job", str(path))
    if request.save_recipe is not None:
        from core.atomic_io import atomic_write_text

        atomic_write_text(Path(request.save_recipe), recipe_json(spec.recipe))
        ctx.events.artifact("recipe", str(request.save_recipe))
    status: Literal["succeeded", "partial", "failed"] = (
        "succeeded" if not failures else "failed" if failures == len(prepared) else "partial")
    return HybridPlanResult(status=status, episodes=tuple(episodes),
                            recipe=spec.recipe.to_dict(), exported=tuple(str(path) for path in exported))


def _status(entries: list[dict[str, Any]], cancelled: bool) -> Literal["succeeded", "partial", "failed", "cancelled"]:
    states = [entry.get("status") for entry in entries]
    succeeded = sum(1 for state in states if state in ("ok", "planned", "exported"))
    if not any(state in ("failed", "cancelled") for state in states) and not cancelled:
        return "succeeded"
    if succeeded:
        return "partial"
    return "cancelled" if cancelled else "failed"


@operation(
    "hybrid.run",
    request=HybridRequest,
    result=HybridResult,
    summary="Hybrider une paire ou une saison (moteur du Hybrid Studio) : vidéo du master, audio / sous-titres "
            "des donneurs calés ; encodage par épisode avec le bloc `encode`.",
    long_running=True, progress=True,
    mutating=True,
    uses_gpu=True,
    errors=(ErrorCode.INVALID_REQUEST, ErrorCode.VALIDATION, ErrorCode.OUTPUT_EXISTS, ErrorCode.TOOL_MISSING,
            ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED),
)
def hybrid_run(request: HybridRequest, ctx: ExecutionContext) -> HybridResult:
    cache = PreparationCache()  # inspections de la préparation reprises par l'encodage des épisodes
    spec, prepared = _prepare(request, ctx, cache)
    logger = EmitterLogger(ctx.events)
    failed_early = next((index for index, item in enumerate(prepared) if item.error is not None), None)
    if failed_early is not None and not request.continue_on_error:
        prepared = prepared[:failed_early + 1]
    check_outputs([item for item in prepared if item.error is None], spec.output_template)
    children = _Children(ctx, cache)
    entries: list[dict[str, Any]] = []
    cancelled = False
    for index, item in enumerate(prepared):
        if ctx.cancel.cancelled:
            cancelled = True
            break
        episode = item.episode
        entry: dict[str, Any] = {"episode": episode.display_name, "reference": str(episode.master_file),
                                 "donor": str(episode.donor_files[0][1]) if episode.donor_files else None,
                                 "donors": [str(path) for _source, path in episode.donor_files]}
        child: EmitterLogger | None = None
        try:
            if item.error is not None:
                raise item.error
            remux_config = item.remux_config
            assert remux_config is not None
            entry["output"] = str(remux_config.output)
            entry["calibration"] = item.calibration.to_dict() if item.calibration is not None else None
            if request.export_workflow is not None:
                destination = Path(request.export_workflow)
                if len(prepared) > 1:
                    destination.mkdir(parents=True, exist_ok=True)
                    destination /= remux_config.output.stem + ".exact-job.json"
                from core.profiles.selectors import remux_config_to_exact_job
                from core.workflows.workflow_store import save_workflow

                job = remux_config_to_exact_job(remux_config)
                if request.encode is not None:
                    from core.json_documents import portable_encode_block

                    job = {**job, "encode": portable_encode_block(request.encode, ctx.base_dir)}
                save_workflow(destination, job)
                entry["status"] = "exported"
            else:
                child = children.logger(index)
                entry["job"] = child.events.job  # job enfant (événements de l'épisode), résultat de service seulement
                encode_config = (children.encode({"encode": request.encode}, remux_config, ctx.config, spec.common,
                                                 child) if request.encode is not None else None)
                accept = bool((request.encode or {}).get("accept_dovi_nvenc_crop"))
                if request.dry_run:
                    if encode_config is not None:  # mêmes contrôles que le lancement, acceptation comprise
                        code = children.preview_encode(ctx.config, spec.common, child, encode_config,
                                                       accept_dovi_nvenc_crop=accept)
                    else:
                        code = children.preview(ctx.config, spec.common, child, remux_config)
                    entry["status"] = "planned" if code == EXIT_OK else (
                        "cancelled" if ctx.cancel.cancelled else "failed")  # annulation absorbée par l'enfant
                else:
                    remux_config.output.parent.mkdir(parents=True, exist_ok=True)
                    remux_config.allow_missing_output_dir = False
                    if encode_config is not None:
                        code = children.run_encode(ctx.config, spec.common, child, encode_config, force=request.force,
                                                   accept_dovi_nvenc_crop=accept)
                    else:
                        code = children.run(ctx.config, spec.common, child, remux_config, force=request.force)
                    entry["status"] = "ok" if code == EXIT_OK else (
                        "cancelled" if ctx.cancel.cancelled else "failed")
            entries.append(entry)
            if entry["status"] == "cancelled":
                cancelled = True
                break
            if entry["status"] == "failed" and not request.continue_on_error:
                break
        except Exception as exc:
            # Annulation (jeton, sonde, Ctrl+C traduit) pendant la résolution de l'encodage ou ailleurs : épisode et
            # enfant annulés, lot arrêté ; sinon échec. L'enfant reçoit toujours un état final.
            status = "cancelled" if error_code(exc) is ErrorCode.CANCELLED or ctx.cancel.cancelled else "failed"
            if child is not None:
                children.finish(child, status)
            entries.append({**entry, "status": status, "error": str(exc)})
            if item.error is None:
                logger.emit("error", str(exc))
            if status == "cancelled":
                ctx.cancel.cancel()
                cancelled = True
                break
            if not request.continue_on_error:
                break
        except BaseException as exc:  # Ctrl+C non intercepté : état final de l'enfant (comme _Children), rapport, propagation
            status = "cancelled" if error_code(exc) is ErrorCode.CANCELLED or ctx.cancel.cancelled else "failed"
            if child is not None:
                children.finish(child, status)
            entries.append({**entry, "status": status, "error": str(exc) or type(exc).__name__})
            try:
                _write_report(request, entries)
            except Exception as report_error:  # jamais à la place de l'interruption d'origine
                logger.emit("error", f"Rapport des épisodes non écrit : {report_error}")
            raise
    _write_report(request, entries)
    return HybridResult(status=_status(entries, cancelled),
                        episodes=tuple({**entry, "job": entry.get("job")} for entry in entries))


def _write_report(request: HybridRequest, entries: list[dict[str, Any]]) -> None:
    """Rapport JSON des épisodes (``report_json``) : aussi après une interruption propagée."""
    if request.report_json is None:
        return
    from core.atomic_io import atomic_write_text

    episodes = [{key: value for key, value in entry.items() if key != "job"} for entry in entries]  # rapport inchangé
    atomic_write_text(Path(request.report_json), json.dumps({"version": 1, "episodes": episodes},
                                                             ensure_ascii=False, indent=2, allow_nan=False) + "\n")
