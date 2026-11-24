"""Merge DoVi — alignement : décodage réel (FFmpeg), barres exactes et fenêtres par PTS."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from core.workflows.merge_dovi_align.frames import read_frame_table
from core.workflows.merge_dovi_align.probe import ProbeError, decode_all, decode_window

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg absent")

W, H = 320, 180


def _encode_frames(path: Path, lumas: list[np.ndarray], *, codec: list[str], extra: list[str] = ()) -> Path:
    """Images Y 10 bits données (chroma neutre) → fichier ; ``color_range`` limité."""
    raw = bytearray()
    for luma in lumas:
        raw += luma.astype("<u2").tobytes()
        raw += np.full((H // 2, W // 2), 512, dtype="<u2").tobytes() * 2
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p10le",
        "-s", f"{W}x{H}", "-r", "24000/1001", "-color_range", "tv", "-i", "-", *codec, "-color_range", "tv",
        *extra, str(path),
    ], input=bytes(raw), check=True, capture_output=True)
    return path


def _letterbox(top: int, bottom: int, *, bar: int = 0, content: int = 400) -> np.ndarray:
    luma = np.full((H, W), bar, dtype=np.uint16)
    luma[top:H - bottom] = content
    return luma


def test_bars_are_exact_and_never_hide_a_visible_pixel(tmp_path: Path) -> None:
    frames = [_letterbox(20, 20) for _ in range(7)]
    frames[1][5, 100] = 81            # juste au-dessus du seuil (noir 64 + 16) : ligne visible
    frames[2][5, 100] = 80            # au seuil : reste noir
    frames[3][5, 100] = 940           # pixel clair entouré de pixels sous le noir : jamais compensé
    frames[4][:, :40] = 0             # pilier gauche 40, droit 30
    frames[4][:, W - 30:] = 0
    frames[5][:] = 64                 # image noire
    frames[6][H - 1, W - 1] = 900     # pixel isolé dans le coin inférieur droit
    path = _encode_frames(tmp_path / "bars.mkv", frames, codec=["-c:v", "ffv1"])
    table = read_frame_table(path)
    signatures = decode_all(table)
    assert len(signatures) == 7 and signatures.ranks.tolist() == list(range(7))
    bars = list(zip(signatures.top.tolist(), signatures.bottom.tolist(),
                    signatures.left.tolist(), signatures.right.tolist()))
    assert bars[0] == (20, 20, 0, 0)
    assert bars[1][0] == 5 and bars[2][0] == 20 and bars[3][0] == 5
    assert bars[4] == (20, 20, 40, 30)
    assert bars[5] == (-1, -1, -1, -1)
    assert bars[6][1] == 0 and bars[6][3] == 0


@pytest.fixture(scope="module")
def bframes_mkv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """96 images à motif changeant, images B, départ à 5 s (PTS non nuls)."""
    if subprocess.run(["ffmpeg", "-hide_banner", "-h", "encoder=libx264"], capture_output=True).returncode:
        pytest.skip("libx264 absent")
    root = tmp_path_factory.mktemp("align_probe")
    frames = [np.full((H, W), 64 + (index * 37) % 800, dtype=np.uint16) for index in range(96)]
    for index, luma in enumerate(frames):
        luma[(index * 3) % H, :] = 900
    return _encode_frames(root / "bframes.mkv", frames, codec=[
        "-c:v", "libx264", "-preset", "ultrafast", "-bf", "3", "-g", "24", "-pix_fmt", "yuv420p10le",
    ], extra=["-output_ts_offset", "5"])


def test_window_matches_full_pass_by_exact_pts(bframes_mkv: Path) -> None:
    table = read_frame_table(bframes_mkv)
    assert table.indexable and table.frames == 96 and table.pts[0] >= 5000
    full = decode_all(table)
    assert full.ranks.tolist() == list(range(96))
    for start in (0, 37, 90):
        window = decode_window(table, start, 6)
        stop = min(96, start + 6)
        assert window.ranks.tolist() == list(range(start, stop))
        assert np.array_equal(window.vignettes, full.vignettes[start:stop])
        assert np.array_equal(window.top, full.top[start:stop])


def test_window_refused_on_non_indexable_input(tmp_path: Path, bframes_mkv: Path) -> None:
    ts = tmp_path / "copy.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(bframes_mkv), "-c", "copy",
                    str(ts)], check=True, capture_output=True)
    table = read_frame_table(ts)
    assert not table.indexable
    with pytest.raises(ProbeError):
        decode_window(table, 0, 4)
    assert len(decode_all(table)) == 96
