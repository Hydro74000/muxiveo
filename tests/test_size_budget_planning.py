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


@pytest.mark.parametrize("known_source_bitrate", [False, True])
def test_unlimited_vbr_is_estimated_or_unknown_in_size_budget(monkeypatch, known_source_bitrate):
    workflow = EncodeWorkflow()
    monkeypatch.setattr(workflow, "_stream_info", lambda *_args: {"bit_rate": "1000000"} if known_source_bitrate else {})
    sized = VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.SIZE)
    unlimited = VideoEncodeSettings(codec="nvencc_hevc", rate_control="vbr_quality", bitrate_kbps=0, stream_index=1)
    config = EncodeConfig(source=Path("source.mkv"), output=Path("out.mkv"), video=sized,
                          video_tracks=[sized, unlimited], copy_subtitles=False, duration_s=60)
    budget = workflow._size_budget(config)
    assert budget.measured_bps == 0
    if known_source_bitrate:
        assert budget.estimated_bps > budget.target_bps * 0.005
        assert any("estimé depuis le débit source" in warning for warning in budget.warnings)
    else:
        assert "vidéo #2" in budget.unknown


def test_unlimited_vbr_storage_estimate_uses_source_size():
    from core.workflows.encode.runtime.storage_guard import estimate_inject_video_bytes

    video = VideoEncodeSettings(codec="nvencc_hevc", rate_control="vbr", quality_mode=QualityMode.BITRATE,
                                bitrate_kbps=0)
    config = EncodeConfig(source=Path("source.mkv"), output=Path("out.mkv"), video=video)
    source_size = 2 * 1024**3
    assert estimate_inject_video_bytes(config, duration_s=3600, source_size=source_size,
                                      size_to_bitrate_kbps=lambda _config: 5000) == source_size
