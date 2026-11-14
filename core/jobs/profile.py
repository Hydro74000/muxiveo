"""Profils décisionnels v1 : chargement et construction du job de remux (CLI, service, Hybrid)."""

from __future__ import annotations

import json
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.profiles.decision import (
    DECISION_PROFILE_KIND,
    DecisionProfileError,
    DecisionProfileManager,
    apply_decision_profile,
    profile_output_template,
    profile_variables,
    validate_decision_profile,
)
from core.service.context import CancelToken
from core.workflows.preparation_cache import PreparationContext
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry, normalize_mux_backend

from core.jobs.batch import (
    BatchDiscovery,
    BatchSteps,
    execute_job,
    plan_job_encode,
    BatchOutcome,
    PlannedBatchJob,
    PreviewStep,
    RunStep,
    assert_unique_batch_outputs,
    job_primary_input,
    log_batch_failures,
    write_batch_summary,
)
from core.jobs.constants import EXIT_ARGS, EXIT_OK, EXIT_PARTIAL, EXIT_VALIDATION, EXIT_WORKFLOW
from core.service.errors import ErrorCode, error_code
from core.jobs.errors import JobError
from core.jobs.inspection import inspect_sources, source_path_items
from core.jobs.json_io import deep_merge, json_default, load_json
from core.jobs.loader import apply_metadata_overrides
from core.jobs.logging import JobLogger as Logger
from core.jobs.options import CommonOptions
from core.jobs.remux_config import (
    apply_donor_calibration,
    merge_tmdb_tag_overrides,
    resolve_final_output,
    resolve_metadata_file_title,
    resolve_tmdb_metadata,
)
from core.jobs.tmdb import auto_tmdb_metadata_enabled


def _profile_path_candidates(raw_profile: str | Path, config: AppConfig | None = None) -> list[Path]:
    raw_text = str(raw_profile or "").strip()
    raw = Path(raw_text).expanduser()
    candidates: list[Path] = []

    def add(path: Path) -> None:
        if path not in candidates:
            candidates.append(path)

    add(raw)
    if not raw.suffix:
        add(raw.with_suffix(".json"))

    if config is not None:
        default_dir = Path(config.profiles_dir) / "decision"
        # L'identité exacte prime sur un fichier historique homonyme, notamment
        # « Film » / « film » sur un volume insensible à la casse.
        safe_name = raw.stem if raw.suffix.lower() == ".json" else raw_text
        if safe_name:
            add(DecisionProfileManager(default_dir).path_for_name(safe_name))
        default_base = Path(raw.name) if raw.is_absolute() else raw
        add(default_dir / default_base)
        if not default_base.suffix:
            add(default_dir / default_base.with_suffix(".json"))

    return candidates


def resolve_decision_profile_path(raw_profile: str | Path, config: AppConfig | None = None) -> Path:
    for candidate in _profile_path_candidates(raw_profile, config):
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(path) for path in _profile_path_candidates(raw_profile, config))
    raise JobError(f"Profil introuvable : {raw_profile}. Chemins testés : {searched}", EXIT_ARGS)


def load_decision_profile(path: str | Path, config: AppConfig | None = None) -> dict[str, Any]:
    profile_path = resolve_decision_profile_path(path, config)
    profile = load_json(profile_path)
    try:
        validate_decision_profile(profile)
    except DecisionProfileError as exc:
        raise JobError(str(exc), EXIT_ARGS) from exc
    if profile.get("kind") != DECISION_PROFILE_KIND:
        raise JobError("Le fichier n'est pas un decision-profile v1.", EXIT_ARGS)
    return profile


def snapshot_decision_profile(path: str | Path, config: AppConfig | None = None) -> tuple[tuple[str, str], dict[str, Any]]:
    """Fige le document validé et son identité, même après réécriture sans changement de date."""
    resolved = resolve_decision_profile_path(path, config).resolve()
    profile = load_decision_profile(resolved, config)
    digest = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return (str(resolved), digest), profile


