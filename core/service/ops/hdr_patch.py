"""Opération ``hdr.patch`` : correction des métadonnées HDR sans réencodage (onglet Édition du HDR Studio).

La requête décrit les corrections (valeurs saisies ou proposées) ; le service relit le fichier (métadonnées
dynamiques, mesure de l'image en cache ou lancée), résout le plan comme l'onglet Édition (valeurs proposées,
« Garder identiques » MDCV ↔ L6 ↔ écran du RPU, propositions de réalignement retenues) puis l'exécute par
``MetadataPatcher`` : flux extrait, outils, blocs vidéo remplacés dans une copie, publication protégée.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.hdr_inspection import InspectionCache, InspectionReport, snapshot_report
from core.hdr_inspection.measure import ImageMeasurement, frame_count_check, packet_counter
from core.hdr_inspection.patch import (
    L11_CONTENT_TYPES,
    Hdr10PlusEdit,
    MasteringEdit,
    MetadataPatcher,
    analyze_rpu_gaps,
    rpu_completion_warning,
    PatchCancelled,
    PatchError,
    PatchPlan,
    RpuEdit,
    StaticEdit,
    suggested_mastering,
)
from core.hdr_inspection.realign import REALIGN_KEYS, Realignment, chosen_edits, realign, recompute_scenes
from core.hdr_inspection.snapshot import PRIMARY_SETS
from core.hdr_inspection.tonemap import suggested_light_levels
from core.inspector import FileInspector
from core.jobs.constants import EXIT_ARGS, EXIT_EXISTS, EXIT_VALIDATION, EXIT_WORKFLOW
from core.jobs.errors import JobError
from core.jobs.logging import EmitterLogger
from core.output_commit import ExistingPolicy, OutputBusyError, OutputCommitError, OutputRefusedError
from core.service.context import ExecutionContext
from core.service.errors import ApiError, ErrorCode
from core.service.models import Model, field
from core.service.operations import operation
from core.service.ops.hdr import dynamic_report, load_checked_measurement, measure_image, refuse_frame_mismatch
from core.workdir import create_process_work_dir

_PATCHABLE = {".mkv", ".hevc", ".h265", ".265"}


# ---------------------------------------------------------------------------
# Requête
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LightLevelsPatch(Model):
    """MaxCLL / MaxFALL écrits dans le flux (SEI) et le conteneur (Colour)."""

    mode: Literal["manual", "suggested"] = field(
        default="manual", description="manual : valeurs saisies ; suggested : mesure > HDR10+ > L1 (onglet Luminance).")
    max_cll: int | None = field(default=None, minimum=0, maximum=65535, description="MaxCLL (nits).")
    max_fall: int | None = field(default=None, minimum=0, maximum=65535, description="MaxFALL (nits).")


@dataclass(frozen=True)
class MasteringPatch(Model):
    """Écran de mastering (MDCV, SMPTE ST 2086)."""

    mode: Literal["manual", "suggested"] = field(
        default="manual", description="manual : valeurs saisies ; suggested : L6 > écran du RPU > écran usuel.")
    primaries: Literal["Display P3", "BT.2020", "BT.709"] | None = field(
        default=None, description="Primaires (défaut : celles du fichier, sinon Display P3).")
    min_nits: float | None = field(default=None, minimum=0, maximum=50, description="Luminance min. (nits).")
    max_nits: float | None = field(default=None, minimum=1, maximum=10000, description="Luminance max. (nits).")


@dataclass(frozen=True)
class L6Patch(Model):
    """L6 Dolby Vision (HDR10 porté par le RPU)."""

    max_mastering_nits: int = field(minimum=1, maximum=10000)
    min_mastering_nits: float = field(minimum=0, maximum=1)
    max_cll: int = field(minimum=0, maximum=10000)
    max_fall: int = field(minimum=0, maximum=10000)


@dataclass(frozen=True)
class ActiveAreaPatch(Model):
    """Zone active L5 de toutes les images (pixels retirés de chaque bord)."""

    left: int = field(default=0, minimum=0)
    right: int = field(default=0, minimum=0)
    top: int = field(default=0, minimum=0)
    bottom: int = field(default=0, minimum=0)


@dataclass(frozen=True)
class L11Patch(Model):
    """L11 Dolby Vision : type de contenu."""

    content_type: int = field(minimum=0, maximum=max(L11_CONTENT_TYPES),
                              description="0 défaut, 1 cinéma, 2 jeu, 3 sport, 4 contenu utilisateur.")
    whitepoint: int = field(default=0, minimum=0, maximum=15)
    reference_mode: bool = False


@dataclass(frozen=True)
class NitsRange(Model):
    min_nits: float = field(minimum=0)
    max_nits: float = field(minimum=1, maximum=10000)


@dataclass(frozen=True)
class RpuPatch(Model):
    """Corrections du RPU Dolby Vision (``dovi_tool editor``)."""

    l6: L6Patch | None = None
    active_area: ActiveAreaPatch | None = None
    l11: L11Patch | None = None
    source_display: NitsRange | None = field(default=None, description="Écran de mastering du RPU (min/max PQ).")
    remove_cmv4: bool = field(default=False, description="Retirer les métadonnées CM v4.0 (RPU CM v4.0).")


@dataclass(frozen=True)
class Hdr10PlusPatch(Model):
    """Corrections du HDR10+."""

    remove: bool = field(default=False, description="Retirer le HDR10+ (exclut les autres options).")
    targeted_display_nits: int | None = field(default=None, minimum=1, maximum=10000,
                                              description="TargetedSystemDisplayMaximumLuminance de toutes les images.")
    profile_a: bool = field(default=False, description="Retirer la courbe de Bézier (profil A).")
    cap_nits: float | None = field(default=None, minimum=1, maximum=10000,
                                   description="Plafond des MaxScl, AverageRGB et percentiles.")
    recompute: bool = field(default=False, description="Recalculer le HDR10+ depuis la mesure complète (profil A).")


@dataclass(frozen=True)
class HdrPatchRequest(Model):
    """Correction des métadonnées HDR d'une piste vidéo (MKV ou HEVC brut) vers un nouveau fichier."""

    source: Path = field(description="Fichier source (MKV ou HEVC brut).")
    output: Path = field(description="Fichier corrigé (différent de la source).")
    track: int | None = field(default=None, description="Index ffprobe de la piste vidéo (défaut : première).")
    light_levels: LightLevelsPatch | None = None
    mastering: MasteringPatch | None = None
    rpu: RpuPatch | None = None
    hdr10plus: Hdr10PlusPatch | None = None
    realign: tuple[Literal["all", "static", "mdcv", "l6", "hdr10plus"], ...] = field(
        default=(), description="Propositions de réalignement d'après la mesure à retenir (all : toutes).")
    measurement: Literal["cached", "fast", "full"] = field(
        default="cached", description="Mesure de l'image : en cache seulement, ou lancée (rapide / complète).")
    display_peak: float | None = field(default=None, minimum=100, maximum=10000,
                                       description="Écran simulé du réalignement (défaut : dernier pic choisi dans HDR Studio, sinon 1000).")
    sync_mastering: bool = field(
        default=True, description="Dolby Vision : MDCV, mastering du L6 et écran du RPU gardés identiques.")
    force: bool = field(default=False, description="Remplacer la sortie si elle existe.")
    dry_run: bool = field(default=False, description="Résoudre et renvoyer le plan sans écrire.")


