"""NLMeans 10 bits sur GPU dédié (Vulkan) : détection, choix du GPU, parallélisme, commandes."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.workflows.encode import EncodeConfig, EncodeWorkflow, VideoEncodeSettings
from core.workflows.encode.domain.codecs import (
    EncodeCodecDomainCallbacks,
    build_vf,
    hardware_input_args,
    vulkan_filter_device_args,
    vulkan_filters_compatible,
)
from core.workflows.encode.models import VideoFilterSettings
from core.workflows.encode.vulkan import (
    VulkanCapability,
    choose_vulkan_device,
    parse_vulkan_devices,
    vulkan_parallelism,
)
from ui.main_window import DashboardPage

_LISTING = """
[Vulkan @ 0x55d730c3a4c0] GPU listing:
[Vulkan @ 0x55d730c3a4c0]     0: AMD Ryzen 7 7800X3D 8-Core Processor (RADV RAPHAEL_MENDOCINO) (integrated) (0x164e)
[Vulkan @ 0x55d730c3a4c0]     1: NVIDIA GeForce RTX 4070 Ti SUPER (discrete) (0x2705)
[Vulkan @ 0x55d730c3a4c0]     2: llvmpipe (LLVM 22.1.8, 256 bits) (software) (0x0)
[Vulkan @ 0x55d730c3a4c0] Device 0 selected: AMD Ryzen 7 7800X3D 8-Core Processor (integrated) (0x164e)
"""


def test_discrete_gpu_chosen_by_index_never_igpu_or_software():
    devices = parse_vulkan_devices(_LISTING)
    assert [(d.index, d.kind) for d in devices] == [(0, "integrated"), (1, "discrete"), (2, "software")]
    # FFmpeg prendrait le périphérique 0 (iGPU, plus lent que le CPU) : le GPU dédié est désigné.
    assert choose_vulkan_device(devices).index == 1
    assert choose_vulkan_device(tuple(d for d in devices if d.kind != "discrete")) is None


@pytest.mark.parametrize("size,expected", [((1920, 1080), 2), ((3840, 2160), 2), ((7680, 4320), 1), ((0, 0), 1)])
def test_parallelism_fits_allocation_budget(size, expected):
    assert vulkan_parallelism(*size) == expected


def _nlmeans_video(codec: str = "libx265", **kw) -> VideoEncodeSettings:
    return VideoEncodeSettings(codec=codec, filters=VideoFilterSettings(nlmeans_enabled=True), **kw)


def test_vulkan_chain_uploads_p010_for_high_bit_depth_and_keeps_strength():
    ten = _nlmeans_video(nlmeans_vulkan=True, vulkan_device="1", vulkan_parallelism=2, source_bit_depth=10)
    vf = build_vf(ten)
    assert "format=p010le,hwupload,nlmeans_vulkan=s=2.0:p=5:r=9:t=2,hwdownload,format=p010le,format=yuv420p10le" in vf
    assert "nlmeans=s=" not in vf
    eight = replace(ten, source_bit_depth=8)
    assert "format=yuv420p,hwupload,nlmeans_vulkan" in build_vf(eight)
    assert vulkan_filter_device_args(ten) == ["-init_hw_device", "vulkan=mre_vk:1", "-filter_hw_device", "mre_vk"]
    cb = EncodeCodecDomainCallbacks(platform="linux")
    assert hardware_input_args(ten, callbacks=cb)[:4] == vulkan_filter_device_args(ten)
    # Étage encodeur d'un pipe : pas de filtre, pas de périphérique Vulkan.
    assert "-init_hw_device" not in hardware_input_args(ten, callbacks=cb, piped_frames=True)
    assert "nlmeans=s=2.0:p=5:r=9" in build_vf(replace(ten, nlmeans_vulkan=False))


def test_encoders_with_their_own_filter_device_keep_cpu_nlmeans():
    linux = EncodeCodecDomainCallbacks(platform="linux", vaapi_device="/dev/dri/renderD128")
    windows = EncodeCodecDomainCallbacks(platform="win32", amf_device="0")
    assert not vulkan_filters_compatible(_nlmeans_video(codec="hevc_vaapi"), linux)
    assert not vulkan_filters_compatible(_nlmeans_video(codec="hevc_amf"), windows)
    for codec in ("libx265", "hevc_nvenc", "hevc_qsv", "nvencc_hevc"):
        assert vulkan_filters_compatible(_nlmeans_video(codec=codec), windows)


def test_workflow_resolves_gpu_and_parallelism_from_injected_capability(qt_app, tmp_path, monkeypatch):
    source = tmp_path / "src.mkv"
    source.write_bytes(b"")
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg")
    monkeypatch.setattr(wf, "_source_video_dimensions", lambda *_a, **_k: (3840, 2160))
    wf.set_vulkan_capability("ffmpeg", VulkanCapability(available=True, device="RTX", index=1, nlmeans=True))

    def resolved(codec: str, depth: int = 10) -> VideoEncodeSettings:
        video = _nlmeans_video(codec=codec, source_path=source, source_bit_depth=depth, source_pix_fmt="yuv420p10le")
        config = EncodeConfig(source=source, output=tmp_path / "o.mkv", video=video, video_tracks=[video], audio_tracks=[])
        return wf.resolve_source_color_transfer(config).video

    video = resolved("libx265")
    assert (video.nlmeans_vulkan, video.vulkan_device, video.vulkan_parallelism) == (True, "1", 2)
    assert not resolved("hevc_vaapi").nlmeans_vulkan

    def warnings(codec: str) -> list[str]:
        video = resolved(codec)
        config = EncodeConfig(source=source, output=tmp_path / "o.mkv", video=video, video_tracks=[video], audio_tracks=[])
        return [w for w in wf.config_warnings(config) if "NLMeans" in w]

    # Avertissement 8 bits seulement sur le repli CPU.
    assert not warnings("libx265") and warnings("hevc_vaapi")
    # Sans GPU dédié : repli CPU.
    wf.set_vulkan_capability("ffmpeg", VulkanCapability(reason="aucun GPU dédié"))
    assert not resolved("libx265").nlmeans_vulkan


def test_dashboard_vulkan_badge_and_propagation(qt_app):
    emitted: list[tuple[str, object]] = []
    badge = SimpleNamespace(text="", tip="")
    page = SimpleNamespace(
        _vulkan_badge=badge,
        vulkan_ready=SimpleNamespace(emit=lambda ffmpeg, cap: emitted.append((ffmpeg, cap))),
        _apply_encoder_badge_state=lambda b, label, state: setattr(b, "text", f"{label}:{state}"),
    )
    badge.setToolTip = lambda tip: setattr(badge, "tip", tip)  # type: ignore[attr-defined]
    capability = VulkanCapability(available=True, device="RTX", index=0, nlmeans=True)
    DashboardPage._on_vulkan_detected(cast(Any, page), "ffmpeg", capability)
    assert emitted == [("ffmpeg", capability)]
    assert badge.text == "Vulkan:available" and "RTX" in badge.tip
    DashboardPage._on_vulkan_detected(cast(Any, page), "ffmpeg", VulkanCapability(reason="aucun GPU dédié"))
    assert badge.text == "Vulkan:unavailable" and "aucun GPU dédié" in badge.tip


def test_dashboard_nvof_badge(qt_app):
    from core.workflows.encode.interpolation import NvofCapability

    badge = SimpleNamespace(text="", tip="")
    page = SimpleNamespace(
        _nvof_badge=badge,
        _apply_encoder_badge_state=lambda b, label, state: setattr(b, "text", f"{label}:{state}"),
    )
    badge.setToolTip = lambda tip: setattr(badge, "tip", tip)  # type: ignore[attr-defined]
    DashboardPage._on_nvof_detected(cast(Any, page), NvofCapability(available=True, device="RTX 4070"))
    assert badge.text == "NVOF·CUDA:available" and "RTX 4070" in badge.tip
    DashboardPage._on_nvof_detected(cast(Any, page), NvofCapability(device="GTX 1070", reason="GPU sans NVOFA"))
    assert badge.text == "NVOF·CUDA:unavailable" and "GPU sans NVOFA" in badge.tip and "Vulkan" in badge.tip


def test_encode_panel_forwards_capability_to_workflow(qt_app):
    from core.config import AppConfig
    from ui.panels.encode_panel.panel import EncodePanel

    panel = EncodePanel(AppConfig())
    capability = VulkanCapability(available=True, device="RTX", index=0, nlmeans=True)
    panel.set_vulkan_capability(panel._workflow._ffmpeg, capability)
    assert panel._workflow.vulkan_capability() is capability
    panel.close()


def test_vulkan_detection_on_real_tools():
    import shutil

    from core.workflows.encode.vulkan import detect_vulkan

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg requis")
    capability = detect_vulkan("ffmpeg")
    if capability.nlmeans:
        assert capability.index is not None and any(
            d.index == capability.index and d.kind == "discrete" for d in capability.devices
        )
    else:
        assert capability.reason