def _refresh_sources_tracks(sources: list[SourceInput], tracks: list[TrackEntry]) -> None:
    by_file_id: dict[str, list[TrackEntry]] = {}
    for track in tracks:
        by_file_id.setdefault(track.file_id, []).append(track)
    for source in sources:
        # L'ordre de sortie est porté par track_order. Les sélecteurs exact-job
        # comptent les pistes dans leur ordre d'origine dans le fichier source.
        source.tracks = sorted(
            by_file_id.get(f"src{source.file_index}", source.tracks),
            key=lambda track: (track.mkv_tid, track.is_new),
        )


def _track_order(tracks: list[TrackEntry]) -> list[tuple[int, int, str]]:
    order: list[tuple[int, int, str]] = []
    for track in tracks:
        if not track.enabled:
            continue
        source_index = 0
        if str(track.file_id).startswith("src"):
            try:
                source_index = int(str(track.file_id)[3:])
            except ValueError:
                source_index = 0
        order.append((source_index, int(track.mkv_tid), track.entry_id))
    return order


def _output_variables(profile: dict[str, Any], metadata_job: dict[str, Any] | None) -> dict[str, Any]:
    metadata_variables = (metadata_job or {}).get("variables")
    variables = profile_variables(profile)
    if isinstance(metadata_variables, dict) and variables:
        return deep_merge(metadata_variables, variables)
    if variables:
        return variables
    if isinstance(metadata_variables, dict):
        return dict(metadata_variables)
    return {}