@dataclass(frozen=True)
class HdrPatchResult(Model):
    output: str | None = None
    dry_run: bool = False
    changes: dict[str, Any] = field(default_factory=dict, description="Plan résolu (valeurs écrites).")
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Résolution du plan
# ---------------------------------------------------------------------------


def _invalid(message: str) -> JobError:
    return JobError(message, EXIT_VALIDATION)


def _file_primaries(report: InspectionReport) -> str:
    mastering = report.snapshot.mastering
    return mastering.primaries if mastering is not None and mastering.primaries in PRIMARY_SETS else "Display P3"


def _light_levels(patch: LightLevelsPatch, report: InspectionReport,
                  measurement: ImageMeasurement | None) -> tuple[int, int]:
    cll, fall = patch.max_cll, patch.max_fall
    if patch.mode == "suggested":
        suggested = suggested_light_levels(report, measurement)
        if suggested is None:
            raise _invalid("MaxCLL/MaxFALL proposés : ni mesure, ni HDR10+, ni RPU à exploiter.")
        cll = suggested[0] if cll is None else cll
        fall = (suggested[1] if suggested[1] is not None else report.snapshot.max_fall) if fall is None else fall
    if cll is None or fall is None:
        raise _invalid("MaxCLL et MaxFALL requis (MaxFALL inconnue d'après le L1 : la saisir ou mesurer l'image).")
    return int(cll), int(fall)


