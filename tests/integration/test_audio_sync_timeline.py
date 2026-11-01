"""Analyse de la synchro sur la chronologie de lecture : départ tardif et trou interne comblés."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner
from core.workflows.audio_sync_segments import _active_blocks

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")),
    reason="FFmpeg et ffprobe requis",
)


def _late_audio_with_gap(path: Path) -> None:
    """Vidéo 0–8 s ; audio sans paquet avant 2 s, puis trou de 1 s à 5 s (paquets décalés)."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:r=25:d=8",
        "-itsoffset", "2", "-f", "lavfi", "-i", "anoisesrc=d=5:c=pink:r=48000:a=0.3",
        "-map", "0:v", "-map", "1:a", "-af", r"asetpts=PTS+gte(T\,5)/TB",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "flac", str(path),
    ], check=True, capture_output=True, timeout=60)


@pytest.mark.real_audio_start_probe
def test_analysis_reads_late_start_and_gap_as_silence(tmp_path):
    source = tmp_path / "late.mkv"
    _late_audio_with_gap(source)
    track = AudioSyncTrack(source, 1)
    scanner = AudioSyncScanner()
    assert scanner.start_delay_s(track) == pytest.approx(2.0, abs=0.002)

    envelope = scanner.envelope(track)
    # 8 s de chronologie, et non 5 s d'échantillons mis bout à bout.
    assert abs(len(envelope) - 8000) <= 25
    active = _active_blocks(envelope)
    assert not active[:19].any() and not active[51:59].any()
    assert active[21:49].all() and active[61:79].all()
    # Lecture unique : mêmes valeurs, mixage par mixage.
    mono, side = scanner.envelope_mixes(track, None, (None, "pan=mono|c0=c0"))
    assert np.array_equal(mono, envelope)
    assert np.array_equal(side, scanner.envelope(track, downmix="pan=mono|c0=c0"))

    # Fenêtre ponctuelle sans seek (4 s) puis avec seek (5,5 s) : le trou (5,06–6,06 s, trames
    # FLAC de 96 ms) reste à sa place ; blocs de 100 ms à cheval sur un bord ignorés.
    for start in (4.0, 5.5):
        window = scanner.samples(track, start, 2.0)
        level = np.abs(window).reshape(-1, 1600).mean(axis=1)
        begin = np.arange(len(level)) / 10 + start
        silent, sounding = (begin >= 5.1) & (begin + 0.1 <= 6.0), (begin + 0.1 <= 5.0) | (begin >= 6.1)
        assert silent.any() and sounding.any()
        assert (level[silent] < 1e-4).all() and (level[sounding] > 1e-4).all()
