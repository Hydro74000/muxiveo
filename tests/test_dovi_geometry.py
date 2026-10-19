"""Tests unitaires de la géométrie Dolby Vision NVENC (canevas UHD, multiple de 32) et du réalignement RPU."""

from pathlib import Path
from typing import Any, cast
from unittest.mock import patch, MagicMock

import pytest

from core.workflows.encode.models import EncodeConfig, VideoCropSettings, VideoEncodeSettings
from core.workflows.encode.runtime.dovi_geometry import (
    align_dovi_rpu_geometry,
    align_nvenc_dovi_geometry,
)
from core.workflows.encode.runtime.nvencc import build_nvencc_command
from core.workflows.encode.runtime.nvencc_execution import build_nvencc_pipeline_commands
from core.workflows.encode.runtime.nvencc_routing import NvenccInputRouter, NvenccRoutingCallbacks


def test_align_nvenc_dovi_geometry_hobbit_letterbox():
    """Vérifie que les bandes noires 275px sont ajustées à 280px pour un cadre 1600p (multiple de 32)."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    res = align_nvenc_dovi_geometry(video, (3840, 2160), l5_offsets=(275, 275, 0, 0))

    assert res.video.crop.enabled is True
    assert res.video.crop.top == 280
    assert res.video.crop.bottom == 280
    assert res.video.crop.left == 0
    assert res.video.crop.right == 0
    # Hauteur résultante
    final_h = 2160 - (res.video.crop.top + res.video.crop.bottom)
    assert final_h == 1600
    assert final_h % 32 == 0
    assert res.dovi_rpu_prm is None
    assert res.crop_offsets is not None
    assert res.needs_rpu_alignment is True


@pytest.mark.parametrize("codec", ["nvencc_hevc", "hevc_nvenc"])
def test_align_nvenc_dovi_geometry_fullframe_2160p(codec):
    """Plein cadre 2160p : NVENC coderait 2176 lignes (hors canevas UHD) → rognage 8/8, 2144 lignes."""
    video = VideoEncodeSettings(codec=codec, copy_dv=True)
    res = align_nvenc_dovi_geometry(video, (3840, 2160), l5_offsets=(0, 0, 0, 0))

    assert res.full_frame_crop is True
    assert res.coded_dimensions == (3840, 2176)
    crop = res.video.crop
    assert (crop.enabled, crop.unit, crop.left, crop.top, crop.right, crop.bottom) == (True, "px", 0, 8, 0, 8)
    assert res.crop_offsets == (0, 8, 0, 8)
    assert (2160 - crop.top - crop.bottom) % 32 == 0
    assert res.dovi_rpu_prm is None
    assert res.needs_rpu_alignment is True


def test_align_nvenc_dovi_geometry_fullframe_1080p_keeps_frame():
    """1080p : 1088 lignes codées restent dans le canevas UHD, fenêtre de conformité NVENC conservée."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    res = align_nvenc_dovi_geometry(video, (1920, 1080), l5_offsets=(0, 0, 0, 0))

    assert res.video.crop.enabled is False
    assert res.full_frame_crop is False
    assert res.crop_offsets is None
    assert res.needs_rpu_alignment is False


def test_align_nvenc_dovi_geometry_already_multiple_of_32():
    """Vérifie qu'une vidéo déjà en multiple de 32 (ex. 1920p) n'est ni rognée ni paddée."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    res = align_nvenc_dovi_geometry(video, (3840, 1920), l5_offsets=(0, 0, 0, 0))

    assert res.video.crop.enabled is False
    assert res.dovi_rpu_prm is None
    assert res.needs_rpu_alignment is False


def test_align_nvenc_dovi_geometry_user_crop_snaps():
    """Vérifie qu'un crop manuel utilisateur non multiple de 32 est réaligné."""
    crop = VideoCropSettings(enabled=True, top=270, bottom=270)
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, crop=crop)
    res = align_nvenc_dovi_geometry(video, (3840, 2160), l5_offsets=None)

    assert res.video.crop.enabled is True
    # 2160 - 540 = 1620 -> aligne vers 1600 (diff=20 -> +10 top, +10 bottom -> 280, 280)
    assert res.video.crop.top == 280
    assert res.video.crop.bottom == 280
    final_h = 2160 - (res.video.crop.top + res.video.crop.bottom)
    assert final_h == 1600
    assert final_h % 32 == 0
    assert res.dovi_rpu_prm is None
    assert res.crop_offsets is not None


