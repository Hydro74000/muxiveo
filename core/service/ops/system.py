"""Opérations système : version de l'application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.service.context import ExecutionContext
from core.service.models import Model, field
from core.service.operations import operation
from core.update_check import UpdateCheckError, normalize_update_channel, query_latest_release
from core.version import APP_BUILD_VERSION, APP_NAME


@dataclass(frozen=True)
class VersionRequest(Model):
    """Version de Muxiveo, avec comparaison facultative à la dernière release."""

    check: bool = field(default=False, description="Comparer avec la dernière release du canal (accès réseau).")
    channel: Literal["stable", "unstable"] | None = field(
        default=None, description="Canal de mise à jour ; défaut : réglage [ui] update_channel.")


@dataclass(frozen=True)
class VersionResult(Model):
    """Version installée et, si demandé, état de la mise à jour."""

    name: str
    version: str
    channel: str | None = field(default=None, description="Canal comparé (seulement avec check).")
    latest: str | None = field(default=None, description="Dernière version publiée sur le canal.")
    update_available: bool | None = None
    url: str | None = field(default=None, description="Page de la release.")
    check_failed: bool = field(default=False, description="Vérification demandée mais impossible.")
    check_error: str | None = None


@operation(
    "system.version",
    request=VersionRequest,
    result=VersionResult,
    summary="Version de Muxiveo ; avec check, comparaison à la dernière release du canal.",
)
def system_version(request: VersionRequest, ctx: ExecutionContext) -> VersionResult:
    if not request.check:
        return VersionResult(name=APP_NAME, version=APP_BUILD_VERSION)
    channel = normalize_update_channel(request.channel or getattr(ctx.config, "update_channel", None))
    query = ctx.port("query_latest_release", query_latest_release)
    try:
        info = query(channel)
    except UpdateCheckError as exc:
        ctx.events.log("error", f"Impossible de joindre GitHub pour vérifier les mises à jour : {exc}")
        return VersionResult(name=APP_NAME, version=APP_BUILD_VERSION, channel=channel, check_failed=True,
                             check_error=str(exc))
    if info is None:
        return VersionResult(name=APP_NAME, version=APP_BUILD_VERSION, channel=channel, check_failed=True)
    return VersionResult(name=APP_NAME, version=APP_BUILD_VERSION, channel=channel, latest=info.version,
                         update_available=info.is_newer, url=info.url)
