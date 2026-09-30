"""
Tests for core/workflows/encode/runtime/crop_detector.py
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from core.dovi_profile_detector import DoviDetectionResult, DoviSubProfile
from core.workflows.encode.runtime.crop_detector import (
    align_crop_for_codec,
    detect_black_bars_ffmpeg,
    detect_video_crop,
)


def test_align_crop_for_codec_nvencc_dv_hobbit():
    raw_crop = (275, 275, 0, 0)
    aligned = align_crop_for_codec(raw_crop, dimensions=(3840, 2160), codec="nvencc_hevc", copy_dv=True)
    assert aligned == (280, 280, 0, 0)
    act_h = 2160 - (aligned[0] + aligned[1])
    assert act_h == 1600
    assert act_h % 32 == 0


def test_align_crop_for_codec_libx265_hobbit():
    raw_crop = (275, 275, 0, 0)
    aligned = align_crop_for_codec(raw_crop, dimensions=(3840, 2160), codec="libx265", copy_dv=False)
    assert aligned == (276, 276, 0, 0)
    act_h = 2160 - (aligned[0] + aligned[1])
    assert act_h % 2 == 0
    assert aligned[0] % 2 == 0
    assert aligned[1] % 2 == 0


def test_align_crop_for_codec_zero():
    raw_crop = (0, 0, 0, 0)
    aligned = align_crop_for_codec(raw_crop, dimensions=(3840, 2160), codec="nvencc_hevc", copy_dv=True)
    assert aligned == (0, 0, 0, 0)


def test_align_crop_for_codec_horizontal():
    raw_crop = (0, 0, 140, 140)  # pillarbox
    aligned = align_crop_for_codec(raw_crop, dimensions=(1920, 1080), codec="nvencc_hevc", copy_dv=True)
    # act_w = 1920 - 280 = 1640. 1640 // 32 = 51 -> 1632. diff_w = 8 -> left=144, right=144
    # act_h = 1080. 1080 // 32 = 33 -> 1056. diff_h = 24 -> top=12, bottom=12
    assert aligned == (12, 12, 144, 144)
    act_w = 1920 - (aligned[2] + aligned[3])
    act_h = 1080 - (aligned[0] + aligned[1])
    assert act_w % 32 == 0
    assert act_h % 32 == 0


def test_detect_black_bars_ffmpeg_parses_output():
    mock_stderr = (
        "[Parsed_cropdetect_0 @ 0x123] x1:0 x2:3839 y1:275 y2:1883 w:3840 h:1608 x:0 y:276 "
        "pts:90 limit:64.000000 crop=3840:1608:0:276\n"
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=mock_stderr, stdout="")
        res = detect_black_bars_ffmpeg(
            Path("/fake/movie.mkv"),
            dimensions=(3840, 2160),
            duration_s=600,
        )
        assert res == (276, 276, 0, 0)


def test_detect_video_crop_uses_dovi_l5_when_present():
    with patch("core.workflows.encode.runtime.crop_detector.DoviProfileDetector") as mock_det_cls:
        mock_instance = MagicMock()
        mock_instance.probe_l5_offsets.return_value = (275, 275, 0, 0)
        mock_det_cls.return_value = mock_instance

        res = detect_video_crop(
            Path("/fake/movie.mkv"),
            dimensions=(3840, 2160),
            codec="nvencc_hevc",
            copy_dv=True,
        )
        assert res == (280, 280, 0, 0)