def _mastering(patch: MasteringPatch, report: InspectionReport, measurement: ImageMeasurement | None) -> MasteringEdit:
    if patch.mode == "suggested":
        base = suggested_mastering(report, measurement)[0]
        primaries, low, high = (patch.primaries or base.primaries, base.min_nits if patch.min_nits is None
                                else patch.min_nits, base.max_nits if patch.max_nits is None else patch.max_nits)
    else:
        if patch.min_nits is None or patch.max_nits is None:
            raise _invalid("Écran de mastering : min_nits et max_nits requis (ou mode suggested).")
        primaries, low, high = patch.primaries or _file_primaries(report), patch.min_nits, patch.max_nits
    try:
        return MasteringEdit(primaries, float(low), float(high))
    except ValueError as exc:
        raise _invalid(str(exc)) from exc


def _current_l6_levels(report: InspectionReport) -> tuple[int, int]:
    """MaxCLL/MaxFALL du L6 actuel (préremplissage de l'onglet Édition), sinon du HDR10 statique."""
    dovi = report.dovi
    if dovi is not None and dovi.l6:
        return int(dovi.l6[0].max_cll), int(dovi.l6[0].max_fall)
    return int(report.snapshot.max_cll or 0), int(report.snapshot.max_fall or 0)


@dataclass
class _Resolved:
    light_levels: tuple[int, int] | None = None
    mastering: MasteringEdit | None = None
    l6: tuple[int, float, int, int] | None = None
    source_display: tuple[float, float] | None = None
    recompute: bool = False
    realignment: Realignment | None = None


