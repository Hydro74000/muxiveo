"""Opération ``hdr.inspect`` : inspection HDR (HDR Studio) — en-têtes, métadonnées dynamiques, mesure de
l'image, comparaison de deux pistes, FEL Analyzer, rapport.

Annulation : la mesure de l'image et le FEL Analyzer suivent le jeton de l'appel.
"""

from __future__ import annotations

import dataclasses
import subprocess
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from core.config import AppConfig
from core.fel.engine import FelCancelled
from core.hdr_inspection import (
    DynamicInspector,
    InspectionCache,
    InspectionReport,
    Severity,
    TrackNotFoundError,
    audit,
    snapshot_report,
    video_track,
)
from core.hdr_inspection.advice import Advice, build_advice
from core.hdr_inspection.compare import Side, compare
from core.hdr_inspection.fel_analyzer import FelReport, verdict
from core.hdr_inspection.measure import (
    FrameCheck,
    ImageMeasurement,
    frame_count_check,
    measure_track,
    packet_counter,
)
from core.hdr_inspection.report import report_data, write_report
from core.hdr_inspection.tonemap import TvModel
from core.i18n import translate_text
from core.inspector import FileInspector
from core.jobs.constants import EXIT_ARGS, EXIT_TOOL
from core.jobs.errors import JobError
from core.jobs.logging import EmitterLogger
from core.jobs.logging import JobLogger as Logger
from core.runner import TaskSignals, ToolRunner
from core.service.context import CancelToken, ExecutionContext
from core.service.errors import ApiError, ErrorCode
from core.service.models import Model, field, list_of
from core.service.operations import operation
from core.sidecar_output import (
    PendingFile,
    SidecarGuard,
    discard_pending,
    guard_for,
    publish_pending,
    stage_bytes,
)
from core.workdir import create_process_work_dir

LEVELS = ("static", "dynamic", "measure")
FAIL_ON = {"warn": (Severity.WARN, Severity.ERROR), "error": (Severity.ERROR,)}
#: Encodeurs du test de survie FEL (mêmes familles que l'onglet FEL du HDR Studio).
FEL_ENCODERS = ("libx265", "hevc_nvenc", "hevc_qsv", "hevc_vaapi")
#: Verdict minimal exigé par ``fel_min`` : verdicts acceptés.
FEL_MIN = {"moderate": ("useful", "useful_if_bitrate", "useful_high_bitrate", "moderate"),
           "useful": ("useful", "useful_if_bitrate", "useful_high_bitrate")}


@dataclass(frozen=True)
class FelSurvival(Model):
    """Test de survie à l'encodage du FEL Analyzer."""

    encoder: Literal["libx265", "hevc_nvenc", "hevc_qsv", "hevc_vaapi"]
    bitrate_kbps: int = field(minimum=1, description="Débit vidéo (kbit/s).")


@dataclass(frozen=True)
class HdrInspectRequest(Model):
    """Inspection HDR d'une ou deux sources."""

    inputs: tuple[Path, ...] = field(min_items=1, description="Sources (deux avec compare : A puis B).")
    level: Literal["static", "dynamic", "measure"] | None = field(
        default=None, description="static : en-têtes ; dynamic : + RPU / HDR10+ (défaut) ; measure : + image.")
    track: int | None = field(default=None, description="Index ffprobe de la piste vidéo (défaut : première) ; "
                                                        "absent du fichier → refus.")
    report: Path | None = field(default=None, description="Rapport : HTML (.html) ou JSON (autre extension).")
    compare: bool = field(default=False, description="Comparer B à A.")
    compare_track: int | None = field(
        default=None, description="Avec compare : piste vidéo de B (index ffprobe ; absent → refus). Défaut : index "
                                  "de A s'il existe dans B, sinon première piste vidéo de B (avertissement).")
    offset: int | None = field(default=None, description="Décalage de B en images (défaut : alignement auto).")
    fail_on: Literal["warn", "error"] | None = field(
        default=None, description="Signaler (fail_on_triggered) un contrôle atteignant ce niveau.")
    fast: bool = field(default=False, description="Mesure rapide (images clés ; estimation).")
    no_cache: bool = field(default=False, description="Relire le fichier au lieu du cache.")
    fel: bool = field(default=False, description="FEL Analyzer (source Dolby Vision profil 7).")
    fel_interval: int = field(default=30, minimum=1, description="FEL Analyzer : une image toutes les N secondes.")
    fel_survival: FelSurvival | None = None
    fel_min: Literal["moderate", "useful"] | None = field(
        default=None, description="Signaler (fel_below_min) un verdict FEL inférieur.")
    fel_moments_dir: Path | None = field(
        default=None, description="FEL Analyzer : moments les plus marqués en PNG (sans FEL, avec FEL, écart).")
    fel_exposure: float = field(default=0.0, minimum=-6, maximum=6,
                                description="Exposition (EV) des PNG des moments FEL.")
    advice: bool = field(default=False, description="Recommandations : valeurs proposées, simulation TV, "
                                                     "réalignement d'après la mesure (clé `advice`).")
    display_peak: float | None = field(default=None, minimum=100, maximum=10000,
                                       description="Écran simulé (nits ; défaut : dernier pic choisi dans HDR Studio, sinon 1000).")
    tv_model: Literal["maxcll", "mdcv", "dynamic"] = field(
        default="maxcll", description="Modèle de TV simulé : suit la MaxCLL, le MDCV seul, ou dynamique.")


