"""CLI ``inspect --hdr`` : inspection HDR (en-têtes, métadonnées dynamiques, mesure), comparaison, rapport."""
from __future__ import annotations

import argparse
import dataclasses
import json
import threading
from pathlib import Path

from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_TOOL, EXIT_VALIDATION
from cli.errors import CliError
from cli.logging import Logger
from cli.options import common_options
from core.config import AppConfig
from core.hdr_inspection import DynamicInspector, InspectionCache, InspectionReport, Severity, audit, snapshot_report
from core.hdr_inspection.compare import Side, compare
from core.hdr_inspection.measure import ImageMeasurement, read_measurement
from core.hdr_inspection.report import report_data, write_report
from core.inspector import FileInspector
from core.runner import ToolRunner
from core.workdir import create_process_work_dir

LEVELS = ("static", "dynamic", "measure")
_FAIL_ON = {"warn": (Severity.WARN, Severity.ERROR), "error": (Severity.ERROR,)}


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


def cmd_inspect_hdr(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    level = str(args.hdr)
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
        sides.append(Side(report, measurement))
    comparison = compare(sides[0], sides[1], args.hdr_offset) if args.hdr_compare else None
    payload: dict = {"hdr": [report_data(side.report, measurement=side.measurement) for side in sides]}
    if comparison is not None:
        payload["comparison"] = report_data(sides[0].report, measurement=sides[0].measurement, comparison=comparison,
                                            other=sides[1].report)["comparison"]
    print(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    if args.hdr_report:
        target = write_report(Path(args.hdr_report).expanduser(), sides[0].report, measurement=sides[0].measurement,
                              comparison=comparison, other=sides[1].report if comparison is not None else None)
        logger.emit("info", f"Rapport HDR écrit : {target}")
    if args.hdr_fail_on:
        failing = _FAIL_ON[args.hdr_fail_on]
        if any(f.severity in failing for side in sides for f in side.report.findings) or any(
                side.report.errors for side in sides):
            return EXIT_VALIDATION
    return EXIT_OK
