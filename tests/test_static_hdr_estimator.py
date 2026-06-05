from __future__ import annotations

from pathlib import Path

import pytest

from core.workflows.encode.runtime.static_hdr_estimator import (
    StaticHdrEstimateService,
)


def test_pq_code_value_to_nits_maps_peak_close_to_10000():
    assert StaticHdrEstimateService.pq_code_value_to_nits(940) == pytest.approx(10000, rel=0.01)


def test_signalstats_output_estimates_maxcll_and_master_bucket():
    output = "\n".join(
        [
            "lavfi.signalstats.YMAX=758",
            "lavfi.signalstats.YAVG=580",
        ]
        * 30
    )

    estimate = StaticHdrEstimateService.estimate_from_signalstats_output(output)

    max_cll, max_fall = [int(part) for part in estimate.max_cll.split(",", 1)]
    assert max_cll >= max_fall
    assert estimate.confidence == "medium"
    assert estimate.sample_count == 30
    assert estimate.source == "estimated_p5_to_p8"
    assert "WP(15635,16450)" in estimate.master_display


def test_signalstats_output_ignores_near_black_samples():
    lines: list[str] = []
    for index in range(60):
        is_dark_sample = index < 30
        lines.extend([
            f"frame:{index} pts:{index}",
            "lavfi.signalstats.YBITDEPTH=10",
            f"lavfi.signalstats.YMAX={64 if is_dark_sample else 700}",
            f"lavfi.signalstats.YAVG={64 if is_dark_sample else 500}",
        ])
    output = "\n".join(lines)

    estimate = StaticHdrEstimateService.estimate_from_signalstats_output(output)

    assert estimate.sample_count == 30
    assert estimate.confidence == "medium"
    assert any("quasi noir" in warning for warning in estimate.warnings)


def test_master_display_uses_expected_luminance_buckets():
    assert "L(10000000,1)" in StaticHdrEstimateService.master_display_for_peak(900)
    assert "L(40000000,1)" in StaticHdrEstimateService.master_display_for_peak(2500)
    assert "L(100000000,1)" in StaticHdrEstimateService.master_display_for_peak(6000)


def test_analysis_cmd_samples_middle_eighty_percent():
    service = StaticHdrEstimateService(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool")

    cmd = service._analysis_cmd(Path("converted.hevc"), duration_s=7200.0)

    vf = cmd[cmd.index("-vf") + 1]
    assert "trim=start=720.000:end=6480.000" in vf
    assert "fps=fps=0.01666667" in vf


def test_estimate_runs_extract_convert_analyze_and_cleans_temp(tmp_path):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    calls: list[list[str]] = []

    class FakeEstimator(StaticHdrEstimateService):
        def _run_capture(self, cmd, *, cancel_cb=None):  # noqa: ANN001
            calls.append(list(cmd))
            if "signalstats" in " ".join(cmd):
                return "\n".join(
                    [
                        "lavfi.signalstats.YMAX=700",
                        "lavfi.signalstats.YAVG=500",
                    ]
                    * 80
                )
            return ""

    service = FakeEstimator(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool")

    estimate = service.estimate_p5_to_p8_static_hdr(
        source,
        stream_index=3,
        duration_s=7200.0,
        work_dir=tmp_path,
    )

    assert calls[0][0] == "ffmpeg"
    assert calls[0][calls[0].index("-map") + 1] == "0:3"
    assert calls[1][:4] == ["dovi_tool", "-m", "3", "convert"]
    assert "signalstats" in " ".join(calls[2])
    assert estimate.confidence == "high"
    assert not any(path.name.startswith("static_hdr_p5_") for path in tmp_path.iterdir())
