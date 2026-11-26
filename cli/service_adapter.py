"""Adaptateur CLI de la couche de service (``core/service``).

Traduit arguments et documents en requêtes, exécute les opérations du catalogue, rend le
résultat (sortie historique, ou enveloppe JSON en ``--log-format events`` et ``call``) et
convertit les codes d'erreur publics en codes de sortie.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from core.jobs.json_io import json_default
from core.json_documents import parse_json_text, read_json_document
from core.service.catalog import CATALOG
from core.service.context import CancelToken, ExecutionContext, Overrides
from core.service.errors import ApiError, ErrorCode
from core.service.events import Event, EventType, JobState
from core.service.models import Model
from core.service.operations import API_VERSION, authorize, call, result_state

from cli.constants import (
    EXIT_ARGS,
    EXIT_EXISTS,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_TOOL,
    EXIT_VALIDATION,
    EXIT_WORKFLOW,
)
from cli.logging import Logger

#: Code de sortie du CLI pour chaque code d'erreur public (codes historiques conservés).
EXIT_BY_ERROR: Mapping[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: EXIT_ARGS,
    ErrorCode.FORBIDDEN: EXIT_VALIDATION,
    ErrorCode.UNKNOWN_OPERATION: EXIT_ARGS,
    ErrorCode.VALIDATION: EXIT_VALIDATION,
    ErrorCode.TOOL_MISSING: EXIT_TOOL,
    ErrorCode.OUTPUT_EXISTS: EXIT_EXISTS,
    ErrorCode.BUSY: EXIT_WORKFLOW,
    ErrorCode.WORKFLOW_FAILED: EXIT_WORKFLOW,
    ErrorCode.CANCELLED: EXIT_WORKFLOW,
    ErrorCode.PARTIAL: EXIT_PARTIAL,
}


def exit_code_for(error: ApiError) -> int:
    return EXIT_BY_ERROR.get(error.code, EXIT_WORKFLOW)


def exit_code_for_result(result: Model) -> int:
    """Code de sortie d'un résultat : lot partiel, en échec ou interrompu → ``EXIT_PARTIAL`` (règle historique)."""
    return EXIT_OK if result_state(result) is JobState.SUCCEEDED else EXIT_PARTIAL


class LoggerSink:
    """Rend les journaux et avertissements d'une opération par le ``Logger`` historique."""

    def __init__(self, logger: Logger) -> None:
        self._logger = logger

    def emit(self, event: Event) -> None:
        if event.type not in (EventType.LOG, EventType.WARNING):
            return
        data = dict(event.data)
        message = str(data.pop("message", ""))
        level = str(data.pop("level", "warning" if event.type is EventType.WARNING else "info"))
        self._logger.emit(level, message, **data)


def sink_for(logger: Logger) -> Any:
    event_sink = getattr(logger, "event_sink", None)  # journal minimal accepté (tests, appels internes)
    return event_sink if event_sink is not None else LoggerSink(logger)


def run_operation(
    name: str,
    payload: Mapping[str, Any],
    *,
    config: Any,
    logger: Logger,
    overrides: Overrides | None = None,
    ports: Mapping[str, Callable[..., Any]] | None = None,
    cancel: CancelToken | None = None,
    documents: tuple[Path, ...] = (),
) -> tuple[Model, ExecutionContext]:
    """Exécute une opération pour une commande historique (arguments déjà validés par argparse).

    Chemins de l'argv conservés tels quels (relatifs au cwd) : sorties historiques inchangées.
    Documents (``--config``, templates) tolérants : une clé hors contrat est signalée, jamais refusée.
    ``documents`` : documents lus avant l'opération (:func:`argv_documents`), protégés des annexes.
    """
    # Commande historique (pas une requête de l'API) : valeurs relatives depuis le dossier courant, paramètres libres.
    ctx = ExecutionContext.create(config, sink_for(logger), overrides=overrides, ports=ports, cancel=cancel,
                                  api=False, base_dir=Path.cwd(), allow_privileged=True,
                                  protected_paths=documents)
    result = call(CATALOG, name, dict(payload), ctx, allow_relative=True, strict_documents=False)
    return result, ctx


def envelope(name: str, job: str, *, result: Model | None = None, error: ApiError | None = None) -> dict[str, Any]:
    """Réponse publique d'un appel : statut, résultat ou erreur."""
    payload: dict[str, Any] = {"api_version": API_VERSION, "operation": name, "job": job}
    if error is None:
        payload["status"] = result_state(result).value if result is not None else JobState.SUCCEEDED.value
        payload["result"] = result.to_dict() if result is not None else None
    else:
        payload["status"] = "cancelled" if error.code is ErrorCode.CANCELLED else "failed"
        payload["error"] = error.to_dict()
    return payload


def print_json(payload: Any, *, stream=None) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default), file=stream or sys.stdout)


def overrides_from_args(args: argparse.Namespace) -> Overrides:
    work_dir = getattr(args, "work_dir", None)
    return Overrides(
        ffmpeg=getattr(args, "ffmpeg", None) or None,
        ffprobe=getattr(args, "ffprobe", None) or None,
        mediainfo=getattr(args, "mediainfo", None) or None,
        threads=getattr(args, "threads", None),
        work_dir=Path(work_dir).expanduser() if work_dir else None,
    )