def test_align_nvenc_dovi_geometry_user_extra_params_crop():
    """Vérifie qu'un crop passé dans extra_params est détecté et réaligné."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, extra_params="--crop 0,270,0,270")
    res = align_nvenc_dovi_geometry(video, (3840, 2160), l5_offsets=None)

    assert res.video.crop.enabled is True
    assert res.video.crop.top == 280
    assert res.video.crop.bottom == 280
    assert res.dovi_rpu_prm is None
    assert res.crop_offsets is not None


def test_user_vpp_pad_extra_param_is_kept():
    """Le workflow ne padde plus : --vpp-pad redevient un paramètre avancé utilisateur."""
    video = VideoEncodeSettings(codec="nvencc_hevc", extra_params="--vpp-pad 0,8,0,8")
    cmd = build_nvencc_command("nvencc", video, "/tmp/out.hevc")
    assert cmd[cmd.index("--vpp-pad") + 1] == "0,8,0,8"


def test_align_dovi_rpu_geometry(tmp_path: Path):
    """Vérifie la génération du JSON de configuration et l'appel à dovi_tool editor."""
    raw_rpu = tmp_path / "raw.bin"
    raw_rpu.write_bytes(b"\x00\x00\x00\x01\x19\x02")
    out_rpu = tmp_path / "out.bin"

    import json
    calls = []
    def run(cmd):
        calls.append(cmd)
        if "export" in cmd:
            target = Path(cmd[-1].split("=", 1)[1])
            target.write_text(json.dumps({
                "crop": True,
                "presets": [{"id": 0, "top": 100, "bottom": 100, "left": 0, "right": 0},
                            {"id": 1, "top": 0, "bottom": 0, "left": 0, "right": 0}],
                "edits": {"0-49": 0, "50-99": 1},
            }))
        else:
            Path(cmd[-1]).write_bytes(b"edited rpu")
    res = align_dovi_rpu_geometry(
        dovi_tool_bin="dovi_tool", rpu_input=raw_rpu, output_rpu=out_rpu,
        crop_offsets=(0, 8, 0, 8), run_cmd=run,
    )
    assert res == out_rpu
    data = json.loads((tmp_path / "out.l5.json").read_text())
    assert data["mode"] == 0
    assert "crop" not in data["active_area"]
    assert data["active_area"]["edits"] == {"0-49": 0, "50-99": 1}
    assert [p["top"] for p in data["active_area"]["presets"]] == [92, 0]


def test_nvencc_input_router_auto_crops_hobbit():
    """Vérifie que NvenccInputRouter intègre automatiquement le crop 280,280 pour The Hobbit."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    cfg = EncodeConfig(
        source=Path("/in.mkv"),
        output=Path("/out.mkv"),
        video=video,
    )
    cbs = NvenccRoutingCallbacks(
        primary_video_settings=lambda c: video,
        video_source_path=lambda c: Path("/in.mkv"),
        video_stream_index=lambda c: 0,
        video_codec_of=lambda p, i: "hevc",
        source_video_fps_expr=lambda p: "24000/1001",
        source_is_vfr=lambda p: False,
        nvencc_input_fps_hint=lambda s, i: None,
        nvencc_input_avsync_mode=lambda s, i: "cfr",
        nvencc_dovi_rpu_prm=lambda v: None,
        source_video_dimensions=lambda p: (3840, 2160),
        probe_dovi_l5_offsets=lambda p: (275, 275, 0, 0),
    )
    router = NvenccInputRouter(cbs)
    routing = router.resolve(cfg)

    assert routing.video.crop.enabled is True
    assert routing.video.crop.top == 280
    assert routing.video.crop.bottom == 280
    assert routing.dovi_rpu_prm is None
    assert routing.crop_offsets == (0, 280, 0, 280)


def test_nvencc_input_router_auto_crops_fullframe():
    """NvenccInputRouter ramène un 2160p plein cadre à 2144 lignes (rognage 8/8, RPU réaligné)."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    cfg = EncodeConfig(
        source=Path("/in.mkv"),
        output=Path("/out.mkv"),
        video=video,
    )
    cbs = NvenccRoutingCallbacks(
        primary_video_settings=lambda c: video,
        video_source_path=lambda c: Path("/in.mkv"),
        video_stream_index=lambda c: 0,
        video_codec_of=lambda p, i: "hevc",
        source_video_fps_expr=lambda p: "24000/1001",
        source_is_vfr=lambda p: False,
        nvencc_input_fps_hint=lambda s, i: None,
        nvencc_input_avsync_mode=lambda s, i: "cfr",
        nvencc_dovi_rpu_prm=lambda v: None,
        source_video_dimensions=lambda p: (3840, 2160),
        probe_dovi_l5_offsets=lambda p: (0, 0, 0, 0),
    )
    router = NvenccInputRouter(cbs)
    routing = router.resolve(cfg)

    assert (routing.video.crop.top, routing.video.crop.bottom) == (8, 8)
    assert routing.needs_rpu_alignment is True
    assert routing.crop_offsets == (0, 8, 0, 8)


