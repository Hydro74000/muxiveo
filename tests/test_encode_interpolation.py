"""
tests/test_encode_interpolation.py — Interpolation d'images RIFE (muxiveo-rife).

Couverture (sans binaire ni GPU, subprocess réels limités à Python) :
    - FrameInterpolationSettings : activation, multiplicateur, profils JSON
    - interpolation_source_from_probe : matrice, plage, chroma, départ, VFR
    - build_rife_stage / build_decode_stage
    - VideoOnlyCommandBuilder : pipeline décodage | RIFE | encodeur, offsets, -vf
    - build_encoder_vf / hardware_input_args en mode trames pipées
    - Expansion RPU DoVi / JSON HDR10+ et édition des coupes de scène
    - FrameCountGuard : compte source rapporté au multiplicateur
    - ToolRunner._run_pipeline et NvenccPipeExecutor à N étages
    - Workflow : routage (encode découpé), validation
"""

from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.pipeline_command import PipelineCommand, command_display, command_stages
from core.runner import CommandError, TaskSignals, ToolRunner
from core.workflows.encode import (
    EncodeConfig,
    EncodePreset,
    EncodeWorkflow,
    FrameInterpolationSettings,
    QualityMode,
    VideoEncodeSettings,
    VideoFilterSettings,
)
from core.workflows.encode.domain import EncodeCodecDomainCallbacks, build_encoder_vf, hardware_input_args
from core.matroska.editors.dovi import minimum_dovi_level
from core.workflows.encode.interpolation import (
    INTERPOLATION_MODELS,
    frame_repeats,
    ratio_label,
    InterpolationSource,
    build_rife_stage,
    dovi_scene_cut_edit,
    expand_dynamic_hdr_metadata,
    expand_hdr10plus_json,
    expand_rpu_file,
    ffprobe_beside,
    interpolation_source_from_probe,
    multiply_fps_expr,
    parse_rife_progress,
)
from core.workflows.encode.runtime.metadata_inject import _build_dovi_record_from_rpu
from core.workflows.encode.workflow import _interpolation_decode_cmd
from core.workflows.encode.runtime.frame_count_guard import FrameCountGuard
from core.workflows.encode.runtime.nvencc import nvencc_requires_ffmpeg_filter_pipe
from core.workflows.encode.runtime.nvencc_execution import NvenccPipeExecutor
from core.workflows.encode.runtime.video_preparation import (
    VideoOnlyCommandBuilder,
    VideoOnlyCommandBuilderCallbacks,
)


def _interp(**kw) -> FrameInterpolationSettings:
    return FrameInterpolationSettings(**{"enabled": True, "factor": 2, **kw})


def _video(**kw) -> VideoEncodeSettings:
    return VideoEncodeSettings(**{"codec": "libx265", "interpolation": _interp(), **kw})


# ---------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------

class TestSettings:
    def test_inactive_by_default(self):
        assert not FrameInterpolationSettings().is_active()
        assert VideoEncodeSettings().frame_ratio() == 1

    def test_multiplier_requires_reencode(self):
        assert _video().frame_ratio() == 2
        assert _video(codec="copy").frame_ratio() == 1
        assert _video(interpolation=_interp(factor=1)).frame_ratio() == 1
        assert not _video(interpolation=_interp(factor=1)).interpolates()

    def test_from_dict_and_transform_flag(self):
        video = VideoEncodeSettings(interpolation={"enabled": True, "factor": 3, "quality": "max"})
        assert isinstance(video.interpolation, FrameInterpolationSettings)
        assert video.interpolation.factor == 3
        assert video.has_video_transform()

    def test_preset_roundtrip(self):
        preset = EncodePreset(name="x", interpolation=_interp(quality="fast"))
        restored = EncodePreset(**json.loads(json.dumps(preset.to_json_dict())))
        assert restored.interpolation == _interp(quality="fast")
        assert restored.to_video_settings().frame_ratio() == 2


# ---------------------------------------------------------------------------
# Propriétés de la source
# ---------------------------------------------------------------------------

