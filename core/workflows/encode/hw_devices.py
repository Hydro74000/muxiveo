"""
Helpers pour sélectionner explicitement un device hardware par famille de codec.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from core.subprocess_utils import subprocess_windows_no_window_kwargs


_VAAPI_CODECS = {"hevc_vaapi", "h264_vaapi", "av1_vaapi"}
_QSV_CODECS = {"hevc_qsv", "h264_qsv", "av1_qsv"}
_NVENC_CODECS = {"hevc_nvenc", "h264_nvenc", "av1_nvenc"}
_AMF_CODECS = {"hevc_amf", "h264_amf", "av1_amf"}

_AMD_VENDOR_IDS = {"0x1002"}
_INTEL_VENDOR_IDS = {"0x8086"}
_NVIDIA_VENDOR_IDS = {"0x10de"}

_AMD_DRIVERS = {"amdgpu", "radeon"}
_INTEL_DRIVERS = {"i915", "xe"}
_NVIDIA_DRIVERS = {"nvidia", "nouveau"}

_NULLSRC = "nullsrc=s=256x256:r=25:d=0.1"
_GENERIC_HW_FILTER = "format=nv12"
_HWUPLOAD_FILTER = "format=nv12,hwupload"


@dataclass(frozen=True)
class LinuxRenderNodeInfo:
    path: str
    vendor_id: str | None = None
    driver: str | None = None


def _read_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _driver_name(path: Path) -> str | None:
    try:
        return path.resolve().name.strip() or None
    except OSError:
        return None


def _vendor_kind(node: LinuxRenderNodeInfo) -> str:
    vendor_id = str(node.vendor_id or "").strip().lower()
    driver = str(node.driver or "").strip().lower()

    if vendor_id in _AMD_VENDOR_IDS or driver in _AMD_DRIVERS:
        return "amd"
    if vendor_id in _INTEL_VENDOR_IDS or driver in _INTEL_DRIVERS:
        return "intel"
    if vendor_id in _NVIDIA_VENDOR_IDS or driver in _NVIDIA_DRIVERS:
        return "nvidia"
    if vendor_id or driver:
        return "other"
    return "unknown"


def _first_node(nodes: Iterable[LinuxRenderNodeInfo], *kinds: str) -> LinuxRenderNodeInfo | None:
    wanted = set(kinds)
    for node in nodes:
        if _vendor_kind(node) in wanted:
            return node
    return None


@lru_cache(maxsize=1)
def detect_linux_render_nodes() -> tuple[LinuxRenderNodeInfo, ...]:
    if not sys.platform.startswith("linux"):
        return ()

    sysfs_root = Path("/sys/class/drm")
    dev_root = Path("/dev/dri")
    nodes: list[LinuxRenderNodeInfo] = []

    for render_node in sorted(sysfs_root.glob("renderD*")):
        device_path = dev_root / render_node.name
        if not device_path.exists():
            continue
        nodes.append(
            LinuxRenderNodeInfo(
                path=str(device_path),
                vendor_id=_read_text(render_node / "device" / "vendor"),
                driver=_driver_name(render_node / "device" / "driver"),
            )
        )

    return tuple(nodes)


def select_linux_hwaccel_device(
    codec_id: str,
    *,
    nodes: tuple[LinuxRenderNodeInfo, ...] | None = None,
) -> str | None:
    """
    Retourne le render node DRM à utiliser pour `codec_id`, ou None.

    Politique :
    - QSV  -> Intel uniquement.
    - VAAPI -> AMD en priorité, puis Intel, puis tout GPU non-NVIDIA.
      Cela évite de tomber sur un render node NVIDIA en multi-GPU quand
      l'utilisateur attend en pratique un backend VAAPI Mesa/Intel.
    """
    current_nodes = detect_linux_render_nodes() if nodes is None else tuple(nodes)
    if not current_nodes:
        return None

    if codec_id in _QSV_CODECS:
        node = _first_node(current_nodes, "intel")
        return node.path if node is not None else None

    if codec_id in _VAAPI_CODECS:
        node = (
            _first_node(current_nodes, "amd")
            or _first_node(current_nodes, "intel")
            or _first_node(current_nodes, "other")
        )
        if node is not None:
            return node.path
        if len(current_nodes) == 1:
            return current_nodes[0].path
        return None

    return None


def _is_windows_runtime(ffmpeg_bin: str) -> bool:
    return sys.platform == "win32" or ffmpeg_bin.lower().endswith(".exe")


def _probe_windows_adapter(cmd: list[str]) -> bool:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=5,
            **subprocess_windows_no_window_kwargs(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _windows_probe_command(ffmpeg_bin: str, codec_id: str, adapter_index: int) -> list[str] | None:
    base_cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error"]

    if codec_id in _NVENC_CODECS:
        return [
            *base_cmd,
            "-f", "lavfi", "-i", _NULLSRC,
            "-vf", _GENERIC_HW_FILTER,
            "-frames:v", "1",
            "-c:v", codec_id,
            "-gpu", str(adapter_index),
            "-f", "null", "-",
        ]

    if codec_id in _QSV_CODECS:
        return [
            *base_cmd,
            "-qsv_device", str(adapter_index),
            "-f", "lavfi", "-i", _NULLSRC,
            "-vf", _GENERIC_HW_FILTER,
            "-frames:v", "1",
            "-c:v", codec_id,
            "-f", "null", "-",
        ]

    if codec_id in _AMF_CODECS:
        device_name = f"mre_amf_probe{adapter_index}"
        return [
            *base_cmd,
            "-init_hw_device", f"d3d11va={device_name}:{adapter_index}",
            "-filter_hw_device", device_name,
            "-f", "lavfi", "-i", _NULLSRC,
            "-vf", _HWUPLOAD_FILTER,
            "-frames:v", "1",
            "-c:v", codec_id,
            "-f", "null", "-",
        ]

    return None


@lru_cache(maxsize=64)
def select_windows_hwaccel_device(
    codec_id: str,
    *,
    ffmpeg_bin: str = "ffmpeg",
    max_adapters: int = 8,
) -> str | None:
    """
    Retourne l'index d'adaptateur Windows a utiliser pour `codec_id`, ou None.

    La resolution est basee sur des probes FFmpeg reels, ce qui evite de
    supposer que l'ordre WMI/DirectX/CUDA est identique.
    """
    if not _is_windows_runtime(ffmpeg_bin):
        return None

    if codec_id not in (_NVENC_CODECS | _QSV_CODECS | _AMF_CODECS):
        return None

    for adapter_index in range(max(1, int(max_adapters))):
        cmd = _windows_probe_command(ffmpeg_bin, codec_id, adapter_index)
        if cmd is None:
            return None
        if _probe_windows_adapter(cmd):
            return str(adapter_index)
    return None


__all__ = [
    "LinuxRenderNodeInfo",
    "detect_linux_render_nodes",
    "select_linux_hwaccel_device",
    "select_windows_hwaccel_device",
]
