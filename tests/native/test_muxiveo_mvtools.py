"""Contrat réel du moteur CPU ; aucun VapourSynth système ni GPU requis."""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture(scope="module")
def binary():
    configured = os.environ.get("MUXIVEO_MVTOOLS_BIN")
    if not configured:
        pytest.skip("MUXIVEO_MVTOOLS_BIN non défini")
    path = Path(configured).resolve()
    assert path.is_file()
    return path


def clip(n=19, *, bits=8, chroma="420", width=96, height=64, fps="24:1", cut=None, static=False):
    """Motif non périodique déplacé ; les trames sources sont identifiables."""
    x, y = np.meshgrid(np.arange(width), np.arange(height))
    frames = []
    for i in range(n):
        shift = 0 if static else i
        luma = ((x - shift) * 3 + y * 2 + ((x - shift) // 13 % 2) * 47) % 220 + 16
        if cut is not None and i >= cut:
            luma = 255 - luma
        cw = width // (2 if chroma in ("420", "422") else 1)
        ch = height // (2 if chroma == "420" else 1)
        values = np.concatenate([luma.ravel(), np.full(cw * ch * 2, 128)])
        frames.append((values.astype("<u2") << (bits - 8)).tobytes() if bits > 8 else values.astype("u1").tobytes())
    c = chroma + (f"p{bits}" if bits > 8 else "")
    header = f"YUV4MPEG2 W{width} H{height} F{fps} Ip A1:1 C{c} XCOLORRANGE=LIMITED XTEST=hdr\n".encode()
    return header + b"".join(b"FRAME\n" + f for f in frames), frames


def run(binary, data, *args, ok=True):
    result = subprocess.run([str(binary), "--threads", "2", *args], input=data, capture_output=True, timeout=120)
    if ok:
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result


def unpack(result, frame_size):
    header, body = result.stdout.split(b"\n", 1)
    frames = []
    while body:
        assert body.startswith(b"FRAME\n")
        frames.append(body[6:6 + frame_size])
        assert len(frames[-1]) == frame_size
        body = body[6 + frame_size:]
    return header, frames


def test_self_test(binary):
    result = subprocess.run([str(binary), "--self-test", "--json"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    info = json.loads(result.stdout)
    assert info["ok"] and info["bits"] == [8, 10] and info["modes"] == ["standard", "uhd"]
    assert info["vapoursynth"] == 80 and info["mvtools"] == 29


@pytest.mark.parametrize("source,target", [("24:1", "48/1"), ("24:1", "72/1"), ("24:1", "96/1"),
    ("24000:1001", "60000/1001"), ("24000:1001", "60"), ("24:1", "60")])
@pytest.mark.parametrize("n", [1, 2, 19])
def test_exact_cadence_and_sources(binary, source, target, n):
    data, originals = clip(n, fps=source)
    result = run(binary, data, "--fps", target, "--scene-threshold", "0")
    header, images = unpack(result, len(originals[0]))
    ratio = Fraction(target) / Fraction(source.replace(":", "/"))
    assert len(images) == math.ceil(n * ratio)
    for j, frame in enumerate(images):
        time = j / ratio
        if time.denominator == 1:
            assert frame == originals[int(time)]
        if time >= n - 1:
            assert frame == originals[-1]
    assert b"XTEST=hdr" in header and b"XCOLORRANGE=LIMITED" in header
    assert f"in={n} out={len(images)}".encode() in result.stderr


@pytest.mark.parametrize("mode", ["standard", "uhd"])
@pytest.mark.parametrize("bits,chroma", [(8, "420"), (10, "420"), (12, "422"), (16, "444")])
def test_guards_and_formats(binary, mode, bits, chroma):
    data, originals = clip(21, bits=bits, chroma=chroma, width=98, height=66, cut=8)
    outputs = [run(binary, data, "--fps", "60", "--mode", mode, "--window-frames", str(w),
                   "--scene-threshold", "0").stdout for w in (8, 16)]
    assert outputs[0] == outputs[1]
    _, images = unpack(subprocess.CompletedProcess([], 0, stdout=outputs[0]), len(originals[0]))
    assert len(images) == 53


def test_cuts_static_flash_and_eof(binary):
    data, frames = clip(10, static=True)
    white = bytes([235]) * (96 * 64) + bytes([128]) * (96 * 64 // 2)
    frames[8] = white
    data = data.split(b"\n", 1)[0] + b"\n" + b"".join(b"FRAME\n" + f for f in frames)
    result = run(binary, data, "--factor", "2")
    _, images = unpack(result, len(frames[0]))
    assert images[15] == frames[7]  # coupe exactement à la frontière de fenêtre
    assert images[17] == frames[8]  # flash : répétition, aucun mélange
    assert images[-1] == frames[-1]


@pytest.mark.parametrize("args,data", [(["--factor", "2", "--fps", "60"], b""),
    ([], b"YUV4MPEG2 W32 H64 F24:1 Ip C420\n"),
    ([], b"YUV4MPEG2 W96 H64 F24:1 Ip Cmono\n"),
    ([], b"YUV4MPEG2 W96 H64 F24:1 Ip C420\nFRAME\nshort")])
def test_invalid_input(binary, args, data):
    result = run(binary, data, *args, ok=False)
    assert result.returncode != 0 and b"error:" in result.stderr


def test_isolated_and_missing_runtime(binary, tmp_path):
    copy = tmp_path / binary.name
    shutil.copy2(binary, copy)
    assert run(copy, clip(2)[0], ok=False).returncode == 3
    shutil.copytree(binary.parent / "mvtools-runtime", tmp_path / "mvtools-runtime")
    assert run(copy, clip(2)[0]).returncode == 0


def test_downstream_closed(binary, tmp_path):
    data, _ = clip(40)
    source = tmp_path / "input.y4m"
    source.write_bytes(data)
    with subprocess.Popen([str(binary), "-i", str(source)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        assert child.stdout is not None
        child.stdout.close()
        child.wait(timeout=30)
        assert child.returncode == 4


def test_cancel_while_waiting_for_input(binary):
    with subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        child.terminate()
        child.wait(timeout=10)
        assert child.returncode != 0


@pytest.mark.parametrize("bits", range(8, 17))
@pytest.mark.parametrize("chroma", ["420", "422", "444"])
def test_all_announced_depths(binary, bits, chroma):
    data, frames = clip(2, bits=bits, chroma=chroma)
    result = run(binary, data, "--factor", "2", "--scene-threshold", "0")
    _, output = unpack(result, len(frames[0]))
    assert output[0] == frames[0] and output[2:] == [frames[1], frames[1]]


def test_memory_does_not_grow_with_duration(binary, tmp_path):
    psutil = pytest.importorskip("psutil")
    peaks = []
    for n in (32, 512):
        source = tmp_path / "source.y4m"
        source.write_bytes(clip(n, width=160, height=96)[0])
        with subprocess.Popen([str(binary), "-i", str(source), "--threads", "2", "--scene-threshold", "0"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
            peak = 0
            process = psutil.Process(child.pid)
            while child.poll() is None:
                try:
                    peak = max(peak, process.memory_info().rss)
                except psutil.NoSuchProcess:
                    pass
                time.sleep(0.01)
            assert child.returncode == 0
            peaks.append(peak)
    assert peaks[1] < peaks[0] + 64 * 2**20, peaks