def test_build_nvencc_pipeline_commands_preview_fullframe_crop(tmp_path: Path):
    """Aperçu NVEncC d'un plein cadre 2160p : rognage 8/8 et RPU réaligné, sans padding."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    cfg = EncodeConfig(
        source=Path("/in.mkv"),
        output=Path("/out.mkv"),
        video=video,
        work_dir=tmp_path,
    )
    cbs = NvenccRoutingCallbacks(
        primary_video_settings=lambda c: video,
        video_source_path=lambda c: Path("/in.mkv"),
        video_stream_index=lambda c: 0,
        video_codec_of=lambda p, i: "hevc",
        source_video_fps_expr=lambda p: "24000/1001",
        source_is_vfr=lambda p: False,
        nvencc_input_fps_hint=lambda s, i: None,
        nvencc_input_avsync_mode=lambda s, i: "cfr",
        nvencc_dovi_rpu_prm=lambda v: None,
        source_video_dimensions=lambda p: (3840, 2160),
        probe_dovi_l5_offsets=lambda p: (0, 0, 0, 0),
    )
    router = NvenccInputRouter(cbs)
    cmds = build_nvencc_pipeline_commands(
        cfg,
        nvencc_bin="nvencc",
        ffmpeg_bin="ffmpeg",
        video_tracks=lambda c: [video],
        resolve_input_routing=router.resolve,
    )
    assert cmds is not None
    encode_cmd = cmds[0]
    assert "--vpp-pad" not in encode_cmd
    assert encode_cmd[encode_cmd.index("--crop") + 1] == "0,8,0,8"
    assert "--dolby-vision-rpu" in encode_cmd
    assert "rpu_aligned.bin" in encode_cmd[encode_cmd.index("--dolby-vision-rpu") + 1]


@pytest.mark.parametrize("top,bottom", [(270, 272), (275, 275), (1, 3), (2, 0)])
def test_crop_alignment_keeps_even_offsets_and_matching_rpu(top, bottom):
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, copy_hdr10plus=True,
                                crop=VideoCropSettings(enabled=True, top=top, bottom=bottom))
    res = align_nvenc_dovi_geometry(video, (3840, 2160))
    edges = (res.video.crop.left, res.video.crop.top, res.video.crop.right, res.video.crop.bottom)
    assert all(edge % 2 == 0 for edge in edges)
    assert (2160 - edges[1] - edges[3]) % 32 == 0
    assert res.crop_offsets == edges
    assert res.needs_rpu_alignment
    assert res.dovi_rpu_prm is None
    assert res.video.copy_hdr10plus


def test_percent_crop_becomes_absolute_before_rpu_editing():
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True,
                                crop=VideoCropSettings(enabled=True, unit="percent", top=10, bottom=10))
    res = align_nvenc_dovi_geometry(video, (3840, 2160))
    assert res.video.crop.unit == "px"
    assert res.video.crop.top == res.video.crop.bottom == 216
    assert res.crop_offsets == (0, 216, 0, 216)


@pytest.mark.parametrize("mode", ["preset", "size", "percent"])
def test_resize_removes_dv_without_forcing_crop_or_padding(mode):
    from core.workflows.encode.models import VideoResizeSettings
    resize = VideoResizeSettings(enabled=True, mode=mode, preset="1080p", width=1920, height=1080, percent=50)
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, copy_hdr10plus=True, resize=resize)
    res = align_nvenc_dovi_geometry(video, (3840, 2160), l5_offsets=(275, 275, 0, 0))
    assert not res.video.copy_dv
    assert res.video.copy_hdr10plus
    assert res.video.inject_hdr_meta
    assert res.video.resize == resize
    assert not res.video.crop.enabled
    assert not res.needs_rpu_alignment
    cmd = build_nvencc_command("nvencc", res.video, "out.mkv", input_path="in.mkv")
    assert "--dolby-vision-rpu" not in cmd
    assert "--crop" not in cmd
    assert "--vpp-pad" not in cmd
    assert cmd[cmd.index("--master-display") + 1] == "copy"
    assert "--dhdr10-info" in cmd


def test_noop_resize_is_removed_before_dovi_fullframe_crop():
    from core.workflows.encode.models import VideoResizeSettings
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True,
                                resize=VideoResizeSettings(enabled=True, preset="2160p"))
    res = align_nvenc_dovi_geometry(video, (3840, 2160), (0, 0, 0, 0))
    assert res.video.copy_dv
    assert not res.video.resize.enabled
    assert res.crop_offsets == (0, 8, 0, 8)


@pytest.mark.parametrize("suffix,stream_index,annexb", [(".mp4", 0, True), (".ts", 0, True),
                                                        (".mkv", 0, False), (".mkv", 2, True)])
def test_rpu_extraction_uses_standard_annexb_workflow(tmp_path, suffix, stream_index, annexb):
    from core.workflows.encode.runtime.dovi_geometry import extract_dovi_rpu
    calls = []
    source = tmp_path / ("source" + suffix)
    cleanup = []
    extract_dovi_rpu(source=source, stream_index=stream_index, ffmpeg_bin="configured_ffmpeg",
                     dovi_tool_bin="configured_dovi", output_rpu=tmp_path / "out.bin",
                     work_dir=tmp_path, cleanup_paths=cleanup, run_cmd=calls.append)
    assert len(calls) == (2 if annexb else 1)
    if annexb:
        assert calls[0][0] == "configured_ffmpeg"
        assert calls[0][calls[0].index("-map") + 1] == f"0:{stream_index}"
        assert "hevc_mp4toannexb" in calls[0]
        assert calls[-1][calls[-1].index("-i") + 1].endswith(".hevc")
        assert cleanup
    else:
        assert str(source) in calls[0]
    assert calls[-1][0] == "configured_dovi"


def test_direct_runner_edits_rpu_before_encode_and_does_not_zero_it(tmp_path, qt_app):
    import json
    from types import SimpleNamespace
    from core.runner import TaskSignals
    from core.workflows.encode.runtime.nvencc_execution import NvenccDirectOutputRunner, NvenccDirectOutputRunnerCallbacks
    from core.workflows.encode.runtime.nvencc_routing import NvenccInputRouting

    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, dovi_profile="8.1",
                                crop=VideoCropSettings(enabled=True, top=270, bottom=272))
    config = EncodeConfig(source=tmp_path / "in.mp4", output=tmp_path / "out.mkv", video=video, work_dir=tmp_path)
    geometry = align_nvenc_dovi_geometry(video, (3840, 2160), (275, 275, 0, 0))
    routing = NvenccInputRouting(input_path=config.source, stream_index=0, video=geometry.video,
                                needs_rpu_alignment=True, crop_offsets=geometry.crop_offsets)
    commands = []
    def run(cmd, *args):
        commands.append(cmd)
        if "export" in cmd:
            Path(cmd[-1].split("=", 1)[1]).write_text(json.dumps({
                "presets": [{"id": 0, "top": 300, "bottom": 300, "left": 0, "right": 0}],
                "edits": {"0-99": 0},
            }))
        elif "editor" in cmd:
            Path(cmd[-1]).write_bytes(b"edited")
        return ""
    callbacks = NvenccDirectOutputRunnerCallbacks(
        ffmpeg_bin="ffmpeg", nvencc_bin="nvencc", bins={"dovi_tool": "dovi_tool"},
        check_cancelled=lambda *_: None, log_step=lambda *_: None, log_info=lambda *_: None,
        primary_video_settings=lambda _: video, video_source_path=lambda _: config.source,
        video_stream_index=lambda _: 0, build_encode_plan=lambda _: cast(Any, SimpleNamespace(offset_lookup={})),
        resolve_input_routing=lambda _: routing, build_runtime_remux_cmd=lambda *a, **k: ([], None, []),
        run_cmd=run, finalize_ffmpeg=lambda *a, **k: str(config.output), native_assemble=lambda *a, **k: None,
    )
    failures = []
    progress_events = []
    signals = TaskSignals()
    signals.failed.connect(lambda message, *_: failures.append(message))
    signals.progress.connect(progress_events.append)
    NvenccDirectOutputRunner(callbacks).run(config, [], prep_signals=signals)
    assert failures == []
    assert "Extraction RPU Dolby Vision…" in progress_events
    assert "Réalignement RPU Dolby Vision…" in progress_events
    assert [cmd[0] for cmd in commands] == ["ffmpeg", "dovi_tool", "dovi_tool", "dovi_tool", "nvencc"]
    encode = commands[-1]
    assert geometry.crop_offsets is not None
    assert encode[encode.index("--dolby-vision-rpu") + 1].endswith("rpu_aligned.bin")
    assert encode[encode.index("--crop") + 1] == ",".join(map(str, geometry.crop_offsets))
    assert "--dolby-vision-rpu-prm" not in encode
    config_json = json.loads((tmp_path / "rpu_aligned.l5.json").read_text())
    preset = config_json["active_area"]["presets"][0]
    assert preset["top"] == 300 - geometry.crop_offsets[1]
    assert preset["bottom"] == 300 - geometry.crop_offsets[3]

    # A resize can disable DV in the routed settings while the input config
    # still requests it. Neither mux branch may sanitize the HDR10-only result.
    from dataclasses import replace
    routing = replace(routing, video=replace(geometry.video, copy_dv=False), needs_rpu_alignment=False)
    config.output.write_bytes(b"not a DV container")
    for native_assemble in (None, lambda *a, **k: None):
        resized_callbacks = replace(callbacks, native_assemble=native_assemble)
        with patch("core.workflows.encode.runtime.nvencc_execution.sanitize_dovi_mkv") as sanitize:
            NvenccDirectOutputRunner(resized_callbacks).run(config, [], prep_signals=signals)
            sanitize.assert_not_called()
    assert failures == []


@pytest.mark.parametrize("transform", ["percent_crop", "percent_resize"])
def test_backend_validation_uses_effective_dovi_geometry(tmp_path, transform):
    from types import SimpleNamespace
    from core.workflows.encode.backends.nvencc_backend import NvenccEncodeBackend
    from core.workflows.encode.models import VideoResizeSettings
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    if transform == "percent_crop":
        video.crop = VideoCropSettings(enabled=True, unit="percent", top=10, bottom=10)
    else:
        video.resize = VideoResizeSettings(enabled=True, mode="percent", percent=50)
    geometry = align_nvenc_dovi_geometry(video, (3840, 2160), (0, 0, 0, 0))
    workflow = SimpleNamespace(_nvencc_bin="nvencc", _video_tracks=lambda _: [video],
                               _resolve_nvencc_input_routing=lambda _: SimpleNamespace(video=geometry.video))
    config = EncodeConfig(source=tmp_path / "in.mkv", output=tmp_path / "out.mkv", video=video)
    assert NvenccEncodeBackend().validate(config, plan=None, ctx=cast(Any, SimpleNamespace(workflow=workflow))) == []


def test_geometry_dimensions_follow_selected_stream():
    from core.workflows.encode.runtime.nvencc_routing import source_video_dimensions
    payload = {"streams": [{"index": 0, "codec_type": "video", "width": 1920, "height": 1080},
                           {"index": 2, "codec_type": "video", "width": 3840, "height": 2160}]}
    assert source_video_dimensions(Path("movie.mkv"), stream_index=2,
                                    ffprobe_streams_payload=lambda _: cast(dict[str, object], payload),
                                    ffprobe_stream_dicts=lambda p: cast(list[dict[str, object]], p["streams"])) == (3840, 2160)


def test_align_dovi_rpu_geometry_nested_active_area(tmp_path: Path):
    """Vérifie le parsing correct d'un JSON dovi_tool export encapsulé sous active_area."""
    import json
    raw_rpu = tmp_path / "raw.bin"
    raw_rpu.write_bytes(b"\x00\x00\x00\x01\x19\x02")
    out_rpu = tmp_path / "out.bin"

    def run(cmd):
        if "export" in cmd:
            target = Path(cmd[-1].split("=", 1)[1])
            target.write_text(json.dumps({
                "mode": 0,
                "active_area": {
                    "crop": True,
                    "presets": [{"id": 0, "top": 275, "bottom": 275, "left": 0, "right": 0}],
                    "edits": {"0-99": 0},
                }
            }))
        elif "editor" in cmd:
            Path(cmd[-1]).write_bytes(b"edited rpu")
        return ""

    res = align_dovi_rpu_geometry(
        dovi_tool_bin="dovi_tool", rpu_input=raw_rpu, output_rpu=out_rpu,
        crop_offsets=(0, 280, 0, 280), run_cmd=run,
    )
    assert res == out_rpu
    data = json.loads((tmp_path / "out.l5.json").read_text())
    assert data["active_area"]["presets"][0]["top"] == 0
    assert data["active_area"]["presets"][0]["bottom"] == 0


