"""Tests du lecteur de timestamps source via ffprobe."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from core.matroska.timestamps import (
    MatroskaTimestampReader,
)


def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestMatroskaTimestampReader:
    def test_reads_cfr_packets(self, tmp_path):
        reader = MatroskaTimestampReader()
        packets_payload = json.dumps({
            "packets": [
                {"pts_time": "0.000000", "duration_time": "0.041666"},
                {"pts_time": "0.041666", "duration_time": "0.041666"},
                {"pts_time": "0.083333", "duration_time": "0.041666"},
            ]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv")
        assert seq.pts_ms == (0, 42, 83)
        # Durées calculées depuis deltas + duplication de la dernière.
        assert seq.durations_ms == (42, 41, 41)

    def test_reads_vfr_packets(self, tmp_path):
        reader = MatroskaTimestampReader()
        packets_payload = json.dumps({
            "packets": [
                {"pts_time": "0.000000"},
                {"pts_time": "0.040000"},
                {"pts_time": "0.100000"},  # gap VFR
                {"pts_time": "0.140000"},
            ]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv")
        assert seq.pts_ms == (0, 40, 100, 140)
        assert seq.durations_ms == (40, 60, 40, 40)

    def test_total_duration(self, tmp_path):
        reader = MatroskaTimestampReader()
        packets_payload = json.dumps({
            "packets": [{"pts_time": "0.0"}, {"pts_time": "1.0"}, {"pts_time": "2.0"}]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv")
        # 3 frames à 1 s d'intervalle → durée totale = 2000 + duration[-1]=1000 = 3000
        assert seq.total_duration_ms == 3000

    def test_sorts_packets_by_pts(self, tmp_path):
        reader = MatroskaTimestampReader()
        # B-frames : ordre DTS != ordre PTS.
        packets_payload = json.dumps({
            "packets": [
                {"pts_time": "0.0"},
                {"pts_time": "0.083"},
                {"pts_time": "0.041"},
            ]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv")
        assert list(seq.pts_ms) == sorted(seq.pts_ms)

    def test_can_preserve_packet_order_for_b_frames(self, tmp_path):
        reader = MatroskaTimestampReader()
        packets_payload = json.dumps({
            "packets": [
                {"pts_time": "0.000"},
                {"pts_time": "0.083"},
                {"pts_time": "0.041"},
            ]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv", sort_by_pts=False)

        assert seq.pts_ms == (0, 83, 41)
        assert seq.total_duration_ms == 125

    def test_skips_na_pts(self, tmp_path):
        reader = MatroskaTimestampReader()
        packets_payload = json.dumps({
            "packets": [
                {"pts_time": "N/A"},
                {"pts_time": "0.0"},
                {"pts_time": "0.041"},
            ]
        })
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(packets_payload),
        ):
            seq = reader.read(tmp_path / "src.mkv")
        assert seq.pts_ms == (0, 41)

    def test_raises_when_no_packets(self, tmp_path):
        reader = MatroskaTimestampReader()
        with patch(
            "core.matroska.timestamps.subprocess.run",
            return_value=_completed(json.dumps({"packets": []})),
        ):
            with pytest.raises(RuntimeError, match="Aucun packet"):
                reader.read(tmp_path / "src.mkv")

    def test_raises_when_ffprobe_missing(self, tmp_path):
        reader = MatroskaTimestampReader()
        with patch(
            "core.matroska.timestamps.subprocess.run",
            side_effect=FileNotFoundError("ffprobe"),
        ):
            with pytest.raises(RuntimeError, match="indisponible"):
                reader.read(tmp_path / "src.mkv")


class TestNominalFrameDuration:
    """Durée nominale (DefaultDuration) déduite de ``r_frame_rate`` ou des PTS."""

    @staticmethod
    def _read(tmp_path, pts_ms: list[int], rate: str | None):
        payload: dict = {"packets": [{"pts_time": f"{pts / 1000:.6f}"} for pts in pts_ms]}
        if rate is not None:
            payload["streams"] = [{"r_frame_rate": rate}]
        with patch("core.matroska.timestamps.subprocess.run", return_value=_completed(json.dumps(payload))):
            return MatroskaTimestampReader().read(tmp_path / "x.mkv", sort_by_pts=False)

    def test_exact_ntsc_rate_from_r_frame_rate(self, tmp_path):
        # Ordre de décodage avec images B : PTS arrondis à la milliseconde.
        seq = self._read(tmp_path, [0, 167, 83, 42, 125, 334, 250, 209, 292], "24000/1001")
        assert seq.nominal_frame_duration_ns == 41_708_333

    def test_rate_contradicted_by_timestamps_falls_back_to_median(self, tmp_path):
        seq = self._read(tmp_path, [0, 40, 80, 120, 160], "90000/1")
        assert seq.nominal_frame_duration_ns == 40_000_000

    def test_missing_rate_uses_median_delta(self, tmp_path):
        seq = self._read(tmp_path, [0, 40, 100, 140], None)
        assert seq.nominal_frame_duration_ns == 40_000_000

    def test_single_frame_has_no_nominal_duration(self, tmp_path):
        assert self._read(tmp_path, [0], "24000/1001").nominal_frame_duration_ns == 0
