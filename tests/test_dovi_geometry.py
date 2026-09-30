"""Tests unitaires pour l'alignement géométrique Dolby Vision (multiple de 32) et le réalignement RPU."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core.workflows.encode.models import EncodeConfig, VideoCropSettings, VideoEncodeSettings
from core.workflows.encode.runtime.dovi_geometry import (
    align_dovi_rpu_padding,
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
    assert res.dovi_rpu_prm == "crop=true"
    assert res.vpp_pad is None
    assert res.needs_rpu_pad_alignment is False


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
    assert res.needs_rpu_pad_alignment is True
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
    assert res.needs_rpu_pad_alignment is True
    assert res.pad_offsets == (0, 4, 0, 4)


def test_align_nvencc_dovi_geometry_already_multiple_of_32():
    """Vérifie qu'une vidéo déjà en multiple de 32 (ex. 1920p) n'est ni rognée ni paddée."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True)
    res = align_nvencc_dovi_geometry(video, (3840, 1920), l5_offsets=(0, 0, 0, 0))

    assert res.video.crop.enabled is False
    assert res.vpp_pad is None
    assert res.dovi_rpu_prm is None
    assert res.needs_rpu_pad_alignment is False


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
    assert res.dovi_rpu_prm == "crop=true"


def test_align_nvencc_dovi_geometry_user_extra_params_crop():
    """Vérifie qu'un crop passé dans extra_params est détecté et réaligné."""
    video = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True, extra_params="--crop 0,270,0,270")
    res = align_nvencc_dovi_geometry(video, (3840, 2160), l5_offsets=None)

    assert res.video.crop.enabled is True
    assert res.video.crop.top == 280
    assert res.video.crop.bottom == 280
    assert res.dovi_rpu_prm == "crop=true"


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


def test_align_dovi_rpu_padding(tmp_path: Path):
    """Vérifie la génération du JSON de configuration et l'appel à dovi_tool editor."""
    raw_rpu = tmp_path / "raw.bin"
    raw_rpu.write_bytes(b"\x00\x00\x00\x01\x19\x02")
    out_rpu = tmp_path / "out.bin"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        res = align_dovi_rpu_padding(
            dovi_tool_bin="dovi_tool",
            rpu_input=raw_rpu,
            output_rpu=out_rpu,
            pad_offsets=(0, 8, 0, 8),
            work_dir=tmp_path,
        )
        assert res == out_rpu
        assert mock_run.called
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "dovi_tool"
        assert call_args[1] == "editor"
        assert "-j" in call_args
        json_path = Path(call_args[call_args.index("-j") + 1])
        assert json_path.exists()
        import json
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        presets = data["active_area"]["presets"]
        assert presets[0]["top"] == 8
        assert presets[0]["bottom"] == 8


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
    assert routing.dovi_rpu_prm == "crop=true"
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
    assert routing.needs_rpu_pad_alignment is True
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