class TestInterpolationSource:
    def test_tagged_stream(self):
        info = interpolation_source_from_probe(
            {"color_space": "bt2020nc", "color_range": "tv", "chroma_location": "topleft",
             "start_time": "0.042000", "r_frame_rate": "24000/1001", "avg_frame_rate": "24000/1001"},
            format_start_time="0.000000",
        )
        assert info == InterpolationSource("bt2020nc", "limited", "topleft", 0.042, False, "", "24000/1001")
        assert info.decode_rate == "24000/1001"

    def test_untagged_fallbacks(self):
        hdr = interpolation_source_from_probe({"color_transfer": "smpte2084", "height": 2160})
        sd = interpolation_source_from_probe({"height": 480})
        hd = interpolation_source_from_probe({"height": 1080, "color_range": "pc"})
        assert (hdr.matrix, sd.matrix, hd.matrix) == ("bt2020nc", "bt601", "bt709")
        assert hd.color_range == "full"
        assert hdr.chroma_location == "left"

    def test_tonemap_forces_bt709(self):
        info = interpolation_source_from_probe({"color_space": "bt2020nc"}, tonemap_to_sdr=True)
        assert (info.matrix, info.color_range) == ("bt709", "limited")

    def test_start_offset_relative_to_container(self):
        info = interpolation_source_from_probe({"start_time": "1.500"}, format_start_time="1.400")
        assert info.start_offset_s == pytest.approx(0.1)

    def test_vfr_detection(self):
        vfr = interpolation_source_from_probe({"r_frame_rate": "30/1", "avg_frame_rate": "75510000/2523679"})
        cfr = interpolation_source_from_probe({"r_frame_rate": "30000/1001", "avg_frame_rate": "2997/100"})
        assert vfr.is_vfr and vfr.cfr_rate == "30/1"
        assert not cfr.is_vfr and cfr.cfr_rate == ""


def test_rife_stage_arguments():
    cmd = build_rife_stage(
        "/opt/muxiveo-rife", factor=3, quality="max",
        source=InterpolationSource("bt2020nc", "limited", "topleft"),
        scene_threshold=12.5, gpu=1,
    )
    assert cmd[0] == "/opt/muxiveo-rife"
    assert cmd[cmd.index("--factor") + 1] == "3"
    assert cmd[cmd.index("--model") + 1] == INTERPOLATION_MODELS["max"]
    assert cmd[cmd.index("--matrix") + 1] == "bt2020nc"
    assert cmd[cmd.index("--chroma-loc") + 1] == "topleft"
    assert cmd[cmd.index("--scene-threshold") + 1] == "12.5"
    assert cmd[cmd.index("--gpu") + 1] == "1"
    assert "--gpu" not in build_rife_stage("r", factor=2, quality="?", source=InterpolationSource())


# ---------------------------------------------------------------------------
# Constructeur de commandes vidéo seule
# ---------------------------------------------------------------------------

def _domain(platform: str = "linux", vaapi: str | None = None) -> EncodeCodecDomainCallbacks:
    return EncodeCodecDomainCallbacks(
        platform=platform, nvenc_device=None, vaapi_device=vaapi, qsv_device=None, amf_device=None,
    )


def _builder(source_info: InterpolationSource | None = None, rife_bin: str | None = "/bin/muxiveo-rife",
             domain: EncodeCodecDomainCallbacks | None = None) -> VideoOnlyCommandBuilder:
    return VideoOnlyCommandBuilder(
        VideoOnlyCommandBuilderCallbacks(
            ffmpeg_bin="ffmpeg",
            ffmpeg_progress_args=lambda: ["-progress", "pipe:1", "-nostats"],
            ffmpeg_thread_args=lambda _n: ["-threads", "4"],
            offset_input_args=lambda ms: [] if ms == 0 else (["-itsoffset", f"{ms / 1000:.3f}"] if ms > 0 else ["-ss", f"{-ms / 1000:.3f}"]),
            codec_domain_callbacks=lambda: domain or _domain(),
            primary_video_settings=lambda cfg: cfg.video,
            video_source_path=lambda cfg: cfg.source,
            video_stream_from_settings=lambda v: v.stream_index,
            size_to_bitrate_kbps=lambda _cfg: 4000,
            size_to_bitrate_kbps_for_video=lambda _cfg, _v: 4000,
            rife_bin=rife_bin,
            interpolation_source=lambda _v, _s: source_info or InterpolationSource(start_offset_s=0.04),
        )
    )


