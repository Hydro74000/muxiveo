"""
tests/test_encode_restoration.py — Restauration (réseau R2 de muxiveo-rife ``--restore``).

Couverture (sans binaire ni GPU) :
    - RestoreSettings : valeurs, profils JSON, moteur dans le pipeline sans changement de cadence
    - Options du moteur : niveau, mode rapide, animation, transfert, tramage 8 bits
    - Profondeur « Auto » → 10 bits sous Restauration (D-h)
    - Pipeline décodage (10 bits) | muxiveo-rife --factor 1 | encodeur ; cumul avec l'interpolation
    - Workflow : validation (capacités, graphes, entrelacement), avertissement 8 bits, genre « Animation »
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.media_info_fetcher import is_animation_genre
from core.pipeline_command import PipelineCommand, command_stages
from core.workflows.encode import (
    EncodeConfig,
    EncodePreset,
    EncodeWorkflow,
    FrameInterpolationSettings,
    RestoreSettings,
    VideoEncodeSettings,
)
from core.workflows.encode.domain import EncodeCodecDomainCallbacks
from core.workflows.encode.domain.codecs import resolve_output_bit_depth
from core.workflows.encode.interpolation import (
    InterpolationSource,
    RifeCapabilities,
    build_rife_stage,
    restore_args_for,
    restore_engine_args,
    restore_support_errors,
)
from core.workflows.encode.runtime.video_preparation import (
    VideoOnlyCommandBuilder,
    VideoOnlyCommandBuilderCallbacks,
)
from core.workflows.encode.workflow import _interpolation_decode_cmd


def _restore(**kw) -> RestoreSettings:
    return RestoreSettings(**{"enabled": True, **kw})


def _video(**kw) -> VideoEncodeSettings:
    return VideoEncodeSettings(**{"codec": "libx265", "restoration": _restore(), **kw})


def _caps(options=("restore",), restore_models=("r2", "r2q"), contract=1) -> RifeCapabilities:
    return RifeCapabilities((1, 12, 0), contract, frozenset(options), frozenset(), "ok", frozenset(restore_models))


# ---------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------

class TestSettings:
    def test_inactive_by_default(self):
        video = VideoEncodeSettings(codec="libx265")
        assert not video.restores() and not video.uses_engine()

    def test_engine_without_rate_change(self):
        video = _video()
        assert video.restores() and video.uses_engine() and not video.interpolates()
        assert video.frame_ratio() == 1
        assert video.has_video_transform()

    def test_copy_never_restores(self):
        assert not _video(codec="copy").restores()

    def test_unknown_values_fall_back_to_auto(self):
        settings = RestoreSettings.from_value({"enabled": True, "level": "max", "animation": "yes", "fast": True})
        assert (settings.level, settings.animation, settings.fast) == ("auto", "auto", True)

    def test_preset_roundtrip(self):
        preset = EncodePreset(name="p", restoration={"enabled": True, "level": "fort", "fast": True})  # type: ignore[arg-type]
        restored = preset.to_video_settings().restoration
        assert (restored.enabled, restored.level, restored.fast) == (True, "fort", True)


# ---------------------------------------------------------------------------
# Options du moteur et profondeur
# ---------------------------------------------------------------------------

class TestEngineArgs:
    def test_level_fast_animation_dither(self):
        args = restore_engine_args(_restore(level="fort", fast=True), transfer="pq", animation=True, dither="8")
        assert args[:4] == ["--restore", "fort", "--transfer", "pq"]
        assert "--restore-fast" in args
        assert "--restore-animation" not in args  # niveau imposé : l'animation ne déplace que l'Auto
        assert args[args.index("--restore-dither") + 1] == "8"
        auto = restore_engine_args(_restore(), transfer="", animation=True, model_dir="/m/r2")
        assert auto[auto.index("--transfer") + 1] == "sdr"
        assert "--restore-animation" in auto and auto[auto.index("--restore-model") + 1] == "/m/r2"
        assert "--restore-dither" not in auto and "--restore-fast" not in auto

    @pytest.mark.parametrize("transfer, expected", [("smpte2084", "pq"), ("arib-std-b67", "hlg"), ("bt709", "sdr"),
                                                    ("", "sdr")])
    def test_transfer_from_source(self, transfer, expected):
        args = restore_args_for(_video(), InterpolationSource(transfer=transfer))
        assert args[args.index("--transfer") + 1] == expected

    def test_animation_setting(self):
        assert "--restore-animation" in restore_args_for(_video(restoration=_restore(animation="on")),
                                                         InterpolationSource())
        assert "--restore-animation" not in restore_args_for(_video(restoration=_restore(animation="off")),
                                                             InterpolationSource(), animation=True)
        assert "--restore-animation" in restore_args_for(_video(), InterpolationSource(), animation=True)

    def test_without_restoration_no_args(self):
        assert restore_args_for(VideoEncodeSettings(codec="libx265"), InterpolationSource()) == []

    def test_auto_depth_is_10_bits_under_restoration(self):
        assert resolve_output_bit_depth(VideoEncodeSettings(codec="libx265", source_bit_depth=8)) == 8
        assert resolve_output_bit_depth(_video(source_bit_depth=8)) == 10
        assert "--restore-dither" not in restore_args_for(_video(source_bit_depth=8), InterpolationSource())

    def test_forced_8_bits_dithers_at_8_bit_precision(self):
        video = _video(source_bit_depth=10, bit_depth="8")
        assert resolve_output_bit_depth(video) == 8
        args = restore_args_for(video, InterpolationSource())
        assert args[args.index("--restore-dither") + 1] == "8"


class TestRifeStage:
    def test_restore_only_command(self):
        cmd = build_rife_stage("/opt/muxiveo-rife", quality="quality", source=InterpolationSource("bt2020nc"),
                               gpu=1, interpolate=False, restore_args=["--restore", "auto"])
        assert cmd[cmd.index("--factor") + 1] == "1"
        assert cmd[cmd.index("--restore") + 1] == "auto"
        assert cmd[cmd.index("--matrix") + 1] == "bt2020nc"
        assert cmd[cmd.index("--gpu") + 1] == "1"
        for option in ("--model", "--engine", "--scene-threshold", "--uhd", "--tta", "--ultra"):
            assert option not in cmd

    def test_restore_with_interpolation(self):
        cmd = build_rife_stage("r", factor=2, quality="fast", source=InterpolationSource(),
                               restore_args=["--restore", "leger"])
        assert cmd[cmd.index("--factor") + 1] == "2" and "--model" in cmd
        assert cmd[cmd.index("--restore") + 1] == "leger"


def _builder(source_info: InterpolationSource | None = None) -> VideoOnlyCommandBuilder:
    def primary_video_settings(cfg: EncodeConfig) -> VideoEncodeSettings:
        assert cfg.video is not None
        return cfg.video

    return VideoOnlyCommandBuilder(
        VideoOnlyCommandBuilderCallbacks(
            ffmpeg_bin="ffmpeg",
            ffmpeg_progress_args=lambda: ["-progress", "pipe:1", "-nostats"],
            ffmpeg_thread_args=lambda _n: ["-threads", "4"],
            offset_input_args=lambda _ms: [],
            codec_domain_callbacks=lambda: EncodeCodecDomainCallbacks(
                platform="linux", nvenc_device=None, vaapi_device=None, qsv_device=None, amf_device=None,
            ),
            primary_video_settings=primary_video_settings,
            video_source_path=lambda cfg: cfg.source,
            video_stream_from_settings=lambda v: v.stream_index,
            size_to_bitrate_kbps=lambda _cfg: 4000,
            size_to_bitrate_kbps_for_video=lambda _cfg, _v: 4000,
            rife_bin="/bin/muxiveo-rife",
            interpolation_source=lambda _v, _s: source_info or InterpolationSource(transfer="smpte2084"),
        )
    )


class TestPipeline:
    def test_restore_only_pipeline(self, tmp_path):
        video = _video(source_bit_depth=8)
        cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video)
        [cmd] = _builder().build_video_only_mkv_commands(cfg, video, cfg.source, tmp_path / "v.mkv")
        assert isinstance(cmd, PipelineCommand)
        decode, rife, encode = command_stages(cmd)
        assert decode[decode.index("-vf") + 1].endswith("format=yuv420p10le")
        assert rife[rife.index("--factor") + 1] == "1" and "--model" not in rife
        assert rife[rife.index("--transfer") + 1] == "pq"
        assert "format=" not in encode[encode.index("-vf") + 1]  # images du moteur déjà en 4:2:0 10 bits

    def test_restore_with_interpolation_pipeline(self, tmp_path):
        video = _video(interpolation=FrameInterpolationSettings(enabled=True, factor=2), bit_depth="8")
        cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video)
        [cmd] = _builder(InterpolationSource()).build_video_only_mkv_commands(cfg, video, cfg.source, tmp_path / "v.mkv")
        _decode, rife, encode = command_stages(cmd)
        assert rife[rife.index("--factor") + 1] == "2" and "--restore" in rife
        assert rife[rife.index("--restore-dither") + 1] == "8"
        assert encode[encode.index("-vf") + 1].endswith("format=yuv420p")

    def test_nvencc_decode_stage_in_10_bits(self):
        decode = ["ffmpeg", "-i", "s.mkv", "-map", "0:0", "-f", "yuv4mpegpipe", "-"]
        cmd = _interpolation_decode_cmd(decode, InterpolationSource(), _video())
        assert cmd[cmd.index("-vf") + 1] == "format=yuv420p10le"
        plain = _interpolation_decode_cmd(decode, InterpolationSource(), VideoEncodeSettings(codec="nvencc_hevc"))
        assert "-vf" not in plain


# ---------------------------------------------------------------------------
# Capacités du binaire
# ---------------------------------------------------------------------------

class TestSupport:
    def test_supported(self):
        assert restore_support_errors(_caps(), _restore()) == []
        assert restore_support_errors(_caps(), _restore(fast=True)) == []
        assert restore_support_errors(None, _restore()) == []

    def test_old_binary(self):
        [error] = restore_support_errors(_caps(options=()), _restore())
        assert "1.12.0" in error

    def test_missing_graphs(self):
        [error] = restore_support_errors(_caps(restore_models=("r2",)), _restore(fast=True))
        assert "r2q" in error and "mode rapide" in error
        assert restore_support_errors(_caps(restore_models=()), _restore())

    def test_contract(self):
        [error] = restore_support_errors(_caps(contract=2), _restore())
        assert "contrat 2" in error


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

def _payload(*, tags: dict | None = None, **stream) -> dict:
    base = {"index": 0, "codec_type": "video", "r_frame_rate": "25/1", "avg_frame_rate": "25/1",
            "color_transfer": "bt709", "pix_fmt": "yuv420p", "codec_name": "h264"}
    return {"streams": [{**base, **stream}], "format": {"start_time": "0.000000", "tags": tags or {}}}


@pytest.fixture
def workflow(qt_app, tmp_path):
    _ = qt_app
    rife = tmp_path / "muxiveo-rife"
    rife.write_text("")
    return EncodeWorkflow(ffmpeg_bin="ffmpeg", rife_bin=str(rife))


def _cfg(tmp_path: Path, video: VideoEncodeSettings, **kw) -> EncodeConfig:
    src = tmp_path / "src.mkv"
    src.write_bytes(b"")
    return EncodeConfig(source=src, output=tmp_path / "out.mkv", video=video, **kw)


class TestWorkflow:
    def test_validation_ok(self, workflow, tmp_path):
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_payload()), \
                patch("core.workflows.encode.workflow._rife_capabilities", return_value=_caps()):
            assert workflow._restoration_validation_errors(_cfg(tmp_path, _video())) == []

    def test_validation_errors(self, qt_app, tmp_path):
        _ = qt_app
        wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", rife_bin=None)
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_payload(field_order="tt")):
            text = "\n".join(wf._restoration_validation_errors(_cfg(tmp_path, _video())))
        assert "muxiveo-rife introuvable" in text and "entrelacée" in text

    def test_validation_reads_capabilities(self, workflow, tmp_path):
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_payload()), \
                patch("core.workflows.encode.workflow._rife_capabilities", return_value=_caps(options=())):
            [error] = workflow._restoration_validation_errors(_cfg(tmp_path, _video()))
        assert "1.12.0" in error

    def test_deinterlaced_source_accepted(self, workflow, tmp_path):
        video = _video()
        video.filters.yadif_enabled = True
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_payload(field_order="tt")), \
                patch("core.workflows.encode.workflow._rife_capabilities", return_value=_caps()):
            assert workflow._restoration_validation_errors(_cfg(tmp_path, video)) == []

    def test_copy_refused(self, workflow, tmp_path):
        video = VideoEncodeSettings(codec="copy", restoration=_restore())
        [error] = workflow._restoration_validation_errors(_cfg(tmp_path, video))
        assert "COPY" in error

    def test_inactive_not_checked(self, qt_app, tmp_path):
        _ = qt_app
        wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", rife_bin=None)
        assert wf._restoration_validation_errors(_cfg(tmp_path, VideoEncodeSettings(codec="libx265"))) == []

    def test_forced_8_bits_warns(self, workflow, tmp_path):
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_payload()):
            warned = workflow.config_warnings(_cfg(tmp_path, _video(bit_depth="8")))
            quiet = workflow.config_warnings(_cfg(tmp_path, _video(source_bit_depth=8)))
        assert any("Restauration avec une sortie 8 bits" in m for m in warned)
        assert not any("Restauration" in m for m in quiet)

    def test_animation_from_output_genre(self, workflow, tmp_path):
        cfg = _cfg(tmp_path, _video(), tag_overrides={"GENRE": "Animation, Familial"})
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_payload()):
            resolved = workflow.resolve_source_color_transfer(cfg)
        assert resolved.video is not None and resolved.video.restoration.animation == "on"

    def test_output_genre_overrides_source(self, workflow, tmp_path):
        cfg = _cfg(tmp_path, _video(), tag_overrides={"GENRE": "Drame"})
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload",
                          return_value=_payload(tags={"GENRE": "Animation"})):
            resolved = workflow.resolve_source_color_transfer(cfg)
        assert resolved.video is not None and resolved.video.restoration.animation == "off"

    def test_animation_from_source_tags(self, workflow, tmp_path):
        cfg = _cfg(tmp_path, _video())
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload",
                          return_value=_payload(tags={"genre": "Animación"})):
            resolved = workflow.resolve_source_color_transfer(cfg)
        assert resolved.video is not None and resolved.video.restoration.animation == "on"

    def test_explicit_animation_kept(self, workflow, tmp_path):
        cfg = _cfg(tmp_path, _video(restoration=_restore(animation="off")), tag_overrides={"GENRE": "Animation"})
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_payload()):
            resolved = workflow.resolve_source_color_transfer(cfg)
        assert resolved.video is not None and resolved.video.restoration.animation == "off"


@pytest.mark.parametrize("genre, expected", [
    ("Animation", True), ("Familial, Animation", True), ("Animazione", True), ("アニメーション", True),
    ("Мультфильм", True), ("Action; Animation", True), ("Action, Thriller", False), ("", False),
])
def test_animation_genre_names(genre, expected):
    assert is_animation_genre(genre) is expected
