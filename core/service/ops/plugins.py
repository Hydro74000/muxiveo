"""Opérations des extensions (moteur vidéo MVO, accélération TensorRT, reconstruction FEL).

Messages et codes de sortie identiques à la commande historique ``plugins`` : chaque échec
est publié au journal puis levé (``JobError`` marquée ``logged``, code de sortie conservé).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from core import plugins
from core.jobs.constants import EXIT_ARGS, EXIT_OK, EXIT_TOOL, EXIT_WORKFLOW
from core.jobs.errors import JobError
from core.service.context import ExecutionContext
from core.service.errors import ErrorCode
from core.service.models import Model, field
from core.service.operations import operation
from core.workflows.encode.interpolation import detect_gpu_acceleration

LABELS = {
    plugins.RIFE_PLUGIN_ID: "Moteur vidéo MVO (débruitage, interpolation)",
    plugins.TRT_PLUGIN_ID: "Accélération NVIDIA (TensorRT)",
    plugins.FEL_PLUGIN_ID: "Reconstruction Dolby Vision FEL",
}

PluginName = Literal["mvo-rife", "mvo-rife-trt", "mvo-fel"]


def plugin_status(spec: plugins.PluginSpec, config: Any, feed: list[dict] | None) -> dict[str, Any]:
    """État d'une extension : versions installée / disponible, compatibilité, chemins."""
    platform = plugins.platform_tag()
    installed = plugins.installed_plugin(spec)
    target = plugins.target_version(spec, feed, platform)
    status: dict[str, Any] = {
        "name": spec.id,
        "label": LABELS[spec.id],
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


def _fail(ctx: ExecutionContext, message: str, exit_code: int, *, level: str = "error") -> JobError:
    ctx.events.log(level, message)
    return JobError(message, exit_code, logged=True)


def _require_platform(ctx: ExecutionContext) -> None:
    if plugins.platform_tag() is None:
        raise _fail(ctx, "Extensions non prises en charge sur cette plate-forme.", EXIT_TOOL)


def _statuses(ctx: ExecutionContext, names: list[str]) -> dict[str, dict[str, Any]]:
    return {name: plugin_status(plugins.EXTENSIONS[name], ctx.config, plugins.fetch_feed(plugins.EXTENSIONS[name]))
            for name in names}


def _install(ctx: ExecutionContext, spec: plugins.PluginSpec, status: dict[str, Any], *, accept_license: bool,
             force: bool) -> dict[str, Any]:
    if not status["available_version"]:
        raise _fail(ctx, "Aucune version publiée compatible disponible pour cette extension.", EXIT_WORKFLOW)
    if spec is plugins.TRT and not accept_license:
        raise _fail(ctx, "Installation de NVIDIA TensorRT for RTX : licence à accepter avec --accept-license "
                         f"({plugins.TRT_LICENSE_URL}).", EXIT_ARGS)
    if not status["compatible"] and not force:
        reason = status["status"] or ("GPU NVIDIA Turing ou plus récent requis" if spec is plugins.TRT else "")
        raise _fail(ctx, f"Machine incompatible ({reason}) ; --force pour installer quand même.", EXIT_TOOL)
    display: Callable[[int, int], None] | None = ctx.ports.get("download_progress")

    def progress(done: int, total: int) -> None:
        if total > 0:
            ctx.events.progress(pct=round(done * 100 / total, 1), stage="download", name=spec.id)
        if display is not None:
            display(done, total)

    try:
        installed = plugins.install(spec, version=status["available_version"], progress=progress,
                                    cancel=ctx.cancel.event)
    except plugins.PluginCancelled:
        _end_display(ctx)
        raise  # code public « cancelled » (core.service.errors)
    except plugins.PluginError as exc:
        _end_display(ctx)
        raise _fail(ctx, f"Installation impossible : {exc}", EXIT_WORKFLOW) from exc
    _end_display(ctx)
    if spec is plugins.TRT:
        rife = getattr(ctx.config, "tool_muxiveo_rife", None) or ""
        failures = plugins.warm_up(rife, installed, cancel=ctx.cancel.event,
                                   log=lambda m: ctx.events.log("info", f"Préparation du moteur {m}")) if rife else []
        if failures:
            raise _fail(ctx, "Extension installée mais inutilisable ici : " + "; ".join(failures[:2]), EXIT_TOOL,
                        level="warning")
    ctx.events.log("info", f"{status['label']} {installed.version} installée dans {installed.path}.")
    ctx.events.artifact("plugin", str(installed.path), name=spec.id, version=installed.version)
    return {"name": spec.id, "version": installed.version, "path": str(installed.path)}


def _end_display(ctx: ExecutionContext) -> None:
    end = ctx.ports.get("download_progress_end")
    if end is not None:
        end()


@dataclass(frozen=True)
class PluginsListRequest(Model):
    """État des extensions."""

    names: tuple[PluginName, ...] = field(default=(), description="Extensions (défaut : toutes).")


@dataclass(frozen=True)
class PluginsListResult(Model):
    extensions: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PluginInstallRequest(Model):
    """Installation d'une extension (dernière version compatible)."""

    name: PluginName
    accept_license: bool = field(default=False, description="Licence NVIDIA TensorRT for RTX acceptée (mvo-rife-trt).")
    force: bool = field(default=False, description="Installer même si la machine semble incompatible.")


@dataclass(frozen=True)
class PluginInstallResult(Model):
    name: str
    version: str
    path: str


@dataclass(frozen=True)
class PluginUpdateRequest(Model):
    """Mise à jour d'une extension, ou de toutes celles installées."""

    name: PluginName | None = None


@dataclass(frozen=True)
class PluginUpdateResult(Model):
    """Un élément par extension installée : ``updated``, ``up_to_date`` ou ``failed`` (avec son code de sortie)."""

    extensions: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PluginRemoveRequest(Model):
    name: PluginName


@dataclass(frozen=True)
class PluginRemoveResult(Model):
    name: str
    complete: bool = field(description="Faux : extension désactivée, fichiers retirés au prochain lancement.")


_ERRORS = (ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED)


@operation("plugins.list", request=PluginsListRequest, result=PluginsListResult,
           summary="État des extensions (installées, compatibles, mises à jour).", errors=_ERRORS)
def plugins_list(request: PluginsListRequest, ctx: ExecutionContext) -> PluginsListResult:
    _require_platform(ctx)
    names: list[str] = list(request.names) or list(plugins.EXTENSIONS)
    return PluginsListResult(extensions=tuple(_statuses(ctx, names).values()))


@operation("plugins.install", request=PluginInstallRequest, result=PluginInstallResult,
           summary="Télécharger et installer une extension (TensorRT : licence NVIDIA à accepter).",
           long_running=True, progress=True, mutating=True, uses_gpu=True, errors=_ERRORS)
def plugins_install(request: PluginInstallRequest, ctx: ExecutionContext) -> PluginInstallResult:
    _require_platform(ctx)
    status = _statuses(ctx, [request.name])[request.name]
    done = _install(ctx, plugins.EXTENSIONS[request.name], status, accept_license=request.accept_license,
                    force=request.force)
    return PluginInstallResult(**done)


@operation("plugins.update", request=PluginUpdateRequest, result=PluginUpdateResult,
           summary="Mettre à jour une extension, ou toutes celles installées.", long_running=True, progress=True, mutating=True,
           uses_gpu=True, errors=_ERRORS)
def plugins_update(request: PluginUpdateRequest, ctx: ExecutionContext) -> PluginUpdateResult:
    _require_platform(ctx)
    names: list[str] = [request.name] if request.name else list(plugins.EXTENSIONS)
    installed = [s for s in _statuses(ctx, names).values() if s["installed_version"] is not None]
    if request.name and not installed:
        raise _fail(ctx, f"Extension non installée : muxiveo --cli plugins install {request.name}", EXIT_ARGS)
    items: list[dict[str, Any]] = []
    for status in installed:
        if not status["update_available"]:
            ctx.events.log("info", f"{status['label']} à jour ({status['installed_version']}).")
            items.append({"name": status["name"], "status": "up_to_date", "version": status["installed_version"],
                          "exit_code": EXIT_OK})
            continue
        try:
            # Licence déjà acceptée à l'installation ; compatibilité déjà établie.
            done = _install(ctx, plugins.EXTENSIONS[status["name"]], status, accept_license=True, force=True)
        except JobError as error:
            items.append({"name": status["name"], "status": "failed", "error": error.message,
                          "exit_code": error.exit_code})
            continue
        items.append({**done, "status": "updated", "exit_code": EXIT_OK})
    return PluginUpdateResult(extensions=tuple(items))


@operation("plugins.remove", request=PluginRemoveRequest, result=PluginRemoveResult,
           summary="Supprimer une extension (et le cache de ses moteurs).", mutating=True, errors=_ERRORS)
def plugins_remove(request: PluginRemoveRequest, ctx: ExecutionContext) -> PluginRemoveResult:
    _require_platform(ctx)
    spec = plugins.EXTENSIONS[request.name]
    complete = plugins.remove(spec)
    ctx.events.log("info" if complete else "warning", f"{LABELS[spec.id]} supprimée." if complete else
                   "Extension désactivée ; fichiers encore utilisés retirés au prochain lancement.")
    return PluginRemoveResult(name=spec.id, complete=complete)