@dataclass(frozen=True)
class FelVerdict(Model):
    """Verdict du FEL Analyzer pour une source."""

    code: Literal["useful", "useful_if_bitrate", "useful_high_bitrate", "moderate", "useless"] = field(
        description="Code stable (`fel_analyzer.VERDICTS`).")
    title: str = field(description="Verdict (texte traduit).")
    detail: str = field(description="Explication (texte traduit).")


@dataclass(frozen=True)
class HdrInspectResult(Model):
    """Rapport par source (contrat `muxiveo-hdr-report`), comparaison, verdicts FEL et seuils."""

    hdr: tuple[dict[str, Any], ...] = ()
    comparison: dict[str, Any] | None = None
    report: str | None = None
    fel_verdicts: tuple[dict[str, Any], ...] = field(default=(), schema=list_of(FelVerdict))
    fel_below_min: bool = False
    fail_on_triggered: bool = False
    fel_moments: tuple[str, ...] = field(default=(), description="PNG des moments FEL écrits.")


def dynamic_report(config: AppConfig, info, stream: int | None, cache: InspectionCache | None, refresh: bool,
             ffmpeg: str, work_root: Path, logger: Logger, token: CancelToken | None = None) -> InspectionReport:
    """Niveau 2 (RPU, HDR10+) ; ``token`` arrête les outils en cours (``InspectionCancelled``)."""
    runner = ToolRunner(max_workers=1)
    signals = TaskSignals()
    if token is not None:
        token.on_cancel(signals.cancel)

    def run(cmd: object) -> object:
        return runner._run_cmd(cmd, label="hdr-inspection", signals=signals)  # type: ignore[arg-type]

    owned = create_process_work_dir(work_root, process_name="hdr-inspection")
    try:
        inspector = DynamicInspector(
            ffmpeg_bin=ffmpeg, dovi_tool_bin=str(config.tool_dovi_tool), hdr10plus_bin=str(config.tool_hdr10plus),
            run_cmd=run, work_dir=owned.path, cache=cache,
            on_step=lambda label, _start, _span: logger.emit("info", label),
            cancelled=token.event.is_set if token is not None else None)
        return inspector.analyze(info, stream, refresh=refresh)
    finally:
        owned.remove()


def refuse_frame_mismatch(frame_check: FrameCheck, measurement: ImageMeasurement) -> None:
    """Mesure neuve d'un autre nombre d'images que la piste (confirmé par le comptage exact) : refusée, jamais
    mise en cache ni utilisée (statistiques décalées)."""
    try:
        problem = frame_check(measurement)
    except FelCancelled as exc:
        raise ApiError(ErrorCode.CANCELLED, "Opération annulée.") from exc
    if problem is not None:
        raise JobError(problem, EXIT_TOOL)


def load_checked_measurement(cache: InspectionCache, snapshot: Any, frame_check: FrameCheck) -> ImageMeasurement | None:
    """Mesure en cache de la piste, écartée si son nombre d'images n'est pas celui de la piste ; comptage
    exact annulé : ``ApiError(CANCELLED)``."""
    try:
        return cache.load_measurement(Path(snapshot.path), snapshot.stream_index, check=frame_check)
    except FelCancelled as exc:
        raise ApiError(ErrorCode.CANCELLED, "Opération annulée.") from exc


