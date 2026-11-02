"""
tests/test_encode_restoration.py — Restauration (réseau R2 de muxiveo-rife ``--restore``).

Couverture (sans binaire ni GPU) :
    - RestoreSettings : valeurs, profils JSON, moteur dans le pipeline sans changement de cadence
    - Options du moteur : niveau, mode rapide, animation, transfert, tramage 8 bits
    - Profondeur « Auto » → 10 bits sous Restauration (D-h)
    - Pipeline décodage (10 bits) | muxiveo-rife --factor 1 | encodeur ; cumul avec l'interpolation
    - Workflow : validation (capacités, graphes, entrelacement), avertissement 8 bits, genre « Animation »
    - MaxCLL / MaxFALL mesurés sur l'image restaurée (statistiques du moteur), HDR10+ de la source conservé
"""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

    def test_tonemap_stays_10_bits_before_the_engine(self):
        from core.workflows.encode.domain.codecs import build_vf
        vf = build_vf(_video(tonemap_to_sdr=True))
        assert "format=yuv420p10le" in vf and "format=yuv420p," not in vf
        assert "format=yuv420p," in build_vf(VideoEncodeSettings(codec="libx265", tonemap_to_sdr=True))

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

    def test_vfr_with_dynamic_hdr_copy_refused(self, workflow, tmp_path):
        vfr = _payload(r_frame_rate="30/1", avg_frame_rate="29/1")
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=vfr), \
                patch.object(EncodeWorkflow, "_load_mediainfo_video_track", return_value={}), \
                patch("core.workflows.encode.workflow._rife_capabilities", return_value=_caps()):
            [error] = workflow._restoration_validation_errors(_cfg(tmp_path, _video(copy_dv=True)))
            assert "VFR" in error
            assert workflow._restoration_validation_errors(_cfg(tmp_path, _video())) == []  # cadence normalisée

    def test_with_interpolation_no_duplicate_messages(self, qt_app, tmp_path):
        _ = qt_app
        wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", rife_bin=None)
        video = _video(interpolation=FrameInterpolationSettings(enabled=True, factor=2))
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_payload(field_order="tt")):
            assert wf._restoration_validation_errors(_cfg(tmp_path, video)) == []
            text = "\n".join(wf.validate(_cfg(tmp_path, video)))
        assert text.count("introuvable") == 1 and text.count("entrelacée") == 1

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


# ---------------------------------------------------------------------------
# MaxCLL / MaxFALL de l'image restaurée (D-e)
# ---------------------------------------------------------------------------

def _hdr10(**kw) -> VideoEncodeSettings:
    return _video(**{"source_color_transfer": "smpte2084", "inject_hdr_meta": True, "max_cll": "1000,400",
                     "static_hdr_light_level_source": "source", **kw})


