"""Commandes hybrides et outils autonomes de calibration."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import subprocess

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


def detect_stream_kind(ffprobe: str, path: Path | str, stream_spec: str | int, default: str = "audio") -> str:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".srt", ".vtt", ".ass", ".ssa"}:
        return "subtitle"
    spec_str = str(stream_spec).lower()
    if ":s" in spec_str or spec_str == "s":
        return "subtitle"
    if ":a" in spec_str or spec_str == "a":
        return "audio"
    if ":v" in spec_str or spec_str == "v":
        return "video"
    try:
        cmd = [
            str(ffprobe), "-v", "error",
            "-select_streams", str(stream_spec),
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            str(path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        output = res.stdout.strip().lower()
        if output in {"audio", "subtitle", "video"}:
            return output
    except Exception:
        pass
    return default


def cmd_sync_scan(args, config, logger):
    scan_type = getattr(args, "type", "auto")
    ref_path = Path(args.ref)
    tgt_path = Path(args.target)
    ffprobe = args.ffprobe or config.tool_ffprobe
    ffmpeg = args.ffmpeg or config.tool_ffmpeg

    if scan_type == "auto":
        tgt_kind = detect_stream_kind(ffprobe, tgt_path, args.stream_target, default="audio")
        ref_kind = detect_stream_kind(ffprobe, ref_path, args.stream_ref, default="audio")
    elif scan_type == "subtitle":
        tgt_kind = "subtitle"
        ref_kind = detect_stream_kind(ffprobe, ref_path, args.stream_ref, default="subtitle")
    else:
        tgt_kind = "audio"
        ref_kind = "audio"

    if tgt_kind == "subtitle":
        from core.workflows.subtitle_sync_scan import SubtitleSyncScanner
        sub_scanner = SubtitleSyncScanner(ffmpeg, ffprobe, cancel_event=getattr(args, "cancel_event", None))
        is_ref_sub = (ref_kind == "subtitle")
        result = sub_scanner.scan(
            ref_path, args.stream_ref,
            tgt_path, args.stream_target,
            is_ref_sub=is_ref_sub,
            detect_cuts=(args.detect_cuts if is_ref_sub else False),
            log=lambda message: logger.emit("info", message),
        )
    else:
        result = scanner(args, config).scan(
            AudioSyncTrack(ref_path, args.stream_ref),
            AudioSyncTrack(tgt_path, args.stream_target),
            detect_cuts=args.detect_cuts,
            drift_threshold_ms=args.drift_threshold_ms,
            log=lambda message: logger.emit("info", message),
        )

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


def perform_dynamic_sync(
    sources: list[Any],
    tracks: list[Any],
    config: Any,
    options: Any,
    logger: Any,
) -> SyncCalibration | None:
    """Analyse dynamiquement les sources pour déterminer le décalage (audio ou sous-titres)."""
    if len(sources) < 2:
        return None
    ref_source = sources[0]
    donor_source = sources[1]

    ref_audio = next((t for t in ref_source.tracks if t.track_type == "audio"), None)
    ref_sub = next((t for t in ref_source.tracks if t.track_type == "subtitle" and t.enabled), None)
    if ref_sub is None:
        ref_sub = next((t for t in ref_source.tracks if t.track_type == "subtitle"), None)

    donor_tracks = [t for t in donor_source.tracks if t.enabled]
    target_audio = next((t for t in donor_tracks if t.track_type == "audio"), None)
    sync_audio = None
    if ref_audio is not None and ref_audio.language:
        sync_audio = next((t for t in donor_source.tracks if t.track_type == "audio" and t.language == ref_audio.language), None)
    if sync_audio is None:
        sync_audio = target_audio
    target_sub = next((t for t in donor_tracks if t.track_type == "subtitle"), None)

    # 1. Audio vs Audio si disponible
    if sync_audio and ref_audio:
        logger.emit("info", f"Synchronisation audio dynamique : {ref_source.path.name} #{ref_audio.mkv_tid} vs {donor_source.path.name} #{sync_audio.mkv_tid}…")
        audio_scanner = AudioSyncScanner(
            getattr(options, "ffmpeg", None) or config.tool_ffmpeg,
            getattr(options, "ffprobe", None) or config.tool_ffprobe,
        )
        return audio_scanner.scan(
            AudioSyncTrack(ref_source.path, ref_audio.mkv_tid),
            AudioSyncTrack(donor_source.path, sync_audio.mkv_tid),
            detect_cuts=bool(getattr(options, "detect_cuts", False)),
            drift_threshold_ms=getattr(options, "drift_threshold_ms", 25) or 25,
            log=lambda msg: logger.emit("info", msg),
        )

    # 2. Sous-titre donneur si pas d'audio
    if target_sub:
        from core.workflows.subtitle_sync_scan import SubtitleSyncScanner
        sub_scanner = SubtitleSyncScanner(
            getattr(options, "ffmpeg", None) or config.tool_ffmpeg,
            getattr(options, "ffprobe", None) or config.tool_ffprobe,
        )
        if ref_sub is not None:
            logger.emit("info", f"Synchronisation sous-titres dynamique : {ref_source.path.name} #{ref_sub.mkv_tid} vs {donor_source.path.name} #{target_sub.mkv_tid}…")
            return sub_scanner.scan(
                ref_source.path, ref_sub.mkv_tid,
                donor_source.path, target_sub.mkv_tid,
                is_ref_sub=True,
                detect_cuts=bool(getattr(options, "detect_cuts", False)),
                log=lambda msg: logger.emit("info", msg),
            )
        elif ref_audio is not None:
            logger.emit("info", f"Synchronisation audio vs sous-titre dynamique : {ref_source.path.name} #{ref_audio.mkv_tid} vs {donor_source.path.name} #{target_sub.mkv_tid}…")
            return sub_scanner.scan(
                ref_source.path, ref_audio.mkv_tid,
                donor_source.path, target_sub.mkv_tid,
                is_ref_sub=False,
                detect_cuts=False,
                log=lambda msg: logger.emit("info", msg),
            )

    logger.emit("warning", f"Aucune paire de pistes compatible trouvée pour la synchronisation automatique de {donor_source.path.name}.")
    return None


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
    if not args.profile:
        result.track_order = [(s.file_index, t.mkv_tid, t.entry_id) for s in result.sources for t in s.tracks if t.enabled]
    else:
        allowed = {t.entry_id for t in all_tracks if t.enabled}
        result.track_order = [item for item in result.track_order if len(item) < 3 or item[2] in allowed]
    ref_audio = next((t for t in result.sources[0].tracks if t.track_type == "audio"), None)
    target_audio = next((t for t in donor_tracks if t.track_type == "audio" and t.enabled), None)
    target_sub = next((t for t in donor_tracks if t.track_type == "subtitle" and t.enabled), None)
    ref_sub = next((t for t in result.sources[0].tracks if t.track_type == "subtitle" and t.enabled), None)
    if ref_sub is None:
        ref_sub = next((t for t in result.sources[0].tracks if t.track_type == "subtitle"), None)

    sync_audio = None
    if ref_audio is not None and ref_audio.language:
        sync_audio = next((t for t in result.sources[1].tracks if t.track_type == "audio" and t.language == ref_audio.language), None)
    if sync_audio is None:
        sync_audio = target_audio

    if args.calibration:
        calibration = SyncCalibration.from_dict(json.loads(Path(args.calibration).read_text(encoding="utf-8-sig")))
    elif sync_audio is not None and ref_audio is not None:
        calibration = scanner(args, config).scan(
            AudioSyncTrack(pair.reference, ref_audio.mkv_tid),
            AudioSyncTrack(pair.donor, sync_audio.mkv_tid),
            detect_cuts=args.detect_cuts,
            drift_threshold_ms=args.drift_threshold_ms,
            log=lambda text: logger.emit("info", text),
        )
    elif target_sub is not None:
        from core.workflows.subtitle_sync_scan import SubtitleSyncScanner
        sub_scanner = SubtitleSyncScanner(args.ffmpeg or config.tool_ffmpeg, args.ffprobe or config.tool_ffprobe)
        if ref_sub is not None:
            calibration = sub_scanner.scan(
                pair.reference, ref_sub.mkv_tid,
                pair.donor, target_sub.mkv_tid,
                is_ref_sub=True,
                detect_cuts=args.detect_cuts,
                log=lambda text: logger.emit("info", text),
            )
        elif ref_audio is not None:
            calibration = sub_scanner.scan(
                pair.reference, ref_audio.mkv_tid,
                pair.donor, target_sub.mkv_tid,
                is_ref_sub=False,
                detect_cuts=False,
                log=lambda text: logger.emit("info", text),
            )
        else:
            raise CliError("Une piste de référence audio ou sous-titre est requise.", EXIT_ARGS)
    else:
        raise CliError("Une piste donneuse audio ou sous-titre est requise.", EXIT_ARGS)
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
        ref_title_m = re.search(r"S\d+E\d+\.(.*?)\.(?:1080p|720p|2160p|WEB|HDTV|AMZN)", pair.reference.name)
        ref_ep_title = ref_title_m.group(1) if ref_title_m else ""
        ref_ep_title = re.sub(r"\.(?:REPACK|PROPER)$", "", ref_ep_title, flags=re.IGNORECASE)
        ctx.update({"release_group": args.tag, "group": args.tag,
                    "season_num": pair.season, "episode_num": pair.episode,
                    "ref_episode_title": ref_ep_title})
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
