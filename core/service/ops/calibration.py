"""Opérations ``calibration.*`` : lecture, édition (Synchro Studio), composition et inversion de calibrations
de synchro (fichiers JSON ``SyncCalibration``, ceux de ``sync-scan`` et des exact-jobs)."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from core.jobs.constants import EXIT_ARGS, EXIT_EXISTS, EXIT_VALIDATION
from core.jobs.errors import JobError
from core.json_documents import read_json_document
from core.service.context import ExecutionContext
from core.service.documents import publish_request_document
from core.service.errors import ApiError, ErrorCode
from core.service.models import Model, field
from core.service.operations import operation
from core.workflows.sync_calibration import SyncCalibration, compose_calibrations, invert_calibration
from core.sidecar_output import SidecarGuard, guard_for


@dataclass(frozen=True)
class CalibrationInput(Model):
    """Calibration : fichier JSON ou objet en ligne (un seul des deux)."""

    file: Path | None = field(default=None, description="Fichier JSON (sync-scan, Synchro Studio, exact-job).")
    value: dict[str, Any] | None = field(default=None, description="Calibration en ligne (même format).")


def load_calibration(source: CalibrationInput, label: str = "calibration") -> SyncCalibration:
    if (source.file is None) == (source.value is None):
        raise ApiError(ErrorCode.INVALID_REQUEST, f"{label} : `file` ou `value`, un seul.")
    try:
        payload = read_json_document(source.file) if source.file is not None else source.value
        return SyncCalibration.from_dict(payload)
    except (OSError, ValueError, TypeError) as exc:
        raise JobError(f"{label} illisible : {exc}", EXIT_ARGS) from exc


def describe(calibration: SyncCalibration) -> dict[str, Any]:
    """Calibration normalisée, résumé et plages de sortie (affichage d'un client)."""
    return {
        "calibration": calibration.to_dict(),
        "summary": calibration.summary_lines(),
        "segments": [{"index": index, "start_ms": segment.start_ms, "shift_ms": segment.shift_ms}
                     for index, segment in enumerate(calibration.segments)],
        "output_ranges": [{"start_ms": start, "end_ms": stop, "segment": index}
                          for start, stop, index in calibration.output_ranges()],
        "cuts": calibration.cuts_count,
        "cadence": None if calibration.active_cadence is None else {
            "description": calibration.active_cadence.description,
            "method": calibration.cadence_audio_method or "auto",
        },
    }


@dataclass(frozen=True)
class CalibrationResult(Model):
    calibration: dict[str, Any]
    summary: tuple[str, ...] = ()
    segments: tuple[dict[str, Any], ...] = ()
    output_ranges: tuple[dict[str, Any], ...] = ()
    cuts: int = 0
    cadence: dict[str, Any] | None = None
    file: str | None = field(default=None, description="Fichier écrit (output).")


def _result(calibration: SyncCalibration, written: Path | None = None) -> CalibrationResult:
    data = describe(calibration)
    return CalibrationResult(calibration=data["calibration"], summary=tuple(data["summary"]),
                             segments=tuple(data["segments"]), output_ranges=tuple(data["output_ranges"]),
                             cuts=int(data["cuts"]), cadence=data["cadence"],
                             file=str(written) if written is not None else None)


def _guard(ctx: ExecutionContext, output: Path | None, media: tuple[Path, ...] = ()) -> SidecarGuard:
    """Calibration écrite réservée avant toute analyse : jamais sur un média sondé ni sur le document de la
    requête ; la calibration éditée elle-même reste remplaçable (``force``)."""
    guard = guard_for(ctx, sources=media)
    if output is not None:
        guard.claim(output, "calibration écrite")
    return guard


def _write(calibration: SyncCalibration, output: Path | None, force: bool, ctx: ExecutionContext,
           guard: SidecarGuard) -> Path | None:
    if output is None:
        return None
    guard.verify(output)
    if output.exists() and not force:
        raise JobError(f"Fichier existant : {output} (utiliser --force)", EXIT_EXISTS)
    payload = (json.dumps(calibration.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    publish_request_document(output, payload, force=force,
                          exists_message=f"Fichier existant : {output} (utiliser --force)")
    ctx.events.artifact("calibration", str(output))
    return output


@dataclass(frozen=True)
class CalibrationShowRequest(Model):
    """Calibration lue et décrite (segments, plages de sortie, cadence)."""

    calibration: CalibrationInput


@operation("calibration.show", request=CalibrationShowRequest, result=CalibrationResult,
           summary="Décrire une calibration de synchro : segments, coupes et silences, plages de sortie, cadence.")
def calibration_show(request: CalibrationShowRequest, _ctx: ExecutionContext) -> CalibrationResult:
    return _result(load_calibration(request.calibration))


@dataclass(frozen=True)
class SegmentShift(Model):
    index: int = field(minimum=0, description="Segment (0 = départ).")
    shift_ms: float = field(description="Nouveau décalage du segment (ms) ; l'écart est reporté sur les suivants.")


@dataclass(frozen=True)
class AudioRef(Model):
    """Piste audio : fichier et index ffprobe du flux."""

    path: Path
    stream: int = field(minimum=0)


@dataclass(frozen=True)
class CalibrationEdit(Model):
    """Une édition du Synchro Studio (un seul champ renseigné), appliquée dans l'ordre de la liste."""

    cut: tuple[float, ...] | None = field(default=None, description="[début, fin] de sortie (ms) supprimés.")
    silence: tuple[float, ...] | None = field(default=None, description="[début, fin] de sortie (ms) en silence.")
    remove_junction: int | None = field(default=None, minimum=1, description="Jonction retirée (segment N ≥ 1).")
    shift_ms: float | None = field(default=None, description="Départ fixé à ce décalage (toute la suite suit).")
    shift_from_segment: SegmentShift | None = None
    cadence_method: Literal["atempo", "asetrate", "auto"] | None = field(
        default=None, description="Méthode de conversion de cadence (calibration avec cadence).")


@dataclass(frozen=True)
class CalibrationEditRequest(Model):
    """Éditions du Synchro Studio : coupes, silences, jonctions, décalages, méthode de cadence."""

    calibration: CalibrationInput
    edits: tuple[CalibrationEdit, ...] = field(default=(), description="Éditions, dans l'ordre.")
    resolve_cadence: tuple[AudioRef, ...] = field(
        default=(), description="[référence, cible] : méthode « auto » tranchée par l'analyse de hauteur.")
    output: Path | None = field(default=None, description="Fichier écrit (sinon résultat seul).")
    force: bool = False


def _apply_edit(calibration: SyncCalibration, edit: CalibrationEdit, position: int) -> SyncCalibration:
    chosen = [name for name in ("cut", "silence", "remove_junction", "shift_ms", "shift_from_segment", "cadence_method")
              if getattr(edit, name) is not None]
    if len(chosen) != 1:
        raise ApiError(ErrorCode.INVALID_REQUEST, f"edits[{position}] : un seul champ par édition.")
    try:
        if edit.cut is not None or edit.silence is not None:
            bounds = edit.cut if edit.cut is not None else edit.silence
            assert bounds is not None
            if len(bounds) != 2 or not bounds[0] < bounds[1]:
                raise ValueError("[début, fin] croissants attendus (ms)")
            return calibration.with_edit(float(bounds[0]), float(bounds[1]), silence=edit.silence is not None)
        if edit.remove_junction is not None:
            return calibration.without_junction(int(edit.remove_junction))
        if edit.shift_ms is not None:
            return calibration.with_start_offset(float(edit.shift_ms))
        if edit.shift_from_segment is not None:
            return calibration.with_segment_shift(edit.shift_from_segment.index, edit.shift_from_segment.shift_ms)
        if calibration.active_cadence is None:
            raise ValueError("aucune conversion de cadence dans cette calibration")
        return replace(calibration, cadence_audio_method=str(edit.cadence_method), cadence_pitch_factor=1.0)
    except (ValueError, IndexError) as exc:
        raise JobError(f"edits[{position}] ({chosen[0]}) : {exc}", EXIT_VALIDATION) from exc


@operation("calibration.edit", request=CalibrationEditRequest, result=CalibrationResult, mutating=True,
           summary="Éditer une calibration comme le Synchro Studio : coupe, silence, jonction retirée, décalage "
                   "(du départ ou depuis un segment), méthode de cadence (« auto » tranchée par la hauteur).")
def calibration_edit(request: CalibrationEditRequest, ctx: ExecutionContext) -> CalibrationResult:
    if not request.edits and not request.resolve_cadence:
        raise ApiError(ErrorCode.INVALID_REQUEST, "calibration.edit : au moins une édition (ou resolve_cadence).")
    guard = _guard(ctx, request.output, tuple(Path(item.path) for item in request.resolve_cadence))
    calibration = load_calibration(request.calibration)
    for position, edit in enumerate(request.edits):
        calibration = _apply_edit(calibration, edit, position)
    if request.resolve_cadence:
        if len(request.resolve_cadence) != 2:
            raise ApiError(ErrorCode.INVALID_REQUEST, "resolve_cadence : [référence, cible] attendus.")
        from core.workflows.audio_sync import AudioSyncTrack
        from core.workflows.audio_sync_scan import AudioSyncScanner

        reference, target = request.resolve_cadence
        scanner = AudioSyncScanner(ctx.tool("ffmpeg"), ctx.tool("ffprobe"), cancel_event=ctx.cancel.event)
        try:
            calibration = scanner.resolve_cadence_method(
                AudioSyncTrack(reference.path, reference.stream), AudioSyncTrack(target.path, target.stream),
                calibration, log=lambda message: ctx.events.log("info", message))
        except Exception:
            ctx.cancel.raise_if_cancelled()  # analyse interrompue par le jeton (AudioSyncError) : annulation
            raise
        ctx.cancel.raise_if_cancelled()  # analyse annulée : rien n'est écrit, jamais « succeeded »
    return _result(calibration, _write(calibration, request.output, request.force, ctx, guard))


@dataclass(frozen=True)
class CalibrationComposeRequest(Model):
    """Synchro d'une source mesurée face à une référence elle-même synchronisée (références en chaîne)."""

    inner: CalibrationInput = field(description="Mesure de la cible face à l'audio d'origine de la référence.")
    outer: CalibrationInput = field(description="Position de la référence dans la sortie.")
    output: Path | None = None
    force: bool = False


@operation("calibration.compose", request=CalibrationComposeRequest, result=CalibrationResult, mutating=True,
           summary="Composer deux calibrations (cible face à une référence synchronisée : synchro dans la sortie).")
def calibration_compose(request: CalibrationComposeRequest, ctx: ExecutionContext) -> CalibrationResult:
    guard = _guard(ctx, request.output)
    calibration = compose_calibrations(load_calibration(request.inner, "inner"), load_calibration(request.outer, "outer"))
    return _result(calibration, _write(calibration, request.output, request.force, ctx, guard))


@dataclass(frozen=True)
class CalibrationInvertRequest(Model):
    calibration: CalibrationInput
    output: Path | None = None
    force: bool = False


@operation("calibration.invert", request=CalibrationInvertRequest, result=CalibrationResult, mutating=True,
           summary="Inverser une calibration (synchro de la référence face à la cible).")
def calibration_invert(request: CalibrationInvertRequest, ctx: ExecutionContext) -> CalibrationResult:
    guard = _guard(ctx, request.output)
    calibration = invert_calibration(load_calibration(request.calibration))
    return _result(calibration, _write(calibration, request.output, request.force, ctx, guard))