def measure_image(config: AppConfig, report: InspectionReport, threads: int, work_root: Path,
             logger: Logger, fast: bool = False, cancel: threading.Event | None = None) -> ImageMeasurement:
    from core.fel.engine import FelEngine, measure_sampling_supported, measure_supported

    snapshot = report.snapshot
    if snapshot.codec.lower() != "hevc" or not snapshot.is_pq:
        raise JobError("Mesure réservée aux pistes HEVC PQ (HDR10, Dolby Vision, HDR10+).", EXIT_ARGS)
    if snapshot.dovi is not None and snapshot.dovi.profile == 5:
        raise JobError("Profil 5 : aucune couche HDR10 à mesurer.", EXIT_ARGS)
    engine = FelEngine.installed()
    if not measure_supported(engine, "base"):
        raise JobError("Mesure de l'image : extension mvo-fel 0.4.0 ou plus récente requise.", EXIT_TOOL)
    if fast and not measure_sampling_supported(engine, "keyframes"):
        raise JobError("Mesure rapide : extension mvo-fel 0.5.0 ou plus récente requise.", EXIT_TOOL)
    fps = snapshot.fps or 24000 / 1001
    frames = snapshot.frames or (int(round(snapshot.duration_s * fps)) if snapshot.duration_s else None)
    total = max(1, frames or 1)
    step = max(1, total // 20)
    owned = create_process_work_dir(work_root, process_name="hdr-mesure")
    try:
        def progress(frames: int) -> None:
            if frames % step < 1 or frames == total:
                logger.emit("info", f"Mesure : {frames}/{total} images")

        measurement, backend = measure_track(engine, Path(snapshot.path), snapshot.stream_index, owned.path,
                                             threads=threads, total_frames=frames, fps=fps, fast=fast,
                                             progress=progress, cancel=cancel)
        logger.emit("info", f"Mesure de l'image : {backend}")
        return measurement
    finally:
        owned.remove()


def fel_analysis(config: AppConfig, report: InspectionReport, *, interval_s: int,
                  survival: tuple[str, int] | None, ffmpeg: str, threads: int, logger: Logger,
                  cache: InspectionCache | None = None, token: CancelToken | None = None) -> FelReport:
    """FEL Analyzer d'une source P7 (mêmes mesures que l'onglet FEL) ; MEL ou FEL vide : rien à capturer."""
    from core.fel.engine import FelEngine, FelError, layers_supported
    from core.hdr_inspection.fel_analyzer import (
        analyse_progressive,
        area_at,
        close_capture,
        nothing_to_measure,
        plugin_capture,
        run_survival,
        sample_times,
        summarise,
    )
    from core.hdr_inspection.fel_layer import scan_in_background

    snapshot, dovi = report.snapshot, report.dovi
    if dovi is None or dovi.el_type is None:
        raise JobError("FEL Analyzer : aucune couche d'amélioration Dolby Vision (profil 7 attendu).", EXIT_ARGS)
    kind = nothing_to_measure(dovi)
    if kind is not None:
        reason = translate_text("{kind} : aucun résiduel, l'image est identique à la lecture P8.1 ; rien à mesurer.",
                                kind=kind)
        logger.emit("info", reason)
        return FelReport(None, (), summarise([]), (), notes=(reason,))
    try:
        engine = FelEngine.installed()
    except FelError as exc:
        raise JobError(f"FEL Analyzer : {exc}", EXIT_TOOL) from exc
    if not (layers_supported(engine, "mapped") and layers_supported(engine, "reconstructed")):
        raise JobError("FEL Analyzer : extension mvo-fel 0.4.0 ou plus récente requise.", EXIT_TOOL)
    fps = snapshot.fps or 24000 / 1001
    rate = Fraction(fps).limit_denominator(1001)
    # Arrêt local (levé en fin d'analyse pour le relevé des couches) ; l'annulation de l'appelant le lève aussi,
    # jamais l'inverse.
    cancel = threading.Event()
    if token is not None:
        token.on_cancel(cancel.set)

    def capture_on(device: str):
        return plugin_capture(engine, Path(snapshot.path), snapshot.stream_index, ffmpeg_bin=ffmpeg,
                              width=snapshot.width or 3840, height=snapshot.height or 2160,
                              frame_rate=f"{rate.numerator}/{rate.denominator}", threads=threads, cancelled=cancel,
                              device=device)

    area = area_at(dovi, fps)
    try:
        path = Path(snapshot.path)
        cached = cache.load_fel(path, snapshot.stream_index) if cache is not None else None
        known = cached.facts if cached is not None else None
        # Relevé des couches (lecture complète du fichier) pendant l'échantillonnage, sauf s'il est en cache.
        facts = (lambda: known) if known is not None else \
            scan_in_background(path, snapshot.stream_index, cancelled=cancel.is_set)
        duration = snapshot.duration_s or (snapshot.frames or 0) / fps
        times = sample_times(duration, interval_s)
        grid = set(times)
        reused = sum(1 for s in (cached.samples if cached is not None else ()) if s.time_s in grid)
        logger.emit("info", f"FEL Analyzer : {len(times)} instants (une image toutes les {interval_s} s)"
                    + (f", dont {reused} en cache" if reused else ""))
        reported = [-1]

        def progress(fraction: float) -> None:
            if int(fraction * 10) > reported[0]:
                reported[0] = int(fraction * 10)
                logger.emit("info", f"FEL Analyzer : {reported[0] * 10} %")

        def partial(report: FelReport) -> None:
            _code, title, _detail = verdict(report)
            logger.emit("info", f"FEL Analyzer : verdict provisoire ({len(report.samples)} instants) — {title}")

        capture = None
        try:
            capture = capture_on("cpu")
            result = analyse_progressive(capture, duration, interval_s, area=area, cached=cached,
                                         facts=facts, progress=progress, partial=partial, cancelled=cancel.is_set)
        except (RuntimeError, FelError, OSError) as exc:
            raise JobError(f"FEL Analyzer : capture impossible ({exc}).", EXIT_TOOL) from exc
        finally:
            close_capture(capture)
        if cache is not None:
            cache.store_fel(path, snapshot.stream_index, result)
        if survival is not None:
            encoder, bitrate = survival
            logger.emit("info", f"FEL Analyzer : test de survie {encoder} à {bitrate} kbit/s")
            survival_capture = None
            try:
                survival_capture = capture_on("auto")
                kept = run_survival(survival_capture, result, ffmpeg_bin=ffmpeg, encoder=encoder,
                                    bitrate_kbps=bitrate, fps=fps, area=area, duration_s=duration or None)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
                raise JobError(f"FEL Analyzer : encodeur {encoder} indisponible : {detail or exc}", EXIT_TOOL) from exc
            except (RuntimeError, FelError, OSError) as exc:
                raise JobError(f"FEL Analyzer : test de survie impossible ({exc}).", EXIT_TOOL) from exc
            finally:
                close_capture(survival_capture)
            result = dataclasses.replace(result, survival=kept)
        return result
    finally:
        cancel.set()



def write_fel_moments(report: InspectionReport, fel: FelReport, directory: Path, *, exposure: float, ffmpeg: str,
                      threads: int, logger: Logger, token: CancelToken | None = None) -> list[Path]:
    """Moments les plus marqués du FEL Analyzer écrits et publiés aussitôt (source protégée)."""
    guard = SidecarGuard([(Path(report.snapshot.path), "source")])
    return publish_pending(guard, stage_fel_moments(report, fel, directory, exposure=exposure, ffmpeg=ffmpeg,
                                                    threads=threads, logger=logger, token=token))


def stage_fel_moments(report: InspectionReport, fel: FelReport, directory: Path, *, exposure: float, ffmpeg: str,
                      threads: int, logger: Logger, token: CancelToken | None = None,
                      side: str = "") -> list[PendingFile]:
    """Moments les plus marqués du FEL Analyzer en pleine définition : PNG sans FEL, avec FEL et carte de l'écart
    (comparateur avant / après du HDR Studio), écrits en candidats cachés de ``directory`` (publiés par
    :func:`~core.sidecar_output.publish_pending`). ``side`` : suffixe de côté (``A`` / ``B``) quand A et B portent
    le même nom ; erreur ou annulation : candidats supprimés."""
    from core.fel.engine import FelEngine, FelError
    from core.hdr_inspection.fel_analyzer import (
        active_rows,
        area_at,
        close_capture,
        moment_images,
        plugin_capture,
        render_sdr,
    )
    from core.hdr_inspection.report import png_bytes

    if not fel.moments:
        return []
    snapshot = report.snapshot
    fps = snapshot.fps or 24000 / 1001
    rate = Fraction(fps).limit_denominator(1001)
    area = area_at(report.dovi, fps)
    cancel = threading.Event()
    if token is not None:
        token.on_cancel(cancel.set)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[PendingFile] = []
    capture = None
    try:
        capture = plugin_capture(FelEngine.installed(), Path(snapshot.path), snapshot.stream_index, ffmpeg_bin=ffmpeg,
                                 width=snapshot.width or 3840, height=snapshot.height or 2160,
                                 frame_rate=f"{rate.numerator}/{rate.denominator}", threads=threads,
                                 cancelled=cancel)
        stem = Path(snapshot.path).stem + (f".{side}" if side else "")
        for moment in fel.moments:
            if cancel.is_set():
                break
            pair = getattr(capture, "pair", None)
            if callable(pair):  # session : les deux couches d'un seul décodage
                both = pair(moment.time_s, 1)
                reconstructed, mapped = both[0][0], both[1][0]
            else:
                reconstructed = capture("reconstructed", moment.time_s, 1)[0]
                mapped = capture("mapped", moment.time_s, 1)[0]
            images = moment_images(reconstructed, mapped, active_rows(mapped, area(moment.time_s)), moment.time_s)
            seconds = int(moment.time_s)
            base = f"{stem}.fel-{seconds // 3600:02d}h{seconds // 60 % 60:02d}m{seconds % 60:02d}s"
            for suffix, image in (("sans-fel", render_sdr(images.mapped, exposure)),
                                  ("avec-fel", render_sdr(images.reconstructed, exposure)),
                                  ("ecart", images.difference)):
                written.append(stage_bytes(directory / f"{base}.{suffix}.png", png_bytes(image),
                                           role="image du FEL Analyzer"))
            logger.emit("info", f"FEL Analyzer : moment {base[len(stem) + 5:]} capturé")
        if cancel.is_set():
            raise InterruptedError
    except InterruptedError:
        discard_pending(written)
        if token is not None:
            token.raise_if_cancelled()
        raise
    except (RuntimeError, FelError, OSError) as exc:
        discard_pending(written)
        if token is not None:
            token.raise_if_cancelled()  # capture interrompue par l'annulation : jamais « outil en échec »
        raise JobError(f"FEL Analyzer : capture des moments impossible ({exc}).", EXIT_TOOL) from exc
    except BaseException:
        discard_pending(written)
        raise
    finally:
        cancel.set()
        close_capture(capture)
    return written


@operation(
    "hdr.inspect",
    request=HdrInspectRequest,
    result=HdrInspectResult,
    summary="Inspection HDR : en-têtes, RPU / HDR10+, mesure de l'image, comparaison, FEL Analyzer, rapport.",
    long_running=True,
    mutating=True,
    uses_gpu=True,
    errors=(ErrorCode.INVALID_REQUEST, ErrorCode.TOOL_MISSING, ErrorCode.WORKFLOW_FAILED, ErrorCode.CANCELLED),
)
def hdr_inspect(request: HdrInspectRequest, ctx: ExecutionContext) -> HdrInspectResult:
    level = str(request.level or "dynamic")
    if request.fel and level == "static":
        level = "dynamic"
    inputs = list(request.inputs)
    if request.compare and len(inputs) != 2:
        raise JobError("--hdr-compare : deux sources attendues (A puis B).", EXIT_ARGS)
    if request.report and len(inputs) > 1 and not request.compare:
        raise JobError("--hdr-report : une seule source, ou deux avec --hdr-compare.", EXIT_ARGS)
    if request.offset is not None and not request.compare:
        raise JobError("--hdr-offset s'utilise avec --hdr-compare.", EXIT_ARGS)
    if request.compare_track is not None and not request.compare:
        raise JobError("--hdr-compare-track s'utilise avec --hdr-compare.", EXIT_ARGS)
    # Annexes réservées avant toute analyse : jamais sur une source ni sur le document de la requête.
    guard = guard_for(ctx, sources=inputs)
    report_target = guard.claim(request.report, "rapport HDR") if request.report else None
    if request.fel_moments_dir is not None:
        guard.protect(request.fel_moments_dir, "dossier des moments FEL")  # jamais pris pour une annexe
    same_stem = len(inputs) == 2 and Path(inputs[0]).stem == Path(inputs[1]).stem
    config, logger = ctx.config, EmitterLogger(ctx.events)
    ffmpeg = ctx.tool("ffmpeg")
    inspector = ctx.port("file_inspector", FileInspector)(ffprobe_bin=ctx.tool("ffprobe"),
                                                         mediainfo_bin=ctx.tool("mediainfo"),
                                                         cancel_event=ctx.cancel.event)  # sondes tuées par le jeton
    dynamic = ctx.port("dynamic_report", dynamic_report)
    cache = InspectionCache(Path(config.app_data_dir) / "hdr_inspection")
    work_root = ctx.overrides.work_dir or config.ensure_work_dir()
    work_root.mkdir(parents=True, exist_ok=True)
    threads = max(2, int(ctx.overrides.threads or getattr(config, "ffmpeg_threads", 8) or 8))
    survival = (request.fel_survival.encoder, request.fel_survival.bitrate_kbps) if request.fel_survival else None
    fel_interval = request.fel_interval if request.fel else None
    sides: list[Side] = []
    fel_reports: list[FelReport] = []
    pending: list[PendingFile] = []
    try:
        moments = _inspect_sides(request, ctx, inputs, sides, fel_reports, pending, level=level, same_stem=same_stem,
                                 inspector=inspector, dynamic=dynamic, cache=cache, work_root=work_root,
                                 threads=threads, survival=survival, fel_interval=fel_interval, ffmpeg=ffmpeg,
                                 guard=guard, logger=logger)
    except BaseException:
        discard_pending(pending)  # erreur, collision ou annulation : aucune image publiée
        raise
    return _inspect_result(request, ctx, sides, fel_reports, moments, report_target, guard, logger)


def _inspect_sides(request: HdrInspectRequest, ctx: ExecutionContext, inputs: list[Path], sides: list[Side],
                   fel_reports: list[FelReport], pending: list[PendingFile], *, level: str, same_stem: bool,
                   inspector: Any, dynamic: Any, cache: InspectionCache, work_root: Path, threads: int,
                   survival: tuple[str, int] | None, fel_interval: int | None, ffmpeg: str, guard: SidecarGuard,
                   logger: EmitterLogger) -> list[Path]:
    """Analyse de chaque source ; moments FEL en candidats (``pending``), publiés en une passe à la fin.

    Toutes les sources sont sondées et leurs pistes résolues avant la première analyse : une piste absente est
    refusée sans rien analyser."""
    config = ctx.config
    infos = []
    for path in inputs:
        ctx.cancel.raise_if_cancelled()
        info = inspector.inspect(path)
        ctx.cancel.raise_if_cancelled()  # sonde terminée malgré une annulation : rien n'est publié
        if not info.video_tracks:
            raise JobError(f"Aucune piste vidéo : {path}", EXIT_ARGS)
        infos.append(info)
    tracks = resolve_tracks(request, infos, logger)
    for side_index, (path, info, track) in enumerate(zip(inputs, infos, tracks)):
        report = snapshot_report(info, track)
        snapshot = report.snapshot
        frame_check = frame_count_check(snapshot.frames if snapshot.frames_exact else None, packet_counter(
            ctx.tool("ffprobe"), Path(snapshot.path), snapshot.stream_index, ctx.cancel.event))
        measurement = None if request.no_cache else load_checked_measurement(cache, snapshot, frame_check)
        if level in ("dynamic", "measure") and (snapshot.dovi is not None or snapshot.hdr10plus):
            logger.emit("info", f"Métadonnées dynamiques : {path.name}")
            report = dynamic(config, info, snapshot.stream_index, cache, request.no_cache, ffmpeg, work_root, logger,
                             token=ctx.cancel)
            for message in report.errors:
                logger.emit("warning", message)
            ctx.cancel.raise_if_cancelled()
        if level == "measure" and (measurement is None or (measurement.is_estimate and not request.fast)):
            measurement = measure_image(config, report, threads, work_root, logger, fast=request.fast,
                                        cancel=ctx.cancel.event)
            ctx.cancel.raise_if_cancelled()
            refuse_frame_mismatch(frame_check, measurement)
            cache.store_measurement(Path(snapshot.path), snapshot.stream_index, measurement)
        if measurement is not None:
            report = dataclasses.replace(report, findings=audit(report.snapshot, report.dovi, report.hdr10plus,
                                                                 measurement))
        if fel_interval is not None:
            fel = fel_analysis(config, report, interval_s=fel_interval, survival=survival, ffmpeg=ffmpeg,
                               threads=threads, logger=logger, cache=None if request.no_cache else cache,
                               token=ctx.cancel)
            fel_reports.append(fel)
            if request.fel_moments_dir is not None:
                pending += stage_fel_moments(report, fel, Path(request.fel_moments_dir),
                                             exposure=request.fel_exposure, ffmpeg=ffmpeg, threads=threads,
                                             logger=logger, token=ctx.cancel,
                                             side="AB"[side_index] if same_stem else "")
        sides.append(Side(report, measurement))
    ctx.cancel.raise_if_cancelled()
    moments = publish_pending(guard, pending)
    pending.clear()
    for written in moments:
        ctx.events.artifact("fel-moment", str(written))
    return moments


def resolve_tracks(request: HdrInspectRequest, infos: list[Any], logger: EmitterLogger) -> list[int]:
    """Piste vidéo de chaque source (index ffprobe). ``track`` : piste de A (et de chaque source hors
    comparaison), stricte ; B d'une comparaison : ``compare_track`` stricte, sinon l'index de A s'il existe dans B,
    sinon la première piste vidéo de B (avertissement). Index absent → ``invalid_request``."""
    try:
        first = video_track(infos[0], request.track).index
        tracks = [first]
        for info in infos[1:]:
            if not request.compare:
                tracks.append(video_track(info, request.track).index)
            elif request.compare_track is not None:
                tracks.append(video_track(info, request.compare_track).index)
            elif request.track is None:
                tracks.append(video_track(info).index)  # première piste vidéo de chaque côté
            elif any(item.index == first for item in info.video_tracks):
                tracks.append(first)
            else:
                chosen = video_track(info).index
                logger.emit("warning", f"Piste vidéo #{first} absente de B ({Path(info.path).name}) : première piste "
                                       f"vidéo de B (#{chosen}) comparée (--hdr-compare-track pour la choisir).")
                tracks.append(chosen)
    except TrackNotFoundError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
    return tracks


def _inspect_result(request: HdrInspectRequest, ctx: ExecutionContext, sides: list[Side],
                    fel_reports: list[FelReport], moments: list[Path], report_target: Path | None,
                    guard: SidecarGuard, logger: EmitterLogger) -> HdrInspectResult:
    """Comparaison, recommandations, rapport (annexe réservée) et verdicts."""
    config = ctx.config
    comparison = compare(sides[0], sides[1], request.offset) if request.compare else None
    fels: list[FelReport | None] = list(fel_reports) if fel_reports else [None] * len(sides)
    advices: list[Advice | None] = [None] * len(sides)
    if request.advice:
        peak = float(request.display_peak or getattr(config, "display_peak_nits", 1000) or 1000)
        advices = [build_advice(side.report, side.measurement, display_peak=peak, tv_model=TvModel(request.tv_model))
                   for side in sides]
    payload: dict = {"hdr": [report_data(side.report, measurement=side.measurement, fel=fel, advice=advice)
                             for side, fel, advice in zip(sides, fels, advices)]}
    if comparison is not None:
        payload["comparison"] = report_data(sides[0].report, measurement=sides[0].measurement, comparison=comparison,
                                            other=sides[1].report)["comparison"]
    report_path = None
    if report_target is not None:
        write_report(report_target, sides[0].report, measurement=sides[0].measurement,
                                   fel=fels[0], comparison=comparison,
                                   other=sides[1].report if comparison is not None else None, advice=advices[0],
                                   write=guard.write_text)
        report_path = Path(str(request.report))  # chemin tel que demandé (sorties historiques)
        logger.emit("info", f"Rapport HDR écrit : {report_path}")
        ctx.events.artifact("report", str(report_path))
    verdicts = []
    for fel in fel_reports:
        code, title, detail = verdict(fel)
        verdicts.append({"code": code, "title": title, "detail": detail})
        logger.emit("info", f"{title} — {detail}")
    below_min = bool(request.fel_min) and any(
        verdict(fel)[0] not in FEL_MIN[str(request.fel_min)] for fel in fel_reports)
    fail_on = False
    if request.fail_on:
        failing = FAIL_ON[request.fail_on]
        fail_on = any(f.severity in failing for side in sides for f in side.report.findings) or any(
            side.report.errors for side in sides)
    return HdrInspectResult(hdr=tuple(payload["hdr"]), comparison=payload.get("comparison"),
                            report=str(report_path) if report_path is not None else None,
                            fel_verdicts=tuple(verdicts), fel_below_min=below_min, fail_on_triggered=fail_on,
                            fel_moments=tuple(str(path) for path in moments))