def test_align_dovi_rpu_geometry_missing_edits_fallback(tmp_path: Path):
    """Vérifie la génération automatique de la plage d'édits si un seul preset est présent sans édits."""
    import json
    raw_rpu = tmp_path / "raw.bin"
    raw_rpu.write_bytes(b"\x00\x00\x00\x01\x19\x02")
    out_rpu = tmp_path / "out.bin"

    def run(cmd):
        if "export" in cmd:
            target = Path(cmd[-1].split("=", 1)[1])
            target.write_text(json.dumps({
                "crop": True,
                "presets": [{"id": 0, "top": 100, "bottom": 100, "left": 0, "right": 0}],
                "edits": {},
            }))
        elif "info" in cmd:
            return "Parsing RPU file...\nSummary:\n  Frames: 500\n  Profile: 8\n"
        elif "editor" in cmd:
            Path(cmd[-1]).write_bytes(b"edited rpu")
        return ""

    res = align_dovi_rpu_geometry(
        dovi_tool_bin="dovi_tool", rpu_input=raw_rpu, output_rpu=out_rpu,
        crop_offsets=(0, 100, 0, 100), run_cmd=run,
    )
    assert res == out_rpu
    data = json.loads((tmp_path / "out.l5.json").read_text())
    assert data["active_area"]["edits"] == {"0-499": 0}
    assert data["active_area"]["presets"][0]["top"] == 0


