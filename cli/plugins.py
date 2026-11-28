"""Commande ``plugins`` : extensions facultatives (interpolation mvo-rife, accélération NVIDIA mvo-rife-trt)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any
import json
import sys

from cli.constants import EXIT_OK, LOG_FORMATS
from cli.logging import Logger
from cli.service_adapter import envelope, print_json, render_api_error, run_operation
from core import plugins
from core.config import AppConfig
from core.service.errors import ApiError

class _Progress:
    """Progression du téléchargement : ligne réécrite sur un terminal, palier de 10 % sinon."""

    def __init__(self) -> None:
        self._last = -1
        self._tty = sys.stderr.isatty()

    def __call__(self, done: int, total: int) -> None:
        if total <= 0:
            return
        percent = done * 100 // total
        step = percent if self._tty else percent - percent % 10
        if step == self._last:
            return
        self._last = step
        sys.stderr.write(f"\rTéléchargement : {step} %" if self._tty else f"Téléchargement : {step} %\n")
        sys.stderr.flush()

    def end(self) -> None:
        if self._tty and self._last >= 0:
            sys.stderr.write("\n")


def _print_status(status: dict) -> None:
    state = f"installée ({status['installed_version']})" if status["installed_version"] else "non installée"
    print(f"{status['label']} [{status['name']}] : {state} ; version disponible {status['available_version']}")
    if status["name"] == plugins.TRT_PLUGIN_ID:
        detail = "" if status["ready"] or status["status"] in ("", "compatible") else f" ({status['status']})"
        print(f"  GPU : {status['device'] or '?'} — {'compatible' if status['compatible'] else 'incompatible'}"
              f"{' — active' if status['ready'] else ''}{detail}")
    elif not status["compatible"]:
        print(f"  {status['status']}")
    if status["update_available"]:
        print(f"  Mise à jour disponible : muxiveo --cli plugins update {status['name']}")


def cmd_plugins(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    """list | install | update | remove des extensions (opérations ``plugins.*``)."""
    action = args.plugins_command
    progress = _Progress()
    ports: dict[str, Callable[..., Any]] = {"download_progress": progress, "download_progress_end": progress.end}
    name = getattr(args, "name", None)
    payload: dict = {}
    if action == "list":
        payload = {"names": [name]} if name else {}
    elif action == "install":
        payload = {"name": name, "accept_license": bool(args.accept_license), "force": bool(args.force)}
    elif action in ("update", "remove"):
        payload = {"name": name} if name else {}
    try:
        result, ctx = run_operation(f"plugins.{action}", payload, config=config, logger=logger, ports=ports)
    except ApiError as error:
        return render_api_error(logger, error)
    if args.log_format == "events":
        print_json(envelope(f"plugins.{action}", ctx.events.job, result=result))
    if action == "list" and args.log_format != "events":
        for status in getattr(result, "extensions", ()):
            if args.log_format == "jsonl":
                print(json.dumps(status, ensure_ascii=False))
            else:
                _print_status(status)
    if action == "update":
        return max((int(item.get("exit_code", EXIT_OK)) for item in getattr(result, "extensions", ())),
                   default=EXIT_OK)
    return EXIT_OK


def add_plugins_parser(sub: argparse._SubParsersAction) -> None:
    """Sous-commande ``plugins``."""
    parser = sub.add_parser("plugins", help="Extensions facultatives (interpolation, accélération NVIDIA TensorRT).")
    actions = parser.add_subparsers(dest="plugins_command", required=True)
    names = list(plugins.EXTENSIONS)
    for name, help_text in (
        ("list", "État des extensions (installées, compatibles, mises à jour)."),
        ("install", "Télécharger et installer une extension (TensorRT : licence NVIDIA à accepter)."),
        ("update", "Mettre à jour une extension, ou toutes celles installées."),
        ("remove", "Supprimer une extension (et le cache de ses moteurs)."),
    ):
        action = actions.add_parser(name, help=help_text)
        if name in ("install", "remove"):
            action.add_argument("name", choices=names, help="Extension : " + ", ".join(names) + ".")
        else:
            action.add_argument("name", nargs="?", choices=names, help="Extension (défaut : toutes).")
        action.add_argument("--log-format", choices=LOG_FORMATS, default="text")
        if name == "install":
            action.add_argument("--accept-license", action="store_true",
                                help=f"Accepter la licence NVIDIA TensorRT for RTX ({plugins.TRT_LICENSE_URL}).")
            action.add_argument("--force", action="store_true", help="Installer même si la machine semble incompatible.")
        action.set_defaults(func=cmd_plugins, accept_license=False, force=False)
