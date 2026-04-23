from __future__ import annotations

from unittest.mock import patch

from core.workflows.encode.hw_devices import LinuxRenderNodeInfo, select_linux_hwaccel_device
from core.workflows.encode import hw_devices as hw_devices_mod


class TestSelectLinuxHwaccelDevice:

    def test_vaapi_prefers_amd_over_nvidia(self):
        nodes = (
            LinuxRenderNodeInfo("/dev/dri/renderD128", vendor_id="0x10de", driver="nvidia"),
            LinuxRenderNodeInfo("/dev/dri/renderD129", vendor_id="0x1002", driver="amdgpu"),
        )

        assert select_linux_hwaccel_device("hevc_vaapi", nodes=nodes) == "/dev/dri/renderD129"

    def test_vaapi_falls_back_to_intel_when_no_amd_exists(self):
        nodes = (
            LinuxRenderNodeInfo("/dev/dri/renderD128", vendor_id="0x10de", driver="nvidia"),
            LinuxRenderNodeInfo("/dev/dri/renderD129", vendor_id="0x8086", driver="i915"),
        )

        assert select_linux_hwaccel_device("h264_vaapi", nodes=nodes) == "/dev/dri/renderD129"

    def test_qsv_requires_intel_render_node(self):
        nodes = (
            LinuxRenderNodeInfo("/dev/dri/renderD128", vendor_id="0x10de", driver="nvidia"),
            LinuxRenderNodeInfo("/dev/dri/renderD129", vendor_id="0x8086", driver="xe"),
        )

        assert select_linux_hwaccel_device("av1_qsv", nodes=nodes) == "/dev/dri/renderD129"

    def test_qsv_returns_none_without_intel_node(self):
        nodes = (
            LinuxRenderNodeInfo("/dev/dri/renderD128", vendor_id="0x10de", driver="nvidia"),
            LinuxRenderNodeInfo("/dev/dri/renderD129", vendor_id="0x1002", driver="amdgpu"),
        )

        assert select_linux_hwaccel_device("hevc_qsv", nodes=nodes) is None


class TestSelectWindowsHwaccelDevice:

    def setup_method(self):
        hw_devices_mod.select_windows_hwaccel_device.cache_clear()

    def test_windows_qsv_picks_first_successful_adapter(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            class Result:
                def __init__(self, returncode: int):
                    self.returncode = returncode
            return Result(0 if cmd[cmd.index("-qsv_device") + 1] == "1" else 1)

        with patch("core.workflows.encode.hw_devices.subprocess.run", side_effect=fake_run):
            selected = hw_devices_mod.select_windows_hwaccel_device("hevc_qsv", ffmpeg_bin="ffmpeg.exe")

        assert selected == "1"
        assert any("-qsv_device" in cmd and cmd[cmd.index("-qsv_device") + 1] == "0" for cmd in calls)
        assert any("-qsv_device" in cmd and cmd[cmd.index("-qsv_device") + 1] == "1" for cmd in calls)

    def test_windows_amf_uses_hwupload_probe(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            class Result:
                returncode = 0
            return Result()

        with patch("core.workflows.encode.hw_devices.subprocess.run", side_effect=fake_run):
            selected = hw_devices_mod.select_windows_hwaccel_device("h264_amf", ffmpeg_bin="ffmpeg.exe")

        assert selected == "0"
        cmd = calls[0]
        assert "-init_hw_device" in cmd
        assert "-filter_hw_device" in cmd
        assert "-vf" in cmd
        assert cmd[cmd.index("-vf") + 1] == "format=nv12,hwupload"