def test_align_dovi_rpu_geometry_no_l5_presets_full_frame(tmp_path: Path):
    """Sans bloc L5 dans le RPU source : preset plein cadre synthétisé sur tout le métrage."""
    import json
    raw_rpu = tmp_path / "raw.bin"
    raw_rpu.write_bytes(b"\x00\x00\x00\x01\x19\x02")
    out_rpu = tmp_path / "out.bin"

    def run(cmd):
        if "export" in cmd:
            target = Path(cmd[-1].split("=", 1)[1])
            target.write_text(json.dumps({
                "crop": True,
                "presets": [],
                "edits": {},
            }))
        elif "info" in cmd:
            return "Parsing RPU file...\nSummary:\n  Frames: 250\n  Profile: 8\n"
        elif "editor" in cmd:
            Path(cmd[-1]).write_bytes(b"edited rpu")
        return ""

    res = align_dovi_rpu_geometry(
        dovi_tool_bin="dovi_tool", rpu_input=raw_rpu, output_rpu=out_rpu,
        crop_offsets=(0, 8, 0, 8), run_cmd=run,
    )
    assert res == out_rpu
    data = json.loads((tmp_path / "out.l5.json").read_text())
    assert data["active_area"]["edits"] == {"0-249": 0}
    assert data["active_area"]["presets"][0]["top"] == 0
    assert data["active_area"]["presets"][0]["bottom"] == 0


