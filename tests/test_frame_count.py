"""tests/test_frame_count.py — Frame count fiable face aux statistiques Matroska périmées."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.frame_count import ffprobe_packet_count, frame_count_is_plausible, reliable_frame_count


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fake_tools(mediainfo: str, duration: str | None, fps: str, packets: str):
    def run(cmd, **_kwargs):
        if "--Inform=Video;%FrameCount%" in cmd:
            return _completed(mediainfo)
        if "-count_packets" in cmd:
            return _completed(packets)
        return _completed(json.dumps({"streams": [{"avg_frame_rate": fps}], "format": {"duration": duration}}))
    return run


def test_plausibility_only_tolerates_duration_rounding_and_unknowns():
    assert frame_count_is_plausible(288, 12.012, 24000 / 1001)
    assert frame_count_is_plausible(288, 12.020, 24000 / 1001)
    assert not frame_count_is_plausible(292, 12.012, 24000 / 1001)
    assert not frame_count_is_plausible(288, 4.212, 24000 / 1001)
    assert frame_count_is_plausible(288, None, 23.976)


def test_reliable_frame_count_keeps_consistent_mediainfo_value():
    with patch("core.frame_count.subprocess.run", side_effect=_fake_tools("288", "12.012", "24000/1001", "288")) as run:
        assert reliable_frame_count(Path("a.mkv"), mediainfo_bin="mi", ffprobe_bin="fp") == 288
    assert not any("-count_packets" in call.args[0] for call in run.call_args_list)


def test_reliable_frame_count_counts_packets_when_statistics_are_stale():
    logs: list[str] = []
    with patch("core.frame_count.subprocess.run", side_effect=_fake_tools("288", "4.212", "24000/1001", "98")):
        assert reliable_frame_count(Path("a.mkv"), mediainfo_bin="mi", ffprobe_bin="fp", log=logs.append) == 98
    assert logs and "périmées" in logs[0]


def test_raw_stream_estimate_without_duration_is_recounted():
    """Flux HEVC brut : sans durée, l'estimation mediainfo (parfois fausse) est remplacée par le comptage."""
    with patch("core.frame_count.subprocess.run", side_effect=_fake_tools("16", None, "24000/1001", "48")):
        assert reliable_frame_count(Path("film.hevc"), mediainfo_bin="mi", ffprobe_bin="fp") == 48


def test_display_mode_never_reads_whole_file():
    """``full_scan=False`` (panneau) : valeur invérifiable → None, sans comptage des paquets."""
    with patch("core.frame_count.subprocess.run", side_effect=_fake_tools("16", None, "24000/1001", "48")) as run:
        assert reliable_frame_count(Path("film.hevc"), mediainfo_bin="mi", ffprobe_bin="fp", full_scan=False) is None
    assert not any("-count_packets" in call.args[0] for call in run.call_args_list)
    with patch("core.frame_count.subprocess.run", side_effect=_fake_tools("288", "12.012", "24000/1001", "0")):
        assert reliable_frame_count(Path("a.mkv"), mediainfo_bin="mi", ffprobe_bin="fp", full_scan=False) == 288


@pytest.mark.parametrize("count,duration,fps,packets", [
    ("1000", "39.2", "25/1", "980"),  # petite coupe, moins de 3 %
    ("288", "11.8", "24/1", "283"),  # moins de six images
])
def test_small_cuts_do_not_reuse_stale_statistics(count, duration, fps, packets):
    with patch("core.frame_count.subprocess.run", side_effect=_fake_tools(count, duration, fps, packets)):
        assert reliable_frame_count(Path("cut.mkv"), mediainfo_bin="mi", ffprobe_bin="fp") == int(packets)


def test_packet_count_rejects_partial_stdout_when_ffprobe_fails():
    partial = subprocess.CompletedProcess([], 1, stdout="999\n", stderr="read error")
    with patch("core.frame_count.subprocess.run", return_value=partial):
        assert ffprobe_packet_count("ffprobe", Path("damaged.mkv")) is None
