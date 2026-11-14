"""Commandes ``calibration`` (lire, éditer, composer, inverser) et ``sync-waveform`` (opérations calibration.* et
sync.waveform)."""

from __future__ import annotations

import argparse
import json
from typing import Any

from core.config import AppConfig
from core.jobs.constants import EXIT_ARGS, EXIT_OK
from core.jobs.errors import JobError
from core.service.context import CancelToken

from cli.constants import LOG_FORMATS
from cli.interrupts import interrupt_guard
from cli.logging import Logger
from cli.service_adapter import envelope, print_json, run_operation


def parse_time_ms(text: str) -> float:
    """Instant de sortie : millisecondes (« 1500 ») ou horodatage (« 1:02:03.5 », « 02:03 »)."""
    value = text.strip().replace(",", ".")
    try:
        if ":" not in value:
            return float(value)
        parts = [float(part) for part in value.split(":")]
    except ValueError as exc:
        raise JobError(f"Instant invalide : {text!r} (ms ou HH:MM:SS.mmm).", EXIT_ARGS) from exc
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds * 1000.0


def _range(text: str) -> list[float]:
    start, sep, end = text.partition("-")
    if not sep:
        raise JobError(f"Plage invalide : {text!r} (DÉBUT-FIN).", EXIT_ARGS)
    return [parse_time_ms(start), parse_time_ms(end)]


