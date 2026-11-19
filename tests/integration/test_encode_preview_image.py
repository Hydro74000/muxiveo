"""Aperçu image encodé (audit C03) et image livrée à l'instant demandé (audit C01), outils réels."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe requis")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args], check=True, capture_output=True)


def _cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "XDG_CACHE_HOME": str(tmp_path / "xdg-cache")}
    return subprocess.run([sys.executable, str(ROOT / "main.py"), "--cli", *args, "--ffmpeg", "ffmpeg",
                           "--ffprobe", "ffprobe", "--work-dir", str(tmp_path / "work")],
                          cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=False, timeout=600)


def _png(tmp_path: Path, directory: Path) -> Path:
    images = sorted(directory.glob("*.png"))
    assert len(images) == 1, [p.name for p in directory.iterdir()]
    return images[0]


def _rgb_digest(image: Path) -> str:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(image), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         check=True, capture_output=True).stdout
    return hashlib.sha256(raw).hexdigest()


def _source_frame_digest(source: Path, index: int, out: Path) -> str:
    _ffmpeg("-i", str(source), "-map", "0:v:0", "-vf", f"select=eq(n\\,{index})", "-frames:v", "1",
            "-fps_mode", "passthrough", str(out))
    return _rgb_digest(out)


def test_preview_image_respects_instant_and_resize(tmp_path):
    source = tmp_path / "source.mkv"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=14", "-c:v", "libx264", "-g", "50",
            "-pix_fmt", "yuv420p", str(source))
    previews = tmp_path / "apercus"
    done = _cli(tmp_path, "encode", "-i", str(source), "-o", str(tmp_path / "sortie.mkv"), "--video-codec",
                "libx265", "--resize", "128x72", "--preview-image", "12", "--preview-dir", str(previews))
    assert done.returncode == 0, done.stderr
    image = _png(tmp_path, previews)
    stream = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(image)],
                                       check=True, capture_output=True, text=True).stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (128, 72)  # réglages du job appliqués
    assert ".apercu-12.0s." in image.name                    # instant demandé, pas une scène tirée ailleurs
    assert not (tmp_path / "sortie.mkv").exists()


@pytest.mark.parametrize("offset", [0, 5])
@pytest.mark.parametrize("instant", [0.61, 0.6, 2.02])
def test_lossless_preview_is_exact_source_frame(tmp_path, offset, instant):
    """Sans perte : l'image livrée est au bit près l'image source attendue (première d'instant ≥ demandé)."""
    source = tmp_path / "source.nut"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=4", "-c:v", "ffv1", "-g", "25",
            "-pix_fmt", "yuv420p", "-output_ts_offset", str(offset), str(source))
    previews = tmp_path / "apercus"
    done = _cli(tmp_path, "encode", "-i", str(source), "-o", str(tmp_path / "sortie.mkv"), "--video-codec",
                "libx264", "--crf", "0", "--bit-depth", "8", "--preview-image", str(instant),
                "--preview-dir", str(previews))
    assert done.returncode == 0, done.stderr
    expected = next(index for index in range(100) if index / 25 >= instant - 0.0005)
    assert _rgb_digest(_png(tmp_path, previews)) == _source_frame_digest(source, expected, tmp_path / "ref.png")


def test_open_gop_cut_drops_leading_pictures(tmp_path):
    """Source x265 à GOP ouvert (CRA + RASL) : découpe sur l'image clé, image livrée = image source attendue."""
    source = tmp_path / "source.mkv"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=4", "-c:v", "libx265", "-x265-params",
            "keyint=20:min-keyint=20:scenecut=0:bframes=4:open-gop=1:log-level=error", "-pix_fmt", "yuv420p",
            str(source))
    previews = tmp_path / "apercus"
    instant = 1.30  # entre la CRA de 0,8 s et la suivante (1,6 s) : RASL de tête au point de découpe
    done = _cli(tmp_path, "encode", "-i", str(source), "-o", str(tmp_path / "sortie.mkv"), "--video-codec",
                "libx264", "--crf", "0", "--bit-depth", "8", "--preview-image", str(instant),
                "--preview-dir", str(previews))
    assert done.returncode == 0, done.stderr
    expected = next(index for index in range(100) if index / 25 >= instant - 0.0005)
    assert _rgb_digest(_png(tmp_path, previews)) == _source_frame_digest(source, expected, tmp_path / "ref.png")


def test_keyframe_off_millisecond_grid_is_kept(tmp_path):
    """Base de temps 1/90000, images clés hors grille milliseconde : jamais l'image clé précédente."""
    source = tmp_path / "source.ts"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30000/1001:duration=4", "-c:v", "libx264", "-g", "15",
            "-pix_fmt", "yuv420p", "-output_ts_offset", "0.00004", str(source))
    previews = tmp_path / "apercus"
    instant = 1.05
    done = _cli(tmp_path, "encode", "-i", str(source), "-o", str(tmp_path / "sortie.mkv"), "--video-codec",
                "libx264", "--crf", "0", "--bit-depth", "8", "--preview-image", str(instant),
                "--preview-dir", str(previews))
    assert done.returncode == 0, done.stderr
    expected = next(index for index in range(200) if index * 1001 / 30000 >= instant - 0.0005)
    assert _rgb_digest(_png(tmp_path, previews)) == _source_frame_digest(source, expected, tmp_path / "ref.png")


def test_low_frame_rate_reaches_the_next_frame(tmp_path):
    """1 image/s, instant 0,1 s : l'image livrée est celle de 1 s (première ≥ instant), jamais celle de 0 s."""
    source = tmp_path / "source.nut"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=1:duration=5", "-c:v", "ffv1", "-g", "5",
            "-pix_fmt", "yuv420p", str(source))
    previews = tmp_path / "apercus"
    done = _cli(tmp_path, "encode", "-i", str(source), "-o", str(tmp_path / "sortie.mkv"), "--video-codec",
                "libx264", "--crf", "0", "--bit-depth", "8", "--preview-image", "0.1",
                "--preview-dir", str(previews))
    assert done.returncode == 0, done.stderr
    image = _png(tmp_path, previews)
    assert ".apercu-1.0s." in image.name
    assert _rgb_digest(image) == _source_frame_digest(source, 1, tmp_path / "ref.png")


@pytest.mark.parametrize("instant, expected", [(0.1, 1), (2.5, 3), (4.0, 4)])
def test_low_frame_rate_with_b_frames(tmp_path, instant, expected):
    """1 image/s avec images B (ordre de décodage ≠ affichage) : sonde et découpe atteignent l'image cible."""
    source = tmp_path / "source.mkv"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=1:duration=12", "-c:v", "libx264", "-bf", "3", "-g", "6",
            "-crf", "0", "-pix_fmt", "yuv420p", str(source))
    previews = tmp_path / "apercus"
    done = _cli(tmp_path, "encode", "-i", str(source), "-o", str(tmp_path / "sortie.mkv"), "--video-codec",
                "libx264", "--crf", "0", "--bit-depth", "8", "--preview-image", str(instant),
                "--preview-dir", str(previews))
    assert done.returncode == 0, done.stderr
    image = _png(tmp_path, previews)
    assert f".apercu-{float(expected):.1f}s." in image.name
    assert _rgb_digest(image) == _source_frame_digest(source, expected, tmp_path / "ref.png")
