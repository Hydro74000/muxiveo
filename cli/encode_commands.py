"""Commandes CLI propres à l'encodage : aperçus réels (``encode --preview-…``) et presets (``encode-preset``)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.jobs.constants import EXIT_ARGS, EXIT_OK
from core.jobs.errors import JobError
from core.service.context import CancelToken
from core.service.ops.media import JobOptions

from cli.encode_options import encode_block_from_args
from cli.interrupts import interrupt_guard
from cli.logging import Logger
from cli.service_adapter import envelope, overrides_from_options, print_json, run_operation


def _preview_requests(args: argparse.Namespace) -> list[dict[str, Any]]:
    requests = [{"mode": "image", "timecode_s": float(t)} for t in args.preview_image or []]
    if args.preview_video:
        start, sep, duration = str(args.preview_video).partition(":")
        try:
            item: dict[str, Any] = {"mode": "video", "timecode_s": float(start)}
            if sep:
                item["duration_s"] = float(duration)
        except ValueError as exc:
            raise JobError("--preview-video : SECONDES[:DURÉE] attendu (ex. 600:10).", EXIT_ARGS) from exc
        requests.append(item)
    return requests


def run_previews(job: dict, args: argparse.Namespace, config: AppConfig, options, logger: Logger) -> int:
    """Aperçus réels du job encodé (opération encode.preview_capture), copiés dans le dossier demandé."""
    output = Path(str(job.get("output") or "."))
    target = Path(args.preview_dir).expanduser() if args.preview_dir else output.expanduser().parent
    results = []
    with interrupt_guard(CancelToken()) as cancel:
        for request in _preview_requests(args):
            payload = {"job": job, "options": JobOptions.from_common(options), "output_dir": str(target),
                       "random_scene": bool(args.random_scene), **request}
            result, ctx = run_operation("encode.preview_capture", payload, config=config, logger=logger,
                                        overrides=overrides_from_options(options), cancel=cancel)
            if args.log_format == "events":
                print_json(envelope("encode.preview_capture", ctx.events.job, result=result))
            results.append(result.to_dict())
            for capture in getattr(result, "captures", ()):
                logger.emit("info", f"Aperçu image : {capture['path']}")
            video = getattr(result, "video", None)
            if video:
                logger.emit("info", f"Aperçu vidéo : {video}")
    if args.log_format != "events":
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return EXIT_OK


def _preset_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Champs d'un preset d'après les options d'encodage (format des presets de l'interface)."""
    block = encode_block_from_args(args) or {}
    spec = dict((block.get("video") or {}).get("default") or {})
    preset: dict[str, Any] = {"name": args.name, "description": args.description}
    for key in ("codec", "rate_control", "crf", "cq", "bitrate_kbps", "extra_params", "bit_depth", "crop", "resize",
                "filters", "interpolation", "restoration"):
        if key in spec:
            preset[key] = spec.pop(key)
    if "encoder_preset" in spec:
        preset["preset"] = spec.pop("encoder_preset")
    # HDR10+ généré : intention enregistrable (effective sur toute source Dolby Vision) ; copie / retrait :
    # propres à la source.
    if (spec.get("hdr10plus") or {}).get("mode") == "generate" and len(spec["hdr10plus"]) == 1:
        preset["generate_hdr10plus"] = True
        spec.pop("hdr10plus")
    for nested, mapping in (("fel", {"bake": "bake_dovi_fel", "precision": "fel_precision", "device": "fel_device",
                                     "hdr10plus": "fel_hdr10plus"}),
                            ("tonemap", {"algorithm": "tonemap_algorithm"})):
        for key, target in mapping.items():
            if key in (spec.get(nested) or {}):
                preset[target] = spec[nested][key]
        spec.pop(nested, None)
    for key in ("crop", "resize", "interpolation", "restoration"):
        if isinstance(preset.get(key), dict) and "enabled" not in preset[key] and key != "crop":
            preset[key] = {"enabled": True, **preset[key]}
    if isinstance(preset.get("crop"), dict):
        preset["crop"] = {"enabled": True, **preset["crop"]}
    if block.get("target_size_mb"):
        preset["target_size_mb"] = block["target_size_mb"]
    ignored = sorted(spec)
    if ignored:
        raise JobError("Preset : réglages propres à la source, non enregistrables : " + ", ".join(ignored), EXIT_ARGS)
    return preset


def cmd_encode_preset(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    """``encode-preset list|show|save|import|export|delete``."""
    action = args.preset_command
    name = {"list": "encode_preset.list", "show": "encode_preset.show", "save": "encode_preset.save",
            "import": "encode_preset.import", "export": "encode_preset.export",
            "delete": "encode_preset.delete"}[action]
    payload: dict[str, Any] = {}
    if action in ("show", "delete"):
        payload = {"name": args.name}
    elif action == "save":
        payload = {"preset": _preset_from_args(args), "force": bool(args.force)}
    elif action == "import":
        payload = {"file": args.file, "force": bool(args.force)}
        if args.import_name:
            payload["name"] = args.import_name
    elif action == "export":
        payload = {"name": args.name, "file": args.file, "force": bool(args.force)}
    result, ctx = run_operation(name, payload, config=config, logger=logger)
    if args.log_format == "events":
        print_json(envelope(name, ctx.events.job, result=result))
    elif action == "list":
        for preset in getattr(result, "presets", ()):
            print(f"{preset['name']}  [{preset['codec']} {preset['preset']}]"
                  + (f" — {preset['description']}" if preset["description"] else ""))
        for error in getattr(result, "load_errors", ()):
            logger.emit("warning", f"Preset illisible : {error['path']} ({error['error']})")
    elif action == "show":
        print_json(getattr(result, "preset", {}))
    else:
        logger.emit("info", f"Preset « {getattr(result, 'name', '')} » : {action} effectué.")
    return EXIT_OK
