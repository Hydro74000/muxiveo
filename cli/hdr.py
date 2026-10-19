"""CLI ``inspect --hdr`` : inspection HDR (en-têtes, métadonnées dynamiques, mesure), comparaison, rapport."""
from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import threading
from fractions import Fraction
from pathlib import Path

from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_TOOL, EXIT_VALIDATION
from cli.errors import CliError
from cli.logging import Logger
from cli.options import common_options
from core.config import AppConfig
from core.hdr_inspection import DynamicInspector, InspectionCache, InspectionReport, Severity, audit, snapshot_report
from core.hdr_inspection.compare import Side, compare
from core.hdr_inspection.fel_analyzer import FelReport, verdict
from core.hdr_inspection.measure import ImageMeasurement, read_measurement
from core.hdr_inspection.report import report_data, write_report
from core.i18n import translate_text
from core.inspector import FileInspector
from core.runner import ToolRunner
from core.workdir import create_process_work_dir

LEVELS = ("static", "dynamic", "measure")
_FAIL_ON = {"warn": (Severity.WARN, Severity.ERROR), "error": (Severity.ERROR,)}
#: Encodeurs du test de survie FEL (mêmes familles que l'onglet FEL du HDR Studio).
FEL_ENCODERS = ("libx265", "hevc_nvenc", "hevc_qsv", "hevc_vaapi")
#: Verdict minimal exigé par ``--hdr-fel-min`` : verdicts acceptés.
_FEL_MIN = {"moderate": ("useful", "useful_if_bitrate", "useful_high_bitrate", "moderate"),
            "useful": ("useful", "useful_if_bitrate", "useful_high_bitrate")}


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("valeur strictement positive attendue")
    return number


def _survival_spec(value: str) -> tuple[str, int]:
    """``ENCODEUR:KBPS`` (ex. ``libx265:35000``)."""
    encoder, _sep, bitrate = value.partition(":")
    if encoder not in FEL_ENCODERS or not bitrate.isdigit() or int(bitrate) <= 0:
        raise argparse.ArgumentTypeError(
            f"ENCODEUR:KBPS attendu, encodeur parmi {', '.join(FEL_ENCODERS)} (ex. libx265:35000)")
    return encoder, int(bitrate)


def add_hdr_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("HDR (HDR Studio)")
    group.add_argument("--hdr", choices=LEVELS,
                       help="Inspection HDR : en-têtes (static), + RPU / HDR10+ (dynamic), + mesure de chaque image "
                            "(measure, extension mvo-fel ≥ 0.4.0). Sortie JSON.")
    group.add_argument("--hdr-track", type=int, default=None,
                       help="Index ffprobe de la piste vidéo (défaut : première piste vidéo).")
    group.add_argument("--hdr-report", default="",
                       help="Rapport écrit dans ce fichier : HTML (.html) ou JSON (autre extension).")
    group.add_argument("--hdr-compare", action="store_true",
                       help="Compare la seconde source à la première (deux entrées).")
    group.add_argument("--hdr-offset", type=int, default=None,
                       help="Décalage imposé de B en images (défaut : alignement automatique).")
    group.add_argument("--hdr-fail-on", choices=tuple(_FAIL_ON), default=None,
                       help="Code de sortie 3 si un contrôle atteint ce niveau.")
    group.add_argument("--hdr-fast", action="store_true",
                       help="Avec --hdr measure : mesure rapide (images clés, extension mvo-fel ≥ 0.5.0) ; estimation.")
    group.add_argument("--hdr-no-cache", action="store_true",
                       help="Relit le fichier au lieu du résultat en cache (RPU, HDR10+, mesure).")
    group.add_argument("--hdr-fel", action="store_true",
                       help="FEL Analyzer : apport de la couche FEL d'une source Dolby Vision profil 7 (lit les "
                            "métadonnées dynamiques ; extension mvo-fel ≥ 0.4.0).")
    group.add_argument("--hdr-fel-interval", type=_positive_int, default=30, metavar="SECONDES",
                       help="Avec --hdr-fel : une image mesurée toutes les SECONDES (défaut 30 ; 60 rapide, 10 fin).")
    group.add_argument("--hdr-fel-survival", type=_survival_spec, default=None, metavar="ENCODEUR:KBPS",
                       help="Avec --hdr-fel : test de survie à l'encodage (ex. libx265:35000).")
    group.add_argument("--hdr-fel-min", choices=tuple(_FEL_MIN), default=None,
                       help="Avec --hdr-fel : code de sortie 3 si le verdict est inférieur (moderate, useful).")


