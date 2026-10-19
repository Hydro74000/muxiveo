"""Mesure FEL × RIFE × encodeur, avec placements explicites et télémétrie facultative.

Depuis la racine : python3 -m scripts.benchmark_fel_matrix SOURCE --library LIB
--work-dir DOSSIER. Les GPU sont énumérés séparément pour FEL et RIFE : leurs
indices ne sont pas interchangeables. Aucun média n'est écrit dans le dépôt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from core.config import AppConfig
from core.fel.devices import FelDevice, probe_load
from core.fel.engine import FelEngine
from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.encode.catalog import default_preset_for_codec
from core.workflows.encode.models import FrameInterpolationSettings, VideoEncodeSettings
from scripts.benchmark_fel_workflow import excerpt, probe, run_case


def benchmark_preset(codec: str) -> str:
    """x265 medium demandé ; défaut applicatif pour les encodeurs matériels."""
    return "medium" if codec == "libx265" else default_preset_for_codec(codec)


def numeric(value: str) -> float | None:
    try:
        return float(value.strip())
    except ValueError:
        return None


def tool_versions(rife_bin: str) -> dict[str, str]:
    """Versions locales sans accès réseau ni téléchargement de moteur."""
    versions = {}
    for name, args in (("ffmpeg", ["ffmpeg", "-version"]),
                       ("nvencc", ["nvencc", "--version"]),
                       ("rife", [rife_bin, "--version"]),
                       ("nvidia_driver", ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])):
        try:
            result = subprocess.run(args, check=False, capture_output=True, timeout=5,
                                    **subprocess_text_kwargs())
            text = (result.stdout or result.stderr).strip()
            if result.returncode == 0 and text:
                versions[name] = text.splitlines()[0]
        except (OSError, subprocess.SubprocessError):
            pass
    return versions


class Telemetry:
    """Échantillons système, pas une attribution exacte à chaque processus."""

    def __init__(self, devices: tuple[FelDevice, ...], interval: float = 0.5):
        self.devices = devices
        self.interval = interval
        self.samples: list[dict] = []
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.started = 0.0

    def start(self) -> None:
        self.started = time.perf_counter()
        self.thread.start()

    def _sample(self) -> dict:
        row: dict = {"elapsed_s": time.perf_counter()-self.started, "gpus": {}}
        # NVIDIA : distinguer calcul, encodeur et décodeur, même sous Windows.
        if any(d.vendor == 0x10DE for d in self.devices):
            try:
                result = subprocess.run(["nvidia-smi", "--query-gpu=uuid,utilization.gpu,utilization.encoder,utilization.decoder,memory.used,memory.total,power.draw,clocks.sm,temperature.gpu", "--format=csv,noheader,nounits"],
                                        check=False, capture_output=True, timeout=2,
                                        **subprocess_text_kwargs())
                for line in result.stdout.splitlines() if result.returncode == 0 else []:
                    values = line.split(",")
                    if len(values) != 9:
                        continue
                    key = values[0].strip().lower().removeprefix("gpu-").replace("-", "")
                    row["gpus"][key] = dict(zip(
                        ("compute_percent", "encoder_percent", "decoder_percent", "memory_used_mib",
                         "memory_total_mib", "power_w", "clock_mhz", "temperature_c"),
                        (numeric(v) for v in values[1:]), strict=True))
            except (OSError, subprocess.SubprocessError):
                pass
        # Autres constructeurs : sysfs si disponible ; sinon charge inconnue.
        for key, load in probe_load(tuple(d for d in self.devices if d.vendor != 0x10DE)).items():
            row["gpus"][key] = {"compute_percent": load.busy}
        try:
            fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
            ticks = [int(v) for v in fields[:8]]
            row["cpu_ticks"] = {"total": sum(ticks), "idle": sum(ticks[3:5])}
        except (OSError, ValueError, IndexError):
            pass
        return row

    def _run(self) -> None:
        while not self.done.is_set():
            self.samples.append(self._sample())
            self.done.wait(self.interval)

    def stop(self) -> dict:
        self.done.set()
        self.thread.join(timeout=4)
        devices = {}
        for device in self.devices:
            rows = [r["gpus"].get(device.uuid, {}) for r in self.samples]
            metrics = {}
            for key in {k for row in rows for k in row}:
                values = sorted(row[key] for row in rows if row.get(key) is not None)
                if values:
                    metrics[key] = {"mean": statistics.mean(values), "max": max(values),
                                    "p95": values[int((len(values)-1)*0.95)], "samples": len(values)}
            devices[device.uuid] = {"name": device.name, "metrics": metrics}
        cpu = []
        for before, after in zip(self.samples, self.samples[1:]):
            a, b = before.get("cpu_ticks"), after.get("cpu_ticks")
            if a and b and b["total"] > a["total"]:
                cpu.append(100*(1-(b["idle"]-a["idle"])/(b["total"]-a["total"])))
        return {"interval_s": self.interval, "scope": "whole-machine", "gpus": devices,
                "cpu_system_mean_percent": statistics.mean(cpu) if cpu else None,
                "samples": self.samples}


def matrix_summary(cases: list[dict]) -> list[dict]:
    """Comparer chaque placement au BL avec le même encodeur et le même RIFE."""
    result = []
    for key in dict.fromkeys((c["codec"], c["rife_gpu_choice"]) for c in cases):
        group = [c for c in cases if (c["codec"], c["rife_gpu_choice"]) == key and "error" not in c]
        baseline = [c["seconds"] for c in group if c["fel_choice"] == "off"]
        before = statistics.median(baseline) if baseline else None
        if len({(c["frames"], c["width"], c["height"]) for c in group}) > 1:
            raise RuntimeError(f"Comparaison de géométrie/durée inégales : {key}")
        for choice in dict.fromkeys(c["fel_choice"] for c in group):
            selected = [c for c in group if c["fel_choice"] == choice]
            values = [c["seconds"] for c in selected]
            median = statistics.median(values)
            telemetry: dict[str, dict[str, dict[str, float]]] = {}
            for device in {uuid for c in selected for uuid in c.get("telemetry", {}).get("gpus", {})}:
                metrics = [c["telemetry"]["gpus"].get(device, {}).get("metrics", {}) for c in selected]
                telemetry[device] = {}
                for metric in {name for row in metrics for name in row}:
                    measured = [row[metric] for row in metrics if metric in row]
                    telemetry[device][metric] = {"median_mean": statistics.median(m["mean"] for m in measured),
                                                 "max": max(m["max"] for m in measured)}
            result.append({"codec": key[0], "rife_gpu": key[1], "fel_device": choice,
                           "runs": len(values), "median_s": median, "range_s": [min(values), max(values)],
                           "output_fps": selected[0]["frames"]/median,
                           "gpu_telemetry": telemetry,
                           "added_percent_same_rife": 100*(median/before-1) if before else None})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--resume", type=Path, help="Reprendre ce results.json ; --runs est le total souhaité")
    parser.add_argument("--start", type=float, default=480)
    parser.add_argument("--duration", type=float, default=3)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--threads", type=int, default=12)
    parser.add_argument("--fel-devices", nargs="+", default=["off", "cpu", "all"],
                        help="off, cpu, auto, all (tous les UUID détectés) ou UUID")
    parser.add_argument("--rife-gpus", nargs="+", default=["off", "auto"],
                        help="off, auto, all (hors CPU Vulkan) ou indices propres à RIFE")
    parser.add_argument("--rife-quality", choices=("fast", "balanced", "quality", "ultra", "light"), default="balanced")
    parser.add_argument("--codecs", nargs="+", choices=("libx265", "nvencc_hevc", "hevc_nvenc"), default=["libx265", "nvencc_hevc"])
    args = parser.parse_args()
    if args.runs < 1 or args.duration <= 0 or args.threads < 1:
        parser.error("Durée, passages et threads doivent être positifs.")
    if "nvencc_hevc" in args.codecs and not shutil.which("nvencc"):
        parser.error("NVEncC absent ; choisir --codecs libx265 (ou hevc_nvenc si disponible).")
    app = QCoreApplication.instance() or QCoreApplication([])
    engine = FelEngine(args.library)
    devices = engine.devices()
    rife_bin = AppConfig().tool_muxiveo_rife
    rife_inventory = {}
    if args.rife_gpus != ["off"]:
        result = subprocess.run([rife_bin, "--list-gpus"], check=True, capture_output=True,
                                **subprocess_text_kwargs())
        rife_inventory = json.loads(result.stdout)
    fel_choices = list(dict.fromkeys(c for entry in args.fel_devices for c in
                                    ([d.uuid for d in devices] if entry == "all" else [entry])))
    rife_choices = list(dict.fromkeys(c for entry in args.rife_gpus for c in
                                     ([str(d["index"]) for d in rife_inventory.get("gpus", [])
                                       if d.get("type") != "cpu"] if entry == "all" else [entry])))
    if any(c not in {"off", "cpu", "auto", *(d.uuid for d in devices)} for c in fel_choices):
        parser.error("UUID FEL absent de l'inventaire natif.")
    available_rife = {str(d["index"]) for d in rife_inventory.get("gpus", [])}
    if any(c not in {"off", "auto", *available_rife} for c in rife_choices):
        parser.error("Index RIFE absent de son inventaire.")
    if not fel_choices or not rife_choices:
        parser.error("Aucune configuration matérielle à mesurer.")
    versions = tool_versions(rife_bin)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    if args.resume:
        report = args.resume.resolve()
        root = report.parent
        document = json.loads(report.read_text())
        source = root / "source-excerpt.mkv"
        stream = probe(source, "-count_frames", "-show_streams")["streams"][0]
        if (document["plugin_sha256"] != hashlib.sha256(args.library.read_bytes()).hexdigest()
                or document["threads"] != args.threads or document["rife_quality"] != args.rife_quality
                or document["source"] != str(args.source.resolve())
                or document["excerpt_sha256"] != hashlib.sha256(source.read_bytes()).hexdigest()):
            parser.error("Reprise incompatible avec le moteur, les threads, la source ou la qualité RIFE.")
        if (document.get("tool_versions") != versions
                or document.get("platform") != platform.platform()
                or document.get("machine") != platform.machine()
                or document.get("logical_cpus") != os.cpu_count()
                or document.get("fel_inventory") != [asdict(d) for d in devices]):
            parser.error("Matériel, pilotes ou outils modifiés : démarrer une nouvelle série de mesures.")
        if rife_inventory:
            if document.get("rife_inventory") and document["rife_inventory"] != rife_inventory:
                parser.error("Inventaire RIFE modifié : démarrer une nouvelle série de mesures.")
            document["rife_inventory"] = rife_inventory
    else:
        root = Path(tempfile.mkdtemp(prefix="fel-matrix-", dir=args.work_dir))
        report = root / "results.json"
        source, stream, key = excerpt(args.source, root, args.start, args.duration)
        document = {"date": time.strftime("%Y-%m-%d %H:%M:%S %z"), "platform": platform.platform(),
                    "machine": platform.machine(), "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
                    "source": str(args.source.resolve()), "source_bytes": args.source.stat().st_size,
                    "excerpt_start_s": key, "excerpt_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "frames": int(stream["nb_read_frames"]), "frame_rate": stream["avg_frame_rate"],
                    "threads": args.threads, "rife_quality": args.rife_quality, "rife_inventory": rife_inventory,
                    "tool_versions": versions, "rife_backend": "vulkan (sans extension TRT)",
                    "fel_inventory": [asdict(d) for d in devices], "cases": [],
                    "plugin_sha256": hashlib.sha256(args.library.read_bytes()).hexdigest(),
                    "engine_capabilities": json.loads(engine.lib.mvo_fel_capabilities())}
    print(f"Résultats : {report}", flush=True)
    common = VideoEncodeSettings(source_path=source, copy_dv=True, dovi_source_profile="p7_fel",
                                bit_depth="10", source_bit_depth=10, source_pix_fmt="yuv420p10le",
                                source_codec="hevc", source_color_transfer="smpte2084",
                                input_frame_rate=stream["avg_frame_rate"], crf=18, cq=26)
    with patch.object(FelEngine, "installed", return_value=engine):
        for repeat in range(1, args.runs+1):
            for codec in args.codecs:
                for rife in rife_choices if repeat % 2 else list(reversed(rife_choices)):
                    for fel in fel_choices if repeat % 2 else list(reversed(fel_choices)):
                        identity = {"codec": codec, "fel_choice": fel, "rife_gpu_choice": rife, "repeat": repeat}
                        if any(all(c.get(k) == v for k, v in identity.items()) for c in document["cases"]):
                            continue
                        video = replace(common, codec=codec, preset=benchmark_preset(codec),
                                        bake_dovi_fel=fel != "off", fel_device=fel if fel != "off" else "auto",
                                        interpolation=FrameInterpolationSettings(enabled=rife != "off", factor=2,
                                            quality=args.rife_quality, gpu=int(rife) if rife.isdigit() else -1))
                        try:
                            measured = run_case(root, source, stream, video, args.threads, repeat,
                                                rife_bin=rife_bin, label_suffix=f"-{fel}-rife-{rife}",
                                                monitor=Telemetry(devices))
                            document["cases"].append({**measured, **identity})
                        except Exception as error:
                            print(f"ÉCHEC {identity} : {error}", flush=True)
                            document["cases"].append({**identity, "error": str(error)})
                        document["summary"] = matrix_summary(document["cases"])
                        report.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in document.get("summary", []):
        added = row["added_percent_same_rife"]
        overhead = f"{added:.1f}%" if added is not None else "indisponible (BL non mesuré)"
        print(f"{row['codec']} RIFE={row['rife_gpu']} FEL={row['fel_device']} "
              f"n={row['runs']} médiane={row['median_s']:.3f}s "
              f"surcoût={overhead}", flush=True)
    app.processEvents()
    if any("error" in c for c in document["cases"]):
        raise SystemExit("Des configurations ont échoué ; consulter results.json et leurs journaux.")


if __name__ == "__main__":
    main()