def resolve_plan(request: HdrPatchRequest, report: InspectionReport, measurement: ImageMeasurement | None,
                 *, display_peak: float) -> tuple[PatchPlan, list[str]]:
    """Plan de correction et avertissements, comme l'onglet Édition (``MetadataPatcher`` l'exécute)."""
    snapshot = report.snapshot
    warnings: list[str] = []
    if not snapshot.is_pq:  # l'onglet Édition ne l'impose pas : avertissement seulement
        warnings.append("Transfert PQ non signalé par la piste : vérifier qu'il s'agit bien d'un flux HDR.")
    if Path(request.source).suffix.lower() not in _PATCHABLE:
        raise _invalid("Correction réservée aux fichiers MKV et HEVC bruts.")
    if Path(request.output).expanduser().resolve() == Path(request.source).resolve():
        raise JobError("La sortie doit être un autre fichier que la source.", EXIT_ARGS)
    rpu, hdr10plus = request.rpu, request.hdr10plus
    if rpu is not None and snapshot.dovi is None:
        raise _invalid("Corrections du RPU : la piste n'a pas de Dolby Vision.")
    if hdr10plus is not None and not snapshot.hdr10plus:
        raise _invalid("Corrections HDR10+ : la piste n'a pas de HDR10+.")
    resolved = _Resolved()
    if request.light_levels is not None:
        resolved.light_levels = _light_levels(request.light_levels, report, measurement)
    if request.mastering is not None:
        resolved.mastering = _mastering(request.mastering, report, measurement)
    if rpu is not None and rpu.l6 is not None:
        resolved.l6 = (rpu.l6.max_mastering_nits, rpu.l6.min_mastering_nits, rpu.l6.max_cll, rpu.l6.max_fall)
    if rpu is not None and rpu.source_display is not None:
        resolved.source_display = (rpu.source_display.min_nits, rpu.source_display.max_nits)
    resolved.recompute = bool(hdr10plus is not None and hdr10plus.recompute)

    # Réalignement : propositions retenues, sauf valeurs indiquées explicitement.
    origin = None
    if request.realign:
        if measurement is None:
            raise _invalid("Réalignement : mesure de l'image requise (measurement fast ou full, ou mesure en cache).")
        resolved.realignment = realign(report, measurement, display_peak)
        keys = REALIGN_KEYS if "all" in request.realign else tuple(request.realign)
        edits = chosen_edits(resolved.realignment, keys)
        retained = {"static": edits.light_levels, "mdcv": edits.mastering, "l6": edits.l6,
                    "hdr10plus": edits.hdr10plus_scenes}
        for key in keys:
            if retained[key] is None and "all" not in request.realign:
                warnings.append(f"Réalignement : aucune correction proposée pour {key}.")
        if edits.mastering is not None and resolved.mastering is None:
            resolved.mastering = edits.mastering
            origin = "mdcv"  # « Reporter » : MDCV retenu recopié (L6 retenu ensuite prioritaire)
        if edits.light_levels is not None and resolved.light_levels is None:
            resolved.light_levels = edits.light_levels
        if edits.l6 is not None and (rpu is None or rpu.l6 is None):
            resolved.l6 = edits.l6
        if edits.hdr10plus_scenes is not None:
            if not snapshot.hdr10plus or (hdr10plus is not None and hdr10plus.remove):
                warnings.append("Réalignement : HDR10+ recalculé ignoré (HDR10+ absent ou retiré).")
            else:
                resolved.recompute = True

    # « Garder identiques » (Dolby Vision) : la valeur de mastering saisie (MDCV, sinon L6, sinon écran du RPU)
    # ou le MDCV retenu est recopié dans les autres ; un L6 venu du réalignement seul ne l'est pas (comme la GUI).
    if request.mastering is not None:
        origin = "mdcv"
    elif origin is None and rpu is not None:
        origin = "l6" if rpu.l6 is not None else "source" if rpu.source_display is not None else None
    if snapshot.dovi is not None and request.sync_mastering and origin is not None:
        _sync_mastering(resolved, report, origin)

    static = None
    if resolved.light_levels is not None or resolved.mastering is not None:
        levels = resolved.light_levels or (None, None)
        try:
            static = StaticEdit(max_cll=levels[0], max_fall=levels[1], mastering=resolved.mastering)
        except ValueError as exc:
            raise _invalid(f"{exc} (MaxCLL/MaxFALL retenus : {levels[0]}/{levels[1]})") from exc
    rpu_edit = None
    if snapshot.dovi is not None and (rpu is not None or resolved.l6 is not None or resolved.source_display):
        if rpu is not None and rpu.remove_cmv4 and (report.dovi is None or report.dovi.cm_version != "4.0"):
            raise _invalid("Retrait CM v4.0 : le RPU n'est pas en CM v4.0.")
        area = rpu.active_area if rpu is not None else None
        l11 = rpu.l11 if rpu is not None else None
        if l11 is not None and report.dovi is not None and report.dovi.cm_version != "4.0":
            warnings.append(f"L11 : RPU CM v{report.dovi.cm_version}, dovi_tool n'écrit le L11 que dans un RPU "
                            "CM v4.0 (sans effet).")
        rpu_edit = RpuEdit(
            l6=resolved.l6,
            active_area=(area.left, area.right, area.top, area.bottom) if area is not None else None,
            l11=(l11.content_type, l11.whitepoint, l11.reference_mode) if l11 is not None else None,
            source_display=resolved.source_display,
            remove_cmv4=bool(rpu is not None and rpu.remove_cmv4),
        )
    hdr10plus_edit = None
    if snapshot.hdr10plus and (hdr10plus is not None or resolved.recompute):
        hdr10plus = hdr10plus or Hdr10PlusPatch()
        others = (hdr10plus.targeted_display_nits is not None or hdr10plus.profile_a or hdr10plus.cap_nits is not None
                  or resolved.recompute)
        if hdr10plus.remove and others:
            raise _invalid("HDR10+ : remove exclut les autres options.")
        recompute = None
        if resolved.recompute:
            if measurement is None or measurement.is_estimate:
                raise _invalid("HDR10+ recalculé : mesure complète de l'image requise (measurement full).")
            recompute = (measurement, recompute_scenes(report, measurement, resolved.realignment))
        hdr10plus_edit = Hdr10PlusEdit(remove=hdr10plus.remove, targeted_display=hdr10plus.targeted_display_nits,
                                       drop_bezier=hdr10plus.profile_a or recompute is not None,
                                       cap_nits=hdr10plus.cap_nits, recompute=recompute)
    plan = PatchPlan(Path(request.source), snapshot.stream_index, Path(request.output).expanduser(),
                     static=static, rpu=rpu_edit, hdr10plus=hdr10plus_edit)
    if plan.is_empty():
        raise _invalid("Aucune modification demandée.")
    return plan, warnings


