"""Tests unitaires pour l'alignement géométrique Dolby Vision (multiple de 32) et le réalignement RPU."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core.workflows.encode.models import EncodeConfig, VideoCropSettings, VideoEncodeSettings
from core.workflows.encode.runtime.dovi_geometry import (
    align_dovi_rpu_geometry,
    align_nvencc_dovi_geometry,
)
from core.workflows.encode.runtime.nvencc import build_nvencc_command
from core.workflows.encode.runtime.nvencc_execution import build_nvencc_pipeline_commands
from core.workflows.encode.runtime.nvencc_routing import NvenccInputRouter, NvenccRoutingCallbacks


def test_align_nvencc_dovi_geometry_hobbit_letterbox():
    """Vérifie que les bandes noires 275px sont ajustées à 280px pour un cadre 1600p (multiple de 32)."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    res = align_nvencc_dovi_geometry(video, (3840, 2160), l5_offsets=(275, 275, 0, 0))

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
    assert res.vpp_pad is None
    assert res.needs_rpu_alignment is True


def test_align_nvencc_dovi_geometry_fullframe_2160p():
    """Vérifie qu'une image pleine (L5 offsets à 0) sur 2160p reçoit un padding de 8px haut/bas pour 2176p."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    res = align_nvencc_dovi_geometry(video, (3840, 2160), l5_offsets=(0, 0, 0, 0))

    assert res.video.crop.enabled is False
    assert res.vpp_pad == (0, 8, 0, 8)
    final_h = 2160 + res.vpp_pad[1] + res.vpp_pad[3]
    assert final_h == 2176
    assert final_h % 32 == 0
    assert res.dovi_rpu_prm is None
    assert res.needs_rpu_alignment is True
    assert res.pad_offsets == (0, 8, 0, 8)


def test_align_nvencc_dovi_geometry_fullframe_1080p():
    """Vérifie qu'une image pleine 1080p (non multiple de 32) est paddée à 1088p."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    res = align_nvencc_dovi_geometry(video, (1920, 1080), l5_offsets=(0, 0, 0, 0))

    assert res.video.crop.enabled is False
    assert res.vpp_pad == (0, 4, 0, 4)
    final_h = 1080 + res.vpp_pad[1] + res.vpp_pad[3]
    assert final_h == 1088
    assert final_h % 32 == 0
    assert res.needs_rpu_alignment is True
    assert res.pad_offsets == (0, 4, 0, 4)


def test_align_nvencc_dovi_geometry_already_multiple_of_32():
    """Vérifie qu'une vidéo déjà en multiple de 32 (ex. 1920p) n'est ni rognée ni paddée."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    res = align_nvencc_dovi_geometry(video, (3840, 1920), l5_offsets=(0, 0, 0, 0))

    assert res.video.crop.enabled is False
    assert res.vpp_pad is None
    assert res.dovi_rpu_prm is None
    assert res.needs_rpu_alignment is False


def test_align_nvencc_dovi_geometry_user_crop_snaps():
    """Vérifie qu'un crop manuel utilisateur non multiple de 32 est réaligné."""
    crop = VideoCropSettings(enabled=True, top=270, bottom=270)
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, crop=crop)
    res = align_nvencc_dovi_geometry(video, (3840, 2160), l5_offsets=None)

    assert res.video.crop.enabled is True
    # 2160 - 540 = 1620 -> aligne vers 1600 (diff=20 -> +10 top, +10 bottom -> 280, 280)
    assert res.video.crop.top == 280
    assert res.video.crop.bottom == 280
    final_h = 2160 - (res.video.crop.top + res.video.crop.bottom)
    assert final_h == 1600
    assert final_h % 32 == 0
    assert res.dovi_rpu_prm is None
    assert res.crop_offsets is not None