class TestVideoOnlyBuilder:
    def test_pipeline_stages_and_filters(self, tmp_path):
        video = _video(filters=VideoFilterSettings(yadif_enabled=True), stream_index=3)
        cfg = EncodeConfig(source=tmp_path / "src.mkv", output=tmp_path / "out.mkv", video=video)
        [cmd] = _builder().build_video_only_mkv_commands(cfg, video, cfg.source, tmp_path / "v.mkv")

        assert isinstance(cmd, PipelineCommand)
        decode, rife, encode = cmd.stages()
        assert decode[0] == "ffmpeg" and decode[-1] == "-"
        assert decode[decode.index("-map") + 1] == "0:3"
        assert "yadif" in decode[decode.index("-vf") + 1]
        assert decode[decode.index("-f") + 1] == "yuv4mpegpipe"
        assert rife[0] == "/bin/muxiveo-rife"
        # encodeur : entrée pipe, départ source réappliqué, aucun filtre logiciel
        assert encode[encode.index("-i") + 1] == "pipe:0"
        assert encode[encode.index("-itsoffset") + 1] == "0.040000"
        assert "-vf" not in encode
        assert encode[encode.index("-map") + 1] == "0:0"
        assert "libx265" in encode and encode[-1] == str(tmp_path / "v.mkv")
        assert " | " in command_display(cmd)

    def test_vfr_source_normalized_before_rife(self, tmp_path):
        video = _video()
        cfg = EncodeConfig(source=tmp_path / "s.mp4", output=tmp_path / "o.mkv", video=video)
        info = InterpolationSource(is_vfr=True, cfr_rate="30/1")
        [cmd] = _builder(info).build_video_only_mkv_commands(cfg, video, cfg.source, tmp_path / "v.mkv")
        decode = command_stages(cmd)[0]
        assert decode[decode.index("-vf") + 1] == "fps=30/1"

    def test_negative_offset_cuts_at_decode(self, tmp_path):
        video = _video()
        cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video)
        [cmd] = _builder(InterpolationSource()).build_video_only_mkv_commands(
            cfg, video, cfg.source, tmp_path / "v.mkv", offset_ms=-500,
        )
        decode, _rife, encode = command_stages(cmd)
        assert decode[decode.index("-ss") + 1] == "0.500"
        assert "-itsoffset" not in encode

    def test_positive_offset_added_to_start(self, tmp_path):
        video = _video()
        cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video)
        [cmd] = _builder(InterpolationSource(start_offset_s=0.1)).build_video_only_mkv_commands(
            cfg, video, cfg.source, tmp_path / "v.mkv", offset_ms=250,
        )
        encode = command_stages(cmd)[-1]
        assert encode[encode.index("-itsoffset") + 1] == "0.350000"

    def test_two_pass_interpolates_both_passes(self, tmp_path):
        video = _video(quality_mode=QualityMode.SIZE)
        cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video)
        cmds = _builder().build_video_only_mkv_commands(cfg, video, cfg.source, tmp_path / "v.mkv")
        assert len(cmds) == 2 and all(isinstance(c, PipelineCommand) for c in cmds)

    def test_missing_tool_raises(self, tmp_path):
        video = _video()
        cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video)
        with pytest.raises(Exception, match="muxiveo-rife"):
            _builder(rife_bin=None).build_video_only_mkv_commands(cfg, video, cfg.source, tmp_path / "v.mkv")

    def test_without_interpolation_unchanged(self, tmp_path):
        video = VideoEncodeSettings(codec="libx265")
        cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video)
        [cmd] = _builder().build_video_only_mkv_commands(cfg, video, cfg.source, tmp_path / "v.mkv")
        assert not isinstance(cmd, PipelineCommand)
        assert cmd[cmd.index("-i") + 1] == str(cfg.source)


class TestPipedEncoderArgs:
    def test_vaapi_uploads_piped_frames(self):
        video = _video(codec="hevc_vaapi")
        domain = _domain(vaapi="/dev/dri/renderD128")
        assert build_encoder_vf(video, callbacks=domain, piped_frames=True) == "format=nv12,hwupload"
        args = hardware_input_args(video, callbacks=domain, piped_frames=True)
        assert args == ["-vaapi_device", "/dev/dri/renderD128"]

    def test_nvenc_keeps_software_frames(self):
        video = _video(codec="hevc_nvenc")
        assert "-hwaccel" not in hardware_input_args(video, callbacks=_domain(), piped_frames=True)
        assert build_encoder_vf(video, callbacks=_domain(), piped_frames=True) == ""

    def test_p5_filter_device_stays_on_decode(self):
        video = _video(p5_to_hdr10=True)
        assert "-init_hw_device" in hardware_input_args(video, callbacks=_domain())
        assert "-init_hw_device" not in hardware_input_args(video, callbacks=_domain(), piped_frames=True)


# ---------------------------------------------------------------------------
# Métadonnées dynamiques
# ---------------------------------------------------------------------------

def _rpu_units(data: bytes) -> list[bytes]:
    return [u for u in data.split(b"\x00\x00\x00\x01") if u]


