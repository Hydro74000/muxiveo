"""Commandes ``config path|show|get|set|unset|reset``, ``workdir list|clean`` et ``nvencc status|install|update|remove``."""

from __future__ import annotations

import argparse
import json
from typing import Any

from core.config import AppConfig
from core.jobs.constants import EXIT_OK
from core.service.context import CancelToken

from cli.constants import LOG_FORMATS
from cli.interrupts import interrupt_guard
from cli.logging import Logger
from cli.service_adapter import envelope, overrides_from_args, print_json, run_operation


def _base(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", dest="json_output", action="store_true", help="Résultat JSON sur stdout.")
    parser.add_argument("--log-format", choices=LOG_FORMATS, default="text")
    parser.add_argument("--verbose", action="store_true")


def add_admin_parsers(sub: Any) -> None:
    config = sub.add_parser("config", help="Lire et modifier la configuration (comme le panneau Paramètres).",
                            description="Réglages `section.clé` (config show les liste) : valeur effective et "
                                        "provenance (config.ini, réglage de l'interface, défaut). set écrit "
                                        "config.ini ; unset retire la surcharge (le réglage de l'interface peut "
                                        "réapparaître) ; reset revient au défaut.")
    actions = config.add_subparsers(dest="config_command", required=True)
    path = actions.add_parser("path", help="Emplacements de config.ini et des réglages de l'interface.")
    show = actions.add_parser("show", help="Tous les réglages (ou une section).")
    show.add_argument("section", nargs="?", metavar="SECTION")
    get = actions.add_parser("get", help="Un réglage.")
    get.add_argument("key", metavar="SECTION.CLÉ")
    setter = actions.add_parser("set", help="Écrire un réglage dans config.ini.")
    setter.add_argument("key", metavar="SECTION.CLÉ")
    setter.add_argument("value", metavar="VALEUR")
    unset = actions.add_parser("unset", help="Retirer la surcharge config.ini.")
    unset.add_argument("key", metavar="SECTION.CLÉ")
    reset = actions.add_parser("reset", help="Revenir à la valeur par défaut.")
    reset.add_argument("key", metavar="SECTION.CLÉ")
    for parser in (path, show, get, setter, unset, reset):
        _base(parser)
        parser.set_defaults(func=cmd_config)

    workdir = sub.add_parser("workdir", help="Dossier de travail : éléments abandonnés, nettoyage.")
    work_actions = workdir.add_subparsers(dest="workdir_command", required=True)
    listing = work_actions.add_parser("list", help="Éléments nettoyables (jobs actifs exclus).")
    clean = work_actions.add_parser("clean", help="Nettoyer (comme « Nettoyer » au démarrage).")
    clean.add_argument("--dry-run", action="store_true", help="Lister sans supprimer.")
    for parser in (listing, clean):
        parser.add_argument("--work-dir", help="Dossier de travail (défaut : réglage [paths] work_dir).")
        _base(parser)
        parser.set_defaults(func=cmd_workdir)

    nvencc = sub.add_parser("nvencc", help="NVEncC : état, installation, mise à jour, suppression.")
    nv_actions = nvencc.add_subparsers(dest="nvencc_command", required=True)
    status = nv_actions.add_parser("status", help="Présence, version, provenance.")
    status.add_argument("--check", action="store_true", help="Comparer à la dernière release (GitHub).")
    install = nv_actions.add_parser("install", help="Installer NVEncC (apt / dnf, sinon dossier utilisateur).")
    update = nv_actions.add_parser("update", help="Mettre à jour NVEncC si une version plus récente existe.")
    for parser in (install, update):
        parser.add_argument("--method", choices=("apt", "dnf", "user", "windows"),
                            help="Méthode imposée (défaut : selon la machine).")
        parser.add_argument("--no-sudo", action="store_true", help="Sans aucune élévation (ni sudo ni pkexec) : dossier utilisateur.")
    remove = nv_actions.add_parser("remove", help="Supprimer la copie utilisateur de NVEncC.")
    for parser in (status, install, update, remove):
        _base(parser)
        parser.set_defaults(func=cmd_nvencc)


def _render(name: str, result: Any, ctx: Any, args: argparse.Namespace) -> bool:
    """Événements ou JSON demandés : affichés ici ; sinon l'appelant écrit le texte."""
    if args.log_format == "events":
        print_json(envelope(name, ctx.events.job, result=result))
        return True
    if args.json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return True
    return False


def cmd_config(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    action = args.config_command
    payload: dict[str, Any] = {}
    if action == "show" and args.section:
        payload = {"section": args.section}
    elif action in ("get", "unset", "reset"):
        payload = {"key": args.key}
    elif action == "set":
        payload = {"key": args.key, "value": args.value}
    name = f"config.{action}"
    result, ctx = run_operation(name, payload, config=config, logger=logger)
    if _render(name, result, ctx, args):
        return EXIT_OK
    if action == "path":
        print(f"config.ini : {result.config_ini}")  # type: ignore[attr-defined]
        print(f"réglages   : {result.settings or '—'}")  # type: ignore[attr-defined]
        return EXIT_OK
    for entry in getattr(result, "entries", ()):
        extra = f"  [{entry['source']}]"
        if entry.get("masked"):
            extra += f" ({entry['masked']} : {entry.get('settings_value')})"
        print(f"{entry['key']} = {entry['value']}{extra}")
    for warning in getattr(result, "warnings", ()):
        logger.emit("warning", warning)
    return EXIT_OK


def cmd_workdir(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    action = args.workdir_command
    name = f"workdir.{action}"
    payload = {"dry_run": bool(getattr(args, "dry_run", False))}
    result, ctx = run_operation(name, payload, config=config, logger=logger, overrides=overrides_from_args(args))
    if _render(name, result, ctx, args):
        return EXIT_OK
    entries = getattr(result, "entries", ())
    for entry in entries:
        print(f"{entry['size_bytes'] / 1e6:10.1f} Mo  {entry['path']}")
    removed = getattr(result, "removed", ())
    if action == "clean" and not getattr(result, "dry_run", False):
        logger.emit("info", f"{len(removed)} élément(s) supprimé(s) dans {result.work_dir}.")  # type: ignore[attr-defined]
    elif not entries:
        logger.emit("info", f"Rien à nettoyer dans {result.work_dir}.")  # type: ignore[attr-defined]
    return EXIT_OK


def cmd_nvencc(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    action = args.nvencc_command
    if action == "status":
        name, payload = "nvencc.status", {"check_latest": bool(args.check)}
    elif action == "remove":
        name, payload = "nvencc.remove", {}
    else:
        name = "nvencc.install"
        payload = {"only_if_newer": action == "update"}
        if args.method:
            payload["method"] = args.method
        if args.no_sudo:
            payload["elevate"] = False
    with interrupt_guard(CancelToken()) as cancel:
        result, ctx = run_operation(name, payload, config=config, logger=logger, cancel=cancel)
    if _render(name, result, ctx, args):
        return EXIT_OK
    if name == "nvencc.status":
        data = result.to_dict()
        if not data["supported"]:
            print("NVEncC : plate-forme non prise en charge (Linux et Windows x86-64 seulement).")
            return EXIT_OK
        state = f"{data['path']} ({data['version'] or 'version inconnue'}, {data['provenance']})" if data["path"] \
            else "absent"
        print(f"NVEncC : {state} ; GPU NVIDIA : {'oui' if data['nvidia'] else 'non'} ; installation : {data['method']}")
        if data["latest_version"]:
            print(f"Dernière version : {data['latest_version']}"
                  + (" — mise à jour disponible (nvencc update)" if data["update_available"] else ""))
    else:
        logger.emit("info", str(getattr(result, "message", "")))
    return EXIT_OK