def build_profile_remux_config(
    profile: dict[str, Any],
    *,
    cli_inputs: list[str],
    cli_output: str | None,
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    preview: bool = False,
    metadata_job: dict[str, Any] | None = None,
    select_tracks: Callable[[list[SourceInput]], None] | None = None,
    context: PreparationContext | None = None,
) -> tuple[RemuxConfig, dict[str, Any]]:
    """Applique les heuristiques puis le profil aux candidats bornés par select_tracks."""
    if not cli_inputs:
        raise JobError("Au moins une entrée est requise avec `--profile` (`-i/--input`).", EXIT_ARGS)
    context = context or PreparationContext()
    job = {"sources": [{"path": value} for value in cli_inputs]}
    with context.measure("inspection"):
        sources, _infos, tracks = inspect_sources(job, config, options, logger, cache=context.cache,
                                                  cancel_event=context.cancel_event)
    if select_tracks is not None:
        # Le témoin borne les candidats du profil, sans perdre les pistes
        # d'origine nécessaires aux sélecteurs exact-job et à la synchro.
        select_tracks(sources)
        tracks = [track for source in sources for track in source.tracks if track.enabled]
    candidate_ids = {track.entry_id for track in tracks}
    excluded = [track for source in sources for track in source.tracks if track.entry_id not in candidate_ids]
    from core.workflows.subtitle_heuristics import classify_subtitles, forced_first
    # Enrichir les flags avant les règles : elles peuvent les utiliser pour
    # nommer/sélectionner les pistes, puis surcharger les décisions automatiques.
    with context.measure("sous-titres"):
        classify_subtitles(sources, _infos, ffmpeg=options.ffmpeg or config.tool_ffmpeg,
            forced=options.auto_forced_subs, sdh=options.auto_sdh, threshold=options.forced_threshold,
            all_audio_languages=True, cache=context.cache, cancel_event=context.cancel_event)
    if options.auto_forced_subs:
        order = forced_first([
            (source.file_index, track.mkv_tid, track.entry_id)
            for source in sources for track in source.tracks if track.entry_id in candidate_ids
        ], sources)
        positions = {item[2]: index for index, item in enumerate(order)}
        tracks.sort(key=lambda track: positions[track.entry_id])
    source_index_by_file_id = {f"src{source.file_index}": source.file_index for source in sources}
    result = apply_decision_profile(
        profile,
        tracks,
        source_index_by_file_id=source_index_by_file_id,
    )
    if not result.report.get("valid", True):
        raise JobError(json.dumps(result.report, ensure_ascii=False, default=json_default), EXIT_VALIDATION)
    _refresh_sources_tracks(sources, [*excluded, *result.tracks])
    if options.remove_closed_captions is not None:
        for track in result.tracks:
            if track.is_video and track.has_closed_captions:
                track.remove_closed_captions = options.remove_closed_captions
    metadata = dict(metadata_job or {})
    # Template du profil seulement sans --output-template ; -o fichier reste prioritaire,
    # -o dossier sert de base au nom rendu.
    template = str(metadata.get("output_template") or "").strip() or profile_output_template(profile)
    if template:
        metadata["output_template"] = template
        if cli_output and (str(cli_output).endswith(("/", "\\")) or Path(cli_output).expanduser().is_dir()):
            metadata["output"], cli_output = cli_output, None
    tmdb_title = ""
    tmdb_tags = None
    tmdb_cover = None
    tmdb_details = None
    if metadata.get("tmdb"):
        tmdb_title, tmdb_tags, tmdb_cover, tmdb_details = resolve_tmdb_metadata(
            metadata,
            config,
            sources[0].path,
            logger,
            source_title=_infos[0].title if _infos else "",
        )
    final_track_order = _track_order(result.tracks)
    output = resolve_final_output(
        cli_output=cli_output,
        job=metadata,
        sources=sources,
        details=tmdb_details,
        tracks=result.tracks,
        final_track_order=final_track_order,
        preview=preview,
        variables=_output_variables(profile, metadata),
    )
    tmdb_wins = auto_tmdb_metadata_enabled(metadata)
    tag_overrides = merge_tmdb_tag_overrides(
        tmdb_tags,
        metadata.get("tag_overrides", None),
        tmdb_wins=tmdb_wins,
    )
    # Chemins résolus en absolu : les runners peuvent exécuter les commandes
    # depuis un répertoire de travail différent du cwd de la CLI.
    sources = [replace(source, path=source.path.expanduser().resolve()) for source in sources]
    sync_mode = options.sync_mode or metadata.get("sync_mode", "container")
    sync_subtitles = options.sync_subtitles or metadata.get("sync_subtitles", "mirror")
    clean_nfo = options.clean_nfo if options.clean_nfo is not None else metadata.get("clean_nfo", True)
    crossfade_ms = options.crossfade_ms if options.crossfade_ms is not None else metadata.get("crossfade_ms", 80)

    # Comme sans profil (`apply_donor_calibration`) : une calibration de la ligne de commande ou
    # mesurée remplace celles du document ; celle du document en mode conteneur garde les autres sources.
    from core.workflows.sync_calibration import SyncCalibration
    sync_calibrations: dict[str, Any] = dict(metadata.get("sync_calibrations") or {})
    calib = None
    if options.calibration:
        sync_calibrations = {}
        calib = SyncCalibration.from_dict(json.loads(Path(options.calibration).read_text(encoding="utf-8-sig")))
    elif (options.auto_sync or profile.get("auto_sync") or profile.get("sync")) and len(sources) >= 2:
        from core.jobs.dynamic_sync import perform_dynamic_sync
        sync_calibrations = {}
        calib = perform_dynamic_sync(sources, result.tracks, config, options, logger, **context.cancel_kwargs())
    elif sync_mode == "container" and sync_calibrations.get("1"):
        calib = SyncCalibration.from_dict(sync_calibrations["1"])
    if calib is not None:
        sync_mode, sync_calibrations = apply_donor_calibration(
            calib, sync_mode=sync_mode, sources=sources, sync_subtitles=sync_subtitles,
            sync_calibrations=sync_calibrations,
        )

    remux_config = RemuxConfig(
        sources=sources,
        output=output.expanduser().resolve(),
        track_order=final_track_order,
        keep_chapters=True,
        file_title=resolve_metadata_file_title(metadata, tmdb_title, tmdb_wins=tmdb_wins),
        tag_overrides=tag_overrides if isinstance(tag_overrides, dict) else None,
        tmdb_cover=tmdb_cover,
        work_dir=Path(str(options.work_dir or config.work_dir)).expanduser().resolve(),
        allow_missing_output_dir=preview,
        mux_backend=normalize_mux_backend(str(metadata.get("mux_backend", config.matroska_mux_backend))),
        sync_mode=sync_mode,
        sync_subtitles=sync_subtitles,
        clean_nfo=clean_nfo,
        crossfade_ms=crossfade_ms,
        sync_calibrations=sync_calibrations,
    )
    return remux_config, result.report


def profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    """Résumé d'un profil valide (``profile validate``)."""
    return {
        "valid": True,
        "kind": profile.get("kind"),
        "version": profile.get("version"),
        "name": profile.get("name", ""),
        "rules": len(profile.get("rules", []) if isinstance(profile.get("rules"), list) else []),
    }


@dataclass(frozen=True)
class ProfileBatchSteps:
    """Étapes d'un lot profilé : chargement du profil, découverte, construction, aperçu, exécution
    (et encodage : voir ``core.jobs.batch.BatchSteps``)."""

    load: Callable[[str, AppConfig], dict[str, Any]]
    discover: Callable[..., BatchDiscovery]
    build: Callable[..., tuple[RemuxConfig, Any]]
    preview: PreviewStep
    run: RunStep
    encode: Callable[..., Any] | None = None
    preview_encode: Callable[..., int] | None = None
    run_encode: Callable[..., int] | None = None

    def as_batch_steps(self) -> BatchSteps:
        def unused_build(*_args: Any, **_kwargs: Any) -> RemuxConfig:
            raise RuntimeError("construction propre au profil (ProfileBatchSteps.build)")

        return BatchSteps(build=unused_build, preview=self.preview, run=self.run, encode=self.encode,
                          preview_encode=self.preview_encode, run_encode=self.run_encode)


