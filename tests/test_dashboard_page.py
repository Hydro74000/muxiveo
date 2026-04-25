"""
tests/test_dashboard_page.py - Regressions non-UI pour DashboardPage.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from ui.main_window import DashboardPage


def _completed(*, stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def test_dashboard_scans_software_encoders_with_configured_ffmpeg_path():
    custom_ffmpeg = r"C:\Tools\ffmpeg\bin\ffmpeg.exe"

    def fake_run(cmd, **_kwargs):
        assert cmd == [custom_ffmpeg, "-hide_banner", "-encoders"]
        return _completed(stdout="V....D libx265           libx265 H.265 / HEVC\n")

    with patch("ui.main_window.subprocess.run", side_effect=fake_run):
        detected = DashboardPage._scan_encoder_availability(custom_ffmpeg, ["libx265", "libx264"])

    assert detected == {"libx265": True, "libx264": False}


def test_dashboard_hw_detection_uses_configured_ffmpeg_path():
    custom_ffmpeg = r"C:\Tools\ffmpeg\bin\ffmpeg.exe"
    emitted: list[set[str]] = []
    fake_page = SimpleNamespace(
        _config=SimpleNamespace(tool_ffmpeg=custom_ffmpeg),
        _hw_detected=SimpleNamespace(emit=emitted.append),
    )

    with patch("core.workflows.encode.hardware.HardwareEncoderDetector.detect", return_value={"hevc_nvenc"}) as mock_detect:
        DashboardPage._run_hw_detection(cast(Any, fake_page))

    mock_detect.assert_called_once_with(custom_ffmpeg)
    assert emitted == [{"hevc_nvenc"}]
