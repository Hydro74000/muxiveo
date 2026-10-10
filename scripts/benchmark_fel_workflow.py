"""Compare le workflow DV existant avec et sans reconstruction FEL.

Lancement depuis la racine : python3 -m scripts.benchmark_fel_workflow SOURCE
--library /chemin/libmvo_fel.so --work-dir /chemin/benchmarks.
Les médias et journaux restent dans un nouveau dossier hors dépôt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import statistics
import subprocess
import tempfile
import time
from dataclasses import replace
from contextlib import nullcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from core.fel.engine import FelEngine
from core.pipeline_command import PipelineCommand
from core.runner import ToolRunner
from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.encode import EncodeConfig, EncodeWorkflow, VideoEncodeSettings
from tests.integration._synth import wait_task


class Monitor(Protocol):
    def start(self) -> None: ...
    def stop(self) -> dict: ...


class BenchmarkCleanupError(BaseException):
    """Arrête la matrice si le workflow précédent pourrait encore fonctionner."""


def cancel_and_wait(signals: Any) -> None:
    signals.cancel()
    state = wait_task(signals, timeout=15)
    if state["finished"] is None and not state["failed"] and not state["cancelled"]:
        raise BenchmarkCleanupError("Annulation non confirmée : arrêt du benchmark pour éviter des workflows simultanés.")


def probe(path: Path, *args: str) -> dict:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", *args, "-of", "json", str(path),
    ], check=True, capture_output=True, **subprocess_text_kwargs())
    return json.loads(result.stdout)


def excerpt(source: Path, root: Path, start: float, duration: float) -> tuple[Path, dict, str]:
    """Conserve le CRA et retire les RASL antérieures pour apparier BL/EL/RPU."""
    packets = probe(source, "-read_intervals", f"{start}%+0.5", "-show_packets")["packets"]
    key = next(packet["pts_time"] for packet in packets if "K" in packet.get("flags", ""))
    output = root / "source-excerpt.mkv"
    subprocess.run([
        "ffmpeg", "-v", "error", "-nostdin", "-ss", key, "-i", str(source),
        "-t", str(duration), "-map", "0:v:0", "-c", "copy", "-bsf:v", "noise=drop=lt(pts\\,0)",
        "-map_chapters", "-1", "-map_metadata", "-1", "-map_metadata:s:v:0", "-1",
        "-an", "-sn", "-dn", str(output),
    ], check=True, capture_output=True, **subprocess_text_kwargs())
    stream = probe(output, "-count_frames", "-show_streams")["streams"][0]
    record = next(item for item in stream.get("side_data_list", [])
                  if item.get("side_data_type") == "DOVI configuration record")
    if record["dv_profile"] != 7 or record["el_present_flag"] != 1:
        raise RuntimeError("Le benchmark nécessite une source P7 avec EL.")
    return output, stream, key


def run_case(root: Path, source: Path, stream: dict, video: VideoEncodeSettings,
             threads: int, repeat: int, *, rife_bin: str | None = None,
             label_suffix: str = "", monitor: Monitor | None = None) -> dict:
    enabled = video.bake_dovi_fel is True
    label = f"{video.codec}-{'fel' if enabled else 'bl'}{label_suffix}-{repeat}"
    directory = root / label
    directory.mkdir()
    output = directory / "output.mkv"
    source_duration = int(stream["nb_read_frames"])/float(Fraction(stream["avg_frame_rate"]))
    workflow = EncodeWorkflow(ffmpeg_threads=threads, ram_buffer_enabled=False, generate_nfo=False,
                              nvencc_bin=shutil.which("nvencc"), rife_bin=rife_bin)
    config = EncodeConfig(source=source, output=output, video=video, audio_tracks=[],
                          copy_subtitles=False, keep_chapters=False, work_dir=directory,
                          duration_s=source_duration)
    messages = []
    workflow.log_message.connect(lambda level, message: messages.append((level, message)))
    print(f"DÉBUT {label}", flush=True)
    if monitor:
        monitor.start()
    started = time.perf_counter()
    try:
        signals = workflow.run(config)
        state = wait_task(signals, timeout=1800)
        seconds = time.perf_counter()-started
        if state["finished"] is None and not state["failed"] and not state["cancelled"]:
            raise TimeoutError(f"Délai du workflow dépassé : {label}")
    except BaseException:
        if "signals" in locals():
            cancel_and_wait(signals)
        raise
    finally:
        telemetry = monitor.stop() if monitor else {}
    (directory / "workflow.json").write_text(json.dumps({
        "seconds": seconds, "messages": messages, "progress": state["progress"],
        "failed": str(state["failed"]) if state["failed"] else None,
        "telemetry": telemetry,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if state["failed"] or state["cancelled"] or state["finished"] is None:
        signals.cancel()
        raise RuntimeError(f"Workflow incomplet {label} : {state['failed']}")
    if enabled and not any("reconstruction FEL réussie" in message for _, message in messages):
        raise RuntimeError(f"Reconstruction FEL non confirmée pour {label}")
    if any("repli BL" in message or "reprise complète sur le BL" in message
           for message in [*(message for _, message in messages), *state["progress"]]):
        raise RuntimeError(f"Repli BL inattendu pour {label}")
    # La validation externe est hors du temps mesuré, dans les deux parcours.
    result = probe(output, "-count_frames", "-show_streams")["streams"][0]
    record = next(item for item in result.get("side_data_list", [])
                  if item.get("side_data_type") == "DOVI configuration record")
    expected_frames = int(stream["nb_read_frames"])
    expected_rate = Fraction(stream["avg_frame_rate"])
    if video.interpolation.is_active():
        ratio = video.interpolation.ratio(stream["avg_frame_rate"])
        expected_frames = int(expected_frames * ratio)
        expected_rate *= ratio
    if (int(result["nb_read_frames"]) != expected_frames or record["dv_profile"] != 8
            or record["dv_bl_signal_compatibility_id"] != 1 or record["el_present_flag"] != 0
            or record["rpu_present_flag"] != 1 or result["pix_fmt"] != "yuv420p10le"
            or result["color_transfer"] != "smpte2084"):
        raise RuntimeError(f"Sortie inattendue pour {label}")
    frames = probe(output, "-show_frames", "-show_entries", "frame=best_effort_timestamp_time")["frames"]
    pts = [float(frame["best_effort_timestamp_time"]) for frame in frames]
    pts_error_ms = max(abs(value-pts[0]-float(index/expected_rate))*1000
                       for index, value in enumerate(pts))
    if (len(pts) != expected_frames or any(a >= b for a, b in zip(pts, pts[1:]))
            or pts_error_ms > 1.01):
        raise RuntimeError(f"Horodatages inattendus pour {label} : {pts_error_ms:.6f} ms")
    measured = {"codec": video.codec, "preset": video.preset, "fel": enabled,
                "repeat": repeat, "seconds": seconds, "frames": int(result["nb_read_frames"]),
                "fps": int(result["nb_read_frames"])/seconds, "width": result["width"],
                "height": result["height"], "bytes": output.stat().st_size,
                "source_frames": int(stream["nb_read_frames"]),
                "pts_error_ms": pts_error_ms,
                "output_mbit_s": output.stat().st_size*8/source_duration/1e6,
                "fel_device": video.fel_device, "rife": video.interpolation.is_active(),
                "rife_gpu": video.interpolation.gpu, "telemetry": telemetry,
                "fel_backend": [message for _, message in messages
                                if "moteur choisi" in message or "moteur exécuté" in message or "backend" in message]}
    print(f"FIN {label} : {seconds:.3f} s, {measured['fps']:.3f} i/s", flush=True)
    return measured


def summaries(cases: list[dict]) -> list[dict]:
    result = []
    for codec in dict.fromkeys(case["codec"] for case in cases):
        selected = [case for case in cases if case["codec"] == codec]
        if len({(case["frames"], case["width"], case["height"]) for case in selected}) != 1:
            raise RuntimeError(f"Comparaison de géométrie ou de durée inégales : {codec}")
        baseline = [case["seconds"] for case in selected if not case["fel"]]
        reconstructed = [case["seconds"] for case in selected if case["fel"]]
        before, after = statistics.median(baseline), statistics.median(reconstructed)
        result.append({"codec": codec, "preset": selected[0]["preset"],
                       "bl_median_s": before, "fel_median_s": after,
                       "bl_range_s": [min(baseline), max(baseline)],
                       "fel_range_s": [min(reconstructed), max(reconstructed)],
                       "added_s": after-before, "added_percent": 100*(after/before-1),
                       "time_ratio": after/before, "bl_fps": selected[0]["frames"]/before,
                       "fel_fps": selected[0]["frames"]/after})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--device", default="auto", help="auto, cpu ou UUID Vulkan du plugin")
    parser.add_argument("--start", type=float, default=480)
    parser.add_argument("--duration", type=float, default=3)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--threads", type=int, default=12)
    parser.add_argument("--fast-pipe-probe", action="store_true",
                        help="Expérience : analyse minimale des pipes NUT internes")
    parser.add_argument("--codecs", nargs="+", choices=("libx265", "nvencc_hevc"),
                        default=["libx265", "nvencc_hevc"])
    args = parser.parse_args()
    if args.runs < 1 or args.duration <= 0 or args.threads < 1:
        parser.error("Durée, passages et threads doivent être positifs.")
    if "nvencc_hevc" in args.codecs and not shutil.which("nvencc"):
        parser.error("NVEncC absent ; choisir --codecs libx265.")
    app = QCoreApplication.instance() or QCoreApplication([])
    args.work_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="fel-benchmark-", dir=args.work_dir))
    print(f"Résultats : {root}", flush=True)
    source_stat = args.source.stat()
    source, stream, key = excerpt(args.source, root, args.start, args.duration)
    engine = FelEngine(args.library)
    document = {"date": time.strftime("%Y-%m-%d %H:%M:%S %z"), "platform": platform.platform(),
                "source": str(args.source), "source_bytes": source_stat.st_size,
                "source_mtime_ns": source_stat.st_mtime_ns, "excerpt_start_s": key,
                "frames": int(stream["nb_read_frames"]), "frame_rate": stream["avg_frame_rate"],
                "threads": args.threads, "runs": args.runs, "fel_device": args.device, "fast_pipe_probe": args.fast_pipe_probe,
                "engine_capabilities": json.loads(engine.lib.mvo_fel_capabilities()),
                "plugin_sha256": hashlib.sha256(args.library.read_bytes()).hexdigest(), "cases": []}
    common = VideoEncodeSettings(source_path=source, copy_dv=True, dovi_source_profile="p7_fel",
                                bit_depth="10", source_bit_depth=10, source_pix_fmt="yuv420p10le",
                                source_codec="hevc", source_color_transfer="smpte2084",
                                input_frame_rate=stream["avg_frame_rate"], crf=18, cq=26, fel_device=args.device)
    report = root / "results.json"
    normal_run = ToolRunner._run_cmd

    def fast_pipe_run(self: ToolRunner, command: list[str], *values: Any, **keywords: Any) -> Any:
        if isinstance(command, PipelineCommand) and command.producer is not None:
            for stage in [*command.upstream, command]:
                if Path(stage[0]).stem.casefold() == "ffmpeg" and "-i" in stage and stage[stage.index("-i")+1] == "pipe:0":
                    pos = stage.index("-i")
                    stage[pos:pos] = ["-analyzeduration", "1", "-probesize", "32", "-fpsprobesize", "0"]
            if Path(command[0]).stem.casefold() == "nvencc":
                command.extend(["--input-analyze", "0", "--input-probesize", "32"])
        return normal_run(self, command, *values, **keywords)

    probe_patch = patch.object(ToolRunner, "_run_cmd", new=fast_pipe_run) if args.fast_pipe_probe else nullcontext()
    with patch.object(FelEngine, "installed", return_value=engine), probe_patch:
        for repeat in range(1, args.runs+1):
            for codec in args.codecs:
                # Alterner l'ordre limite les biais de cache et de montée en température.
                for enabled in ([False, True] if repeat % 2 else [True, False]):
                    video = replace(common, codec=codec, preset="medium" if codec == "libx265" else "default",
                                    bake_dovi_fel=enabled)
                    document["cases"].append(run_case(root, source, stream, video, args.threads, repeat))
                    report.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    document["summary"] = summaries(document["cases"])
    final_stat = args.source.stat()
    if (source_stat.st_size, source_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
        raise RuntimeError("La source a changé pendant le benchmark.")
    report.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(document["summary"], ensure_ascii=False, indent=2), flush=True)
    app.processEvents()


if __name__ == "__main__":
    main()