class TestDynamicMetadataExpansion:
    def test_rpu_units_duplicated_in_order(self, tmp_path):
        src, dst = tmp_path / "rpu.bin", tmp_path / "x2.bin"
        src.write_bytes(b"".join(b"\x00\x00\x00\x01\x7c\x01" + bytes([i]) * 3 for i in range(3)))
        assert expand_rpu_file(src, dst, 2) == 6
        units = _rpu_units(dst.read_bytes())
        assert units == [u for u in _rpu_units(src.read_bytes()) for _ in range(2)]

    def test_rpu_requires_start_code(self, tmp_path):
        src = tmp_path / "bad.bin"
        src.write_bytes(b"\x7c\x01\x00")
        with pytest.raises(ValueError):
            expand_rpu_file(src, tmp_path / "out.bin", 2)

    def test_hdr10plus_frames_renumbered(self, tmp_path):
        scenes = [
            {"SceneFrameIndex": 0, "SceneId": 0, "SequenceFrameIndex": 0, "LuminanceParameters": {"A": 1}},
            {"SceneFrameIndex": 0, "SceneId": 1, "SequenceFrameIndex": 1, "LuminanceParameters": {"A": 2}},
        ]
        src = tmp_path / "h.json"
        src.write_text(json.dumps({
            "JSONInfo": {}, "SceneInfo": scenes,
            "SceneInfoSummary": {"SceneFirstFrameIndex": [0, 1], "SceneFrameNumbers": [1, 1]},
        }))
        dst = tmp_path / "h2.json"
        assert expand_hdr10plus_json(src, dst, 2) == 4
        out = json.loads(dst.read_text())
        assert [f["SequenceFrameIndex"] for f in out["SceneInfo"]] == [0, 1, 2, 3]
        assert [f["SceneFrameIndex"] for f in out["SceneInfo"]] == [0, 1, 0, 1]
        assert [f["LuminanceParameters"]["A"] for f in out["SceneInfo"]] == [1, 1, 2, 2]
        assert out["SceneInfoSummary"] == {"SceneFirstFrameIndex": [0, 2], "SceneFrameNumbers": [2, 2]}

    def test_scene_cut_edit_clears_copies(self):
        assert dovi_scene_cut_edit([0, 10], 2) == {"scene_cuts": {"1-1": False, "21-21": False}}
        assert dovi_scene_cut_edit([5], 3) == {"scene_cuts": {"16-17": False}}
        assert dovi_scene_cut_edit([], 2) == {}
        assert dovi_scene_cut_edit([3], 1) == {}

    def test_expand_in_place_with_dovi_tool(self, tmp_path):
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"\x00\x00\x00\x01\x7c\x01\xaa" * 2)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str]) -> None:
            calls.append(cmd)
            if cmd[1] == "export":
                Path(cmd[-1].split("=", 1)[1]).write_text("0\n")
            elif cmd[1] == "editor":
                assert json.loads(Path(cmd[cmd.index("-j") + 1]).read_text()) == {"scene_cuts": {"1-1": False}}
                Path(cmd[cmd.index("-o") + 1]).write_bytes(b"\x00\x00\x00\x01\x7c\x01\xbb" * 4)

        logs: list[str] = []
        expand_dynamic_hdr_metadata(ratio=2, rpu_bin=rpu, dovi_tool_bin="dovi_tool", run_cmd=fake_run, log=logs.append)
        assert [c[1] for c in calls] == ["export", "editor"]
        assert len(_rpu_units(rpu.read_bytes())) == 4
        assert sorted(p.name for p in tmp_path.iterdir()) == ["rpu.bin"]
        assert "1 coupe" in logs[0]

    def test_factor_one_is_noop(self, tmp_path):
        rpu = tmp_path / "rpu.bin"
        rpu.write_bytes(b"\x00\x00\x00\x01\x7c\x01")
        expand_dynamic_hdr_metadata(ratio=1, rpu_bin=rpu)
        assert rpu.read_bytes() == b"\x00\x00\x00\x01\x7c\x01"


def test_frame_count_guard_scales_source(tmp_path):
    guard = FrameCountGuard(mediainfo_bin="mediainfo")
    with patch.object(FrameCountGuard, "_read_video_frame_count", return_value=100):
        audit = guard.audit(source=tmp_path / "s", encoded=tmp_path / "e", known_encoded_frames=200, frame_ratio=2)
    assert (audit.source, audit.encoded) == (200, 200)
    assert audit.is_aligned()[0]


# ---------------------------------------------------------------------------
# Exécution de pipelines
# ---------------------------------------------------------------------------