def overrides_from_options(options: Any) -> Overrides:
    """Surcharges d'outils d'un ``CommonOptions`` (options historiques du CLI)."""
    return Overrides(
        ffmpeg=options.ffmpeg or None,
        ffprobe=options.ffprobe or None,
        mediainfo=options.mediainfo or None,
        threads=options.threads,
        work_dir=Path(options.work_dir).expanduser() if options.work_dir else None,
    )


#: Options de l'argv désignant un document lu avant l'opération (protégé des annexes).
_DOCUMENT_OPTIONS = ("config", "template", "batch", "request", "recipe", "calibration")


def argv_documents(args: argparse.Namespace) -> tuple[Path, ...]:
    """Documents de l'argv lus par la commande (``--config``, ``--template``, ``--batch``, ``--recipe``…)."""
    found: list[Path] = []
    for name in _DOCUMENT_OPTIONS:
        value = getattr(args, name, None)
        for item in value if isinstance(value, (list, tuple)) else [value]:
            if isinstance(item, (str, Path)) and str(item) not in ("", "-"):
                found.append(Path(item).expanduser())
    profile = getattr(args, "profile", None)
    if isinstance(profile, str) and profile and Path(profile).expanduser().is_file():
        found.append(Path(profile).expanduser())
    return tuple(found)


def _read_request(source: str | None) -> tuple[Any, Path]:
    """Requête JSON : fichier (chemins relatifs à son dossier) ou ``-`` (stdin, relatifs au cwd)."""
    if not source:
        return {}, Path.cwd()
    if source == "-":
        try:
            return parse_json_text(sys.stdin.read()), Path.cwd()
        except ValueError as exc:  # syntaxe, constante non finie, imbrication
            raise ApiError(ErrorCode.INVALID_REQUEST, f"Requête JSON illisible : {exc}") from exc
    path = Path(source).expanduser()
    try:
        return read_json_document(path), path.resolve().parent
    except (OSError, ValueError) as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, f"Requête JSON illisible ({path}) : {exc}") from exc


def cmd_call(args: argparse.Namespace, config: Any, logger: Logger) -> int:
    """``call <opération> --request FICHIER|-`` : contrat de l'API, événements sur stderr, réponse sur stdout."""
    name = str(args.operation)
    from cli.interrupts import interrupt_guard

    ctx = ExecutionContext.create(config, sink_for(logger), overrides=overrides_from_args(args),
                                  allow_privileged=bool(getattr(args, "allow_privileged", False)),
                                  protected_paths=argv_documents(args))
    try:
        authorize(CATALOG.get(name), ctx.allow_privileged)  # refus avant toute lecture du fichier ou de stdin
        payload, base_dir = _read_request(args.request)
        with interrupt_guard(ctx.cancel):  # Ctrl+C : annulation propre, réponse « cancelled »
            result = call(CATALOG, name, payload, ctx, strict=True, base_dir=base_dir)
    except BaseException as exc:  # noqa: BLE001 - toute erreur devient une réponse publique
        if isinstance(exc, (SystemExit, GeneratorExit)):
            raise
        error = ApiError.from_exception(exc)
        print_json(envelope(name, ctx.events.job, error=error))
        return exit_code_for(error)
    print_json(envelope(name, ctx.events.job, result=result))
    return exit_code_for_result(result)


def cmd_operations(args: argparse.Namespace, config: Any, logger: Logger) -> int:
    """Catalogue des opérations : liste, ou description complète d'une opération."""
    if args.describe:
        try:
            spec = CATALOG.get(args.describe)
        except ApiError as error:
            logger.emit("error", error.render())
            return exit_code_for(error)
        print_json({"name": spec.name, **spec.describe()})
        return EXIT_OK
    if args.json_output:
        print_json({
            "api_version": API_VERSION,
            "operations": [
                {"name": spec.name, "summary": spec.summary, "long_running": spec.long_running,
                 "mutating": spec.mutating, "uses_gpu": spec.uses_gpu}
                for spec in CATALOG
            ],
        })
        return EXIT_OK
    width = max((len(name) for name in CATALOG.names()), default=0)
    for spec in CATALOG:
        print(f"{spec.name.ljust(width)}  {spec.summary}")
    return EXIT_OK


def render_api_error(logger: Logger, error: ApiError) -> int:
    """Erreur publique d'une commande historique : message au journal (forme historique), code de sortie.

    Le code public reste disponible dans l'événement ``error`` (``--log-format events``).
    """
    if not error.logged:
        logger.emit("error", error.render())
    return exit_code_for(error)


__all__ = [
    "EXIT_BY_ERROR",
    "LoggerSink",
    "cmd_call",
    "cmd_operations",
    "envelope",
    "exit_code_for",
    "exit_code_for_result",
    "overrides_from_args",
    "overrides_from_options",
    "print_json",
    "render_api_error",
    "run_operation",
    "sink_for",
]
