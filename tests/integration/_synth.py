"""Helpers de génération de fichiers médias synthétiques via ffmpeg lavfi.

Génère à la volée des conteneurs minimaux (1 seconde) pour tester les
workflows sans avoir à embarquer de fichiers binaires dans le repo.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt


# ---------------------------------------------------------------------------
# Génération ffmpeg
# ---------------------------------------------------------------------------

def _run_ffmpeg(args: list[str]) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
        check=True,
    )


def make_av_container(
    path: Path,
    *,
    vcodec: str = "libx264",
    acodec: str = "aac",
    duration: float = 1.0,
    pix_fmt: str = "yuv420p",
) -> None:
    """Crée un fichier A/V minimal (video testsrc + sine audio).

    Le conteneur est déterminé par l'extension de ``path``.
    """
    _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", str(duration),
        "-c:v", vcodec,
        "-pix_fmt", pix_fmt,
        "-c:a", acodec,
        "-shortest",
        "-y",
        str(path),
    ])


def make_mkv_with_srt(path: Path, duration: float = 1.0) -> None:
    """MKV contenant video + audio + une piste srt (subrip)."""
    srt = path.with_suffix(".srt")
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
        encoding="utf-8",
    )
    _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-i", str(srt),
        "-t", str(duration),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-c:s", "srt",
        "-shortest",
        "-y",
        str(path),
    ])


def make_mp4_with_mov_text(path: Path, duration: float = 1.0) -> None:
    """MP4 contenant video + audio + une piste mov_text.

    mov_text est le format de sous-titre natif des conteneurs MP4/MOV et
    nécessite conversion vers srt lors d'un remux vers MKV.
    """
    srt = path.with_suffix(".srt")
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello mov_text\n",
        encoding="utf-8",
    )
    _run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-i", str(srt),
        "-t", str(duration),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-c:s", "mov_text",
        "-shortest",
        "-y",
        str(path),
    ])


# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------

def ffprobe_json(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout or "{}")


def streams_of_type(probe: dict, codec_type: str) -> list[dict]:
    return [
        s for s in probe.get("streams", [])
        if s.get("codec_type") == codec_type
    ]


# ---------------------------------------------------------------------------
# Exécution de TaskSignals dans un test
# ---------------------------------------------------------------------------

def wait_task(signals, timeout: float = 30.0) -> dict:
    """Attend la fin d'un workflow Qt via TaskSignals, retourne l'état."""
    app = QCoreApplication.instance()
    assert app is not None, "Q(Core)Application non initialisée"
    state: dict = {
        "finished": None,
        "failed": None,
        "cancelled": False,
        "progress": [],
    }
    done = {"value": False}

    signals.progress.connect(
        lambda msg: state["progress"].append(msg),
        Qt.ConnectionType.QueuedConnection,
    )
    signals.finished.connect(
        lambda res: (state.__setitem__("finished", res), done.__setitem__("value", True)),
        Qt.ConnectionType.QueuedConnection,
    )
    signals.failed.connect(
        lambda msg, exc: (state.__setitem__("failed", (msg, exc)), done.__setitem__("value", True)),
        Qt.ConnectionType.QueuedConnection,
    )
    signals.cancelled.connect(
        lambda: (state.__setitem__("cancelled", True), done.__setitem__("value", True)),
        Qt.ConnectionType.QueuedConnection,
    )

    deadline = time.monotonic() + timeout
    while not done["value"] and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    return state
