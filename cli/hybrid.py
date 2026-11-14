"""Commandes hybrides et outils autonomes de calibration."""
from __future__ import annotations

import json
from pathlib import Path

from core.workflows.audio_sync_scan import AudioSyncScanner, coherent_sync_accepted, quick_check_enabled
from core.jobs.dynamic_sync import perform_dynamic_sync as perform_dynamic_sync  # réexport (tests, scripts)
from core.service.ops.sync import detect_stream_kind as detect_stream_kind  # remplaçable (tests)
from core.workflows.workflow_store import atomic_write_text
from cli.constants import EXIT_OK, EXIT_PARTIAL
from cli.options import common_options
from cli.remux_config import build_remux_config
from cli.runtime import run_remux_config
from cli.interrupts import interrupt_guard
from core.workflows.hybrid import HybridPair as HybridPair  # réexport (tests)
from cli.service_adapter import envelope, overrides_from_args, print_json, run_operation
from core.jobs.hybrid_pairs import HybridOptions, HybridSteps, pairs_for
from core.jobs.hybrid_pairs import prepare_pair as _prepare_pair
from core.jobs.runtime import remux_workflow
from core.service.context import CancelToken


def _write_json(path, value):
    atomic_write_text(Path(path), json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def scanner(args, config):
    return AudioSyncScanner(args.ffmpeg or config.tool_ffmpeg, args.ffprobe or config.tool_ffprobe,
                            cancel_event=getattr(args, "cancel_event", None),
                            quick_check=quick_check_enabled(args, config),
                            accept_coherent=coherent_sync_accepted(args))


def cmd_sync_scan(args, config, logger):
    """``sync-scan`` : opération ``sync.scan`` ; analyseur et détection de flux remplaçables (``scanner``…)."""
    payload = {
        "ref": args.ref, "target": args.target,
        "stream_ref": str(args.stream_ref), "stream_target": str(args.stream_target),
        "type": getattr(args, "type", "auto") or "auto",
        "detect_cuts": bool(args.detect_cuts), "drift_threshold_ms": args.drift_threshold_ms,
        "reject_coherent_sync": bool(getattr(args, "reject_coherent_sync", False)),
        "quick_sync_check": getattr(args, "quick_sync_check", None),
        "cadence_auto": getattr(args, "cadence_auto", None),
        "cadence_method": getattr(args, "cadence_method", None) or None,
    }
    if args.output_json:
        payload["output_json"] = args.output_json
    ports = {
        "audio_scanner": lambda: scanner(args, config),
        "detect_stream_kind": lambda *a, **k: detect_stream_kind(*a, **k),
    }
    result, ctx = run_operation("sync.scan", payload, config=config, logger=logger,
                                overrides=overrides_from_args(args), ports=ports)
    if getattr(args, "log_format", "text") == "events":
        print_json(envelope("sync.scan", ctx.events.job, result=result))
    else:
        print(json.dumps(getattr(result, "calibration", {}), ensure_ascii=False))
    return EXIT_OK


def cmd_shift_subs(args, config, logger):
    """``shift-subs`` : opération ``sync.shift_subtitles``."""
    payload = {"input": args.input, "output": args.output, "force": bool(args.force)}
    if args.calibration:
        payload["calibration"] = args.calibration
    else:
        payload["offset_ms"] = args.offset_ms
    result, ctx = run_operation("sync.shift_subtitles", payload, config=config, logger=logger)
    if getattr(args, "log_format", "text") == "events":
        print_json(envelope("sync.shift_subtitles", ctx.events.job, result=result))
    return EXIT_OK


def pairs_from_args(args):
    """Moteur par paires historique (``prepare_pair``) : conservé pour les scripts ; la commande ``hybrid``
    utilise le moteur du Hybrid Studio."""
    return pairs_for(args.ref, args.donor, args.ref_dir, args.donor_dir)


def hybrid_options(args) -> HybridOptions:
    """Options de la commande ``hybrid`` (moteur par paires, ``core.jobs.hybrid_pairs``)."""
    return HybridOptions(
        output_dir=Path(args.output_dir), common=common_options(args), profile=str(args.profile or ""),
        auto_tmdb=args.auto_tmdb, tmdb_apikey=str(args.tmdb_apikey or ""), no_cover=bool(args.no_cover),
        tag=str(args.tag), output_template=str(getattr(args, "output_template", "") or ""),
        output_all=bool(getattr(args, "output_all", False)), export_workflow=str(args.export_workflow or ""),
        dry_run=bool(args.dry_run), force=bool(args.force), continue_on_error=bool(args.continue_on_error),
        report_json=str(args.report_json or ""),
    )


def _cli_profile():
    from cli import profile

    return profile


def cli_hybrid_steps(args, config) -> HybridSteps:
    """Étapes historiques du CLI, résolues à l'appel (fonctions remplaçables des modules ``cli``)."""
    return HybridSteps(
        build=lambda job, conf, options, log: build_remux_config(job, conf, options, log),
        load_profile=lambda path, conf: _cli_profile().load_decision_profile(path, conf),
        build_profile=lambda profile, **kwargs: _cli_profile().build_profile_remux_config(profile, **kwargs),
        audio_scanner=lambda: scanner(args, config),
        run=lambda conf, options, log, remux_config, *, force: run_remux_config(conf, options, log, remux_config,
                                                                                force=force),
        preview=lambda conf, options, remux_config: remux_workflow(conf, options).execution_preview(remux_config),
    )


def prepare_pair(pair, args, config, logger):
    return _prepare_pair(pair, hybrid_options(args), config, logger, cli_hybrid_steps(args, config))


def _languages(value):
    return [item.strip() for item in str(value).split(",") if item.strip()] if value else None


def hybrid_payload(args) -> dict:
    """Requête ``hybrid.run`` / ``hybrid.plan`` des options de la commande ``hybrid``."""
    from cli.encode_options import encode_block_from_args
    from core.jobs.hybrid_studio import ROLES, parse_pairs, parse_sources
    from core.service.ops.media import JobOptions

    payload: dict = {
        "output_dir": args.output_dir, "options": JobOptions.from_common(common_options(args)),
        "tag": str(args.tag), "no_cover": bool(args.no_cover), "dry_run": bool(args.dry_run),
        "force": bool(args.force), "continue_on_error": bool(args.continue_on_error),
        "output_all": bool(getattr(args, "output_all", False)),
        "output_template": str(getattr(args, "output_template", "") or ""),
        "tmdb_apikey": str(args.tmdb_apikey or ""), "matching": getattr(args, "matching", "episode") or "episode",
    }
    for key in ("ref", "donor", "ref_dir", "donor_dir", "profile", "export_workflow", "report_json", "recipe",
                "sync", "shift_ms"):
        if getattr(args, key, None) not in (None, ""):
            payload[key] = getattr(args, key)
    for key in ("keep_master_audio", "donor_audio_langs", "donor_sub_langs", "exclude_langs"):
        languages = _languages(getattr(args, key, None))
        if languages is not None:
            payload[key] = languages
    if args.auto_tmdb is not None:
        payload["auto_tmdb"] = args.auto_tmdb
    sources = parse_sources(getattr(args, "source", None) or [])
    if sources:
        roles = {role: name for name, role in ROLES.items()}
        payload["sources"] = [{"role": roles[role], "path": str(path)} for role, path in sources]
    pairs = parse_pairs(getattr(args, "pair", None) or [])
    if pairs:
        payload["pairs"] = [{"master": str(master), "donors": [str(d) for d in donors]} for master, donors in pairs]
    block = encode_block_from_args(args)
    if block is not None:
        payload["encode"] = block
    if getattr(args, "export_jobs", None):
        payload["export_jobs"] = args.export_jobs
    if getattr(args, "save_recipe", None):
        payload["save_recipe"] = args.save_recipe
    return payload


def cmd_hybrid(args, config, logger):
    """``hybrid`` : moteur du Hybrid Studio (opérations hybrid.plan / hybrid.run) ; Ctrl+C arrête."""
    plan = bool(getattr(args, "plan", False) or getattr(args, "export_jobs", None) or getattr(args, "save_recipe", None))
    name = "hybrid.plan" if plan else "hybrid.run"
    payload = hybrid_payload(args)
    if not plan:
        for key in ("export_jobs", "save_recipe"):
            payload.pop(key, None)
    with interrupt_guard(CancelToken()) as cancel:
        result, ctx = run_operation(name, payload, config=config, logger=logger,
                                    overrides=overrides_from_args(args), cancel=cancel)
    if getattr(args, "log_format", "text") == "events":
        print_json(envelope(name, ctx.events.job, result=result))
    elif plan:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif args.dry_run:
        try:
            print(json.dumps(list(getattr(result, "episodes", ())), ensure_ascii=False, indent=2))
        except UnicodeEncodeError:
            print(json.dumps(list(getattr(result, "episodes", ())), ensure_ascii=True, indent=2))
    if plan:
        failed = any("error" in entry for entry in getattr(result, "episodes", ()))
        return EXIT_PARTIAL if failed else EXIT_OK
    return EXIT_OK if getattr(result, "status", "") == "succeeded" else EXIT_PARTIAL