def run_profile_batch(
    profile_path: str,
    *,
    cli_inputs: list[str] | None,
    input_dirs: list[str] | None,
    recursive: bool,
    include_patterns: list[str] | None,
    exclude_patterns: list[str] | None,
    output_dir: str | None,
    dry_run: bool,
    force: bool,
    continue_on_error: bool,
    summary_path: str | None,
    config: AppConfig,
    options: CommonOptions,
    logger: Logger,
    auto_tmdb: bool = False,
    tmdb: bool = False,
    tmdb_id: int | None = None,
    tmdb_apikey: str = "",
    output_template: str = "",
    output_all: bool = False,
    no_cover: bool = False,
    no_attach: bool = False,
    encode_block: dict[str, Any] | None = None,
    steps: ProfileBatchSteps,
    cancel: CancelToken | None = None,
    job_logger: Callable[[int], Logger] | None = None,
) -> BatchOutcome:
    """Applique un profil à un lot de fichiers ; étapes injectées, annulation = arrêt du lot."""
    batch_steps = steps.as_batch_steps()
    if not output_dir:
        raise JobError("`profile batch` requiert `--output-dir`.", EXIT_ARGS)
    profile = steps.load(profile_path, config)
    output_template = output_template or profile_output_template(profile)
    metadata_template: dict[str, Any] = {}
    apply_metadata_overrides(
        metadata_template,
        auto_tmdb=auto_tmdb,
        tmdb=tmdb,
        tmdb_id=tmdb_id,
        tmdb_apikey=tmdb_apikey,
        no_cover=no_cover,
        no_attach=no_attach,
    )
    if output_template:
        metadata_template["output_template"] = output_template
        if output_all:
            metadata_template["output_all"] = True
        metadata_template["_batch_output_dir"] = str(Path(output_dir).expanduser())
    elif output_all:
        metadata_template["output_all"] = True
    discovery = steps.discover(
        cli_inputs=cli_inputs,
        input_dirs=input_dirs,
        output_dir=output_dir,
        recursive=recursive,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        output_template=output_template,
        output_all=output_all,
    )
    if not discovery.jobs:
        raise JobError("Aucun fichier vidéo compatible trouvé pour le batch profil.", EXIT_ARGS)
    logger.emit(
        "info",
        f"Découverte batch profil : {discovery.selected}/{discovery.scanned} fichier(s) sélectionné(s).",
        event="profile_batch_discovery",
        scanned=discovery.scanned,
        selected=discovery.selected,
        roots=discovery.roots,
        recursive=recursive,
    )
    # Phase 1 — préparation de tous les jobs sans écriture ; collisions de
    # sorties refusées avant le premier traitement, même avec --force.
    planned: list[PlannedBatchJob] = []
    for job_index, job in enumerate(discovery.jobs):
        if cancel is not None and cancel.cancelled:
            break  # Ctrl+C pendant la préparation : aucun job lancé (phase 2)
        entry = PlannedBatchJob(job_index, job_primary_input(job), str(job.get("output") or ""))
        entry.template = output_template
        entry.generated_output = True
        planned.append(entry)
        try:
            inputs = [item["path"] for item in source_path_items(job)]
            remux_config, _report = steps.build(
                profile,
                cli_inputs=inputs,
                cli_output=entry.output_label,
                config=config,
                options=options,
                logger=logger,
                preview=dry_run,
                metadata_job=metadata_template,
            )
            entry.remux_config = remux_config
            entry.output_label = str(remux_config.output)
            if encode_block:
                plan_job_encode(batch_steps, entry, {"encode": encode_block}, remux_config, config, options, logger)
        except Exception as exc:
            entry.error = exc
            if not continue_on_error:
                break
    assert_unique_batch_outputs(planned)

    # Phase 2 — exécution dans l'ordre.
    failures = 0
    summary_jobs: list[dict[str, Any]] = []
    # Annulation pendant la découverte ou la préparation : aucun job lancé, lot annulé.
    cancelled = cancel is not None and cancel.cancelled
    for entry in planned:
        if cancel is not None and cancel.cancelled:
            cancelled = True
            break
        job_index = entry.index
        input_label = entry.input_label
        output_label = entry.output_label
        job_log = job_logger(job_index) if job_logger is not None else logger
        try:
            if entry.error is not None:
                raise entry.error
            remux_config = entry.remux_config
            if not dry_run and output_label:
                Path(output_label).expanduser().parent.mkdir(parents=True, exist_ok=True)
            rc = execute_job(batch_steps, entry, config, options, job_log, dry_run=dry_run, force=force)
            status = "success" if rc == EXIT_OK else "failed"
            if rc != EXIT_OK and cancel is not None and cancel.cancelled:
                status = "cancelled"
            failures += 0 if rc == EXIT_OK else 1
            summary_jobs.append({"job_index": job_index, "input": input_label, "output": output_label, "status": status, "exit_code": rc})
            if status == "cancelled":
                cancelled = True
                break
        except Exception as exc:
            failures += 1
            # Annulation levée par une étape (sonde, inspection) : job annulé, lot arrêté.
            job_cancelled = (cancel is not None and cancel.cancelled) or error_code(exc) is ErrorCode.CANCELLED
            summary_jobs.append(
                {
                    "job_index": job_index,
                    "input": input_label,
                    "output": output_label,
                    "status": "cancelled" if job_cancelled else "failed",
                    "exit_code": getattr(exc, "exit_code", EXIT_WORKFLOW),
                    "error": str(exc),
                }
            )
            logger.emit("error", f"Job profil batch échoué : {exc}", event="profile_batch_job", job_index=job_index,
                        status="cancelled" if job_cancelled else "failed")
            if job_cancelled:
                cancelled = True
                break
            if not continue_on_error:
                break
    total = len(summary_jobs)
    exit_code = EXIT_OK if failures == 0 and not cancelled else EXIT_PARTIAL
    summary: dict[str, Any] = {
        "total": total,
        "successes": total - failures,
        "failures": failures,
        "exit_code": exit_code,
        "jobs": summary_jobs,
    }
    if cancelled:
        summary["cancelled"] = True
    write_batch_summary(summary_path, summary)
    log_batch_failures(logger, summary_jobs, event="profile_batch_failure")
    logger.emit("info", f"Batch profil terminé : {total - failures}/{total} succès.", event="profile_batch_summary", total=total, failures=failures)
    return BatchOutcome(exit_code, summary, cancelled)