_PY = sys.executable
_PRODUCER = [_PY, "-c", "import sys; sys.stdout.write('abc\\n' * 3)"]
_UPPER = [_PY, "-c", "import sys; sys.stderr.write('rife info\\n'); sys.stdout.write(sys.stdin.read().upper())"]
_CONSUMER = [_PY, "-c", "import sys; d = sys.stdin.read(); print('frames=%d' % d.count('ABC'))"]


class TestRunnerPipeline:
    def test_stages_are_chained(self, qt_app):
        _ = qt_app
        lines: list[str] = []
        out = ToolRunner()._run_cmd(PipelineCommand(_CONSUMER, [_PRODUCER, _UPPER]), progress_cb=lines.append)
        assert "frames=3" in out
        assert any(line.startswith("$ ") and " | " in line for line in lines)
        assert any("rife info" in line for line in lines)

    def test_upstream_failure_fails_command(self, qt_app):
        _ = qt_app
        failing = [_PY, "-c", "import sys; sys.stdin.read(); sys.stderr.write('gpu lost\\n'); sys.exit(3)"]
        with pytest.raises(CommandError) as exc:
            ToolRunner()._run_cmd(PipelineCommand(_CONSUMER, [_PRODUCER, failing]))
        assert exc.value.returncode == 3
        assert "gpu lost" in exc.value.stderr

    def test_final_failure_reported_first(self, qt_app):
        _ = qt_app
        consumer = [_PY, "-c", "import sys; sys.exit(5)"]
        with pytest.raises(CommandError) as exc:
            ToolRunner()._run_cmd(PipelineCommand(consumer, [_PRODUCER]))
        assert exc.value.returncode == 5


def test_nvencc_executor_runs_intermediate_stage(qt_app):
    _ = qt_app
    signals = TaskSignals()
    out = NvenccPipeExecutor().run(
        decode_cmd=PipelineCommand(_UPPER, [_PRODUCER]),
        encode_cmd=_CONSUMER,
        cwd=Path.cwd(),
        signals=signals,
    )
    assert "frames=3" in out


def test_nvencc_pipe_forced_by_interpolation():
    assert nvencc_requires_ffmpeg_filter_pipe(_video(codec="nvencc_hevc"))
    assert not nvencc_requires_ffmpeg_filter_pipe(VideoEncodeSettings(codec="nvencc_hevc"))


# ---------------------------------------------------------------------------
# Workflow : routage et validation
# ---------------------------------------------------------------------------

def _probe_payload(**stream) -> dict:
    base = {"index": 0, "codec_type": "video", "r_frame_rate": "25/1", "avg_frame_rate": "25/1"}
    return {"streams": [{**base, **stream}], "format": {"start_time": "0.000000"}}


@pytest.fixture
def interp_workflow(qt_app, tmp_path):
    _ = qt_app
    rife = tmp_path / "muxiveo-rife"
    rife.write_text("")
    return EncodeWorkflow(ffmpeg_bin="ffmpeg", rife_bin=str(rife))


def _cfg(tmp_path: Path, video: VideoEncodeSettings, **kw) -> EncodeConfig:
    src = tmp_path / "src.mkv"
    src.write_bytes(b"")
    return EncodeConfig(source=src, output=tmp_path / "out.mkv", video=video, **kw)