def _sync_mastering(resolved: _Resolved, report: InspectionReport, origin: str) -> None:
    mastering, l6, display = resolved.mastering, resolved.l6, resolved.source_display
    if origin == "mdcv" and mastering is not None:
        low, high = mastering.min_nits, mastering.max_nits
    elif origin == "l6" and l6 is not None:
        low, high = l6[1], float(l6[0])
    elif origin == "source" and display is not None:
        low, high = display
    else:
        return
    if mastering is None:
        try:
            resolved.mastering = MasteringEdit(_file_primaries(report), float(low), float(high))
        except ValueError as exc:
            raise _invalid(str(exc)) from exc
    if l6 is None:
        resolved.l6 = (int(round(high)), float(low), *_current_l6_levels(report))
    if display is None:
        resolved.source_display = (float(low), float(high))


def describe_plan(plan: PatchPlan) -> dict[str, Any]:
    """Plan résolu sérialisable (valeurs écrites)."""
    static, rpu, hdr10plus = plan.static, plan.rpu, plan.hdr10plus
    return {
        "source": str(plan.source), "track": plan.stream_index, "output": str(plan.output),
        "static": None if static is None or static.is_empty() else {
            "max_cll": static.max_cll, "max_fall": static.max_fall,
            "mastering": None if static.mastering is None else {
                **dataclasses.asdict(static.mastering), "master_display": static.mastering.master_display()}},
        "rpu": None if rpu is None or rpu.is_empty() else rpu.editor_config(),
        "hdr10plus": None if hdr10plus is None or hdr10plus.is_empty() else {
            "remove": hdr10plus.remove, "targeted_display_nits": hdr10plus.targeted_display,
            "profile_a": hdr10plus.drop_bezier, "cap_nits": hdr10plus.cap_nits,
            "recompute": hdr10plus.recompute is not None},
    }


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------


