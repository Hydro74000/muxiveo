"""Commandes ``tmdb search|details``, ``profile list|show|save|delete|capture`` et ``bluray-titles``."""

from __future__ import annotations

import argparse
import json
from typing import Any

from core.config import AppConfig
from core.jobs.constants import EXIT_ARGS, EXIT_OK
from core.jobs.errors import JobError
from core.jobs.loader import load_job
from core.jobs.options import JobOverrides
from core.json_documents import read_json_document
from core.service.ops.media import JobOptions

from cli.constants import LOG_FORMATS
from cli.logging import Logger
from cli.options import common_options
from cli.service_adapter import envelope, overrides_from_args, print_json, run_operation


def _base(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", dest="json_output", action="store_true", help="Résultat JSON sur stdout.")
    parser.add_argument("--log-format", choices=LOG_FORMATS, default="text")
    parser.add_argument("--verbose", action="store_true")


def add_library_parsers(sub: Any) -> None:
    tmdb = sub.add_parser("tmdb", help="Rechercher sur TMDB et lire une fiche.")
    actions = tmdb.add_subparsers(dest="tmdb_command", required=True)
    search = actions.add_parser("search", help="Rechercher un film ou une série.")
    search.add_argument("query", metavar="TITRE")
    search.add_argument("--kind", choices=("all", "movie", "tv"), default="all")
    search.add_argument("--year", default="", help="Année (repli sans année si aucun résultat).")
    details = actions.add_parser("details", help="Fiche complète et balises MKV.")
    details.add_argument("tmdb_id", type=int, metavar="ID")
    details.add_argument("--kind", choices=("movie", "tv"), default="movie")
    details.add_argument("--season", type=int)
    details.add_argument("--episode", type=int)
    for parser in (search, details):
        parser.add_argument("--language", default="fr-FR", help="Langue des titres (défaut fr-FR).")
        parser.add_argument("--tmdb-apikey", default="", help="Clé API (défaut : réglage [metadata]).")
        _base(parser)
        parser.set_defaults(func=cmd_tmdb)

    titles = sub.add_parser("bluray-titles", help="Lister les titres (playlists) d'un Blu-ray.")
    titles.add_argument("path", metavar="DISQUE", help="Racine du disque, dossier BDMV ou fichier .mpls / .m2ts.")
    titles.add_argument("--min-duration", type=float, default=0.0, metavar="S", help="Durée minimale (s).")
    _base(titles)
    titles.set_defaults(func=cmd_bluray_titles)


def add_profile_management_parsers(profile_sub: Any, add_common: Any) -> None:
    """Sous-commandes de gestion des profils enregistrés (ajoutées au parseur ``profile``)."""
    listing = profile_sub.add_parser("list", help="Profils enregistrés (communs avec l'interface).")
    show = profile_sub.add_parser("show", help="Afficher un profil enregistré (JSON).")
    show.add_argument("name", metavar="NOM")
    save = profile_sub.add_parser("save", help="Enregistrer un profil depuis un fichier JSON (validé).")
    save.add_argument("file", metavar="FICHIER")
    save.add_argument("--name", dest="profile_name", help="Nom enregistré (défaut : celui du fichier).")
    save.add_argument("--force", action="store_true", help="Remplacer un profil du même nom.")
    delete = profile_sub.add_parser("delete", help="Supprimer un profil enregistré.")
    delete.add_argument("name", metavar="NOM")
    for parser in (listing, show, save, delete):
        _base(parser)
        parser.set_defaults(func=cmd_profile_store)
    capture = profile_sub.add_parser(
        "capture", help="Capturer un profil d'après un job (comme « Capturer » du panneau Conteneur).",
        description="Construit le job (sources -i, ou --config) puis en capture la sélection, les métadonnées, les "
                    "flags, l'ordre et les variantes audio ; --save l'enregistre parmi les profils de l'interface.")
    add_common(capture)
    capture.add_argument("-i", "--input", action="append")
    capture.add_argument("-o", "--output")
    capture.add_argument("--name", dest="profile_name", default="", help="Nom du profil.")
    capture.add_argument("--description", default="")
    for part in ("selection", "metadata", "flags", "order", "audio-variants"):
        capture.add_argument(f"--no-{part}", action="store_true", help=f"Ne pas capturer : {part}.")
    capture.add_argument("--save", action="store_true", help="Enregistrer le profil capturé.")
    capture.add_argument("--force", action="store_true")
    capture.add_argument("--json", dest="json_output", action="store_true")
    capture.set_defaults(func=cmd_profile_capture)


def _emit(name: str, result: Any, ctx: Any, args: argparse.Namespace) -> None:
    if args.log_format == "events":
        print_json(envelope(name, ctx.events.job, result=result))
    else:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def cmd_tmdb(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    if args.tmdb_command == "search":
        name, payload = "tmdb.search", {"query": args.query, "kind": args.kind, "year": args.year}
    else:
        name, payload = "tmdb.details", {"tmdb_id": args.tmdb_id, "kind": args.kind}
        for key in ("season", "episode"):
            if getattr(args, key) is not None:
                payload[key] = getattr(args, key)
    payload.update(language=args.language, api_key=args.tmdb_apikey)
    result, ctx = run_operation(name, payload, config=config, logger=logger)
    if args.log_format != "events" and not args.json_output and name == "tmdb.search":
        for item in getattr(result, "results", ()):
            print(f"{item['tmdb_id']}\t{item['kind']}\t{item['year'] or '----'}\t{item['title']}")
        return EXIT_OK
    _emit(name, result, ctx, args)
    return EXIT_OK


def cmd_bluray_titles(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    result, ctx = run_operation("media.bluray_titles", {"path": args.path, "min_duration_s": args.min_duration},
                                config=config, logger=logger)
    if args.log_format != "events" and not args.json_output:
        for title in getattr(result, "titles", ()):
            minutes, seconds = divmod(int(title["duration_s"]), 60)
            print(f"{title['playlist']:05d}.mpls\t{minutes // 60}:{minutes % 60:02d}:{seconds:02d}\t"
                  f"{len(title['segments'])} segment(s)\t{title['size_bytes'] / 1e9:.1f} Go")
        return EXIT_OK
    _emit("media.bluray_titles", result, ctx, args)
    return EXIT_OK


def cmd_profile_store(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    action = args.profile_command
    payload: dict[str, Any] = {}
    if action in ("show", "delete"):
        payload = {"name": args.name}
    elif action == "save":
        try:
            document = read_json_document(args.file)
        except (OSError, ValueError) as exc:
            raise JobError(f"Profil illisible ({args.file}) : {exc}", EXIT_ARGS) from exc
        if not isinstance(document, dict):
            raise JobError("Profil : objet JSON attendu.", EXIT_ARGS)
        if args.profile_name:
            document["name"] = args.profile_name
        payload = {"profile": document, "force": bool(args.force)}
    name = f"profile.{action}"
    result, ctx = run_operation(name, payload, config=config, logger=logger)
    if args.log_format == "events" or args.json_output or action == "show":
        _emit(name, result, ctx, args)
    elif action == "list":
        for profile in getattr(result, "profiles", ()):
            print(f"{profile['name']}  ({profile['rules']} règle(s))"
                  + (f" — {profile['description']}" if profile["description"] else ""))
        for error in getattr(result, "load_errors", ()):
            logger.emit("warning", f"Profil illisible : {error['path']} ({error['error']})")
    else:
        logger.emit("info", f"Profil {action} : {getattr(result, 'path', '')}")
    return EXIT_OK


def cmd_profile_capture(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    options = common_options(args)
    job = load_job(JobOverrides.from_namespace(args))
    payload = {"job": job, "options": JobOptions.from_common(options), "name": args.profile_name,
               "description": args.description, "save": bool(args.save), "force": bool(args.force),
               **{f"include_{part.replace('-', '_')}": not getattr(args, f"no_{part.replace('-', '_')}")
                  for part in ("selection", "metadata", "flags", "order", "audio-variants")}}
    result, ctx = run_operation("profile.capture", payload, config=config, logger=logger,
                                overrides=overrides_from_args(args))
    if args.save and args.log_format != "events" and not args.json_output:
        logger.emit("info", f"Profil enregistré : {getattr(result, 'path', '')}")
        return EXIT_OK
    _emit("profile.capture", result, ctx, args)
    return EXIT_OK
