"""Sélection du moteur, compatibilité des profils et pipelines communs."""
from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from core.pipeline_command import command_stages
from core.workflows.encode import EncodeConfig, EncodeError, EncodePreset, EncodeWorkflow, FrameInterpolationSettings, VideoEncodeSettings
from core.workflows.encode.interpolation import InterpolationSource, build_interpolation_stage, mvtools_thread_count, parse_interpolation_progress, frame_repeats
from tests.test_encode_interpolation import _builder


def test_old_profile_and_independent_parameters():
    old = FrameInterpolationSettings.from_value({"enabled": True, "quality": "light", "tta": 4, "gpu": 1})
    assert old.backend == "rife" and old.mvtools_mode == "standard"
    selected = replace(old, backend="mvtools", mvtools_mode="uhd")
    restored = EncodePreset(**json.loads(json.dumps(EncodePreset(name="x", interpolation=selected).to_json_dict())))
    assert restored.interpolation == selected
    assert replace(restored.interpolation, backend="rife") == replace(old, mvtools_mode="uhd")


@pytest.mark.parametrize("mode,budget,expected", [("standard", 1, 1), ("standard", 6, 3), ("standard", 64, 4), ("uhd", 6, 6)])
def test_thread_budget(mode, budget, expected):
    assert mvtools_thread_count(mode, budget) == expected


def test_stage_and_progress():
    settings = FrameInterpolationSettings(enabled=True, backend="mvtools", mvtools_mode="uhd", target_fps="60000/1001", tta=8)
    args = build_interpolation_stage(settings, InterpolationSource("bt2020nc", "limited", "topleft"),
                                     mvtools_bin="/opt/muxiveo-mvtools", thread_budget=5)
    assert args[0] == "/opt/muxiveo-mvtools" and args[args.index("--fps") + 1] == "60000/1001"
    assert args[args.index("--threads") + 1] == "5" and "--tta" not in args and "--factor" not in args
    result = parse_interpolation_progress("[muxiveo-mvtools] progress in=8 out=21 scenes=0 static=0 fps=4.2")
    assert result is not None and result.frames_in == 8 and result.frames_out == 21
    with pytest.raises(EncodeError, match="introuvable"):
        build_interpolation_stage(settings, InterpolationSource(), rife_bin="available-rife")


def test_ffmpeg_filters_and_per_job_budget(tmp_path):
    video = VideoEncodeSettings(codec="libx265", interpolation=FrameInterpolationSettings(enabled=True, backend="mvtools", mvtools_mode="uhd"))
    config = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video)
    builder = _builder()
    builder._cb = replace(builder._cb, mvtools_bin="/opt/muxiveo-mvtools")
    command = builder.build_video_only_mkv_commands(config, video, config.source, tmp_path / "v.mkv", thread_count=3)[0]
    decode, interpolate, encode = command_stages(command)
    assert interpolate[0] == "/opt/muxiveo-mvtools" and interpolate[interpolate.index("--threads") + 1] == "3"
    assert decode[-1] == "-" and encode[encode.index("-i") + 1] == "pipe:0"


def test_nvencc_stage_keeps_color_and_offset(tmp_path, monkeypatch):
    workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg", mvtools_bin="/opt/muxiveo-mvtools", ffmpeg_threads=8)
    monkeypatch.setattr(workflow, "_interpolation_source", lambda *_: InterpolationSource("bt2020nc", "limited", "topleft", 0.04))
    video = VideoEncodeSettings(codec="nvencc_hevc", interpolation=FrameInterpolationSettings(enabled=True, backend="mvtools"))
    stages = command_stages(workflow._wrap_decode_with_interpolation(["ffmpeg", "-i", "s.mkv", "-f", "yuv4mpegpipe", "-"], video, tmp_path / "s.mkv"))
    assert stages[-1][0] == "/opt/muxiveo-mvtools" and "--matrix" in stages[-1]
    assert stages[-1][stages[-1].index("--threads") + 1] == "4"


def test_hdr_counts_share_exact_cadence():
    video = VideoEncodeSettings(codec="libx265", interpolation=FrameInterpolationSettings(enabled=True, backend="mvtools", target_fps="60"))
    ratio = video.frame_ratio("24000/1001")
    assert ratio == Fraction(1001, 400)
    assert sum(frame_repeats(i, ratio) for i in range(19)) == 48