class TestWorkflowInterpolation:
    def test_single_track_routes_to_split_encode(self, interp_workflow, tmp_path):
        cfg = _cfg(tmp_path, _video())
        assert interp_workflow._needs_split_video_encode(cfg)
        assert interp_workflow._mux_pipeline_kind(cfg) == "multi_video"
        assert not interp_workflow._needs_split_video_encode(_cfg(tmp_path, VideoEncodeSettings()))

    def test_dynamic_hdr_keeps_inject_pipeline(self, interp_workflow, tmp_path):
        cfg = _cfg(tmp_path, _video(copy_hdr10plus=True), copy_hdr10plus=True)
        assert interp_workflow._needs_metadata_inject(cfg)
        assert not interp_workflow._needs_split_video_encode(cfg)

    def test_validation_ok(self, interp_workflow, tmp_path):
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_probe_payload()):
            assert interp_workflow._interpolation_validation_errors(_cfg(tmp_path, _video())) == []

    def test_validation_errors(self, qt_app, tmp_path):
        _ = qt_app
        wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", rife_bin=None)
        payload = _probe_payload(field_order="tt", r_frame_rate="30/1", avg_frame_rate="29/1")
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=payload):
            errors = wf._interpolation_validation_errors(_cfg(tmp_path, _video(interpolation=_interp(factor=8))))
        text = "\n".join(errors)
        assert "x8" in text
        assert "muxiveo-rife introuvable" in text
        assert "entrelacée" in text

    def test_mediainfo_vfr_normalized(self, interp_workflow, tmp_path):
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_probe_payload(r_frame_rate="30/1", avg_frame_rate="30/1")), \
                patch.object(EncodeWorkflow, "_load_mediainfo_video_track", return_value={"FrameRate_Mode": "VFR", "FrameRate_Mode_Original": "VFR"}):
            info = interp_workflow._interpolation_source(_video(), tmp_path / "s.mp4")
        assert info.is_vfr and info.cfr_rate == "30/1"

    def test_vfr_not_normalized_with_dynamic_hdr(self, interp_workflow, tmp_path):
        payload = _probe_payload(r_frame_rate="30/1", avg_frame_rate="29/1")
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=payload):
            info = interp_workflow._interpolation_source(_video(copy_dv=True), tmp_path / "s.mkv")
        assert info.is_vfr and info.cfr_rate == ""

    def test_matroska_original_vfr_ignored(self, interp_workflow, tmp_path):
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_probe_payload()), \
                patch.object(EncodeWorkflow, "_load_mediainfo_video_track", return_value={"FrameRate_Mode": "CFR", "FrameRate_Mode_Original": "VFR"}):
            assert not interp_workflow._interpolation_source(_video(), tmp_path / "s.mkv").is_vfr

    def test_copy_codec_rejected(self, interp_workflow, tmp_path):
        errors = interp_workflow._interpolation_validation_errors(_cfg(tmp_path, _video(codec="copy")))
        assert errors and "COPY" in errors[0]

    def test_interlaced_with_deinterlace_accepted(self, interp_workflow, tmp_path):
        video = _video(filters=VideoFilterSettings(yadif_enabled=True))
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_probe_payload(field_order="tt")):
            assert interp_workflow._interpolation_validation_errors(_cfg(tmp_path, video)) == []

    def test_nvencc_decode_wrapper(self, interp_workflow, tmp_path):
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_probe_payload(color_space="bt709")):
            wrapped = interp_workflow._wrap_decode_with_interpolation(["ffmpeg", "-"], _video(), tmp_path / "s.mkv")
        assert isinstance(wrapped, PipelineCommand)
        assert wrapped.upstream == [["ffmpeg", "-fps_mode", "passthrough", "-"]]
        assert Path(wrapped[0]).name == "muxiveo-rife"
        plain = interp_workflow._wrap_decode_with_interpolation(["ffmpeg", "-"], VideoEncodeSettings(), tmp_path / "s.mkv")
        assert plain == ["ffmpeg", "-"] and not isinstance(plain, PipelineCommand)

    def test_source_probe_uses_selected_stream(self, interp_workflow, tmp_path):
        payload = {
            "streams": [
                {"index": 0, "codec_type": "video", "color_space": "bt709"},
                {"index": 2, "codec_type": "video", "color_space": "bt2020nc", "start_time": "0.5"},
            ],
            "format": {"start_time": "0.0"},
        }
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=payload):
            info = interp_workflow._interpolation_source(_video(stream_index=2), tmp_path / "s.mkv")
        assert (info.matrix, info.start_offset_s) == ("bt2020nc", 0.5)


# ---------------------------------------------------------------------------
# Cadence de sortie : niveau Dolby Vision, GOP, décodage NVEncC
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("width", "height", "fps", "level"),
    [
        (3840, 2160, 23.976, 6),
        (3840, 2160, 47.952, 8),
        (3840, 2160, 59.94, 9),
        (3840, 1600, 47.952, 8),
        (1920, 1080, 47.952, 5),
        (1920, 1080, 23.976, 3),
        (3840, 2160, 119.88, 10),
    ],
)
def test_minimum_dovi_level(width, height, fps, level):
    assert minimum_dovi_level(width, height, fps) == level


def test_multiply_fps_expr():
    assert multiply_fps_expr("24000/1001", 2) == "48000/1001"
    assert multiply_fps_expr("25", 3) == "75/1"
    assert multiply_fps_expr(None, 2) is None
    assert multiply_fps_expr("abc", 2) is None


def test_ffprobe_beside():
    assert ffprobe_beside("ffmpeg") == "ffprobe"
    assert Path(ffprobe_beside(str(Path("tools") / "ffmpeg.exe"))) == Path("tools") / "ffprobe.exe"


