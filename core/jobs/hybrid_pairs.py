"""Hybridation par paires (référence + donneur) : préparation, refus des collisions, exécution.

Moteur historique de la commande ``hybrid`` (le Hybrid Studio utilise ``core.workflows.hybrid_matrix``).
Les étapes (construction du job, profil, analyse de synchro, exécution) sont injectées
(:class:`HybridSteps`) : le CLI y branche ses fonctions, la couche de service les siennes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from core.config import AppConfig
from core.jobs.batch import PlannedBatchJob, assert_unique_batch_outputs
from core.jobs.constants import EXIT_ARGS, EXIT_OK, EXIT_PARTIAL
from core.jobs.errors import JobError
from core.jobs.logging import JobLogger as Logger
from core.jobs.options import CommonOptions
from core.profiles.selectors import remux_config_to_exact_job
from core.service.context import CancelToken
from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.hybrid import HybridPair, episode_key, pair_directories
from core.workflows.remux_models import RemuxConfig
from core.workflows.sync_calibration import SyncCalibration
from core.workflows.workflow_store import atomic_write_text, save_workflow


@dataclass(frozen=True)
class HybridOptions:
    """Réglages d'une hybridation par paires (équivalent des options de la commande ``hybrid``)."""

    output_dir: Path
    common: CommonOptions = field(default_factory=CommonOptions)
    profile: str = ""
    auto_tmdb: int | None = None  # None : sans TMDB ; 0 : recherche ; > 0 : ID TMDB
    tmdb_apikey: str = ""
    no_cover: bool = False
    tag: str = "MVO"
    output_template: str = ""
    output_all: bool = False
    export_workflow: str = ""
    dry_run: bool = False
    force: bool = False
    continue_on_error: bool = False
    report_json: str = ""


@dataclass(frozen=True)
class HybridSteps:
    """Étapes d'une paire : construction (job, profil), analyse de synchro audio, exécution."""

    build: Callable[[dict[str, Any], AppConfig, CommonOptions, Logger], RemuxConfig]
    load_profile: Callable[[str, AppConfig], dict[str, Any]]
    build_profile: Callable[..., tuple[RemuxConfig, Any]]
    audio_scanner: Callable[[], Any]
    run: Callable[..., int]  # (config, options, logger, remux_config, *, force) -> code de sortie
    preview: Callable[[AppConfig, CommonOptions, RemuxConfig], dict[str, Any]]


@dataclass(frozen=True)
class HybridOutcome:
    exit_code: int
    episodes: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False


# Valeurs par défaut d'une hybridation (option absente) : réécriture physique, sous-titres en miroir, fondu 80 ms.
def _sync_mode(common: CommonOptions) -> str:
    return common.sync_mode or "physical"


def _sync_subtitles(common: CommonOptions) -> str:
    return common.sync_subtitles or "mirror"


def _crossfade_ms(common: CommonOptions) -> int:
    return 80 if common.crossfade_ms is None else common.crossfade_ms


def _clean_nfo(common: CommonOptions) -> bool:
    return True if common.clean_nfo is None else common.clean_nfo


def pairs_for(ref: str | None, donor: str | None, ref_dir: str | None, donor_dir: str | None) -> list[HybridPair]:
    """Paires à hybrider : une paire de fichiers, ou deux dossiers appariés par épisode."""
    if ref and donor and not (ref_dir or donor_dir):
        ref_path, donor_path = Path(ref).expanduser().resolve(), Path(donor).expanduser().resolve()
        if not ref_path.is_file() or not donor_path.is_file() or ref_path == donor_path:
            raise JobError("Sources hybrides invalides.", EXIT_ARGS)
        try:
            season, episode = episode_key(ref_path)
        except ValueError:
            season, episode = 0, 0
        return [HybridPair(ref_path, donor_path, season, episode)]
    if ref_dir and donor_dir and not (ref or donor):
        return pair_directories(Path(ref_dir).expanduser(), Path(donor_dir).expanduser())
    raise JobError("Choisir --ref/--donor ou --ref-dir/--donor-dir.", EXIT_ARGS)


