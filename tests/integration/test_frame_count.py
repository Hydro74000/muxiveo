"""Petites découpes Matroska avec statistiques MediaInfo périmées."""

from pathlib import Path
import shutil
import subprocess

import pytest

from core.frame_count import ffprobe_packet_count, reliable_frame_count
from core.workflows.encode.runtime.frame_count_guard import FrameCountGuard
from tests.integration._synth import _run_ffmpeg


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe requis",
)


def test_small_real_cut_passes_frame_count_guard(tmp_path, monkeypatch):
    media_info = pytest.importorskip("pymediainfo").MediaInfo
    if not media_info.can_parse():
        pytest.skip("libmediainfo requise")

    original, cut, encoded = (tmp_path / name for name in ("original.mkv", "cut.mkv", "encoded.mkv"))
    _run_ffmpeg([
        "-f", "lavfi", "-i", "color=s=64x64:r=25", "-frames:v", "1000",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-metadata:s:v:0", "NUMBER_OF_FRAMES=1000",
        "-metadata:s:v:0", "_STATISTICS_TAGS=NUMBER_OF_FRAMES", str(original),
    ])
    _run_ffmpeg(["-i", str(original), "-t", "39.2", "-c", "copy", str(cut)])
    _run_ffmpeg([
        "-i", str(cut), "-c:v", "libx264", "-preset", "ultrafast",
        "-map_metadata", "-1", str(encoded),
    ])

    def mi_count(path):
        track = next(t for t in media_info.parse(str(path)).tracks if t.track_type == "Video")
        return str(track.frame_count or "")

    assert mi_count(cut) == "1000"
    assert ffprobe_packet_count("ffprobe", cut) == 980
    assert ffprobe_packet_count("ffprobe", encoded) == 980

    # Même bibliothèque MediaInfo que le CLI ; seul le transport est adapté
    # pour les postes disposant de libmediainfo sans l'exécutable mediainfo.
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] == "review-mediainfo":
            return subprocess.CompletedProcess(cmd, 0, mi_count(Path(cmd[-1])), "")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    assert reliable_frame_count(cut, mediainfo_bin="review-mediainfo", ffprobe_bin="ffprobe") == 980
    guard = FrameCountGuard(mediainfo_bin="review-mediainfo")
    audit = guard.audit(source=cut, encoded=encoded)
    assert audit.source == audit.encoded == 980
    assert guard.enforce(audit) == audit