def test_dovi_record_level_raised_for_interpolation(tmp_path):
    summary = SimpleNamespace(stdout="Summary:\n  Frames: 75\n  Profile: 8\n", stderr="")
    with patch("core.workflows.encode.runtime.metadata_inject.subprocess.run", return_value=summary):
        base = _build_dovi_record_from_rpu(rpu_bin=tmp_path / "r.bin", dovi_tool_bin="dovi_tool")
        raised = _build_dovi_record_from_rpu(rpu_bin=tmp_path / "r.bin", dovi_tool_bin="dovi_tool", min_level=8)
    assert base is not None and raised is not None
    assert (base.level, raised.level) == (6, 8)


class TestNvenccInterpolationDecode:
    _DECODE = ["ffmpeg", "-hide_banner", "-i", "s.mkv", "-map", "0:0", "-f", "yuv4mpegpipe", "-strict", "-1", "-"]

    def test_passthrough_added(self):
        cmd = _interpolation_decode_cmd(self._DECODE, InterpolationSource())
        assert cmd[cmd.index("-fps_mode") + 1] == "passthrough"
        assert cmd.index("-fps_mode") < cmd.index("-f")

    def test_vfr_filter_appended_or_created(self):
        created = _interpolation_decode_cmd(self._DECODE, InterpolationSource(is_vfr=True, cfr_rate="30/1"))
        assert created[created.index("-vf") + 1] == "fps=30/1"
        with_vf = [*self._DECODE[:6], "-vf", "yadif", *self._DECODE[6:]]
        appended = _interpolation_decode_cmd(with_vf, InterpolationSource(is_vfr=True, cfr_rate="30/1"))
        assert appended[appended.index("-vf") + 1] == "yadif,fps=30/1"

    def test_nvencc_dynamic_hdr_accepted(self, qt_app, tmp_path):
        _ = qt_app
        rife = tmp_path / "muxiveo-rife"
        rife.write_text("")
        wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", rife_bin=str(rife), nvencc_bin="nvencc")
        video = _video(codec="nvencc_hevc", copy_dv=True, copy_hdr10plus=True)
        with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=_probe_payload()):
            assert wf._interpolation_validation_errors(_cfg(tmp_path, video)) == []


# ---------------------------------------------------------------------------
# Progression : muxiveo-rife pilote la barre, NVEncC filtré
# ---------------------------------------------------------------------------

_RIFE_LINE = "[muxiveo-rife] progress in=1507 out=3012 interpolated=1451 scenes=16 static=39 fps=25.08"


def test_parse_rife_progress_percent_and_eta():
    progress = parse_rife_progress(_RIFE_LINE)
    assert progress is not None
    assert (progress.frames_in, progress.frames_out, progress.fps_out) == (1507, 3012, 25.08)
    assert progress.percent(65520) == pytest.approx(2.3, abs=0.01)
    # débit source = 25.08 × 1507/3012 ≈ 12.55 trames/s
    assert progress.eta_seconds(65520) == pytest.approx((65520 - 1507) / (25.08 * 1507 / 3012), rel=1e-6)
    assert progress.percent(None) is None
    assert parse_rife_progress("[muxiveo-rife] info: 3840x2160") is None


def test_workflow_parse_progress_uses_rife(interp_workflow, tmp_path):
    cfg = _cfg(tmp_path, _video(codec="nvencc_hevc"), duration_s=2732.73)
    payload = _probe_payload(avg_frame_rate="24000/1001", r_frame_rate="24000/1001")
    with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=payload):
        event = interp_workflow.parse_progress(cfg, _RIFE_LINE)
    assert event is not None and not event.should_log
    assert event.fps == 25.08
    assert event.percent == pytest.approx(1507 * 100 / round(2732.73 * 24000 / 1001), rel=1e-6)
    assert event.eta_seconds is not None and event.eta_seconds > 0


_CR_PRODUCER = [_PY, "-c", "import sys; sys.stdout.write('abc\\n' * 3)"]
_CR_CONSUMER = [
    _PY, "-c",
    "import sys; sys.stdin.read(); "
    "sys.stdout.write('10 frames: 5.0 fps, 1 kbps\\r20 frames: 6.0 fps, 1 kbps\\rencoded 30 frames, 6.0 fps\\n')",
]


@pytest.mark.parametrize("with_rife", [True, False])
def test_nvencc_executor_splits_cr_and_filters_progress(qt_app, with_rife):
    _ = qt_app
    signals = TaskSignals()
    lines: list[str] = []
    signals.progress.connect(lines.append)
    decode = PipelineCommand(_UPPER, [_CR_PRODUCER]) if with_rife else _CR_PRODUCER
    NvenccPipeExecutor().run(decode_cmd=decode, encode_cmd=_CR_CONSUMER, cwd=Path.cwd(), signals=signals)
    qt_app.processEvents()
    assert "[nvencc] encoded 30 frames, 6.0 fps" in lines
    progress = [line for line in lines if "frames: " in line]
    if with_rife:
        assert progress == []  # RIFE rapporte la position source
    else:
        assert progress == ["10 frames: 5.0 fps, 1 kbps", "20 frames: 6.0 fps, 1 kbps"]