def prepare_pair(pair: HybridPair, options: HybridOptions, config: AppConfig, logger: Logger,
                 steps: HybridSteps) -> tuple[RemuxConfig, SyncCalibration]:
    """Job de remux d'une paire et calibration de synchro du donneur."""
    common = options.common
    outdir = Path(options.output_dir).expanduser().resolve()
    job: dict[str, Any] = {"sources": [str(pair.reference), str(pair.donor)],
                           "output": str(outdir / (pair.reference.stem + ".Hybrid.mkv")),
                           "mux_backend": "native", "sync_mode": common.sync_mode,
                           "sync_subtitles": common.sync_subtitles, "crossfade_ms": common.crossfade_ms,
                           "clean_nfo": common.clean_nfo, "chapters": {"source_index": 0},
                           "_allow_missing_output_dir": True}
    if options.auto_tmdb is not None:
        job["tmdb"] = {"enabled": True, "kind": "tv", "season": str(pair.season),
                       "episode": str(pair.episode), "cover": not options.no_cover}
        if options.auto_tmdb:
            job["tmdb"]["id"] = options.auto_tmdb
        if options.tmdb_apikey:
            job["tmdb"]["api_key"] = options.tmdb_apikey
    profile: dict = {}
    if options.profile:
        profile = steps.load_profile(options.profile, config)
        result, _ = steps.build_profile(profile,
                                        cli_inputs=[str(pair.reference), str(pair.donor)], cli_output=job["output"],
                                        config=config, options=common, logger=logger, preview=True,
                                        metadata_job=job)
        result = replace(result, mux_backend="native", sync_mode=_sync_mode(common),
                         sync_subtitles=_sync_subtitles(common), crossfade_ms=_crossfade_ms(common),
                         clean_nfo=_clean_nfo(common))
    else:
        result = steps.build(job, config, common, logger)
    # La référence fournit toujours la vidéo et les chapitres. Le profil reste
    # maître de la sélection des pistes audio/sous-titres.
    for track in result.sources[0].tracks:
        if track.track_type == "video":
            track.enabled = True
    ref_audio = next((t for t in result.sources[0].tracks if t.track_type == "audio"), None)
    donor_tracks = result.sources[1].tracks
    for track in donor_tracks:
        if track.track_type == "video":
            track.enabled = False
        if track.track_type == "audio" and ref_audio and track.language == ref_audio.language:
            track.enabled = False
        if track.track_type == "subtitle" and ref_audio and track.language == ref_audio.language:
            track.enabled = False
    all_tracks = [track for source in result.sources for track in source.tracks]
    ref_video = next((t for t in result.sources[0].tracks if t.track_type == "video"), None)
    if not options.profile:
        result.track_order = [(s.file_index, t.mkv_tid, t.entry_id) for s in result.sources for t in s.tracks
                              if t.enabled]
    else:
        allowed = {t.entry_id for t in all_tracks if t.enabled}
        filtered_order = [item for item in result.track_order if len(item) < 3 or item[2] in allowed]
        final_order: list[Any] = []
        if ref_video:
            final_order.append((0, ref_video.mkv_tid, ref_video.entry_id))
        for item in filtered_order:
            if ref_video and (item[0] == 0 and item[1] == ref_video.mkv_tid):
                continue
            final_order.append(item)
        result.track_order = final_order
    ref_audio = next((t for t in result.sources[0].tracks if t.track_type == "audio"), None)
    target_audio = next((t for t in donor_tracks if t.track_type == "audio" and t.enabled), None)
    target_sub = next((t for t in donor_tracks if t.track_type == "subtitle" and t.enabled), None)
    ref_sub = next((t for t in result.sources[0].tracks if t.track_type == "subtitle" and t.enabled), None)
    if ref_sub is None:
        ref_sub = next((t for t in result.sources[0].tracks if t.track_type == "subtitle"), None)

    sync_audio = None
    if ref_audio is not None and ref_audio.language:
        sync_audio = next((t for t in result.sources[1].tracks
                           if t.track_type == "audio" and t.language == ref_audio.language), None)
    if sync_audio is None:
        sync_audio = target_audio

    cadence_mismatch = None
    cadence_auto: Any = common.cadence_auto
    if cadence_auto is None:
        cadence_auto = getattr(config, "sync_cadence_auto_apply", True)
    cadence_method = common.cadence_method or getattr(config, "sync_cadence_audio_method", "auto") or "auto"

    if cadence_auto and len(result.sources) >= 2:
        from core.workflows.cadence import detect_cadence_from_metadata
        from core.workflows.hybrid_matrix import _extract_source_video_fps

        ref_fps = _extract_source_video_fps(result.sources[0])
        donor_fps = _extract_source_video_fps(result.sources[1])
        if ref_fps and donor_fps:
            cadence_mismatch = detect_cadence_from_metadata(ref_fps, donor_fps)
            if cadence_mismatch:
                logger.emit(
                    "info",
                    f"Différence de cadence vidéo détectée ({ref_fps} vs {donor_fps}) : "
                    f"{cadence_mismatch.description} — méthode audio : {cadence_method}",
                )

    calibration: SyncCalibration
    if common.calibration:
        calibration = SyncCalibration.from_dict(json.loads(Path(common.calibration).read_text(encoding="utf-8-sig")))
    elif sync_audio is not None and ref_audio is not None:
        calibration = steps.audio_scanner().scan(
            AudioSyncTrack(pair.reference, ref_audio.mkv_tid),
            AudioSyncTrack(pair.donor, sync_audio.mkv_tid),
            detect_cuts=common.detect_cuts,
            drift_threshold_ms=common.drift_threshold_ms,
            cadence_mismatch=cadence_mismatch,
            cadence_audio_method=cadence_method,
            log=lambda text: logger.emit("info", text),
            warn=lambda text, **fields: logger.emit("warning", text, **fields),
        )
    elif target_sub is not None:
        from core.workflows.subtitle_sync_scan import SubtitleSyncScanner

        sub_scanner = SubtitleSyncScanner(common.ffmpeg or config.tool_ffmpeg, common.ffprobe or config.tool_ffprobe)
        if ref_sub is not None:
            calibration = sub_scanner.scan(
                pair.reference, ref_sub.mkv_tid, pair.donor, target_sub.mkv_tid, is_ref_sub=True,
                detect_cuts=common.detect_cuts, log=lambda text: logger.emit("info", text),
            )
        elif ref_audio is not None:
            calibration = sub_scanner.scan(
                pair.reference, ref_audio.mkv_tid, pair.donor, target_sub.mkv_tid, is_ref_sub=False,
                detect_cuts=False, log=lambda text: logger.emit("info", text),
            )
        else:
            raise JobError("Une piste de référence audio ou sous-titre est requise.", EXIT_ARGS)
    else:
        raise JobError("Une piste donneuse audio ou sous-titre est requise.", EXIT_ARGS)

    # Cadence ou hauteur (calibration chaînée importée) : seule la réécriture physique les conserve.
    from core.jobs.remux_config import apply_donor_calibration

    result.sync_mode, result.sync_calibrations = apply_donor_calibration(
        calibration, sync_mode=result.sync_mode, sources=result.sources, sync_subtitles=_sync_subtitles(common),
        sync_calibrations=result.sync_calibrations,
    )
    if common.auto_forced_subs:
        from core.workflows.subtitle_heuristics import forced_first

        result.track_order = forced_first(result.track_order, result.sources)
    # Template explicite, sinon celui du profil, sinon nom par défaut (comme Hybrid Studio).
    from core.profiles.decision import profile_output_template, profile_variables

    template = options.output_template or profile_output_template(profile)
    if template:
        from core.jobs.output_template import render_output_filename
        from core.jobs.remux_config import resolve_tmdb_metadata
        from core.workflows.hybrid_matrix import MatrixEpisode, matrix_output_context

        details = resolve_tmdb_metadata(job, config, pair.reference, logger)[3] if job.get("tmdb") else None
        # Même contexte que Hybrid Studio : {season_episode}, {season} et {episode} sans TMDB.
        context = matrix_output_context(
            MatrixEpisode(season=pair.season, episode=pair.episode, master_file=pair.reference), pair.reference,
            details=details, tracks=all_tracks, track_order=cast(Any, list(result.track_order)),
            variables=profile_variables(profile), group=options.tag, output_all=options.output_all,
        )
        try:
            result.output = outdir / render_output_filename(template, context)
        except ValueError as exc:
            raise JobError(str(exc), EXIT_ARGS) from exc
    return result, calibration


