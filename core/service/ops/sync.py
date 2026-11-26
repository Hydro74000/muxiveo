"""Opérations de synchronisation : analyse du calage (audio ou sous-titres), recalage de sous-titres texte."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.jobs.constants import EXIT_EXISTS
from core.jobs.errors import JobError
from core.jobs.options import call_supported
from core.service.context import ExecutionContext
from core.service.documents import publish_request_document
from core.subprocess_utils import ProbeCancelledError, run_probe
from core.service.errors import ErrorCode, error_code
from core.service.models import Model, field
from core.service.operations import operation
from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner, coherent_sync_accepted, quick_check_enabled
from core.workflows.subtitle_sync import shift_text
from core.workflows.sync_calibration import SyncCalibration
from core.sidecar_output import guard_for


def detect_stream_kind(ffprobe: str, path: Path | str, stream_spec: str | int, default: str = "audio", *,
                       cancel_event: threading.Event | None = None) -> str:
    """Nature d'un flux (``audio`` / ``subtitle`` / ``video``) : extension, spécificateur, sinon ffprobe (sonde
    annulable par ``cancel_event`` : l'annulation remonte)."""
    path = Path(path)
    if path.suffix.lower() in {".srt", ".vtt", ".ass", ".ssa"}:
        return "subtitle"
    spec_str = str(stream_spec).lower()
    if ":s" in spec_str or spec_str == "s":
        return "subtitle"
    if ":a" in spec_str or spec_str == "a":
        return "audio"
    if ":v" in spec_str or spec_str == "v":
        return "video"
    try:
        cmd = [str(ffprobe), "-v", "error", "-select_streams", str(stream_spec),
               "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)]
        res = run_probe(cmd, timeout=10, cancel_event=cancel_event)
        # CSV : champs vides ajoutés par les side data de flux (Dolby Vision) → « video, ».
        output = res.stdout.strip().split(",", 1)[0].strip().lower()
        if output in {"audio", "subtitle", "video"}:
            return output
    except ProbeCancelledError:
        raise
    except Exception:  # nosec B110
        pass
    return default


@dataclass(frozen=True)
class SyncScanRequest(Model):
    """Calage d'une piste cible face à une référence (corrélation acoustique ou sous-titres)."""

    ref: Path = field(description="Fichier de référence.")
    target: Path = field(description="Fichier à caler.")
    stream_ref: str = field(default="0:a:0", description="Flux de référence (spécificateur FFmpeg).")
    stream_target: str = field(default="0:a:0", description="Flux cible (spécificateur FFmpeg).")
    type: Literal["auto", "audio", "subtitle"] = field(default="auto", description="Nature de l'analyse.")
    detect_cuts: bool = field(default=False, description="Détecter coupures et ruptures (multi-segments).")
    drift_threshold_ms: int = field(default=25, description="Seuil de dérive d'une coupure (ms).")
    quick_sync_check: bool | None = field(default=None, description="Synchro rapide ; défaut : réglage [sync].")
    reject_coherent_sync: bool = field(default=False,
                                       description="Refuser une synchro cohérente en partie non vérifiée.")
    cadence_auto: bool | None = field(default=None, description="Conversion de cadence ; défaut : réglage [sync].")
    cadence_method: Literal["auto", "atempo", "asetrate"] | None = None
    output_json: Path | None = field(default=None, description="Écrire aussi la calibration dans ce fichier.")


@dataclass(frozen=True)
class SyncScanResult(Model):
    calibration: dict[str, Any] = field(description="Calibration (contrat `sync-calibration`).")


@dataclass(frozen=True)
class ShiftSubtitlesRequest(Model):
    """Recalage d'un fichier de sous-titres texte (SRT, ASS…)."""

    input: Path
    output: Path
    offset_ms: float | None = field(default=None, description="Décalage linéaire (ms).")
    calibration: Path | None = field(default=None, description="Calibration JSON (coupures, cadence).")
    force: bool = False


@dataclass(frozen=True)
class ShiftSubtitlesResult(Model):
    output: str


def _audio_scanner(request: SyncScanRequest, ctx: ExecutionContext) -> Any:
    return AudioSyncScanner(ctx.tool("ffmpeg"), ctx.tool("ffprobe"), cancel_event=ctx.cancel.event,
                            quick_check=quick_check_enabled(request, ctx.config),
                            accept_coherent=coherent_sync_accepted(request))


@operation("sync.scan", request=SyncScanRequest, result=SyncScanResult,
           summary="Analyser le calage d'une piste (audio ou sous-titres) face à une référence.",
           long_running=True, mutating=True,
           errors=(ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED))
def sync_scan(request: SyncScanRequest, ctx: ExecutionContext) -> SyncScanResult:
    # Calibration réservée avant l'analyse : jamais sur la référence, la cible ni le document de la requête.
    guard = guard_for(ctx, sources=(request.ref, request.target))
    if request.output_json is not None:
        guard.claim(request.output_json, "calibration écrite")
    ffprobe, ffmpeg = ctx.tool("ffprobe"), ctx.tool("ffmpeg")
    port = ctx.port("detect_stream_kind", detect_stream_kind)

    def detect(*args: Any, default: str) -> str:  # jeton transmis à toute fonction qui l'accepte (port CLI compris)
        return str(call_supported(port, *args, default=default, cancel_event=ctx.cancel.event))

    log = ctx.events.log
    if request.type == "auto":
        tgt_kind = detect(ffprobe, request.target, request.stream_target, default="audio")
        ref_kind = detect(ffprobe, request.ref, request.stream_ref, default="audio")
    elif request.type == "subtitle":
        tgt_kind = "subtitle"
        ref_kind = detect(ffprobe, request.ref, request.stream_ref, default="subtitle")
    else:
        tgt_kind = ref_kind = "audio"

    if tgt_kind == "subtitle":
        from core.workflows.subtitle_sync_scan import SubtitleSyncScanner

        is_ref_sub = ref_kind == "subtitle"
        result = _cancellable_analysis(ctx, lambda: SubtitleSyncScanner(ffmpeg, ffprobe, cancel_event=ctx.cancel.event).scan(
            request.ref, request.stream_ref, request.target, request.stream_target, is_ref_sub=is_ref_sub,
            detect_cuts=request.detect_cuts if is_ref_sub else False, log=lambda message: log("info", message),
        ))
    else:
        cadence_mismatch = None
        cadence_auto = request.cadence_auto
        if cadence_auto is None:
            cadence_auto = getattr(ctx.config, "sync_cadence_auto_apply", True)
        cadence_method = request.cadence_method or getattr(ctx.config, "sync_cadence_audio_method", "auto") or "auto"
        if cadence_auto:
            try:
                from core.inspector import FileInspector
                from core.workflows.cadence import detect_cadence_from_metadata

                inspector = FileInspector(ffprobe_bin=ffprobe, mediainfo_bin=ctx.tool("mediainfo"),
                                          cancel_event=ctx.cancel.event)
                ref_info = inspector.inspect(request.ref)
                tgt_info = inspector.inspect(request.target)
                ref_fps = next((v.frame_rate for v in ref_info.video_tracks if v.frame_rate), None)
                tgt_fps = next((v.frame_rate for v in tgt_info.video_tracks if v.frame_rate), None)
                if ref_fps and tgt_fps:
                    cadence_mismatch = detect_cadence_from_metadata(ref_fps, tgt_fps)
                    if cadence_mismatch:
                        log("info", f"Différence de cadence détectée ({ref_fps} vs {tgt_fps}) : "
                                    f"{cadence_mismatch.description} — méthode audio : {cadence_method}")
            except Exception as exc:
                if error_code(exc) is ErrorCode.CANCELLED:
                    raise  # annulation : jamais avalée par l'avertissement « cadences »
                log("warning", f"Impossible d'analyser les cadences vidéo : {exc}")
        make_scanner = ctx.port("audio_scanner", lambda: _audio_scanner(request, ctx))
        result = _cancellable_analysis(ctx, lambda: make_scanner().scan(
            AudioSyncTrack(request.ref, request.stream_ref),
            AudioSyncTrack(request.target, request.stream_target),
            detect_cuts=request.detect_cuts,
            drift_threshold_ms=request.drift_threshold_ms,
            cadence_mismatch=cadence_mismatch,
            cadence_audio_method=cadence_method,
            log=lambda message: log("info", message),
            warn=lambda message, **fields: log("warning", message, **fields),
        ))
    calibration = result.to_dict()
    if request.output_json is not None:
        guard.write_text(Path(request.output_json),
                         json.dumps(calibration, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        ctx.events.artifact("calibration", str(request.output_json))
    return SyncScanResult(calibration=calibration)


def _cancellable_analysis(ctx: ExecutionContext, analyse: Any) -> Any:
    """Analyse de synchro sous le jeton : interrompue par lui (``AudioSyncError`` / ``SubtitleSyncError``) →
    annulation ; revenue malgré lui → annulation aussi (rien n'est écrit, jamais « succeeded »)."""
    try:
        result = analyse()
    except Exception:
        ctx.cancel.raise_if_cancelled()
        raise
    ctx.cancel.raise_if_cancelled()
    return result


@operation("sync.shift_subtitles", request=ShiftSubtitlesRequest, result=ShiftSubtitlesResult,
           summary="Recaler un fichier de sous-titres texte (décalage ou calibration).", mutating=True,
           errors=(ErrorCode.INVALID_REQUEST, ErrorCode.OUTPUT_EXISTS, ErrorCode.WORKFLOW_FAILED))
def sync_shift_subtitles(request: ShiftSubtitlesRequest, ctx: ExecutionContext) -> ShiftSubtitlesResult:
    if (request.offset_ms is None) == (request.calibration is None):
        raise JobError("Indiquer soit offset_ms, soit calibration.", 2)
    if request.output.exists() and not request.force:
        raise JobError(str(request.output), EXIT_EXISTS)
    calibration = (SyncCalibration.from_dict(json.loads(Path(request.calibration).read_text(encoding="utf-8-sig")))
                   if request.calibration is not None else SyncCalibration.linear(float(request.offset_ms or 0.0)))
    with Path(request.input).open(encoding="utf-8-sig", newline="") as stream:
        shifted = shift_text(stream.read(), calibration, suffix=Path(request.input).suffix)
    # Destination réservée jusqu'à la publication : sans force, jamais d'écrasement, même concurrent.
    publish_request_document(request.output, shifted.encode("utf-8"), force=request.force,
                             exists_message=str(request.output))
    ctx.events.artifact("subtitles", str(request.output))
    return ShiftSubtitlesResult(output=str(request.output))


@dataclass(frozen=True)
class WaveformRequest(Model):
    """Forme d'onde d'une piste audio sur une plage (pics d'amplitude), pour un client graphique."""

    source: Path = field(description="Fichier source.")
    stream: str = field(default="0:a:0", description="Flux : index ffprobe (« 3 ») ou spécificateur (« 0:a:1 »).")
    start_s: float = field(default=0.0, minimum=0, description="Début de la plage (s).")
    duration_s: float = field(default=60.0, minimum=0.1, maximum=3600, description="Durée de la plage (s).")
    resolution_ms: int = field(default=10, minimum=1, maximum=1000, description="Largeur d'une tranche (ms).")


@dataclass(frozen=True)
class WaveformResult(Model):
    start_s: float
    resolution_ms: int
    peaks: tuple[float, ...] = field(default=(), description="Pic d'amplitude (0–1) de chaque tranche.")


@operation("sync.waveform", request=WaveformRequest, result=WaveformResult,
           summary="Forme d'onde d'une piste audio sur une plage (pics par tranche), données du Synchro Studio.",
           long_running=True, errors=(ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED))
def sync_waveform(request: WaveformRequest, ctx: ExecutionContext) -> WaveformResult:
    from core.service.errors import ApiError
    from core.subprocess_utils import ProbeCancelledError
    from core.workflows.sync_waveform import WaveformError, track_peaks

    try:
        peaks = track_peaks(ctx.tool("ffmpeg"), request.source, request.stream, start_s=request.start_s,
                            duration_s=request.duration_s, resolution_ms=request.resolution_ms,
                            cancel=ctx.cancel.event)
    except ProbeCancelledError as exc:
        raise ApiError(ErrorCode.CANCELLED, "Forme d'onde annulée.") from exc
    except (WaveformError, OSError) as exc:
        raise JobError(f"Forme d'onde impossible : {exc}", 6) from exc
    return WaveformResult(start_s=request.start_s, resolution_ms=request.resolution_ms,
                          peaks=tuple(round(float(value), 4) for value in peaks))
