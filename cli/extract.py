"""Commande ``extract`` : pistes, couches Dolby Vision / HDR10+ et pièces jointes (opération media.extract)."""

from __future__ import annotations

import argparse
from typing import Any

from core.config import AppConfig
from core.jobs.constants import EXIT_ARGS, EXIT_OK
from core.jobs.errors import JobError
from core.service.context import CancelToken

from cli.constants import LOG_FORMATS
from cli.interrupts import interrupt_guard
from cli.logging import Logger
from cli.service_adapter import envelope, overrides_from_args, print_json, run_operation


def add_extract_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "extract",
        help="Extraire des pistes, couches Dolby Vision / HDR10+ ou pièces jointes.",
        description="Extrait des éléments d'une source vers des fichiers autonomes : piste en flux élémentaire "
                    "(ou Matroska avec --format mkv), couche EL / BL d'un Dolby Vision profil 7, RPU, HDR10+, "
                    "pièce jointe. Chaque sortie est écrite dans un fichier .partial puis publiée.",
        epilog="Exemples :\n"
               "  extract -i film.mkv --track a:0 --output-dir pistes/\n"
               "  extract -i film.mkv --track v:0 --layer el -o film.el.hevc\n"
               "  extract -i film.mkv --track v:0 --layer rpu --track v:0 --layer hdr10plus\n"
               "  extract -i film.mkv --all --output-dir film/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-i", "--input", required=True, metavar="FICHIER", help="Fichier source.")
    parser.set_defaults(track=[], track_layers=[])
    parser.add_argument("--track", action=_TrackAction, metavar="SEL",
                        help="Piste : index ffprobe ou nature et rang (v:0, a:1, s:0) ; répétable.")
    parser.add_argument("--layer", action=_LayerAction, choices=("stream", "el", "bl", "rpu", "hdr10plus"),
                        help="Couche de la piste --track qui précède (défaut : stream) : flux, EL / BL "
                             "(Dolby Vision profil 7), RPU, HDR10+.")
    parser.add_argument("--attachment", action="append", default=[], metavar="N|NOM",
                        help="Pièce jointe : rang affiché par inspect ou nom de fichier ; répétable.")
    parser.add_argument("--all", dest="extract_all", action="store_true",
                        help="Toutes les pistes extractibles et toutes les pièces jointes.")
    parser.add_argument("-o", "--output", metavar="SORTIE", help="Fichier de sortie (un seul élément).")
    parser.add_argument("--output-dir", metavar="DOSSIER", help="Dossier des sorties (défaut : celui de la source).")
    parser.add_argument("--format", dest="extract_format", choices=("raw", "mkv"), default="raw",
                        help="raw : flux élémentaire (défaut) ; mkv : piste dans un conteneur Matroska.")
    parser.add_argument("--force", action="store_true", help="Remplacer les sorties existantes.")
    parser.add_argument("--dry-run", action="store_true", help="Lister les sorties prévues sans rien extraire.")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Résultat JSON sur stdout.")
    parser.add_argument("--ffmpeg", help="Chemin ffmpeg override.")
    parser.add_argument("--ffprobe", help="Chemin ffprobe override.")
    parser.add_argument("--mediainfo", help="Chemin mediainfo override.")
    parser.add_argument("--work-dir", help="Répertoire de travail override.")
    parser.add_argument("--log-format", choices=LOG_FORMATS, default="text")
    parser.add_argument("--verbose", action="store_true", help="Journal détaillé.")
    parser.set_defaults(func=cmd_extract)


class _TrackAction(argparse.Action):
    """``--track`` : ajoute la piste avec la couche ``stream`` (remplacée par un ``--layer`` qui suit)."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        namespace.track = [*getattr(namespace, "track", []), str(values)]
        namespace.track_layers = [*getattr(namespace, "track_layers", []), "stream"]


class _LayerAction(argparse.Action):
    """``--layer`` : couche du dernier ``--track``."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        layers = list(getattr(namespace, "track_layers", []))
        if not layers:
            parser.error("--layer doit suivre un --track.")
        layers[-1] = str(values)
        namespace.track_layers = layers


def build_request(args: argparse.Namespace) -> dict[str, Any]:
    if args.extract_all and (args.track or args.attachment):
        raise JobError("--all exclut --track et --attachment.", EXIT_ARGS)
    if not args.extract_all and not (args.track or args.attachment):
        raise JobError("Indiquer --track, --attachment ou --all.", EXIT_ARGS)
    items: list[dict[str, Any]] = [{"track": track, "layer": layer}
                                   for track, layer in zip(args.track, args.track_layers, strict=True)]
    items += [{"attachment": str(attachment)} for attachment in args.attachment]
    if args.output:
        if args.extract_all or len(items) != 1:
            raise JobError("-o n'accepte qu'un élément ; utiliser --output-dir.", EXIT_ARGS)
        items[0]["output"] = args.output
    request: dict[str, Any] = {"source": args.input, "container": args.extract_format == "mkv",
                               "force": bool(args.force), "dry_run": bool(args.dry_run)}
    if args.extract_all:
        request["all"] = True
    else:
        request["items"] = items
    if args.output_dir:
        request["output_dir"] = args.output_dir
    return request


def cmd_extract(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    request = build_request(args)
    with interrupt_guard(CancelToken()) as cancel:
        result, ctx = run_operation("media.extract", request, config=config, logger=logger,
                                    overrides=overrides_from_args(args), cancel=cancel)
    if args.log_format == "events":
        print_json(envelope("media.extract", ctx.events.job, result=result))
    elif args.json_output:
        print_json(result.to_dict())
    else:
        for output in getattr(result, "outputs", ()):
            print(f"{output['path']}  [{output['format'] or output['kind']}]")
    return EXIT_OK
