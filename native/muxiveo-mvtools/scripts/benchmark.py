#!/usr/bin/env python3
"""Banc synthétique reproductible : référence 120 i/s, qualité, débit et pic RSS."""
from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import psutil

SCENES = ("bars", "grid", "stairs", "pan", "zoom", "occlusion")


def render(scene, tick, width, height, bits):
    """La position tick est exprimée en images de la référence 120 i/s."""
    x, y = np.meshgrid(np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32))
    dx = tick * 0.8
    if scene == "zoom":
        scale = 1 + tick / 600
        x = (x - width / 2) / scale + width / 2
        y = (y - height / 2) / scale + height / 2
    x -= dx
    if scene in ("bars", "occlusion"):
        # Barreaux de 8 pixels espacés de 24, avec contours sous-pixel.
        distance = np.abs((x + 12) % 24 - 12)
        luma = 32 + 184 * np.clip(4.5 - distance, 0, 1)
    elif scene in ("grid", "zoom"):
        distance = np.minimum(np.abs((x + 12) % 24 - 12), np.abs((y + 12) % 24 - 12))
        luma = 32 + 184 * np.clip(2.5 - distance, 0, 1)
    elif scene == "stairs":
        distance = ((x + np.floor(y / 10) * 8) % 40)
        luma = 32 + 184 * np.clip(12.5 - distance, 0, 1)
    else:
        luma = 126 + 54 * np.sin(x / 11) + 35 * np.cos(y / 17) + 20 * np.cos((x + y) / 7)
    if scene == "occlusion":
        mask = (x + 2 * dx > width / 3) & (x + 2 * dx < width * 2 / 3) & (y > height / 4) & (y < height * 3 / 4)
        luma = np.where(mask, 180, luma)
    return np.rint(luma * (1 << (bits - 8))).astype("<u2" if bits > 8 else "u1")


def write_source(path, scene, n, width, height, bits):
    c = "420" + (f"p{bits}" if bits > 8 else "")
    chroma = np.full(width * height // 2, 128 << (bits - 8), dtype="<u2" if bits > 8 else "u1").tobytes()
    with path.open("wb") as stream:
        stream.write(f"YUV4MPEG2 W{width} H{height} F24:1 Ip C{c} XCOLORRANGE=LIMITED\n".encode())
        for i in range(n):
            stream.write(b"FRAME\n" + render(scene, i * 5, width, height, bits).tobytes() + chroma)


def measure(command, destination):
    """RSS du seul moteur, sans les allocations du générateur/lecteur du banc."""
    with destination.open("wb") as output, tempfile.TemporaryFile() as log:
        start = time.monotonic()
        child = subprocess.Popen(command, stdout=output, stderr=log)
        process = psutil.Process(child.pid)
        peak = 0
        while child.poll() is None:
            try:
                peak = max(peak, process.memory_info().rss)
            except psutil.NoSuchProcess:
                pass
            time.sleep(0.01)
        elapsed = time.monotonic() - start
        log.seek(0)
        stderr = log.read().decode(errors="replace")
        if child.returncode:
            raise RuntimeError(stderr)
    return elapsed, peak, stderr


def quality(output, scene, n, width, height, bits):
    raw = output.read_bytes().split(b"\n", 1)[1]
    dtype = "<u2" if bits > 8 else "u1"
    frame_bytes = width * height * 3 // 2 * (2 if bits > 8 else 1)
    maes, edges, flicker, motions = [], [], [], []
    previous_error = previous_image = None
    repeated = 0
    count = 0
    while raw:
        if not raw.startswith(b"FRAME\n") or len(raw) < 6 + frame_bytes:
            raise RuntimeError("sortie Y4M tronquée")
        image = np.frombuffer(raw[6:6 + frame_bytes], dtype=dtype, count=width * height).reshape(height, width).astype(np.float32) / (1 << (bits - 8))
        raw = raw[6 + frame_bytes:]
        tick = min(count * 2, (n - 1) * 5)
        truth = render(scene, tick, width, height, bits).astype(np.float32) / (1 << (bits - 8))
        error = image - truth
        maes.append(float(np.abs(error).mean()))
        edges.append(float(np.abs(np.diff(image, axis=1) - np.diff(truth, axis=1)).mean()))
        if previous_error is not None:
            flicker.append(float(np.abs(error - previous_error).mean()))
            repeated += int(np.array_equal(image, previous_image))
        # Décalage résiduel horizontal par rapport à la référence (proxy de mouvement).
        if scene not in ("zoom", "occlusion") and count % 5:
            candidates = [float(np.abs(image[:, 8:-8] - np.roll(truth, shift, axis=1)[:, 8:-8]).mean()) for shift in range(-8, 9)]
            motions.append(abs(int(np.argmin(candidates)) - 8))
        previous_error, previous_image = error, image
        count += 1
    assert count == math.ceil(n * 2.5)
    return {"frames_out": count, "luma_mae_8bit": float(np.mean(maes)),
            "horizontal_edge_error": float(np.mean(edges)), "temporal_error_variation": float(np.mean(flicker)),
            "residual_horizontal_shift_px": float(np.mean(motions)) if motions else None,
            "repeated_adjacent_frames": repeated}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mvtools-bin", type=Path, required=True)
    parser.add_argument("--rife-bin", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=144)
    parser.add_argument("--bits", type=int, default=10)
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--scenes", nargs="+", choices=SCENES, default=list(SCENES))
    parser.add_argument("--resource-only", action="store_true")
    args = parser.parse_args()
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for scene in args.scenes:
            source = Path(tmp) / "source.y4m"
            write_source(source, scene, args.frames, args.width, args.height, args.bits)
            engines = [("mvtools-standard", args.mvtools_bin, ["--mode", "standard", "--threads", str(max(1, min(4, args.threads // 2)))]),
                       ("mvtools-uhd", args.mvtools_bin, ["--mode", "uhd", "--threads", str(args.threads)])]
            if args.rife_bin:
                engines.append(("rife-v4.6", args.rife_bin, ["--model", "rife-v4.6"]))
            for engine, binary, options in engines:
                output = Path(tmp) / "output.y4m"
                elapsed, peak, stderr = measure([str(binary.resolve()), "-i", str(source), "--fps", "60",
                                                "--scene-threshold", "0", *options], output)
                row = {"engine": engine, "scene": scene, "seconds": elapsed, "peak_rss_bytes": peak,
                       "fps_out": math.ceil(args.frames * 2.5) / elapsed, "stderr": stderr.strip()}
                if not args.resource_only:
                    row.update(quality(output, scene, args.frames, args.width, args.height, args.bits))
                rows.append(row)
                print(engine, scene, f"{elapsed:.2f}s {peak / 2**20:.1f}Mio", flush=True)
    report = {"platform": platform.platform(), "cpu": platform.processor(), "logical_cpus": psutil.cpu_count(),
              "width": args.width, "height": args.height, "bits": args.bits, "source_frames": args.frames,
              "thread_budget": args.threads, "reference_fps": 120, "source_fps": 24, "target_fps": 60, "results": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