def test_align_nvencc_dovi_geometry_user_extra_params_crop():
    """Vérifie qu'un crop passé dans extra_params est détecté et réaligné."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, extra_params="--crop 0,270,0,270")
    res = align_nvencc_dovi_geometry(video, (3840, 2160), l5_offsets=None)

    assert res.video.crop.enabled is True
    assert res.video.crop.top == 280
    assert res.video.crop.bottom == 280
    assert res.dovi_rpu_prm is None
    assert res.crop_offsets is not None


def test_build_nvencc_command_with_vpp_pad():
    """Vérifie que --vpp-pad est correctement émis par build_nvencc_command."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    cmd = build_nvencc_command(
        "nvencc",
        video,
        "/tmp/out.hevc",
        vpp_pad=(0, 8, 0, 8),
    )
    assert "--vpp-pad" in cmd
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
        pad_offsets=(0, 8, 0, 8), work_dir=tmp_path, run_cmd=run,
    )
    assert res == out_rpu
    data = json.loads((tmp_path / "dovi_geometry_edit.json").read_text())
    assert data["mode"] == 0
    assert "crop" not in data["active_area"]
    assert data["active_area"]["edits"] == {"0-49": 0, "50-99": 1}
    assert [p["top"] for p in data["active_area"]["presets"]] == [108, 8]


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
    assert routing.vpp_pad is None


def test_nvencc_input_router_auto_pads_fullframe():
    """Vérifie que NvenccInputRouter intègre automatiquement le padding 8,8 pour un 2160p plein écran."""
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

    assert routing.video.crop.enabled is False
    assert routing.vpp_pad == (0, 8, 0, 8)
    assert routing.needs_rpu_alignment is True
    assert routing.pad_offsets == (0, 8, 0, 8)


def test_build_nvencc_pipeline_commands_preview_with_vpp_pad(tmp_path: Path):
    """Vérifie la prévisualisation de la commande NVEncC avec padding et RPU aligné."""
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
    assert "--vpp-pad" in encode_cmd
    assert encode_cmd[encode_cmd.index("--vpp-pad") + 1] == "0,8,0,8"
    assert "--dolby-vision-rpu" in encode_cmd
    assert "rpu_aligned.bin" in encode_cmd[encode_cmd.index("--dolby-vision-rpu") + 1]


@pytest.mark.parametrize("top,bottom", [(270, 272), (275, 275), (1, 3), (2, 0)])
def test_crop_alignment_keeps_even_offsets_and_matching_rpu(top, bottom):
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, copy_hdr10plus=True,
                                crop=VideoCropSettings(enabled=True, top=top, bottom=bottom))
    res = align_nvencc_dovi_geometry(video, (3840, 2160))
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
    res = align_nvencc_dovi_geometry(video, (3840, 2160))
    assert res.video.crop.unit == "px"
    assert res.video.crop.top == res.video.crop.bottom == 216
    assert res.crop_offsets == (0, 216, 0, 216)
    assert res.vpp_pad is None


@pytest.mark.parametrize("mode", ["preset", "size", "percent"])
def test_resize_removes_dv_without_forcing_crop_or_padding(mode):
    from core.workflows.encode.models import VideoResizeSettings
    resize = VideoResizeSettings(enabled=True, mode=mode, preset="1080p", width=1920, height=1080, percent=50)
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, copy_hdr10plus=True, resize=resize)
    res = align_nvencc_dovi_geometry(video, (3840, 2160), l5_offsets=(275, 275, 0, 0))
    assert not res.video.copy_dv
    assert res.video.copy_hdr10plus
    assert res.video.inject_hdr_meta
    assert res.video.resize == resize
    assert not res.video.crop.enabled
    assert res.vpp_pad is None
    assert not res.needs_rpu_alignment
    cmd = build_nvencc_command("nvencc", res.video, "out.mkv", input_path="in.mkv")
    assert "--dolby-vision-rpu" not in cmd
    assert "--crop" not in cmd
    assert "--vpp-pad" not in cmd
    assert cmd[cmd.index("--master-display") + 1] == "copy"
    assert "--dhdr10-info" in cmd