def test_align_dovi_rpu_geometry_zero_offsets(tmp_path: Path):
    """Vérifie que des offsets nuls copient directement le RPU sans appel dovi_tool."""
    raw_rpu = tmp_path / "raw.bin"
    raw_rpu.write_bytes(b"original rpu content")
    out_rpu = tmp_path / "out.bin"

    calls: list[list[str]] = []
    res = align_dovi_rpu_geometry(
        dovi_tool_bin="dovi_tool", rpu_input=raw_rpu, output_rpu=out_rpu,
        crop_offsets=(0, 0, 0, 0), run_cmd=calls.append,
    )
    assert res == out_rpu
    assert out_rpu.read_bytes() == b"original rpu content"
    assert calls == []



def test_align_crop_for_codec_aligns_hevc_nvenc_dovi_on_32():
    """FFmpeg hevc_nvenc + DV : bandes recadrées au multiple de 32, comme NVEncC."""
    from core.workflows.encode.runtime.crop_detector import align_crop_for_codec
    assert align_crop_for_codec((275, 275, 0, 0), (3840, 2160), "hevc_nvenc", copy_dv=True) == (280, 280, 0, 0)
    assert align_crop_for_codec((275, 275, 0, 0), (3840, 2160), "hevc_nvenc", copy_dv=False) == (276, 276, 0, 0)