class _EditAction(argparse.Action):
    """Éditions conservées dans l'ordre de la ligne de commande."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        edits = list(getattr(namespace, "edits", None) or [])
        edits.append((self.dest, str(values)))
        namespace.edits = edits


def _edit_payload(kind: str, value: str) -> dict[str, Any]:
    if kind in ("cut", "silence"):
        return {kind: _range(value)}
    if kind == "remove_junction":
        if not value.isdigit():
            raise JobError("--remove-junction : numéro de segment ≥ 1 attendu.", EXIT_ARGS)
        return {kind: int(value)}
    if kind == "shift_ms":
        return {kind: parse_time_ms(value) if ":" in value else float(value)}
    if kind == "shift_from_segment":
        index, sep, shift = value.partition(":")
        if not sep or not index.isdigit():
            raise JobError("--shift-from-segment : N:MS attendu (ex. 2:-120).", EXIT_ARGS)
        return {kind: {"index": int(index), "shift_ms": float(shift)}}
    return {kind: value}


def _source(value: str) -> dict[str, Any]:
    return {"file": value}


def _audio_ref(value: str) -> dict[str, Any]:
    path, sep, stream = value.rpartition(":")
    if not sep or not stream.isdigit():
        raise JobError(f"--resolve-cadence : FICHIER:INDEX attendu, reçu {value!r}.", EXIT_ARGS)
    return {"path": path, "stream": int(stream)}


def add_calibration_parsers(sub: Any) -> None:
    calibration = sub.add_parser("calibration", help="Lire, éditer, composer ou inverser une calibration de synchro.",
                                 description="Calibrations de synchro (fichiers JSON de sync-scan, du Synchro Studio "
                                             "ou des exact-jobs) : mêmes éditions que le Synchro Studio.")
    actions = calibration.add_subparsers(dest="calibration_command", required=True)
    show = actions.add_parser("show", help="Décrire une calibration (segments, plages de sortie, cadence).")
    show.add_argument("file", metavar="FICHIER")
    edit = actions.add_parser(
        "edit", help="Éditer une calibration comme le Synchro Studio.",
        description="Éditions appliquées dans l'ordre de la ligne de commande. Instants de SORTIE en ms ou "
                    "HH:MM:SS.mmm.",
        epilog="Exemples :\n"
               "  calibration edit calib.json --cut 600000-602500 -o calib2.json\n"
               "  calibration edit calib.json --silence 1:00:00-1:00:04 --shift-from-segment 2:-120 -o calib2.json\n"
               "  calibration edit calib.json --cadence-method auto --resolve-cadence ref.mkv:1 cible.mkv:2",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    edit.add_argument("file", metavar="FICHIER")
    edit.add_argument("--cut", action=_EditAction, metavar="DÉBUT-FIN", help="Supprimer cette section de la sortie.")
    edit.add_argument("--silence", action=_EditAction, metavar="DÉBUT-FIN",
                      help="Insérer un silence de la longueur de cette section.")
    edit.add_argument("--remove-junction", action=_EditAction, metavar="N", help="Retirer la jonction du segment N.")
    edit.add_argument("--shift-ms", action=_EditAction, metavar="MS", help="Départ fixé à ce décalage (la suite suit).")
    edit.add_argument("--shift-from-segment", action=_EditAction, metavar="N:MS",
                      help="Décalage du segment N fixé à MS ; l'écart est reporté sur les segments suivants.")
    edit.add_argument("--cadence-method", action=_EditAction, choices=("atempo", "asetrate", "auto"),
                      help="Méthode de conversion de cadence (calibration avec cadence).")
    edit.add_argument("--resolve-cadence", nargs=2, metavar=("REF:INDEX", "CIBLE:INDEX"),
                      help="Trancher la méthode « auto » par l'analyse de hauteur (fichiers et index ffprobe).")
    compose = actions.add_parser("compose", help="Composer deux calibrations (référence elle-même synchronisée).")
    compose.add_argument("inner", metavar="MESURE", help="Cible face à l'audio d'origine de la référence.")
    compose.add_argument("outer", metavar="POSITION", help="Position de la référence dans la sortie.")
    invert = actions.add_parser("invert", help="Inverser une calibration (référence face à la cible).")
    invert.add_argument("file", metavar="FICHIER")
    for parser in (show, edit, compose, invert):
        if parser is not show:
            parser.add_argument("-o", "--output", metavar="SORTIE", help="Calibration écrite (sinon JSON sur stdout).")
            parser.add_argument("--force", action="store_true", help="Remplacer le fichier de sortie.")
        parser.add_argument("--log-format", choices=LOG_FORMATS, default="text")
        parser.add_argument("--verbose", action="store_true")
        parser.set_defaults(func=cmd_calibration, edits=[])

    waveform = sub.add_parser("sync-waveform", help="Forme d'onde d'une piste audio sur une plage (JSON).")
    waveform.add_argument("-i", "--input", required=True, metavar="FICHIER")
    waveform.add_argument("--stream", default="0:a:0", help="Flux : index ffprobe ou spécificateur (défaut 0:a:0).")
    waveform.add_argument("--start", default="0", metavar="INSTANT", help="Début (s ou HH:MM:SS.mmm).")
    waveform.add_argument("--duration", type=float, default=60.0, metavar="S", help="Durée en secondes (défaut 60).")
    waveform.add_argument("--resolution-ms", type=int, default=10, metavar="MS", help="Largeur d'une tranche (ms).")
    waveform.add_argument("--ffmpeg", help="Chemin ffmpeg override.")
    waveform.add_argument("--log-format", choices=LOG_FORMATS, default="text")
    waveform.add_argument("--verbose", action="store_true")
    waveform.set_defaults(func=cmd_sync_waveform)


def build_calibration_request(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    action = args.calibration_command
    if action == "show":
        return "calibration.show", {"calibration": _source(args.file)}
    request: dict[str, Any] = {"force": bool(args.force)}
    if args.output:
        request["output"] = args.output
    if action == "compose":
        return "calibration.compose", {**request, "inner": _source(args.inner), "outer": _source(args.outer)}
    if action == "invert":
        return "calibration.invert", {**request, "calibration": _source(args.file)}
    if not args.edits and not args.resolve_cadence:
        raise JobError("calibration edit : au moins une édition attendue.", EXIT_ARGS)
    request["calibration"] = _source(args.file)
    request["edits"] = [_edit_payload(kind, value) for kind, value in args.edits]
    if args.resolve_cadence:
        request["resolve_cadence"] = [_audio_ref(value) for value in args.resolve_cadence]
    return "calibration.edit", request


def cmd_calibration(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    name, request = build_calibration_request(args)
    with interrupt_guard(CancelToken()) as cancel:  # Ctrl+C (analyse de cadence) : « cancelled », code 6
        result, ctx = run_operation(name, request, config=config, logger=logger, cancel=cancel)
    if args.log_format == "events":
        print_json(envelope(name, ctx.events.job, result=result))
    elif getattr(result, "file", None):
        logger.emit("info", f"Calibration écrite : {result.file}")  # type: ignore[attr-defined]
        for line in getattr(result, "summary", ()):
            logger.emit("info", f"  • {line}")
    else:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_sync_waveform(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    from cli.service_adapter import overrides_from_args

    start = args.start.strip()
    start_s = parse_time_ms(start) / 1000.0 if ":" in start else float(start.replace(",", "."))
    request = {"source": args.input, "stream": str(args.stream), "start_s": start_s, "duration_s": args.duration,
               "resolution_ms": args.resolution_ms}
    with interrupt_guard(CancelToken()) as cancel:
        result, ctx = run_operation("sync.waveform", request, config=config, logger=logger,
                                    overrides=overrides_from_args(args), cancel=cancel)
    if args.log_format == "events":
        print_json(envelope("sync.waveform", ctx.events.job, result=result))
    else:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    return EXIT_OK