def run_hybrid(pairs: list[HybridPair], options: HybridOptions, config: AppConfig, logger: Logger,
               steps: HybridSteps, *, prepare: Callable[[HybridPair], tuple[RemuxConfig, SyncCalibration]] | None = None,
               cancel: CancelToken | None = None) -> HybridOutcome:
    """Prépare toutes les paires (collisions refusées d'avance), puis exporte, planifie ou exécute dans l'ordre."""
    common = options.common
    if common.forced_threshold <= 0 or common.drift_threshold_ms <= 0 or not 0 <= (common.crossfade_ms or 0) <= 1000:
        raise JobError("Seuils hybrides invalides.", EXIT_ARGS)
    prepare_one = prepare or (lambda pair: prepare_pair(pair, options, config, logger, steps))
    report: list[dict[str, Any]] = []
    prepared: list[tuple[Any, Any, Any, Exception | None]] = []
    for pair in pairs:
        if cancel is not None and cancel.cancelled:
            break
        try:
            result, calibration = prepare_one(pair)
        except Exception as exc:
            prepared.append((pair, None, None, exc))
            if not options.continue_on_error:
                break
            continue
        prepared.append((pair, result, calibration, None))
    assert_unique_batch_outputs([
        PlannedBatchJob(index, str(pair.reference), str(result.output), remux_config=result,
                        template=options.output_template)
        for index, (pair, result, _calibration, _error) in enumerate(prepared)
        if result is not None
    ])
    cancelled = False
    for pair, result, calibration, error in prepared:
        if cancel is not None and cancel.cancelled:
            cancelled = True
            break
        try:
            if error is not None:
                raise error
            entry = {"reference": str(pair.reference), "donor": str(pair.donor),
                     "output": str(result.output), "calibration": calibration.to_dict()}
            if options.export_workflow:
                destination = Path(options.export_workflow)
                if len(pairs) > 1:
                    destination.mkdir(parents=True, exist_ok=True)
                    destination /= f"S{pair.season:02d}E{pair.episode:02d}.exact-job.json"
                save_workflow(destination, remux_config_to_exact_job(result))
                entry["status"] = "exported"
            elif options.dry_run:
                entry["plan"] = steps.preview(config, common, result)
                entry["status"] = "planned"
            else:
                result.output.parent.mkdir(parents=True, exist_ok=True)
                result.allow_missing_output_dir = False
                code = steps.run(config, common, logger, result, force=options.force)
                entry["status"] = "ok" if code == EXIT_OK else "failed"
                if code != EXIT_OK and cancel is not None and cancel.cancelled:
                    entry["status"] = "cancelled"
            report.append(entry)
            if entry["status"] == "cancelled":
                cancelled = True
                break
        except Exception as exc:
            report.append({"reference": str(pair.reference), "status": "failed", "error": str(exc)})
            logger.emit("error", str(exc))
            if not options.continue_on_error:
                break
    if options.report_json:
        atomic_write_text(Path(options.report_json),
                          json.dumps({"version": 1, "episodes": report}, ensure_ascii=False, indent=2,
                                     allow_nan=False) + "\n")
    failed = any(entry["status"] in ("failed", "cancelled") for entry in report)
    return HybridOutcome(EXIT_PARTIAL if failed else EXIT_OK, report, cancelled)
