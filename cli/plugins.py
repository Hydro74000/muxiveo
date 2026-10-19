"""Commande ``plugins`` : extensions facultatives (interpolation mvo-rife, accélération NVIDIA mvo-rife-trt)."""

from __future__ import annotations

import argparse
import json
import sys

from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_TOOL, EXIT_WORKFLOW
from cli.logging import Logger
from core import plugins
from core.config import AppConfig
from core.workflows.encode.interpolation import detect_gpu_acceleration

_LABELS = {
    plugins.RIFE_PLUGIN_ID: "Interpolation d'images (MVO-RIFE)",
    plugins.TRT_PLUGIN_ID: "Accélération NVIDIA (TensorRT)",
    plugins.FEL_PLUGIN_ID: "Reconstruction Dolby Vision FEL",
}


def _status(spec: plugins.PluginSpec, config: AppConfig, feed: list[dict] | None) -> dict:
    platform = plugins.platform_tag()
    installed = plugins.installed_plugin(spec)
    target = plugins.target_version(spec, feed, platform)
    status = {
        "name": spec.id,
        "label": _LABELS[spec.id],
        "platform": platform,
        "pinned_version": spec.min_version,
        "available_version": target,
        "installed_version": installed.version if installed else None,
        "path": str(installed.path) if installed else None,
        "update_available": plugins.update_available(spec, target),
        "compatible": spec.supports(platform),
        "status": "" if spec.supports(platform) else "plate-forme non prise en charge",
    }
    if spec is plugins.TRT:
        rife = getattr(config, "tool_muxiveo_rife", None) or ""
        trt = detect_gpu_acceleration(rife, trt_plugin=str(installed.path) if installed else "").trt
        status.update(
            enabled=bool(getattr(config, "trt_enabled", True)), compatible=trt.compatible, ready=trt.ready,
            device=trt.device, status=trt.reason, engine_cache=str(plugins.trt_engine_cache_dir()),
        )
    elif spec is plugins.FEL:
        status["library"] = str(plugins.main_file(spec, installed)) if installed else None
    else:
        status["executable"] = str(plugins.main_file(spec, installed)) if installed else None
    return status


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


def _install(spec: plugins.PluginSpec, args: argparse.Namespace, config: AppConfig, logger: Logger, status: dict) -> int:
    if not status["available_version"]:
        logger.emit("error", "Aucune version publiée compatible disponible pour cette extension.")
        return EXIT_WORKFLOW
    if spec is plugins.TRT and not args.accept_license:
        logger.emit("error", "Installation de NVIDIA TensorRT for RTX : licence à accepter avec --accept-license "
                             f"({plugins.TRT_LICENSE_URL}).")
        return EXIT_ARGS
    if not status["compatible"] and not args.force:
        reason = status["status"] or ("GPU NVIDIA Turing ou plus récent requis" if spec is plugins.TRT else "")
        logger.emit("error", f"Machine incompatible ({reason}) ; --force pour installer quand même.")
        return EXIT_TOOL
    progress = _Progress()
    try:
        installed = plugins.install(spec, version=status["available_version"], progress=progress)
    except plugins.PluginError as exc:
        progress.end()
        logger.emit("error", f"Installation impossible : {exc}")
        return EXIT_WORKFLOW
    progress.end()
    if spec is plugins.TRT:
        rife = getattr(config, "tool_muxiveo_rife", None) or ""
        failures = plugins.warm_up(rife, installed, log=lambda m: logger.emit("info", f"Préparation du moteur {m}")) if rife else []
        if failures:
            logger.emit("warning", "Extension installée mais inutilisable ici : " + "; ".join(failures[:2]))
            return EXIT_TOOL
    logger.emit("info", f"{status['label']} {installed.version} installée dans {installed.path}.")
    return EXIT_OK


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
    """list | install | update | remove des extensions."""
    if plugins.platform_tag() is None:
        logger.emit("error", "Extensions non prises en charge sur cette plate-forme.")
        return EXIT_TOOL
    action = args.plugins_command
    names = [args.name] if getattr(args, "name", None) else list(plugins.EXTENSIONS)
    if action == "remove":
        spec = plugins.EXTENSIONS[args.name]
        complete = plugins.remove(spec)
        logger.emit("info" if complete else "warning", f"{_LABELS[spec.id]} supprimée." if complete else
                    "Extension désactivée ; fichiers encore utilisés retirés au prochain lancement.")
        return EXIT_OK
    statuses = {name: _status(plugins.EXTENSIONS[name], config, plugins.fetch_feed(plugins.EXTENSIONS[name]))
                for name in names}
    if action == "list":
        for status in statuses.values():
            if args.log_format == "jsonl":
                print(json.dumps(status, ensure_ascii=False))
            else:
                _print_status(status)
        return EXIT_OK
    if action == "install":
        return _install(plugins.EXTENSIONS[args.name], args, config, logger, statuses[args.name])
    # update : extensions nommées, sinon toutes celles installées
    code = EXIT_OK
    installed = [s for s in statuses.values() if s["installed_version"] is not None]
    if args.name and not installed:
        logger.emit("error", f"Extension non installée : muxiveo --cli plugins install {args.name}")
        return EXIT_ARGS
    for status in installed:
        if not status["update_available"]:
            logger.emit("info", f"{status['label']} à jour ({status['installed_version']}).")
            continue
        args.accept_license = True  # licence déjà acceptée à l'installation
        args.force = True
        code = max(code, _install(plugins.EXTENSIONS[status["name"]], args, config, logger, status))
    return code


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
        action.add_argument("--log-format", choices=("text", "jsonl"), default="text")
        if name == "install":
            action.add_argument("--accept-license", action="store_true",
                                help=f"Accepter la licence NVIDIA TensorRT for RTX ({plugins.TRT_LICENSE_URL}).")
            action.add_argument("--force", action="store_true", help="Installer même si la machine semble incompatible.")
        action.set_defaults(func=cmd_plugins, accept_license=False, force=False)