class TestLightLevels:
    def _prepare(self, workflow, video, tmp_path, supported=True):
        with patch("core.workflows.encode.workflow._rife_supports", return_value=supported):
            return workflow._prepare_restore_statistics(video, work_dir=tmp_path, index=1)

    def test_measured_on_restored_image(self, workflow, tmp_path):
        prepared = self._prepare(workflow, _hdr10(), tmp_path)
        assert prepared.restore_statistics == tmp_path / "restore_statistics_1.jsonl"
        assert prepared.max_cll == "" and prepared.static_hdr_light_level_source == "restore_frame_statistics"
        assert prepared.restore_source_light_levels == "1000,400"
        assert prepared.restore_source_statistics == tmp_path / "restore_source_statistics_1.jsonl"
        args = restore_args_for(prepared, InterpolationSource(transfer="smpte2084"))
        assert args[args.index("--frame-statistics") + 1] == str(prepared.restore_statistics)
        assert args[args.index("--source-statistics") + 1] == str(prepared.restore_source_statistics)

    @pytest.mark.parametrize("kw", [
        {"static_hdr_light_level_source": "manual"},             # valeurs saisies : conservées
        {"source_color_transfer": "bt709"},                      # SDR
        {"inject_hdr_meta": False},
        {"restoration": RestoreSettings()},                      # sans Restauration
        {"tonemap_to_sdr": True},
    ])
    def test_not_measured(self, workflow, tmp_path, kw):
        video = _hdr10(**kw)
        assert self._prepare(workflow, video, tmp_path) is video

    def test_old_engine_keeps_source_values(self, workflow, tmp_path):
        messages: list[tuple[str, str]] = []
        workflow.log_message.connect(lambda level, text: messages.append((level, text)))
        video = _hdr10()
        assert self._prepare(workflow, video, tmp_path, supported=False) is video
        assert any(level == "WARN" and "conservés" in text for level, text in messages)

    def test_restored_statistics_take_precedence(self, tmp_path):
        from core.fel.hdr10plus import deferred_light_levels, light_level_origin, light_level_statistics_path
        fel = SimpleNamespace(statistics=tmp_path / "fel.jsonl")
        video = _hdr10(static_hdr_light_level_source="restore_frame_statistics", restore_statistics=tmp_path / "r.jsonl")
        assert deferred_light_levels(video)
        video.fel_context = fel  # type: ignore[assignment]
        assert light_level_statistics_path(video) == tmp_path / "r.jsonl" and light_level_origin(video) == "l'image restaurée"
        video.restore_statistics = None
        assert light_level_statistics_path(video) == tmp_path / "fel.jsonl"
        assert light_level_origin(video) == "l'image reconstruite"

    def test_nvencc_writes_restored_levels_without_recomputing_hdr10plus(self, tmp_path, monkeypatch):
        import core.workflows.encode.runtime.nvencc_execution as execution
        statistics = tmp_path / "r.jsonl"
        lines = [{"format": "mvo-fel-frame-statistics", "version": 1, "bins": 1024, "width": 4, "height": 2}]
        for index in range(2):
            lines.append({"frame": index, "scene_start": index == 0, "max_nits": [812.4, 500.0, 100.0], "avg_nits": 41.6,
                          "y9999_nits": 600.0, "y100": 0.5, "maxrgb_percentiles": [0, 1, 2, 4, 8, 16, 64]})
        lines.append({"scene_first": 0, "frames": 2, "maxrgb_hist": [[600, 16]]})
        statistics.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
        intermediate = tmp_path / "nvencc.mkv"
        intermediate.write_bytes(b"mkv")
        commands: list[list[str]] = []

        def run(cmd, _label):
            commands.append(cmd)
            Path(cmd[-1]).write_bytes(b"hevc")
        rewrite = Mock(side_effect=lambda **kw: kw["output"].write_bytes(b"new"))
        monkeypatch.setattr(execution.MatroskaHevcPayloadRewriter, "rewrite", lambda self, **kw: rewrite(**kw))
        sei = Mock(side_effect=lambda src, dst, **kw: (dst.write_bytes(b"cll"), SimpleNamespace(applied=True))[1])
        monkeypatch.setattr(execution, "replace_content_light_level_sei_file", sei)
        log = Mock()
        video = _hdr10(codec="nvencc_hevc", copy_hdr10plus=True, static_hdr_light_level_source="restore_frame_statistics",
                       restore_statistics=statistics)
        execution._apply_fel_hdr10plus(
            video=video, intermediate=intermediate, statistics_path=None, light_statistics_path=statistics, cwd=tmp_path,
            frame_ratio=Fraction(1), encoded_frames=2, ffmpeg_bin="ffmpeg", hdr10plus_bin="hdr10plus_tool",
            dovi_bin="dovi_tool", run=run, cancelled=lambda: False, log=log, progress=Mock(),
        )
        assert len(commands) == 1 and "hevc_mp4toannexb" in commands[0]  # HDR10+ de la source intact
        assert sei.call_args.kwargs == {"max_cll": "812,42"} and rewrite.call_args.kwargs["light_levels"] == (812, 42)
        assert any("l'image restaurée" in str(call) for call in log.call_args_list)


