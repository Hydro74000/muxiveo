"""Commandes hybrides et outils autonomes de calibration."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner
from core.workflows.hybrid import HybridPair, episode_key, pair_directories
from core.workflows.sync_calibration import SyncCalibration
from core.workflows.workflow_store import atomic_write_text, save_workflow
from core.workflows.subtitle_sync import shift_file
from core.profiles.selectors import remux_config_to_exact_job
from cli.constants import EXIT_ARGS, EXIT_EXISTS, EXIT_OK, EXIT_PARTIAL
from cli.errors import CliError
from cli.options import common_options
from cli.remux_config import build_remux_config
from cli.runtime import run_remux_config


def _write_json(path, value):
    atomic_write_text(Path(path), json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def scanner(args, config):
    return AudioSyncScanner(args.ffmpeg or config.tool_ffmpeg, args.ffprobe or config.tool_ffprobe, cancel_event=getattr(args, "cancel_event", None))


def cmd_sync_scan(args, config, logger):
    result = scanner(args, config).scan(
        AudioSyncTrack(Path(args.ref), args.stream_ref), AudioSyncTrack(Path(args.target), args.stream_target),
        detect_cuts=args.detect_cuts, drift_threshold_ms=args.drift_threshold_ms,
        log=lambda message: logger.emit("info", message))
    if args.output_json:
        _write_json(args.output_json, result.to_dict())
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return EXIT_OK


def cmd_shift_subs(args, config, logger):
    source, output = Path(args.input), Path(args.output)
    if output.exists() and not args.force:
        raise CliError(str(output), EXIT_EXISTS)
    calibration = (SyncCalibration.from_dict(json.loads(Path(args.calibration).read_text(encoding="utf-8-sig")))
                   if args.calibration else SyncCalibration.linear(args.offset_ms))
    shift_file(source, output, calibration)
    return EXIT_OK


def pairs_from_args(args):
    if args.ref and args.donor and not (args.ref_dir or args.donor_dir):
        ref, donor = Path(args.ref).expanduser().resolve(), Path(args.donor).expanduser().resolve()
        if not ref.is_file() or not donor.is_file() or ref == donor:
            raise CliError("Sources hybrides invalides.", EXIT_ARGS)
        try:
            season, episode = episode_key(ref)
        except ValueError:
            season, episode = 0, 0
        return [HybridPair(ref, donor, season, episode)]
    if args.ref_dir and args.donor_dir and not (args.ref or args.donor):
        return pair_directories(Path(args.ref_dir).expanduser(), Path(args.donor_dir).expanduser())
    raise CliError("Choisir --ref/--donor ou --ref-dir/--donor-dir.", EXIT_ARGS)


def prepare_pair(pair, args, config, logger):
    options = common_options(args)
    outdir = Path(args.output_dir).expanduser().resolve()
    job = {"sources": [str(pair.reference), str(pair.donor)],
           "output": str(outdir / (pair.reference.stem + ".Hybrid.mkv")),
           "mux_backend": "native", "sync_mode": args.sync_mode,
           "sync_subtitles": args.sync_subtitles, "crossfade_ms": args.crossfade_ms,
           "clean_nfo": args.clean_nfo, "chapters": {"source_index": 0},
           "_allow_missing_output_dir": True}
    if args.auto_tmdb is not None:
        job["tmdb"] = {"enabled": True, "kind": "tv", "season": str(pair.season),
                       "episode": str(pair.episode), "cover": not args.no_cover}
        if args.auto_tmdb:
            job["tmdb"]["id"] = args.auto_tmdb
        if args.tmdb_apikey:
            job["tmdb"]["api_key"] = args.tmdb_apikey
    if args.profile:
        from cli.profile import build_profile_remux_config, load_decision_profile
        result, _ = build_profile_remux_config(load_decision_profile(args.profile, config),
            cli_inputs=[str(pair.reference), str(pair.donor)], cli_output=job["output"],
            config=config, options=options, logger=logger, preview=True, metadata_job=job)
        result = replace(result, mux_backend="native", sync_mode=args.sync_mode,
                         sync_subtitles=args.sync_subtitles, crossfade_ms=args.crossfade_ms, clean_nfo=args.clean_nfo)
    else:
        result = build_remux_config(job, config, options, logger)
    # La référence fournit toujours la vidéo et les chapitres. Le profil reste
    # maître de la sélection des pistes audio/sous-titres.
    donor_tracks = result.sources[1].tracks
    for track in donor_tracks:
        if track.track_type == "video":
            track.enabled = False
    all_tracks = [track for source in result.sources for track in source.tracks]
    if not args.profile:
        result.track_order = [(s.file_index, t.mkv_tid, t.entry_id) for s in result.sources for t in s.tracks if t.enabled]
    else:
        allowed = {t.entry_id for t in all_tracks if t.enabled}
        result.track_order = [item for item in result.track_order if len(item) < 3 or item[2] in allowed]
    ref_audio = next((t for t in result.sources[0].tracks if t.track_type == "audio"), None)
    target_audio = next((t for t in donor_tracks if t.track_type == "audio" and t.enabled), None)
    if ref_audio is None or target_audio is None:
        raise CliError("Une piste audio de référence et une piste donneuse sont requises.", EXIT_ARGS)
    if args.calibration:
        calibration = SyncCalibration.from_dict(json.loads(Path(args.calibration).read_text(encoding="utf-8-sig")))
    else:
        calibration = scanner(args, config).scan(AudioSyncTrack(pair.reference, ref_audio.mkv_tid),
            AudioSyncTrack(pair.donor, target_audio.mkv_tid), detect_cuts=args.detect_cuts,
            drift_threshold_ms=args.drift_threshold_ms, log=lambda text: logger.emit("info", text))
    if result.sync_mode == "physical":
        result.sync_calibrations = {"1": calibration.to_dict()}
    elif len(calibration.segments) != 1:
        raise CliError("Le mode container ne prend pas en charge les coupures.", EXIT_ARGS)
    else:
        for track in donor_tracks:
            if track.track_type == "audio" or (track.track_type == "subtitle" and args.sync_subtitles == "mirror"):
                track.time_shift_ms = round(calibration.segments[0].shift_ms)
    if args.auto_forced_subs:
        from core.workflows.subtitle_heuristics import forced_first
        result.track_order = forced_first(result.track_order, result.sources)
    if args.output_template:
        from cli.output_template import build_output_context, render_output_template
        from cli.remux_config import resolve_tmdb_metadata
        details = resolve_tmdb_metadata(job, config, pair.reference, logger)[3] if job.get("tmdb") else None
        ctx = build_output_context(pair.reference, details, tracks=all_tracks, track_order=list(result.track_order))
        ctx.update({"release_group": args.tag, "group": args.tag,
                    "season_num": pair.season, "episode_num": pair.episode})
        name = render_output_template(args.output_template, ctx)
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise CliError("Le template doit rester dans le dossier de sortie.", EXIT_ARGS)
        result.output = outdir / name
    return result, calibration


def cmd_hybrid(args, config, logger):
    pairs = pairs_from_args(args)
    if args.forced_threshold <= 0 or args.drift_threshold_ms <= 0 or not 0 <= args.crossfade_ms <= 1000:
        raise CliError("Seuils hybrides invalides.", EXIT_ARGS)
    report, outputs = [], set()
    for pair in pairs:
        try:
            result, calibration = prepare_pair(pair, args, config, logger)
            key = str(result.output).casefold()
            if key in outputs:
                raise CliError("Collision des noms de sortie.", EXIT_EXISTS)
            outputs.add(key)
            entry = {"reference": str(pair.reference), "donor": str(pair.donor),
                     "output": str(result.output), "calibration": calibration.to_dict()}
            if args.export_workflow:
                destination = Path(args.export_workflow)
                if len(pairs) > 1:
                    destination.mkdir(parents=True, exist_ok=True)
                    destination /= f"S{pair.season:02d}E{pair.episode:02d}.exact-job.json"
                save_workflow(destination, remux_config_to_exact_job(result))
                entry["status"] = "exported"
            elif args.dry_run:
                from cli.runtime import workflow
                entry["plan"] = workflow(config, common_options(args), logger).execution_preview(result)
                entry["status"] = "planned"
            else:
                result.output.parent.mkdir(parents=True, exist_ok=True)
                result.allow_missing_output_dir = False
                code = run_remux_config(config, common_options(args), logger, result, force=args.force)
                entry["status"] = "ok" if code == EXIT_OK else "failed"
            report.append(entry)
        except Exception as exc:
            report.append({"reference": str(pair.reference), "status": "failed", "error": str(exc)})
            logger.emit("error", str(exc))
            if not args.continue_on_error:
                break
    if args.report_json:
        _write_json(args.report_json, {"version": 1, "episodes": report})
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_PARTIAL if any(e["status"] == "failed" for e in report) else EXIT_OK
