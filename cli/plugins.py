"""Commande ``plugins`` : extensions facultatives (accélération NVIDIA TensorRT de MVO-RIFE), sans interface."""

from __future__ import annotations

import argparse
import json
import sys

from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_TOOL, EXIT_WORKFLOW
from cli.logging import Logger
from core import plugins
from core.config import AppConfig
from core.version import MVO_RIFE_TRT_VERSION
from core.workflows.encode.interpolation import detect_gpu_acceleration


def _status(config: AppConfig) -> dict:
    installed = plugins.installed_plugin()
    rife = getattr(config, "tool_muxiveo_rife", None) or ""
    trt = detect_gpu_acceleration(rife, trt_plugin=str(installed.path) if installed else "").trt
    return {
        "name": plugins.TRT_PLUGIN_ID,
        "label": "Accélération NVIDIA (TensorRT)",
        "platform": plugins.platform_tag(),
        "pinned_version": MVO_RIFE_TRT_VERSION,
        "installed_version": installed.version if installed else None,
        "path": str(installed.path) if installed else None,
        "enabled": bool(getattr(config, "trt_enabled", True)),
        "compatible": trt.compatible,
        "ready": trt.ready,
        "device": trt.device,
        "status": trt.reason,
        "update_available": bool(installed and installed.version != MVO_RIFE_TRT_VERSION),
        "engine_cache": str(plugins.trt_engine_cache_dir()),
    }


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


def _install(args: argparse.Namespace, config: AppConfig, logger: Logger, status: dict) -> int:
    if not args.accept_license:
        logger.emit("error", "Installation de NVIDIA TensorRT for RTX : licence à accepter avec --accept-license "
                             f"({plugins.TRT_LICENSE_URL}).")
        return EXIT_ARGS
    if not status["compatible"] and not args.force:
        logger.emit("error", f"Machine incompatible ({status['status'] or 'GPU NVIDIA Turing ou plus récent requis'}) ; "
                             "--force pour installer quand même.")
        return EXIT_TOOL
    progress = _Progress()
    try:
        installed = plugins.install(progress=progress)
    except plugins.PluginError as exc:
        progress.end()
        logger.emit("error", f"Installation impossible : {exc}")
        return EXIT_WORKFLOW
    progress.end()
    rife = getattr(config, "tool_muxiveo_rife", None) or ""
    failures = plugins.warm_up(rife, installed, log=lambda m: logger.emit("info", f"Préparation du moteur {m}")) if rife else []
    if failures:
        logger.emit("warning", "Extension installée mais inutilisable ici : " + "; ".join(failures[:2]))
        return EXIT_TOOL
    logger.emit("info", f"Accélération NVIDIA (TensorRT) {installed.version} installée dans {installed.path}.")
    return EXIT_OK


def cmd_plugins(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    """list | install | update | remove de l'accélération NVIDIA (TensorRT)."""
    if plugins.platform_tag() is None:
        logger.emit("error", "Extensions non prises en charge sur cette plate-forme (Linux ou Windows x86-64 requis).")
        return EXIT_TOOL
    action = args.plugins_command
    if action == "remove":
        complete = plugins.remove()
        logger.emit("info" if complete else "warning", "Accélération NVIDIA (TensorRT) supprimée." if complete else
                    "Extension désactivée ; fichiers encore utilisés retirés au prochain lancement.")
        return EXIT_OK
    status = _status(config)
    if action == "list":
        if args.log_format == "jsonl":
            print(json.dumps(status, ensure_ascii=False))
        else:
            state = f"installée ({status['installed_version']})" if status["installed_version"] else "non installée"
            print(f"{status['label']} [{status['name']}] : {state} ; version épinglée {status['pinned_version']}")
            detail = "" if status["ready"] or status["status"] in ("", "compatible") else f" ({status['status']})"
            print(f"  GPU : {status['device'] or '?'} — {'compatible' if status['compatible'] else 'incompatible'}"
                  f"{' — active' if status['ready'] else ''}{detail}")
            if status["update_available"]:
                print("  Mise à jour disponible : muxiveo --cli plugins update")
        return EXIT_OK
    if action == "update":
        if status["installed_version"] is None:
            logger.emit("error", "Extension non installée : muxiveo --cli plugins install --accept-license")
            return EXIT_ARGS
        if not status["update_available"]:
            logger.emit("info", f"Accélération NVIDIA (TensorRT) à jour ({status['installed_version']}).")
            return EXIT_OK
        args.accept_license = True  # licence déjà acceptée à l'installation
        args.force = True
    return _install(args, config, logger, status)


def add_plugins_parser(sub: argparse._SubParsersAction) -> None:
    """Sous-commande ``plugins``."""
    parser = sub.add_parser("plugins", help="Extensions facultatives (accélération NVIDIA TensorRT de MVO-RIFE).")
    actions = parser.add_subparsers(dest="plugins_command", required=True)
    for name, help_text in (
        ("list", "État de l'extension (installée, compatible, active)."),
        ("install", "Télécharger et installer l'extension (licence NVIDIA à accepter)."),
        ("update", "Mettre à jour vers la version épinglée par Muxiveo."),
        ("remove", "Supprimer l'extension et le cache de ses moteurs."),
    ):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("--log-format", choices=("text", "jsonl"), default="text")
        if name == "install":
            action.add_argument("--accept-license", action="store_true",
                                help=f"Accepter la licence NVIDIA TensorRT for RTX ({plugins.TRT_LICENSE_URL}).")
            action.add_argument("--force", action="store_true", help="Installer même si le GPU semble incompatible.")
        action.set_defaults(func=cmd_plugins, accept_license=False, force=False)