def test_resolve_ffmpeg_dovi_geometry_hevc_nvenc_fullframe():
    from core.workflows.encode.runtime.dovi_geometry import resolve_ffmpeg_dovi_geometry
    video = VideoEncodeSettings(codec="hevc_nvenc", copy_dv=True)
    res = resolve_ffmpeg_dovi_geometry(video, (3840, 2160), l5_offsets=(0, 0, 0, 0))
    assert (res.crop.top, res.crop.bottom) == (8, 8)
    assert res.dovi_rpu_crop == (0, 8, 0, 8)
    # Idempotent : l'aperçu, la validation et l'exécution résolvent la même géométrie.
    again = resolve_ffmpeg_dovi_geometry(res, (3840, 2160), l5_offsets=(0, 0, 0, 0))
    assert (again.crop, again.dovi_rpu_crop) == (res.crop, res.dovi_rpu_crop)


def test_resolve_ffmpeg_dovi_geometry_hevc_nvenc_letterbox_follows_nvencc():
    from core.workflows.encode.runtime.dovi_geometry import resolve_ffmpeg_dovi_geometry
    video = VideoEncodeSettings(codec="hevc_nvenc", copy_dv=True)
    res = resolve_ffmpeg_dovi_geometry(video, (3840, 2160), l5_offsets=(275, 275, 0, 0))
    assert (res.crop.top, res.crop.bottom) == (280, 280)
    assert res.dovi_rpu_crop == (0, 280, 0, 280)


@pytest.mark.parametrize("codec", ["libx265", "hevc_qsv", "hevc_vaapi", "hevc_amf"])
def test_resolve_ffmpeg_dovi_geometry_keeps_other_encoders_frame(codec):
    """Hors NVENC : 2160 codées exactement, pas de recadrage imposé ni de bandes rognées d'office."""
    from core.workflows.encode.runtime.dovi_geometry import resolve_ffmpeg_dovi_geometry
    video = VideoEncodeSettings(codec=codec, copy_dv=True)
    res = resolve_ffmpeg_dovi_geometry(video, (3840, 2160), l5_offsets=(275, 275, 0, 0))
    assert not res.crop.enabled
    assert res.dovi_rpu_crop is None


@pytest.mark.parametrize(("crop", "expected"), [
    (VideoCropSettings(enabled=True, top=270, bottom=272), (0, 270, 0, 272)),
    (VideoCropSettings(enabled=True, unit="percent", top=10, bottom=10), (0, 216, 0, 216)),
    (VideoCropSettings(enabled=True, auto=True), None),
])
def test_resolve_ffmpeg_dovi_geometry_rpu_follows_user_crop(crop, expected):
    from core.workflows.encode.runtime.dovi_geometry import resolve_ffmpeg_dovi_geometry
    video = VideoEncodeSettings(codec="libx265", copy_dv=True, crop=crop)
    res = resolve_ffmpeg_dovi_geometry(video, (3840, 2160))
    assert res.crop == crop
    assert res.dovi_rpu_crop == expected


