"""Opérations média : inspection des sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.inspector import FileInspector
from core.jobs.inspection import config_template_from_info
from core.jobs.serializers import serialize_file_info
from core.service.context import ExecutionContext
from core.service.errors import ErrorCode
from core.service.models import Model, field
from core.service.operations import operation


@dataclass(frozen=True)
class InspectRequest(Model):
    """Inspection rapide (ffprobe + mediainfo) d'une ou plusieurs sources."""

    inputs: tuple[Path, ...] = field(min_items=1, description="Fichiers à inspecter.")
    config_template: bool = field(
        default=False, description="Renvoyer un exact-job minimal de la première source au lieu des fiches.")
    output: str | None = field(
        default=None, description="Sortie inscrite dans le modèle de job (avec config_template).")


@dataclass(frozen=True)
class InspectResult(Model):
    """Fiches des sources, ou modèle de job."""

    files: tuple[dict[str, Any], ...] = field(default=(), description="Une fiche par source (pistes, HDR, chapitres…).")
    template: dict[str, Any] | None = field(default=None, description="Exact-job minimal (config_template).")


@operation(
    "media.inspect",
    request=InspectRequest,
    result=InspectResult,
    summary="Inspecter des sources : pistes, HDR, chapitres, pièces jointes ; ou générer un exact-job minimal.",
    errors=(ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED),
)
def media_inspect(request: InspectRequest, ctx: ExecutionContext) -> InspectResult:
    inspector = FileInspector(ffprobe_bin=ctx.tool("ffprobe"), mediainfo_bin=ctx.tool("mediainfo"))
    infos = []
    for path in request.inputs:
        ctx.cancel.raise_if_cancelled()
        infos.append(inspector.inspect(path))
    if request.config_template:
        return InspectResult(template=config_template_from_info(infos[0], output=request.output or ""))
    return InspectResult(files=tuple(serialize_file_info(info) for info in infos))
