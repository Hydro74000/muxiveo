"""Le transport GPU direct conserve l'encodeur et respecte les cas incompatibles."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.fel import devices, direct
from core.fel.devices import FelDevice, FelDevicePlan
from core.fel.engine import FelError, FelSource
from core.pipeline_command import PipelineCommand
from core.runner import CommandError, TaskCancelledError, ToolRunner
from core.workflows.encode.models import VideoEncodeSettings

GPU = FelDevice("b"*32, "RTX", 0x10DE, 0, "discrete", 16 << 30)
AMD = FelDevice("a"*32, "AMD", 0x1002, 1, "integrated", 8 << 30)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(direct.sys, "platform", "linux")
    monkeypatch.setattr(devices, "probe_load", lambda _: {})
    monkeypatch.setattr(direct, "probe_load", lambda _: {})
    monkeypatch.setattr(direct, "compatible_driver", lambda _: True)
    binary = tmp_path / "ffmpeg-fel"
    binary.touch()
    engine = SimpleNamespace(path=tmp_path/"libmvo_fel.so", direct_ffmpeg=binary)
    plan = FelDevicePlan(GPU.uuid, (GPU, AMD))
    source = FelSource(engine, tmp_path/"a:b'[film],;.mkv", 2, device_plan=plan)
    video = VideoEncodeSettings(codec="hevc_nvenc", bit_depth="10", fel_context=SimpleNamespace(source=source))
    plan.video = video
    original = ["ffmpeg", "-progress", "pipe:1", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                "-i", str(source.path), "-vf", "setparams=color_trc=smpte2084:range=tv", "-map", "0:2"]
    fallback = PipelineCommand(["ffmpeg", "-f", "nut", "-i", "pipe:0"], [], producer=source)
    return fallback, original, video


def test_direct_uses_same_device_and_preserves_encoder_options(prepared):
    fallback, original, video = prepared
    cmd = direct.direct_command(fallback, original, video, 12)
    assert isinstance(cmd, direct.DirectFelCommand)
    suffix = ["-c:v", "hevc_nvenc", "-preset:v", "p5", "-cq:v", "26", "-pix_fmt", "p010le", "out.hevc"]
    cmd.extend(suffix)
    before = devices._ACTIVE.copy()
    with cmd.execution() as resolved:
        assert not isinstance(resolved, PipelineCommand)
        assert devices._ACTIVE[GPU.uuid] == before[GPU.uuid]+1
        assert resolved[-len(suffix):] == [*suffix[:-2], "cuda", "out.hevc"]
        assert "-hwaccel" not in resolved and "-vf" not in resolved
        assert resolved[resolved.index("-map")+1] == "[fel]"
        graph = resolved[resolved.index("-filter_complex")+1]
        assert ":stream=2:threads=12," in graph and ",mvo_fel_cuda[fel]" in graph
        assert "pipe:0" not in resolved
    assert devices._ACTIVE == before
    assert cmd[-len(suffix):] == suffix
    assert original[0] == "ffmpeg"


@pytest.mark.parametrize("choice", ["cpu", AMD.uuid])
def test_manual_other_device_keeps_nut(prepared, choice):
    fallback, original, video = prepared
    video.fel_context.source.device_plan.choice = choice
    cmd = direct.direct_command(fallback, original, video, 12)
    before = devices._ACTIVE.copy()
    with cmd.execution() as resolved:
        assert type(resolved) is PipelineCommand
        assert resolved.producer is fallback.producer
    assert devices._ACTIVE == before


def test_ambiguous_multiple_encoder_gpus_keep_nut(prepared):
    fallback, original, video = prepared
    video.fel_context.source.device_plan.devices += (replace(GPU, uuid="c"*32),)
    with direct.direct_command(fallback, original, video, 12).execution() as resolved:
        assert type(resolved) is PipelineCommand


@pytest.mark.parametrize("settings", [
    {"codec": "libx265"}, {"codec": "nvencc_hevc"}, {"bit_depth": "8"},
    {"tonemap_to_sdr": True}, {"p5_to_hdr10": True}, {"interpolation": {"enabled": True}},
    {"extra_params": "-vf scale=1920:1080"},
])
def test_unsupported_workflows_keep_existing_pipeline(prepared, settings):
    fallback, original, video = prepared
    assert direct.direct_command(fallback, original, replace(video, **settings), 12) is fallback


@pytest.mark.parametrize("chain", ["scale=1920:1080", "zscale=dither=error_diffusion",
                                  "setparams=color_trc=bt709", "crop=iw-1-0:ih-0-0:1:0"])
def test_incompatible_filters_keep_existing_pipeline(prepared, chain):
    fallback, original, video = prepared
    original[original.index("-vf")+1] = chain
    assert direct.direct_command(fallback, original, video, 12) is fallback


def test_exact_even_crop_without_resize():
    chain = direct.direct_filter("crop=iw-0-0:ih-280-280:0:280")
    assert ":crop_y=280:" in chain and ":crop_h=ih-280-280:" in chain
    assert direct.direct_filter("crop=iw-0-0:ih-280-280:0:280,scale=1920:800") is None


@pytest.mark.parametrize("failure", [
    CommandError([], 1, "MVO_FEL_RECOVERABLE : EL désalignée"),
    CommandError([], 1, "MVO_FEL_SOURCE_ERROR : lecture impossible"),
    CommandError([], 1, "No space left on device"), TaskCancelledError(),
])
def test_runner_only_retries_reconstruction_errors(prepared, monkeypatch, failure):
    fallback, original, video = prepared
    cmd = direct.direct_command(fallback, original, video, 12)
    cmd.extend(["-c:v", "hevc_nvenc", "out.hevc"])
    runner = ToolRunner()
    invoke = ToolRunner._run_cmd
    monkeypatch.setattr(runner, "_run_cmd", Mock(side_effect=failure))
    expected = FelError if isinstance(failure, CommandError) and "RECOVERABLE" in failure.stderr else type(failure)
    before = devices._ACTIVE.copy()
    with pytest.raises(expected):
        invoke(runner, cmd)
    assert devices._ACTIVE == before


def test_platform_and_absent_binary_keep_nut(prepared, monkeypatch):
    fallback, original, video = prepared
    monkeypatch.setattr(direct.sys, "platform", "win32")
    assert direct.direct_command(fallback, original, video, 12) is fallback
    monkeypatch.setattr(direct.sys, "platform", "linux")
    video.fel_context.source.engine.direct_ffmpeg = Path("/absent/ffmpeg-fel")
    assert direct.direct_command(fallback, original, video, 12) is fallback


def test_old_driver_keeps_existing_nut_transport(prepared, monkeypatch):
    fallback, original, video = prepared
    monkeypatch.setattr(direct, "compatible_driver", lambda _: False)
    with direct.direct_command(fallback, original, video, 12).execution() as resolved:
        assert type(resolved) is PipelineCommand


@pytest.mark.parametrize("version,accepted", [("570.0", True), ("580.82.07", True), ("550.127", False), ("N/A", False)])
def test_driver_gate_checks_actual_version(monkeypatch, version, accepted):
    monkeypatch.setattr(direct.subprocess, "run", Mock(return_value=SimpleNamespace(returncode=0, stdout=version)))
    assert direct.compatible_driver(GPU.uuid) is accepted
