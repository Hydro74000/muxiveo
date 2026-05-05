"""Tests ciblés sur la couche backend encode (ffmpeg / nvencc)."""

from __future__ import annotations

from pathlib import Path

from core.workflows.encode.backends import (
    backend_capabilities_for_codec,
    backend_for_codec,
    backend_id_for_codec,
)
from core.workflows.encode.models import EncodeConfig, QualityMode, VideoEncodeSettings
from core.workflows.encode.workflow import EncodeWorkflow


def _make_video(codec: str) -> VideoEncodeSettings:
    return VideoEncodeSettings(
        codec=codec,
        quality_mode=QualityMode.CRF,
        crf=18,
        cq=20,
        bitrate_kbps=5000,
        target_size_mb=4000,
        preset="slow",
        extra_params="",
        inject_hdr_meta=False,
        master_display="",
        max_cll="",
        tonemap_to_sdr=False,
        tonemap_algorithm="hable",
    )


def _make_config(tmp_path: Path, codec: str) -> EncodeConfig:
    return EncodeConfig(
        source=tmp_path / "in.mkv",
        output=tmp_path / "out.mkv",
        video=_make_video(codec),
        audio_tracks=[],
        copy_subtitles=False,
        duration_s=3600.0,
    )


class TestBackendSelection:
    def test_nvencc_codecs_route_to_nvencc_backend(self):
        assert backend_id_for_codec("nvencc_hevc") == "nvencc"
        assert backend_for_codec("nvencc_av1").backend_id == "nvencc"

    def test_other_codecs_route_to_ffmpeg_backend(self):
        assert backend_id_for_codec("libx265") == "ffmpeg"
        assert backend_id_for_codec("hevc_nvenc") == "ffmpeg"
        assert backend_for_codec("copy").backend_id == "ffmpeg"


class TestBackendCapabilities:
    def test_ffmpeg_backend_keeps_size_mode(self):
        caps = backend_capabilities_for_codec("libx265")
        assert caps.backend_id == "ffmpeg"
        assert caps.supports_multi_video is True
        assert QualityMode.SIZE in caps.quality_modes

    def test_nvencc_backend_hides_size_mode(self):
        caps = backend_capabilities_for_codec("nvencc_hevc")
        assert caps.backend_id == "nvencc"
        assert caps.supports_multi_video is False
        assert QualityMode.SIZE not in caps.quality_modes
        assert caps.supports_dynamic_hdr is True
        assert caps.supports_manual_static_hdr is True

    def test_nvencc_h264_disables_dynamic_hdr(self):
        caps = backend_capabilities_for_codec("nvencc_h264")
        assert caps.supports_dynamic_hdr is False
        assert caps.supports_manual_static_hdr is False


class TestBackendProgressParsing:
    def test_nvencc_progress_is_normalized(self):
        backend = backend_for_codec("nvencc_hevc")
        event = backend.parse_progress(
            "[19.5%] 39612 frames: 190.46 fps, 9631 kbps, remain 0:14:17, GPU 7%, VE 95%, VD 54%, est out size 9717.3MB"
        )
        assert event is not None
        assert event.percent == 19.5
        assert event.frame == 39612
        assert event.fps == 190.46
        assert event.eta_seconds == 14 * 60 + 17
        assert event.should_log is False

    def test_nvencc_plain_progress_without_percent_is_normalized(self):
        backend = backend_for_codec("nvencc_hevc")
        event = backend.parse_progress(
            "192 frames: 179.94 fps, 893 kbps, GPU 7%, VE 97%, VD 36%"
        )
        assert event is not None
        assert event.percent is None
        assert event.frame == 192
        assert event.fps == 179.94
        assert event.eta_seconds is None
        assert event.should_log is False

    def test_nvencc_encoded_summary_is_normalized(self):
        backend = backend_for_codec("nvencc_hevc")
        event = backend.parse_progress(
            "encoded 191733 frames, 193.83 fps, 9176.07 kbps, 8747.55 MB"
        )
        assert event is not None
        assert event.percent is None
        assert event.frame == 191733
        assert event.fps == 193.83
        assert event.eta_seconds is None
        assert event.should_log is False

    def test_ffmpeg_progress_is_normalized(self):
        backend = backend_for_codec("libx265")
        event = backend.parse_progress(
            "frame=  240 fps=42.0 q=28.0 size=    1024kB time=00:00:10.00 bitrate= 838.9kbits/s speed=1.75x"
        )
        assert event is not None
        assert event.frame == 240
        assert event.fps == 42.0
        assert event.elapsed_seconds == 10.0
        assert event.should_log is False


class TestWorkflowProgressDelegation:
    def test_workflow_uses_backend_specific_progress_parser_for_nvencc(self, tmp_path: Path):
        wf = EncodeWorkflow(nvencc_bin="/usr/bin/NVEncC")
        config = _make_config(tmp_path, "nvencc_hevc")
        event = wf.parse_progress(
            config,
            "[19.7%] 40076 frames: 190.49 fps, 9608 kbps, remain 0:14:15, GPU 7%, VE 95%, VD 53%, est out size 9694.7MB",
        )
        assert event is not None
        assert event.percent == 19.7

    def test_workflow_uses_backend_specific_progress_parser_for_nvencc_plain_lines(self, tmp_path: Path):
        wf = EncodeWorkflow(nvencc_bin="/usr/bin/NVEncC")
        config = _make_config(tmp_path, "nvencc_hevc")
        event = wf.parse_progress(
            config,
            "362 frames: 193.07 fps, 739 kbps, GPU 7%, VE 97%, VD 36%",
        )
        assert event is not None
        assert event.percent is None
        assert event.frame == 362
        assert event.fps == 193.07

    def test_workflow_uses_backend_specific_progress_parser_for_ffmpeg(self, tmp_path: Path):
        wf = EncodeWorkflow()
        config = _make_config(tmp_path, "libx265")
        event = wf.parse_progress(
            config,
            "frame=  480 fps=48.0 q=24.0 time=00:00:20.00 bitrate=4200.0kbits/s speed=2.00x",
        )
        assert event is not None
        assert event.elapsed_seconds == 20.0
