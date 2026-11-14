"""Opération d'extraction : pistes, couches Dolby Vision / HDR10+ et pièces jointes d'une source."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.inspector import FileInspector
from core.jobs.extract import (
    ExtractionPlan,
    ExtractTools,
    check_outputs,
    extract,
    plan_all,
    plan_attachment,
    plan_track,
    required_tools,
    skipped_tracks,
    tool_runner,
)
from core.service.context import ExecutionContext
from core.service.errors import ApiError, ErrorCode
from core.service.models import Model, field
from core.service.operations import operation


@dataclass(frozen=True)
class ExtractItem(Model):
    """Élément à extraire : une piste (et l'une de ses couches) ou une pièce jointe."""

    track: str | None = field(default=None, description="Piste : index ffprobe (\"3\") ou nature et rang "
                                                         "(\"v:0\", \"a:1\", \"s:0\").")
    layer: Literal["stream", "el", "bl", "rpu", "hdr10plus"] = field(
        default="stream", description="Couche d'une piste vidéo HEVC : flux, EL / BL (Dolby Vision profil 7), "
                                      "RPU, HDR10+.")
    attachment: str | None = field(default=None, description="Pièce jointe : rang (`inspect`) ou nom de fichier.")
    output: Path | None = field(default=None, description="Fichier de sortie (défaut : nom proposé, output_dir).")


@dataclass(frozen=True)
class ExtractRequest(Model):
    """Extraction de pistes, couches HDR ou pièces jointes d'une source vers des fichiers autonomes."""

    source: Path = field(description="Fichier source.")
    items: tuple[ExtractItem, ...] = field(default=(), description="Éléments à extraire (exclusif avec all).")
    all: bool = field(default=False, description="Toutes les pistes extractibles et toutes les pièces jointes.")
    output_dir: Path | None = field(default=None, description="Dossier des sorties (défaut : celui de la source).")
    container: bool = field(default=False, description="Pistes en Matroska (.mkv / .mka / .mks) au lieu du flux "
                                                       "élémentaire.")
    force: bool = field(default=False, description="Remplacer les sorties existantes.")
    dry_run: bool = field(default=False, description="Lister les sorties prévues sans rien extraire.")


@dataclass(frozen=True)
class ExtractResult(Model):
    outputs: tuple[dict[str, Any], ...] = field(
        default=(), description="Une entrée par élément : nature, piste, couche, format, chemin.")
    dry_run: bool = False


def _plans(request: ExtractRequest, info: Any) -> list[tuple[ExtractionPlan, Path | None]]:
    if request.all == bool(request.items):
        raise ApiError(ErrorCode.INVALID_REQUEST, "Extraction : indiquer `items` ou `all`, pas les deux.")
    if request.all:
        return [(plan, None) for plan in plan_all(info, container=request.container)]
    plans: list[tuple[ExtractionPlan, Path | None]] = []
    for position, item in enumerate(request.items):
        if (item.track is None) == (item.attachment is None):
            raise ApiError(ErrorCode.INVALID_REQUEST, f"items[{position}] : `track` ou `attachment`, un seul.")
        if item.attachment is not None:
            if item.layer != "stream":
                raise ApiError(ErrorCode.INVALID_REQUEST, f"items[{position}] : `layer` réservé aux pistes.")
            plans.append((plan_attachment(info, item.attachment), item.output))
        else:
            assert item.track is not None
            plans.append((plan_track(info, item.track, layer=item.layer, container=request.container), item.output))
    return plans


@operation(
    "media.extract",
    request=ExtractRequest,
    result=ExtractResult,
    summary="Extraire des pistes (flux élémentaire ou Matroska), couches Dolby Vision (EL, BL, RPU), HDR10+ "
            "ou pièces jointes.",
    long_running=True,
    mutating=True,
    errors=(ErrorCode.VALIDATION, ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED,
            ErrorCode.BUSY),
)
def media_extract(request: ExtractRequest, ctx: ExecutionContext) -> ExtractResult:
    info = FileInspector(ffprobe_bin=ctx.tool("ffprobe"), mediainfo_bin=ctx.tool("mediainfo"),
                         cancel_event=ctx.cancel.event).inspect(request.source)
    planned = _plans(request, info)
    if request.all:
        for track in skipped_tracks(info, [plan for plan, _out in planned]):
            ctx.events.warning(f"Piste {track} non extractible sans OCR : ignorée.", code="extract_skipped")
    output_dir = Path(request.output_dir or Path(request.source).parent)
    targets = [(plan, out if out is not None else output_dir / plan.default_name) for plan, out in planned]
    check_outputs([target for _plan, target in targets], source=Path(request.source), force=request.force)
    results = [{**plan.describe(), "path": str(target)} for plan, target in targets]
    if request.dry_run:
        return ExtractResult(outputs=tuple(results), dry_run=True)
    tools = ExtractTools(ffmpeg=ctx.tool("ffmpeg"), dovi_tool=ctx.tool("dovi_tool"),
                         hdr10plus_tool=ctx.tool("hdr10plus"))
    required_tools([plan for plan, _target in targets], tools)
    total = len(targets)
    for position, (plan, target) in enumerate(targets):
        ctx.cancel.raise_if_cancelled()
        ctx.events.progress(pct=100.0 * position / total, stage=plan.label or plan.kind)
        ctx.events.log("info", f"Extraction {position + 1}/{total} : {plan.label or plan.kind} → {target}")

        def stage_pct(pct: int, done: int = position) -> None:
            ctx.events.progress(pct=(done + pct / 100.0) * 100.0 / total)

        extract(info, plan, target, tools=tools, run=tool_runner(ctx.cancel, stage_pct), force=request.force)
        ctx.events.artifact("output", str(target))
    ctx.events.progress(pct=100.0)
    return ExtractResult(outputs=tuple(results))