def _load(request: HdrPatchRequest, ctx: ExecutionContext,
          logger: EmitterLogger) -> tuple[InspectionReport, ImageMeasurement | None]:
    config = ctx.config
    info = ctx.port("file_inspector", FileInspector)(ffprobe_bin=ctx.tool("ffprobe"), mediainfo_bin=ctx.tool("mediainfo"),
                                                    cancel_event=ctx.cancel.event).inspect(request.source)
    ctx.cancel.raise_if_cancelled()  # sonde terminée malgré une annulation : ni plan ni écriture
    if not info.video_tracks:
        raise JobError(f"Aucune piste vidéo : {request.source}", EXIT_ARGS)
    report = snapshot_report(info, request.track)
    snapshot = report.snapshot
    cache = InspectionCache(Path(config.app_data_dir) / "hdr_inspection")
    work_root = Path(ctx.overrides.work_dir or config.ensure_work_dir())
    if snapshot.dovi is not None or snapshot.hdr10plus:
        logger.emit("info", f"Métadonnées dynamiques : {Path(request.source).name}")
        report = ctx.port("dynamic_report", dynamic_report)(config, info, snapshot.stream_index, cache, False,
                                                           ctx.tool("ffmpeg"), work_root, logger, token=ctx.cancel)
        for message in report.errors:
            logger.emit("warning", message)
        ctx.cancel.raise_if_cancelled()
    frame_check = frame_count_check(snapshot.frames if snapshot.frames_exact else None, packet_counter(
        ctx.tool("ffprobe"), Path(snapshot.path), snapshot.stream_index, ctx.cancel.event))
    measurement = load_checked_measurement(cache, snapshot, frame_check)
    wanted = request.measurement
    if wanted != "cached" and (measurement is None or (measurement.is_estimate and wanted == "full")):
        threads = max(2, int(ctx.overrides.threads or getattr(config, "ffmpeg_threads", 8) or 8))
        measurement = ctx.port("measure_image", measure_image)(config, report, threads, work_root, logger,
                                                              fast=wanted == "fast", cancel=ctx.cancel.event)
        ctx.cancel.raise_if_cancelled()
        refuse_frame_mismatch(frame_check, measurement)
        cache.store_measurement(Path(snapshot.path), snapshot.stream_index, measurement)
    return report, measurement


def _execute(plan: PatchPlan, ctx: ExecutionContext, logger: EmitterLogger,
             warnings: list[str] | None = None) -> Path:
    from core.runner import CommandError, TaskSignals, ToolRunner

    config = ctx.config
    signals = TaskSignals()
    ctx.cancel.on_cancel(signals.cancel)
    runner = ToolRunner(max_workers=1)
    span, last_pct, stage = [0.0, 1.0], [-1], ["extract_video"]

    def on_step(label: str, start: float, part: float) -> None:
        span[:] = [start, part]
        last_pct[0] = -1
        logger.emit("info", label)
        ctx.events.progress(pct=round(start * 100, 1), stage=stage[0], label=label)

    def on_pct(pct: int) -> None:
        pct = max(0, min(100, pct))
        if pct < last_pct[0]:  # second balayage d'un outil (inject-rpu, hdr10plus_tool inject)
            span[0] += span[1]
        last_pct[0] = pct
        ctx.events.progress(pct=round((span[0] + span[1] * pct / 100) * 100, 1))

    def run(cmd: list[str]) -> object:
        return runner._run_cmd(cmd, label="hdr-patch", progress_pct_cb=on_pct, signals=signals)

    owned = create_process_work_dir(Path(ctx.overrides.work_dir or config.ensure_work_dir()),
                                    process_name="hdr-correction")
    try:
        patcher = MetadataPatcher(
            ffmpeg_bin=ctx.tool("ffmpeg"), dovi_tool_bin=str(config.tool_dovi_tool),
            hdr10plus_bin=str(config.tool_hdr10plus), run_cmd=run, work_dir=owned.path, on_step=on_step,
            on_stage=lambda code: stage.__setitem__(0, code),
            cancelled=ctx.cancel.event.is_set, progress=lambda fraction: ctx.events.progress(pct=round(fraction * 100, 1)),
            warn=lambda code, message, fields: _warn(ctx, warnings, code, message, fields))
        return patcher.run(plan)
    except PatchCancelled as exc:
        raise ApiError(ErrorCode.CANCELLED, "Correction annulée.") from exc
    except OutputBusyError as exc:
        raise ApiError(ErrorCode.BUSY, str(exc)) from exc
    except (OutputCommitError, OutputRefusedError) as exc:  # sortie apparue ou changée : jamais remplacée sans force
        raise JobError(str(exc), EXIT_EXISTS) from exc
    except (PatchError, CommandError) as exc:
        raise JobError(f"Correction impossible : {exc}", EXIT_WORKFLOW) from exc
    finally:
        owned.remove()