def test_noop_resize_is_removed_before_dovi_padding():
    from core.workflows.encode.models import VideoResizeSettings
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True,
                                resize=VideoResizeSettings(enabled=True, preset="2160p"))
    res = align_nvencc_dovi_geometry(video, (3840, 2160), (0, 0, 0, 0))
    assert res.video.copy_dv
    assert not res.video.resize.enabled
    assert res.vpp_pad == (0, 8, 0, 8)


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
    geometry = align_nvencc_dovi_geometry(video, (3840, 2160), (275, 275, 0, 0))
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
        video_stream_index=lambda _: 0, build_encode_plan=lambda _: SimpleNamespace(offset_lookup={}),
        resolve_input_routing=lambda _: routing, build_runtime_remux_cmd=lambda *a, **k: ([], None, []),
        run_cmd=run, finalize_ffmpeg=lambda *a, **k: str(config.output), native_assemble=lambda *a, **k: None,
    )
    failures = []
    signals = TaskSignals()
    signals.failed.connect(lambda message, *_: failures.append(message))
    NvenccDirectOutputRunner(callbacks).run(config, [], prep_signals=signals)
    assert failures == []
    assert [cmd[0] for cmd in commands] == ["ffmpeg", "dovi_tool", "dovi_tool", "dovi_tool", "nvencc"]
    encode = commands[-1]
    assert encode[encode.index("--dolby-vision-rpu") + 1].endswith("rpu_aligned.bin")
    assert encode[encode.index("--crop") + 1] == ",".join(map(str, geometry.crop_offsets))
    assert "--dolby-vision-rpu-prm" not in encode
    config_json = json.loads((tmp_path / "dovi_geometry_edit.json").read_text())
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
    geometry = align_nvencc_dovi_geometry(video, (3840, 2160), (0, 0, 0, 0))
    workflow = SimpleNamespace(_nvencc_bin="nvencc", _video_tracks=lambda _: [video],
                               _resolve_nvencc_input_routing=lambda _: SimpleNamespace(video=geometry.video))
    config = EncodeConfig(source=tmp_path / "in.mkv", output=tmp_path / "out.mkv", video=video)
    assert NvenccEncodeBackend().validate(config, plan=None, ctx=SimpleNamespace(workflow=workflow)) == []


def test_geometry_dimensions_follow_selected_stream():
    from core.workflows.encode.runtime.nvencc_routing import source_video_dimensions
    payload = {"streams": [{"index": 0, "codec_type": "video", "width": 1920, "height": 1080},
                           {"index": 2, "codec_type": "video", "width": 3840, "height": 2160}]}
    assert source_video_dimensions(Path("movie.mkv"), stream_index=2,
                                    ffprobe_streams_payload=lambda _: payload,
                                    ffprobe_stream_dicts=lambda p: p["streams"]) == (3840, 2160)


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
        crop_offsets=(0, 280, 0, 280), work_dir=tmp_path, run_cmd=run,
    )
    assert res == out_rpu
    data = json.loads((tmp_path / "dovi_geometry_edit.json").read_text())
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
        crop_offsets=(0, 100, 0, 100), work_dir=tmp_path, run_cmd=run,
    )
    assert res == out_rpu
    data = json.loads((tmp_path / "dovi_geometry_edit.json").read_text())
    assert data["active_area"]["edits"] == {"0-499": 0}
    assert data["active_area"]["presets"][0]["top"] == 0


def test_align_dovi_rpu_geometry_no_l5_presets_full_frame(tmp_path: Path):
    """Vérifie la création d'un preset plein cadre avec translation quand le RPU source n'a aucun bloc L5."""
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
        pad_offsets=(0, 8, 0, 8), work_dir=tmp_path, run_cmd=run,
    )
    assert res == out_rpu
    data = json.loads((tmp_path / "dovi_geometry_edit.json").read_text())
    assert data["active_area"]["edits"] == {"0-249": 0}
    assert data["active_area"]["presets"][0]["top"] == 8
    assert data["active_area"]["presets"][0]["bottom"] == 8


def test_align_dovi_rpu_geometry_zero_offsets(tmp_path: Path):
    """Vérifie que des offsets nuls copient directement le RPU sans appel dovi_tool."""
    raw_rpu = tmp_path / "raw.bin"
    raw_rpu.write_bytes(b"original rpu content")
    out_rpu = tmp_path / "out.bin"

    calls = []
    res = align_dovi_rpu_geometry(
        dovi_tool_bin="dovi_tool", rpu_input=raw_rpu, output_rpu=out_rpu,
        crop_offsets=(0, 0, 0, 0), pad_offsets=(0, 0, 0, 0),
        work_dir=tmp_path, run_cmd=calls.append,
    )
    assert res == out_rpu
    assert out_rpu.read_bytes() == b"original rpu content"
    assert calls == []

