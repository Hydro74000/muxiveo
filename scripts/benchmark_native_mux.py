"""Compare l'assemblage MKV natif et FFmpeg sur la même source, sans encodage.

Exécution depuis la racine : python3 -m scripts.benchmark_native_mux SOURCE
--work-dir DOSSIER --report RAPPORT.json [--runs 3]. Les sorties temporaires
sont supprimées ; la source n'est jamais modifiée. Le profil cProfile est
mesuré séparément des chronométrages pour ne pas fausser la comparaison.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import platform
import statistics
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import CodeType

from core.matroska.assembly import (
    MatroskaAssemblyAttachment, MatroskaAssemblyPlan, MatroskaAssemblyTrack, assembly_output_contract,
    compile_assembly_plan,
)
from core.matroska.mux_plan import deterministic_source_identity
from core.matroska.progress import native_mux_progress_callback
from core.matroska.reader import MatroskaReader
from core.matroska.validation import validate_matroska_output
from core.matroska.writer import MatroskaWriter


def native_run(source: Path, output: Path, ffprobe: str) -> dict[str, float]:
    timings: dict[str, float] = {}
    started = time.perf_counter()
    reader = MatroskaReader(source)
    identity = deterministic_source_identity(source)
    assembly = MatroskaAssemblyPlan(
        output=output,
        ordered_tracks=tuple(
            MatroskaAssemblyTrack(source, index, identity)
            for index, _ in enumerate(reader.tracks())
        ),
        attachments=tuple(
            MatroskaAssemblyAttachment(source, index, identity)
            for index, _ in enumerate(reader.attachment_headers())
        ),
        chapter_source=source,
        tag_copy_sources=(source,),
        segment_info_source=source,
    )
    contract = assembly_output_contract(assembly)
    timings["contract_s"] = time.perf_counter() - started
    compiled = compile_assembly_plan(replace(assembly, expected_output_contract=contract))
    timings["compile_s"] = time.perf_counter() - started - timings["contract_s"]

    def validate(path, packet_validation):
        validation_started = time.perf_counter()
        errors = validate_matroska_output(path, contract, packet_validation=packet_validation)
        if errors:
            raise RuntimeError("; ".join(errors))
        timings["semantic_validation_s"] = time.perf_counter() - validation_started
        probe_started = time.perf_counter()
        subprocess.run([
            ffprobe, "-v", "error", "-show_entries", "format=format_name",
            "-of", "json", str(path),
        ], check=True, stdout=subprocess.DEVNULL)
        timings["ffprobe_s"] = time.perf_counter() - probe_started

    write_started = time.perf_counter()
    emit_progress = native_mux_progress_callback(lambda line: None)

    def progress(event):
        timings["packets"] = event.packets_written
        if "first_progress_s" not in timings:
            timings["first_progress_s"] = time.perf_counter() - started
        emit_progress(event)

    MatroskaWriter().write(
        compiled, external_validator=validate,
        progress_cb=progress,
    )
    timings["write_including_validation_s"] = time.perf_counter() - write_started
    timings["total_s"] = time.perf_counter() - started
    timings["output_bytes"] = output.stat().st_size
    return timings


def ffmpeg_run(source: Path, output: Path, ffmpeg: str, ffprobe: str) -> dict[str, float]:
    started = time.perf_counter()
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-map", "0", "-map_metadata", "0", "-c", "copy",
        "-f", "matroska", str(output),
    ], check=True)
    copied = time.perf_counter()
    errors = validate_matroska_output(output)
    if errors:
        raise RuntimeError("; ".join(errors))
    subprocess.run([
        ffprobe, "-v", "error", "-show_entries", "format=format_name",
        "-of", "json", str(output),
    ], check=True, stdout=subprocess.DEVNULL)
    return {
        "copy_s": copied - started,
        "total_s": time.perf_counter() - started,
        "output_bytes": output.stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs doit être positif")
    source = args.source.resolve(strict=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[dict[str, float]]] = {"native": [], "ffmpeg": []}
    with tempfile.TemporaryDirectory(prefix="native-mux-bench-", dir=args.work_dir) as directory:
        output = Path(directory) / "output.mkv"
        for index in range(args.runs + 1):
            # Une chauffe exclue, puis ordre alterné pour limiter le biais de cache.
            order = ("native", "ffmpeg") if index % 2 == 0 else ("ffmpeg", "native")
            for backend in order:
                if backend == "native":
                    result = native_run(source, output, args.ffprobe)
                else:
                    result = ffmpeg_run(source, output, args.ffmpeg, args.ffprobe)
                output.unlink()
                if index:
                    results[backend].append(result)
                    print(json.dumps({"backend": backend, "run": index, **result}), flush=True)
        profiler = cProfile.Profile()
        profiler.runcall(native_run, source, output, args.ffprobe)
        profile = [
            {"function": (
                f"{entry.code.co_filename}:{entry.code.co_firstlineno}:{entry.code.co_name}"
                if isinstance(entry.code, CodeType) else str(entry.code)
             ), "calls": entry.callcount,
             "self_s": entry.inlinetime, "cumulative_s": entry.totaltime}
            for entry in sorted(profiler.getstats(), key=lambda entry: entry.totaltime, reverse=True)[:30]
        ]
    args.report.write_text(json.dumps({
        "source": str(source), "source_bytes": source.stat().st_size,
        "python": platform.python_version(), "platform": platform.platform(),
        "ffmpeg": subprocess.check_output([args.ffmpeg, "-version"], text=True).splitlines()[0],
        "results": results,
        "medians_s": {name: statistics.median(row["total_s"] for row in rows)
                      for name, rows in results.items()},
        "native_profile": profile,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