def _statistics(peaks: list[float], highs: list[float], avg: float = 40.0):
    from core.fel.hdr10plus import FrameRecord, FrameStatistics, SceneRecord
    frames = tuple(FrameRecord(i == 0, (p, p * 0.8, p * 0.5), avg, h, 0.5, (0, 1, 2, 4, 8, 16, h))
                   for i, (p, h) in enumerate(zip(peaks, highs)))
    return FrameStatistics(4, 2, frames, (SceneRecord(0, len(frames)),))


def _write(path: Path, statistics) -> Path:
    lines = [{"format": "mvo-fel-frame-statistics", "version": 1, "bins": 1024, "width": 4, "height": 2}]
    for index, f in enumerate(statistics.frames):
        lines.append({"frame": index, "scene_start": f.scene_start, "max_nits": list(f.max_nits), "avg_nits": f.avg_nits,
                      "y9999_nits": f.y9999_nits, "y100": f.y100, "maxrgb_percentiles": list(f.percentiles)})
    lines.append({"scene_first": 0, "frames": len(statistics.frames), "maxrgb_hist": []})
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


class TestWrittenLightLevels:
    def _video(self, tmp_path: Path, source_stats, declared: str = "4342,2342") -> VideoEncodeSettings:
        return _hdr10(restore_statistics=tmp_path / "r.jsonl", restore_source_light_levels=declared,
                      restore_source_statistics=_write(tmp_path / "s.jsonl", source_stats),
                      static_hdr_light_level_source="restore_frame_statistics")

    def test_isolated_peaks_keep_source_values(self, tmp_path):
        from core.fel.hdr10plus import written_light_levels
        # grain retiré : pic 3494 → 3024, porté par quelques pixels (99,98 % de la source à 1800 nits)
        source = _statistics([3494.0] * 8, [1800.0] * 8, avg=108.6)
        value, message = written_light_levels(self._video(tmp_path, source), _statistics([3024.0] * 8, [1750.0] * 8, 108.2))
        assert value == "4342,2342" and "conservés" in message and "3494,109" in message
        # pic relevé de quelques pixels seulement
        value, _ = written_light_levels(self._video(tmp_path, source), _statistics([3600.0] * 8, [1810.0] * 8, 108.6))
        assert value == "4342,2342"

    def test_visible_change_writes_the_measure(self, tmp_path):
        from core.fel.hdr10plus import written_light_levels
        source = _statistics([1200.0] * 8, [1150.0] * 8, avg=70.0)
        # hautes lumières étendues : plus de 0,02 % des pixels au-delà du pic restauré
        value, message = written_light_levels(self._video(tmp_path, source), _statistics([1000.0] * 8, [900.0] * 8, 70.0))
        assert value == "1000,70" and "écart visible" in message
        # MaxFALL déplacé de plus de 1 %
        value, _ = written_light_levels(self._video(tmp_path, source), _statistics([1200.0] * 8, [1100.0] * 8, 65.0))
        assert value == "1200,65"

    def test_dark_content_fall_tolerance(self, tmp_path):
        from core.fel.hdr10plus import written_light_levels
        source = _statistics([284.0] * 8, [200.0] * 8, avg=3.01)
        # MaxFALL de quelques nits : quelques centièmes d'écart, invisibles
        value, _ = written_light_levels(self._video(tmp_path, source, "498,92"), _statistics([288.0] * 8, [199.0] * 8, 3.06))
        assert value == "498,92"

    def test_without_source_values_or_restoration(self, tmp_path):
        from core.fel.hdr10plus import written_light_levels
        stats = _statistics([900.0] * 4, [700.0] * 4)
        for declared in ("", "0,0"):
            assert written_light_levels(self._video(tmp_path, stats, declared), stats)[0] == "900,40"
        fel = _hdr10(static_hdr_light_level_source="fel_frame_statistics", restore_source_light_levels="1000,400")
        value, message = written_light_levels(fel, stats)
        assert value == "900,40" and "reconstruite" in message
        broken = self._video(tmp_path, stats)
        broken.restore_source_statistics = tmp_path / "absent.jsonl"
        assert written_light_levels(broken, stats)[0] == "900,40"
