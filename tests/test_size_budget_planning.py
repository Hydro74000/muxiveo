"""Entrées non finies du budget : inconnues, jamais une impossibilité mesurée."""

import math
from pathlib import Path

import pytest

from core.workflows.encode.models import EncodeConfig, QualityMode, VideoEncodeSettings
from core.workflows.encode.planning.size_budget import stream_bitrate_bps, stream_pixel_rate
from core.workflows.encode.workflow import EncodeWorkflow


@pytest.mark.parametrize("value", ["inf", "nan", "1e999", "-inf"])
def test_nonfinite_stream_values_are_unknown(value):
    assert stream_bitrate_bps({"bit_rate": value}) == 0
    assert stream_bitrate_bps({"bit_rate": value, "tags": {"BPS": "12000"}}) == 12000
    assert stream_pixel_rate({"width": value, "height": 1080, "avg_frame_rate": "25/1"}) == 1


@pytest.mark.parametrize("duration", [float("inf"), float("nan")])
def test_nonfinite_job_duration_is_refused_and_preview_stays_finite(duration, monkeypatch):
    workflow = EncodeWorkflow()
    monkeypatch.setattr(workflow, "_stream_info", lambda *_args: {})
    config = EncodeConfig(source=Path("source.mkv"), output=Path("out.mkv"),
                          video=VideoEncodeSettings(quality_mode=QualityMode.SIZE),
                          copy_subtitles=False, duration_s=duration)
    assert "non finie" in " ".join(workflow.size_target_errors(config))
    assert math.isfinite(workflow._size_budget(config).video_bps)
