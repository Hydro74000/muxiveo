"""Tests fonctionnels du binaire natif ``muxiveo-rife`` (native/muxiveo-rife).

Exécutés sur un vrai périphérique Vulkan (llvmpipe/lavapipe accepté en CI).
Binaire : ``MUXIVEO_RIFE_BIN`` ou ``muxiveo-rife`` dans le PATH ; ignorés sinon.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest


def _rife_bin() -> str | None:
    candidate = os.environ.get("MUXIVEO_RIFE_BIN") or shutil.which("muxiveo-rife")
    return candidate if candidate and Path(candidate).is_file() else None


RIFE_BIN = _rife_bin() or ""


def _gpus() -> list[dict]:
    if not RIFE_BIN:
        return []
    out = subprocess.run([RIFE_BIN, "--list-gpus"], capture_output=True, text=True, timeout=120, check=False)
    try:
        return list(json.loads(out.stdout or "{}").get("gpus") or [])
    except ValueError:
        return []


GPUS = _gpus()

pytestmark = pytest.mark.skipif(
    not RIFE_BIN or not GPUS or shutil.which("ffmpeg") is None,
    reason="muxiveo-rife, un périphérique Vulkan et ffmpeg requis",
)


# ---------------------------------------------------------------------------
# y4m minimal
# ---------------------------------------------------------------------------

@dataclass
class Y4m:
    header: dict[str, str]
    frames: list[bytes]

    @property
    def fps(self) -> str:
        return self.header["F"]


def parse_y4m(data: bytes) -> Y4m:
    end = data.index(b"\n")
    tokens = data[:end].decode().split()
    assert tokens[0] == "YUV4MPEG2"
    header = {tok[0]: tok[1:] for tok in tokens[1:]}
    width, height = int(header["W"]), int(header["H"])
    cs = header.get("C", "420jpeg")
    depth = int(cs.split("p", 1)[1]) if "p" in cs[3:4] and cs[4:5].isdigit() else 8
    bps = 2 if depth > 8 else 1
    sub = {"420": (2, 2), "422": (2, 1), "444": (1, 1)}[cs[:3]]
    cw, ch = -(-width // sub[0]), -(-height // sub[1])
    size = (width * height + 2 * cw * ch) * bps
    frames: list[bytes] = []
    pos = end + 1
    while pos < len(data):
        line_end = data.index(b"\n", pos)
        assert data[pos:line_end].startswith(b"FRAME")
        pos = line_end + 1
        frames.append(data[pos:pos + size])
        pos += size
    return Y4m(header, frames)


def make_y4m(lavfi: str, *, frames: int, pix_fmt: str = "yuv420p", extra: list[str] | None = None) -> bytes:
    cmd = [
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", lavfi, "-frames:v", str(frames),
        *(extra or []), "-pix_fmt", pix_fmt, "-f", "yuv4mpegpipe", "-strict", "-1", "-",
    ]
    return subprocess.run(cmd, capture_output=True, check=True).stdout


def run_rife(data: bytes, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [RIFE_BIN, "--quiet", "-g", str(_test_gpu()), *args],
        input=data, capture_output=True, timeout=600, check=False,
    )


def _test_gpu() -> int:
    """GPU des tests : ``MUXIVEO_RIFE_GPU`` sinon le périphérique par défaut."""
    return int(os.environ.get("MUXIVEO_RIFE_GPU", "-1"))


def _psnr(a: bytes, b: bytes, depth: int) -> float:
    dtype = np.uint16 if depth > 8 else np.uint8
    x = np.frombuffer(a, dtype=dtype).astype(np.float64)
    y = np.frombuffer(b, dtype=dtype).astype(np.float64)
    mse = float(np.mean((x - y) ** 2))
    peak = float((1 << depth) - 1)
    return 99.0 if mse == 0 else 10 * np.log10(peak * peak / mse)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_version_and_gpu_listing() -> None:
    out = subprocess.run([RIFE_BIN, "--version"], capture_output=True, text=True, check=True)
    assert out.stdout.startswith("muxiveo-rife ")
    assert all({"index", "name", "type"} <= set(gpu) for gpu in GPUS)


@pytest.mark.parametrize("pix_fmt", ["yuv420p", "yuv420p10le"])
def test_factor_two_keeps_originals_bit_exact(pix_fmt: str) -> None:
    data = make_y4m("testsrc2=size=160x96:rate=25", frames=8, pix_fmt=pix_fmt)
    src = parse_y4m(data)
    proc = run_rife(data, "--factor", "2", "--matrix", "bt709")
    assert proc.returncode == 0, proc.stderr.decode()
    out = parse_y4m(proc.stdout)
    assert out.fps == "50:1"
    assert len(out.frames) == 2 * len(src.frames)
    assert out.frames[0::2] == src.frames
    # dernière trame : duplication de la dernière source (pas de trame suivante)
    assert out.frames[-1] == src.frames[-1]
    # trames générées distinctes des sources (mouvement réel)
    assert out.frames[1] not in (src.frames[0], src.frames[1])
    assert b"done in=8 out=16" in proc.stderr


def test_factor_three_and_target_fps() -> None:
    data = make_y4m("testsrc2=size=128x64:rate=24000/1001", frames=5)
    proc = run_rife(data, "--factor", "3", "--matrix", "bt709")
    assert proc.returncode == 0, proc.stderr.decode()
    out = parse_y4m(proc.stdout)
    assert out.fps == "72000:1001"
    assert len(out.frames) == 15

    proc = run_rife(data, "--fps", "60000/1001", "--matrix", "bt709")
    assert proc.returncode == 0, proc.stderr.decode()
    out = parse_y4m(proc.stdout)
    assert out.fps == "60000:1001"
    assert len(out.frames) == 13  # ceil(5 × 2,5)


@pytest.mark.parametrize(
    ("pix_fmt", "min_psnr"),
    [("yuv420p", 60.0), ("yuv420p10le", 60.0), ("yuv444p16le", 70.0)],
)
def test_conversion_roundtrip_is_near_lossless(pix_fmt: str, min_psnr: float) -> None:
    data = make_y4m("testsrc2=size=160x96:rate=25", frames=2, pix_fmt=pix_fmt)
    src = parse_y4m(data)
    # testsrc2 est converti en YUV par swscale avec la matrice BT.601
    proc = run_rife(data, "--debug-roundtrip", "--matrix", "bt601")
    assert proc.returncode == 0, proc.stderr.decode()
    out = parse_y4m(proc.stdout)
    depth = 16 if "16" in pix_fmt else (10 if "10" in pix_fmt else 8)
    for a, b in zip(src.frames, out.frames):
        assert _psnr(a, b, depth) >= min_psnr


def test_scene_cut_duplicates_previous_frame() -> None:
    first = make_y4m("testsrc2=size=128x64:rate=25", frames=3)
    second = make_y4m("smptebars=size=128x64:rate=25", frames=3)
    data = first + second[second.index(b"\n") + 1:]
    src = parse_y4m(data)
    proc = run_rife(data, "--matrix", "bt709")
    assert proc.returncode == 0, proc.stderr.decode()
    out = parse_y4m(proc.stdout)
    assert out.frames[5] == src.frames[2]  # trame entre la coupe : duplication
    assert b"scenes=1" in proc.stderr


def test_static_pairs_are_duplicated() -> None:
    data = make_y4m("color=c=gray:size=96x64:rate=25", frames=4)
    src = parse_y4m(data)
    proc = run_rife(data, "--matrix", "bt709")
    assert proc.returncode == 0, proc.stderr.decode()
    out = parse_y4m(proc.stdout)
    assert all(frame == src.frames[0] for frame in out.frames)
    assert b"interpolated=0" in proc.stderr


def test_interlaced_input_is_rejected() -> None:
    data = make_y4m("testsrc2=size=96x64:rate=25", frames=2)
    data = data.replace(b" Ip ", b" It ", 1)
    proc = run_rife(data, "--matrix", "bt709")
    assert proc.returncode == 2
    assert b"entrelac" in proc.stderr


def test_unknown_model_fails_with_gpu_exit_code() -> None:
    data = make_y4m("testsrc2=size=96x64:rate=25", frames=2)
    proc = run_rife(data, "--model", "rife-inexistant", "--matrix", "bt709")
    assert proc.returncode == 3