# ---------------------------------------------------------------------------
# Cadence cible non entière (ex. 23,976 -> 59,94 = x2,5)
# ---------------------------------------------------------------------------

_X25 = Fraction(5, 2)


def test_target_fps_ratio_and_repeats():
    settings = _interp(target_fps="60000/1001")
    assert settings.ratio("24000/1001") == _X25
    assert settings.ratio(None) == 1  # cadence source inconnue : pas de rapport
    assert _video(interpolation=settings).frame_ratio("24000/1001") == _X25
    assert [frame_repeats(i, _X25) for i in range(4)] == [3, 2, 3, 2]
    assert sum(frame_repeats(i, _X25) for i in range(5)) == 13  # ceil(5 x 2,5), règle muxiveo-rife
    assert (ratio_label(2), ratio_label(_X25)) == ("x2", "x2,5")


def test_rife_stage_target_fps():
    cmd = build_rife_stage("rife", target_fps="60000/1001", quality="balanced", source=InterpolationSource())
    assert cmd[cmd.index("--fps") + 1] == "60000/1001"
    assert "--factor" not in cmd


def test_rpu_and_hdr10plus_follow_uneven_cadence(tmp_path):
    rpu, rpu_out = tmp_path / "rpu.bin", tmp_path / "rpu25.bin"
    rpu.write_bytes(b"".join(b"\x00\x00\x00\x01\x7c\x01" + bytes([i]) for i in range(4)))
    assert expand_rpu_file(rpu, rpu_out, _X25) == 10
    assert [u[-1] for u in _rpu_units(rpu_out.read_bytes())] == [0, 0, 0, 1, 1, 2, 2, 2, 3, 3]

    frames = [
        {"SceneFrameIndex": 0, "SceneId": 0, "SequenceFrameIndex": 0},
        {"SceneFrameIndex": 1, "SceneId": 0, "SequenceFrameIndex": 1},
        {"SceneFrameIndex": 0, "SceneId": 1, "SequenceFrameIndex": 2},
    ]
    src, dst = tmp_path / "h.json", tmp_path / "h25.json"
    src.write_text(json.dumps({"SceneInfo": frames, "SceneInfoSummary": {"SceneFirstFrameIndex": [0, 2], "SceneFrameNumbers": [2, 1]}}))
    assert expand_hdr10plus_json(src, dst, _X25) == 8
    out = json.loads(dst.read_text())
    assert [f["SequenceFrameIndex"] for f in out["SceneInfo"]] == list(range(8))
    assert [f["SceneFrameIndex"] for f in out["SceneInfo"]] == [0, 1, 2, 3, 4, 0, 1, 2]
    assert out["SceneInfoSummary"] == {"SceneFirstFrameIndex": [0, 5], "SceneFrameNumbers": [5, 3]}


def test_scene_cut_edit_uneven_cadence():
    # coupes aux trames source 0, 1, 3 -> sorties 0-2, 3-4, 8-9
    assert dovi_scene_cut_edit([0, 1, 3], _X25) == {"scene_cuts": {"1-2": False, "4-4": False, "9-9": False}}


def test_frame_count_guard_rounds_up_uneven_ratio(tmp_path):
    guard = FrameCountGuard(mediainfo_bin="mediainfo")
    with patch.object(FrameCountGuard, "_read_video_frame_count", return_value=5):
        audit = guard.audit(source=tmp_path / "s", encoded=tmp_path / "e", known_encoded_frames=13, frame_ratio=_X25)
    assert (audit.source, audit.encoded) == (13, 13)


def test_target_fps_must_exceed_source(interp_workflow, tmp_path):
    payload = _probe_payload(avg_frame_rate="60/1", r_frame_rate="60/1")
    video = _video(interpolation=_interp(target_fps="60000/1001"))
    with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=payload):
        errors = interp_workflow._interpolation_validation_errors(_cfg(tmp_path, video))
    assert errors and "doit dépasser" in errors[0]
    ok_payload = _probe_payload(avg_frame_rate="24000/1001", r_frame_rate="24000/1001")
    with patch.object(EncodeWorkflow, "_ffprobe_streams_payload", return_value=ok_payload):
        assert interp_workflow._interpolation_validation_errors(_cfg(tmp_path, video)) == []
