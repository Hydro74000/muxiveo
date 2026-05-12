"""Argparse construction for mediarecode-cli."""

from __future__ import annotations

import argparse

from cli.commands import cmd_batch, cmd_inspect, cmd_preview, cmd_remux, cmd_schema, cmd_validate


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Fichier JSON job/template.")
    parser.add_argument("--ffmpeg", help="Chemin ffmpeg override.")
    parser.add_argument("--ffprobe", help="Chemin ffprobe override.")
    parser.add_argument("--mediainfo", help="Chemin mediainfo override.")
    parser.add_argument("--work-dir", help="Répertoire de travail override.")
    parser.add_argument("--threads", type=int, help="Nombre de threads ffmpeg.")
    parser.add_argument("--log-format", choices=("text", "jsonl"), default="text")
    parser.add_argument("--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mediarecode-cli", description="Mediarecode headless CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="Inspecter une ou plusieurs sources.")
    _add_common_options(inspect)
    inspect.add_argument("input", nargs="+")
    inspect.add_argument("--config-template", action="store_true")
    inspect.add_argument("--rules-preview", action="store_true", help="Afficher les pistes après application des rules.")
    inspect.add_argument("--output")
    inspect.set_defaults(func=cmd_inspect)

    schema = sub.add_parser("schema", help="Afficher le schéma JSON du contrat CLI.")
    schema.add_argument("--output", help="Ecrire le schéma JSON dans un fichier.")
    schema.add_argument("--log-format", choices=("text", "jsonl"), default="text")
    schema.set_defaults(func=cmd_schema)

    for name, help_text, func in (
        ("validate", "Valider une config remux.", cmd_validate),
        ("preview", "Afficher la commande ffmpeg prévue.", cmd_preview),
        ("remux", "Exécuter un remux.", cmd_remux),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_common_options(p)
        p.add_argument("-i", "--input", action="append")
        p.add_argument("-o", "--output")
        p.add_argument("--languages", help="Langues audio/sous-titres autorisées, séparées par virgules.")
        p.add_argument("--tmdb", action="store_true", help="Activer TMDB; premier résultat si pas d'ID.")
        p.add_argument("--tmdb-id", type=int, help="ID TMDB explicite.")
        p.add_argument("--force", action="store_true", help="Autoriser l'écrasement de la sortie.")
        p.add_argument("--dry-run", action="store_true", help="Valider et afficher la commande sans exécuter.")
        p.add_argument("--nfo", dest="nfo", action="store_true", default=None)
        p.add_argument("--no-nfo", dest="nfo", action="store_false")
        p.add_argument("--writing-application", default="")
        if name in {"validate", "preview"}:
            p.add_argument("--json", dest="json_output", action="store_true", help="Sortie JSON structurée.")
        if name == "remux":
            p.add_argument("--save", help="Sauvegarder les options/règles en template réutilisable.")
        p.set_defaults(func=func)

    batch = sub.add_parser("batch", help="Appliquer un template à plusieurs jobs.")
    _add_common_options(batch)
    batch.add_argument("--template", required=True)
    batch.add_argument("--batch", help="JSON contenant `jobs` ou `inputs`.")
    batch.add_argument("-i", "--input", action="append", help="Entrée batch simple; répétable.")
    batch.add_argument("--output-dir")
    batch.add_argument("--force", action="store_true")
    batch.add_argument("--dry-run", action="store_true", help="Valider et afficher les commandes sans exécuter.")
    batch.add_argument("--continue-on-error", action="store_true")
    batch.add_argument("--summary", help="Ecrire un rapport JSON final.")
    batch.add_argument("--nfo", dest="nfo", action="store_true", default=None)
    batch.add_argument("--no-nfo", dest="nfo", action="store_false")
    batch.add_argument("--writing-application", default="")
    batch.set_defaults(func=cmd_batch)
    return parser