def _warn(ctx: ExecutionContext, collected: list[str] | None, code: str, message: str, fields: dict) -> None:
    """Avertissement de l'exécution : événement ``warning`` (code stable) et rappel dans le résultat."""
    ctx.events.warning(message, code=code, **fields)
    if collected is not None:
        collected.append(message)


@operation(
    "hdr.patch",
    queue="job", request=HdrPatchRequest,
    result=HdrPatchResult,
    summary="Corriger les métadonnées HDR sans réencodage : MaxCLL/MaxFALL, MDCV, RPU (L6, L5, L11, écran, CM v4.0), "
            "HDR10+ (retrait, TSDML, profil A, plafond, recalcul), réalignement d'après la mesure.",
    long_running=True, progress=True,
    mutating=True,
    uses_gpu=True,
    errors=(ErrorCode.INVALID_REQUEST, ErrorCode.VALIDATION, ErrorCode.TOOL_MISSING, ErrorCode.OUTPUT_EXISTS,
            ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED, ErrorCode.BUSY),
)
def hdr_patch(request: HdrPatchRequest, ctx: ExecutionContext) -> HdrPatchResult:
    logger = EmitterLogger(ctx.events)
    output = Path(request.output).expanduser()
    if output.exists() and not request.force and not request.dry_run:
        raise JobError(f"Sortie déjà existante : {output} (utiliser --force)", EXIT_EXISTS)
    report, measurement = _load(request, ctx, logger)
    peak = float(request.display_peak or getattr(ctx.config, "display_peak_nits", 1000) or 1000)
    plan, warnings = resolve_plan(request, report, measurement, display_peak=peak)
    plan = dataclasses.replace(plan, existing=ExistingPolicy.from_force(request.force))
    for message in warnings:
        ctx.events.warning(message, code="hdr_patch_plan")
    try:  # images sans RPU : relevées une fois, annoncées par le dry-run, complétées par l'exécution
        gaps = analyze_rpu_gaps(plan, report, cancelled=ctx.cancel.event.is_set)
    except PatchCancelled as exc:
        raise ApiError(ErrorCode.CANCELLED, "Correction annulée.") from exc
    plan = dataclasses.replace(plan, rpu_gaps=gaps, rpu_gaps_checked=True)
    changes = describe_plan(plan)
    if request.dry_run:
        ctx.cancel.raise_if_cancelled()  # dry-run annulé : jamais rendu comme réussi
        if gaps is not None and gaps.missing:
            message, fields = rpu_completion_warning(gaps)
            ctx.events.warning(message, code="dovi_rpu_completed", **fields)
            warnings = [*warnings, message]
        return HdrPatchResult(dry_run=True, changes=changes, warnings=tuple(warnings))
    logger.emit("info", f"Correction des métadonnées : {plan.source.name} → {plan.output.name}")
    executed: list[str] = []
    published = _execute(plan, ctx, logger, executed)
    ctx.events.artifact("output", str(published))
    return HdrPatchResult(output=str(published), changes=changes, warnings=(*warnings, *executed))