def _dynamic(config: AppConfig, info, stream: int | None, cache: InspectionCache | None, refresh: bool,
             ffmpeg: str, work_root: Path, logger: Logger) -> InspectionReport:
    runner = ToolRunner(max_workers=1)

    def run(cmd: object) -> object:
        return runner._run_cmd(cmd, label="hdr-inspection")  # type: ignore[arg-type]

    owned = create_process_work_dir(work_root, process_name="hdr-inspection")
    try:
        inspector = DynamicInspector(
            ffmpeg_bin=ffmpeg, dovi_tool_bin=str(config.tool_dovi_tool), hdr10plus_bin=str(config.tool_hdr10plus),
            run_cmd=run, work_dir=owned.path, cache=cache,
            on_step=lambda label, _start, _span: logger.emit("info", label))
        return inspector.analyze(info, stream, refresh=refresh)
    finally:
        owned.remove()


def _measure(config: AppConfig, report: InspectionReport, threads: int, work_root: Path,
             logger: Logger, fast: bool = False) -> ImageMeasurement:
    from core.fel.engine import FelEngine, measure_sampling_supported, measure_supported

    snapshot = report.snapshot
    if snapshot.codec.lower() != "hevc" or not snapshot.is_pq:
        raise CliError("Mesure réservée aux pistes HEVC PQ (HDR10, Dolby Vision, HDR10+).", EXIT_ARGS)
    if snapshot.dovi is not None and snapshot.dovi.profile == 5:
        raise CliError("Profil 5 : aucune couche HDR10 à mesurer.", EXIT_ARGS)
    engine = FelEngine.installed()
    if not measure_supported(engine, "base"):
        raise CliError("Mesure de l'image : extension mvo-fel 0.4.0 ou plus récente requise.", EXIT_TOOL)
    if fast and not measure_sampling_supported(engine, "keyframes"):
        raise CliError("Mesure rapide : extension mvo-fel 0.5.0 ou plus récente requise.", EXIT_TOOL)
    fps = snapshot.fps or 24000 / 1001
    frames = snapshot.frames or (int(round(snapshot.duration_s * fps)) if snapshot.duration_s else None)
    total = max(1, frames or 1)
    step = max(1, total // 20)
    owned = create_process_work_dir(work_root, process_name="hdr-mesure")
    try:
        statistics = owned.path / "mesure.jsonl"

        def progress(frames: int) -> None:
            if frames % step < 1 or frames == total:
                logger.emit("info", f"Mesure : {frames}/{total} images")

        backend = engine.measure(Path(snapshot.path), snapshot.stream_index, statistics, layer="base",
                                 threads=threads, progress=progress, cancelled=threading.Event(),
                                 sampling="keyframes" if fast else "all")
        logger.emit("info", f"Mesure de l'image : {backend}")
        return read_measurement(statistics, "base", total_frames=frames, fps=fps)
    finally:
        owned.remove()


def _fel_analysis(config: AppConfig, report: InspectionReport, *, interval_s: int,
                  survival: tuple[str, int] | None, ffmpeg: str, threads: int, logger: Logger,
                  cache: InspectionCache | None = None) -> FelReport:
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
        raise CliError("FEL Analyzer : aucune couche d'amélioration Dolby Vision (profil 7 attendu).", EXIT_ARGS)
    kind = nothing_to_measure(dovi)
    if kind is not None:
        reason = translate_text("{kind} : aucun résiduel, l'image est identique à la lecture P8.1 ; rien à mesurer.",
                                kind=kind)
        logger.emit("info", reason)
        return FelReport(None, (), summarise([]), (), notes=(reason,))
    try:
        engine = FelEngine.installed()
    except FelError as exc:
        raise CliError(f"FEL Analyzer : {exc}", EXIT_TOOL) from exc
    if not (layers_supported(engine, "mapped") and layers_supported(engine, "reconstructed")):
        raise CliError("FEL Analyzer : extension mvo-fel 0.4.0 ou plus récente requise.", EXIT_TOOL)
    fps = snapshot.fps or 24000 / 1001
    rate = Fraction(fps).limit_denominator(1001)
    cancel = threading.Event()

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
            raise CliError(f"FEL Analyzer : capture impossible ({exc}).", EXIT_TOOL) from exc
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
                raise CliError(f"FEL Analyzer : encodeur {encoder} indisponible : {detail or exc}", EXIT_TOOL) from exc
            except (RuntimeError, FelError, OSError) as exc:
                raise CliError(f"FEL Analyzer : test de survie impossible ({exc}).", EXIT_TOOL) from exc
            finally:
                close_capture(survival_capture)
            result = dataclasses.replace(result, survival=kept)
        return result
    finally:
        cancel.set()


def cmd_inspect_hdr(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    fel_interval = args.hdr_fel_interval if args.hdr_fel else None
    # Le type de couche (FEL / MEL) et la zone active viennent du RPU : niveau dynamique au minimum.
    level = str(args.hdr or "dynamic")
    if args.hdr_fel and level == "static":
        level = "dynamic"
    inputs = [Path(value).expanduser() for value in args.input]
    if args.hdr_compare and len(inputs) != 2:
        raise CliError("--hdr-compare : deux sources attendues (A puis B).", EXIT_ARGS)
    if args.hdr_report and len(inputs) > 1 and not args.hdr_compare:
        raise CliError("--hdr-report : une seule source, ou deux avec --hdr-compare.", EXIT_ARGS)
    if args.hdr_offset is not None and not args.hdr_compare:
        raise CliError("--hdr-offset s'utilise avec --hdr-compare.", EXIT_ARGS)
    options = common_options(args)
    ffmpeg = str(options.ffmpeg or config.tool_ffmpeg)
    inspector = FileInspector(ffprobe_bin=str(options.ffprobe or config.tool_ffprobe),
                              mediainfo_bin=str(options.mediainfo or config.tool_mediainfo))
    cache = InspectionCache(Path(config.app_data_dir) / "hdr_inspection")
    work_root = Path(options.work_dir).expanduser() if options.work_dir else config.ensure_work_dir()
    work_root.mkdir(parents=True, exist_ok=True)
    threads = max(2, int(options.threads or getattr(config, "ffmpeg_threads", 8) or 8))
    sides: list[Side] = []
    fel_reports: list[FelReport] = []
    for path in inputs:
        info = inspector.inspect(path)
        if not info.video_tracks:
            raise CliError(f"Aucune piste vidéo : {path}", EXIT_ARGS)
        report = snapshot_report(info, args.hdr_track)
        snapshot = report.snapshot
        measurement = None if args.hdr_no_cache else cache.load_measurement(Path(snapshot.path), snapshot.stream_index)
        if level in ("dynamic", "measure") and (snapshot.dovi is not None or snapshot.hdr10plus):
            logger.emit("info", f"Métadonnées dynamiques : {path.name}")
            report = _dynamic(config, info, snapshot.stream_index, cache, args.hdr_no_cache, ffmpeg, work_root, logger)
            for message in report.errors:
                logger.emit("warning", message)
        if level == "measure" and (measurement is None or (measurement.is_estimate and not args.hdr_fast)):
            measurement = _measure(config, report, threads, work_root, logger, fast=args.hdr_fast)
            cache.store_measurement(Path(snapshot.path), snapshot.stream_index, measurement)
        if measurement is not None:
            report = dataclasses.replace(report, findings=audit(report.snapshot, report.dovi, report.hdr10plus,
                                                                 measurement))
        if fel_interval is not None:
            fel_reports.append(_fel_analysis(config, report, interval_s=fel_interval, survival=args.hdr_fel_survival,
                                             ffmpeg=ffmpeg, threads=threads, logger=logger,
                                             cache=None if args.hdr_no_cache else cache))
        sides.append(Side(report, measurement))
    comparison = compare(sides[0], sides[1], args.hdr_offset) if args.hdr_compare else None
    fels: list[FelReport | None] = list(fel_reports) if fel_reports else [None] * len(sides)
    payload: dict = {"hdr": [report_data(side.report, measurement=side.measurement, fel=fel)
                             for side, fel in zip(sides, fels)]}
    if comparison is not None:
        payload["comparison"] = report_data(sides[0].report, measurement=sides[0].measurement, comparison=comparison,
                                            other=sides[1].report)["comparison"]
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    if args.hdr_report:
        target = write_report(Path(args.hdr_report).expanduser(), sides[0].report, measurement=sides[0].measurement,
                              fel=fels[0], comparison=comparison,
                              other=sides[1].report if comparison is not None else None)
        logger.emit("info", f"Rapport HDR écrit : {target}")
    for fel in fel_reports:
        _code, title, detail = verdict(fel)
        logger.emit("info", f"{title} — {detail}")
    if args.hdr_fel_min and any(verdict(fel)[0] not in _FEL_MIN[args.hdr_fel_min] for fel in fel_reports):
        return EXIT_VALIDATION
    if args.hdr_fail_on:
        failing = _FAIL_ON[args.hdr_fail_on]
        if any(f.severity in failing for side in sides for f in side.report.findings) or any(
                side.report.errors for side in sides):
            return EXIT_VALIDATION
    return EXIT_OK