def test_resolve_ffmpeg_dovi_geometry_ignores_copy_and_non_dv():
    from core.workflows.encode.runtime.dovi_geometry import resolve_ffmpeg_dovi_geometry
    for video in (VideoEncodeSettings(codec="hevc_nvenc", copy_dv=False),
                  VideoEncodeSettings(codec="copy", copy_dv=True)):
        assert resolve_ffmpeg_dovi_geometry(video, (3840, 2160), (0, 0, 0, 0)) is video


def test_crop_dovi_rpu_realigns_l5_only_when_cropped(tmp_path: Path):
    import json
    from dataclasses import replace
    from core.workflows.encode.runtime.dovi_geometry import crop_dovi_rpu
    rpu = tmp_path / "rpu.bin"
    rpu.write_bytes(b"rpu")
    calls: list[list[str]] = []

    def run(cmd):
        calls.append(cmd)
        if "export" in cmd:
            Path(cmd[-1].split("=", 1)[1]).write_text(json.dumps({
                "presets": [{"id": 0, "top": 280, "bottom": 280, "left": 0, "right": 0}], "edits": {"0-9": 0},
            }))
        elif "editor" in cmd:
            Path(cmd[-1]).write_bytes(b"edited")
        return ""

    video = VideoEncodeSettings(codec="libx265", copy_dv=True)
    assert crop_dovi_rpu(video=video, rpu_bin=rpu, dovi_tool_bin="dovi_tool", run_cmd=run) == rpu
    assert calls == []
    logs: list[str] = []
    out = crop_dovi_rpu(video=replace(video, dovi_rpu_crop=(0, 280, 0, 280)), rpu_bin=rpu,
                        dovi_tool_bin="dovi_tool", run_cmd=run, log=logs.append)
    assert out == tmp_path / "rpu.crop.bin"
    assert out.read_bytes() == b"edited"
    assert json.loads((tmp_path / "rpu.crop.l5.json").read_text())["active_area"]["presets"][0]["top"] == 0
    assert logs and "(0, 280, 0, 280)" in logs[0]


_SUMMARY_P7 = "Parsing RPU file...\n\nSummary:\n  Frames: 60\n  Profile: 7 (FEL)\n  DM version: 1 (CM v2.9)\n"
_SUMMARY_P8 = "Parsing RPU file...\n\nSummary:\n  Frames: 60\n  Profile: 8\n  DM version: 1 (CM v2.9)\n"


@pytest.mark.parametrize(("summary", "converted"), [(_SUMMARY_P7, True), (_SUMMARY_P8, False), ("", False)])
def test_convert_p7_rpu_to_p81_uses_editor_mode_2(tmp_path: Path, summary: str, converted: bool):
    """RPU P7 : éditeur ``{"mode": 2}`` (``-m`` ignoré par inject-rpu) ; P8 ou illisible : inchangé."""
    from core.workflows.encode.runtime.dovi_geometry import convert_p7_rpu_to_p81

    rpu = tmp_path / "rpu.bin"
    rpu.write_bytes(b"rpu")
    out = tmp_path / "rpu.p81.bin"
    calls: list[list[str]] = []
    edits: list[str] = []

    def run(cmd: list[str]) -> str:
        calls.append(cmd)
        edits.append(Path(cmd[cmd.index("-j") + 1]).read_text(encoding="utf-8"))
        return ""

    probe = MagicMock(stdout=summary, stderr="")
    with patch("core.workflows.encode.runtime.dovi_geometry.subprocess.run", return_value=probe) as info:
        assert convert_p7_rpu_to_p81(dovi_tool_bin="dovi_tool", rpu_bin=rpu, output_rpu=out, run_cmd=run) is converted
    assert info.call_args[0][0] == ["dovi_tool", "info", "-i", str(rpu), "--summary"]
    if converted:
        assert calls == [["dovi_tool", "editor", "-i", str(rpu), "-j", str(out.with_suffix(".mode2.json")), "-o", str(out)]]
        assert edits == ['{"mode": 2}']
        assert not out.with_suffix(".mode2.json").exists()
    else:
        assert calls == []
