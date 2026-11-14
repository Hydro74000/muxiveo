"""Opération ``app.update_install`` : installer la dernière version (installation intégrée de l'interface).

Même déroulé que la fenêtre de mise à jour, sans relancer l'interface : AppImage remplacée tout de suite
(``installed``) ; installeur Windows ou échange de dossiers (archive source, application macOS) confié à un
processus qui attend la fin de celui-ci (``scheduled``) — le CLI quitte aussitôt, le compte rendu est relu
au démarrage suivant (``previous``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from core.jobs.constants import EXIT_ARGS, EXIT_VALIDATION, EXIT_WORKFLOW
from core.jobs.errors import JobError
from core.service.context import ExecutionContext
from core.service.errors import ApiError, ErrorCode
from core.service.models import Model, field
from core.service.operations import operation

_BLOCKERS = {
    "not-writable": "dossier de l'installation non modifiable",
    "translocated": "application macOS lancée hors du dossier Applications",
    "python-inside": "Python situé dans le dossier des sources (Windows)",
    "no-hdiutil": "hdiutil / ditto introuvables",
    "no-marker": "archive source antérieure au marqueur d'installation : remplacer une fois à la main",
}


@dataclass(frozen=True)
class UpdateInstallRequest(Model):
    """Installation de la dernière version du canal."""

    channel: Literal["stable", "unstable"] | None = field(default=None, description="Défaut : [ui] update_channel.")
    confirm: bool = field(default=False, description="Confirmation explicite (installation non interactive).")


@dataclass(frozen=True)
class UpdateInstallResult(Model):
    status: Literal["up_to_date", "installed", "scheduled"]
    current: str
    latest: str | None = None
    kind: str = ""
    previous: tuple[dict[str, Any], ...] = field(default=(), description="Comptes rendus des mises à jour passées.")
    message: str = ""


@operation("app.update_install", request=UpdateInstallRequest, result=UpdateInstallResult, long_running=True,
           mutating=True, summary="Télécharger et installer la dernière version (sans relancer l'interface).",
           errors=(ErrorCode.INVALID_REQUEST, ErrorCode.VALIDATION, ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED))
def app_update_install(request: UpdateInstallRequest, ctx: ExecutionContext) -> UpdateInstallResult:
    from core import update_install as up
    from core.update_check import query_latest_release
    from core.version import APP_VERSION

    kind = ctx.port("install_kind", up.detect_install_kind)()
    previous = tuple(ctx.port("collect_update_results", up.collect_update_results)(kind))
    channel = request.channel or str(getattr(ctx.config, "update_channel", "stable") or "stable")
    info = ctx.port("query_latest_release", query_latest_release)(channel)
    if info is None:
        raise JobError("Dernière version introuvable (réseau ou flux de mise à jour indisponible).", EXIT_WORKFLOW)
    if not info.is_newer:
        return UpdateInstallResult(status="up_to_date", current=APP_VERSION, latest=info.version, kind=kind.value,
                                   previous=previous, message=f"À jour ({channel} : {info.version}).")
    if kind not in up.SELF_INSTALL_KINDS:
        hint = up.manual_update_hint()
        raise JobError("Installation intégrée indisponible pour ce mode d'installation"
                       + (f" : {hint}" if hint else f" ; télécharger depuis {info.url}"), EXIT_VALIDATION)
    blocker = ctx.port("self_install_blocker", up.self_install_blocker)(kind)
    if blocker:
        raise JobError(f"Installation intégrée impossible : {_BLOCKERS.get(blocker, blocker)}.", EXIT_VALIDATION)
    if not up.can_self_update(info, kind):
        raise JobError(f"Aucun paquet compatible dans la release {info.version} ({info.url}).", EXIT_VALIDATION)
    if not request.confirm:
        raise JobError(f"Mise à jour {info.version} disponible : confirmer l'installation (--yes).", EXIT_ARGS)

    def progress(done: int, total: int) -> None:
        if total > 0:
            ctx.events.progress(pct=round(done * 100 / total, 1), stage=f"Téléchargement de {info.version}")

    try:
        downloaded = ctx.port("download_update", up.download_update)(
            info, kind, progress, lambda: ctx.cancel.cancelled, relaunch=False)
        if ctx.cancel.cancelled:
            raise ApiError(ErrorCode.CANCELLED, "Mise à jour annulée.")
        status = ctx.port("apply_update", up.apply_update)(kind, downloaded, relaunch=False)
    except up.UpdateInstallError as exc:
        if ctx.cancel.cancelled:
            raise ApiError(ErrorCode.CANCELLED, "Mise à jour annulée.") from exc
        raise JobError(f"Mise à jour impossible : {exc}", EXIT_WORKFLOW) from exc
    message = (f"Version {info.version} installée." if status == up.APPLY_INSTALLED
               else f"Version {info.version} prête : installation à la fin de ce processus "
                    "(compte rendu au prochain démarrage).")
    return UpdateInstallResult(status=status, current=APP_VERSION, latest=info.version, kind=kind.value,
                               previous=previous, message=message)
