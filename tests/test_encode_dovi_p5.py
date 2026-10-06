"""Lot 5 de l'audit onglet Video : Dolby Vision (matrice V30, sources P5, gardes)."""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import pytest

from core.dovi_profile_detector import DoviSubProfile
from core.workflows.encode import EncodeConfig, EncodeWorkflow, VideoEncodeSettings
from core.workflows.encode.domain import EncodeCodecDomainCallbacks, build_encoder_vf, hardware_input_args
from core.workflows.encode.domain.codecs import P5_TO_HDR10_FILTER, build_vf
from core.workflows.encode.dovi_policy import (
    BASE_LAYER_NORMALIZE_ERROR,
    P5_COPY_NORMALIZE_ERROR,
    dovi_transfer_error,
    resolve_dovi_plan,
    sub_profile_from_track,
)
from core.workflows.encode.hdr_policy import hdr_warnings
from core.workflows.encode.interpolation import interpolation_source_from_probe
from core.workflows.encode.runtime.dovi_static_hdr import (
    estimate_static_hdr_from_rpu,
    l9_primaries,
    RpuFrame,
    RpuSummary,
    parse_rpu_frame,
    parse_rpu_summary,
    pq_code_to_nits,
    static_hdr_from_rpu_data,
)
from core.workflows.encode.runtime.frame_count_guard import FrameCountAuditError, FrameCountGuard
from core.workflows.encode.runtime.nvencc import (
    NVENCC_P5_RANGE_TWEAK,
    NVENCC_P5_TONEMAP,
    build_decode_pipe_cmd,
    build_nvencc_command,
    nvencc_pipe_encode_video,
)
from core.workflows.encode.runtime.nvencc_execution import NvenccDoviRpuMonitor, NvenccPipeExecutor
from core.workflows.encode.runtime.nvencc_p5 import parse_check_features
from core.workflows.encode.runtime.nvencc_routing import NvenccInputRouter, NvenccRoutingCallbacks
from core.workflows.encode.models import EncodeError, FrameInterpolationSettings, VideoFilterSettings

P5 = DoviSubProfile.P5
P7 = DoviSubProfile.P7_FEL


def _plan(codec: str, sub: DoviSubProfile, *, copy_dv: bool = True, profile: str = "0"):
    return resolve_dovi_plan(codec=codec, copy_dv=copy_dv, dovi_profile=profile, sub_profile=sub)


# ---------------------------------------------------------------------------
# Matrice V30
# ---------------------------------------------------------------------------


def test_v30_copy_keep_is_passthrough_for_every_profile():
    for sub in (P5, P7, DoviSubProfile.P8_1, DoviSubProfile.P8_4, DoviSubProfile.P8_2, DoviSubProfile.UNKNOWN):
        plan = _plan("copy", sub)
        assert plan.passthrough and not plan.color_convert and not plan.error and not plan.normalize_copy


def test_v30_copy_normalize_matrix():
    assert _plan("copy", P5, profile="2").error == P5_COPY_NORMALIZE_ERROR
    p7 = _plan("copy", P7, profile="2")
    assert p7.normalize_copy and p7.output_profile == "8.1" and not p7.error and "sans réencodage" in p7.notes[0]
    assert _plan("copy", DoviSubProfile.P8_1, profile="2").normalize_copy
    for sub in (DoviSubProfile.P8_4, DoviSubProfile.P8_2):
        assert _plan("copy", sub, profile="2").error == BASE_LAYER_NORMALIZE_ERROR


def test_v30_reencode_matrix():
    p5 = _plan("libx265", P5)
    assert p5.color_convert and p5.output_profile == "8.1" and not p5.error
    assert _plan("nvencc_hevc", P7).output_profile == "8.1"
    assert _plan("libx265", DoviSubProfile.P8_4).output_profile == "8.4"
    assert _plan("libx265", DoviSubProfile.P8_2).output_profile == "8.2"
    assert _plan("libx265", DoviSubProfile.P8_4, profile="2").error == BASE_LAYER_NORMALIZE_ERROR
    # P5 sans copie DV : conversion des couleurs quand même, aucun profil DV.
    no_dv = _plan("hevc_nvenc", P5, copy_dv=False)
    assert no_dv.color_convert and no_dv.output_profile == "" and not no_dv.error


def test_v30_profile_transfer_coherence_and_track_mapping():
    assert dovi_transfer_error("8.1", "hlg")
    assert dovi_transfer_error("8.4", "pq")
    assert dovi_transfer_error("8.1", "pq") == "" and dovi_transfer_error("8.4", "hlg") == ""
    assert sub_profile_from_track(5, None) is P5
    assert sub_profile_from_track(8, 4) is DoviSubProfile.P8_4
    assert sub_profile_from_track(8, 1) is DoviSubProfile.P8_1
    assert sub_profile_from_track(None, None) is DoviSubProfile.UNKNOWN


# ---------------------------------------------------------------------------
# HDR10 statique estimé depuis le RPU (RV5-03, RV5-04)
# ---------------------------------------------------------------------------

_SUMMARY_SINGLE = """Summary:
  Frames: 1817
  Profile: 5
  RPU mastering display: 0.0001/1000 nits
  RPU content light level (L1): MaxCLL: 757.66 nits, MaxFALL: 142.90 nits
  L6 metadata: Mastering display: 0.0001/1000 nits. MaxCLL: 0 nits, MaxFALL: 0 nits
"""
_SUMMARY_MULTI = """Summary:
  RPU content light level (L1): MaxCLL: 838.20 nits, MaxFALL: 10.05 nits
  L6 metadata
    Mastering display: 0.0001/1000 nits. MaxCLL: 900 nits, MaxFALL: 200 nits
    Mastering display: 0.0050/4000 nits. MaxCLL: 3500 nits, MaxFALL: 400 nits
  L9 MDP: DCI-P3 D65 (10), BT.2020 (4)
"""
_P3 = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)"


def test_pq_codes_to_nits():
    assert pq_code_to_nits(3079) == pytest.approx(1000, rel=0.01)
    assert pq_code_to_nits(0) == 0.0


def test_static_hdr_l6_zero_falls_back_to_l1_for_light_levels():
    estimate = static_hdr_from_rpu_data(parse_rpu_summary(_SUMMARY_SINGLE), RpuFrame())
    assert estimate.master_display == f"{_P3}L(10000000,1)"
    assert estimate.max_cll == "758,143"
    assert (estimate.luminance_source, estimate.max_cll_source, estimate.primaries_source) == (
        "rpu_l6", "rpu_l1", "default_p3_d65",
    )
    assert any("P3-D65" in warning for warning in estimate.warnings)


def test_static_hdr_without_l6_uses_structured_source_pq_not_default():
    summary = RpuSummary(l1=(3000.0, 300.0))
    estimate = static_hdr_from_rpu_data(summary, RpuFrame(source_min_pq=7, source_max_pq=3079))
    assert estimate.luminance_source == "rpu_source_pq"
    assert estimate.master_display.endswith("L(10000000,1)")
    default = static_hdr_from_rpu_data(RpuSummary(), RpuFrame())
    assert default.luminance_source == "default" and default.max_cll == ""


_BT2020 = ((8500, 39850), (6550, 2300), (35400, 14600), (15635, 16450))
_P3_TUPLE = ((13250, 34500), (7500, 3000), (34000, 16000), (15635, 16450))


def test_static_hdr_multiple_sets_aggregate_per_field():
    summary = parse_rpu_summary(_SUMMARY_MULTI)
    assert summary.l9_sets == 2
    # Jeux L9 comptés sur toutes les trames : le plus fréquent l'emporte (L5-A09).
    estimate = static_hdr_from_rpu_data(summary, RpuFrame(primaries=_P3_TUPLE), {_P3_TUPLE: 1, _BT2020: 99})
    # pics : maximum ; minimum des minima.
    assert estimate.master_display == "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,1)"
    assert estimate.max_cll == "3500,400" and (estimate.max_cll_source, estimate.max_fall_source) == ("rpu_l6", "rpu_l6")
    assert any("2 jeux de primaires L9" in warning for warning in estimate.warnings)
    # Sans comptage : primaires de la première trame, avec avertissement.
    first = static_hdr_from_rpu_data(summary, RpuFrame(primaries=_P3_TUPLE))
    assert first.master_display.startswith(_P3) and any("première trame" in w for w in first.warnings)


def test_l5_a07_custom_l9_uses_dovi_32767_scale_and_presets():
    def custom(r, g, b, w):
        names = (("red", r), ("green", g), ("blue", b), ("white", w))
        return ", ".join(
            f'"source_primary_{name}_x": {round(xy[0] * 32767)}, "source_primary_{name}_y": {round(xy[1] * 32767)}'
            for name, xy in names
        )

    text = (
        'Parsing RPU file...\n{"vdr_dm_data": {"source_min_pq": 62, "source_max_pq": 3696, '
        '"cmv40_metadata": {"ext_metadata_blocks": [{"Level9": {"length": 17, "source_primary_index": 255, '
        + custom((0.708, 0.292), (0.170, 0.797), (0.131, 0.046), (0.3127, 0.329)) + '}}]}}}'
    )
    frame = parse_rpu_frame(text)
    assert (frame.source_min_pq, frame.source_max_pq) == (62, 3696)
    (gx, gy), (bx, by), (rx, ry), (wx, wy) = frame.primaries or ((0, 0),) * 4
    for got, want in zip((gx, gy, bx, by, rx, ry, wx, wy), (8500, 39850, 6550, 2300, 35400, 14600, 15635, 16450)):
        assert abs(got - want) <= 2
    assert l9_primaries({"length": 1, "source_primary_index": 1}) == (
        (15000, 30000), (7500, 3000), (32000, 16500), (15635, 16450),
    )
    assert l9_primaries({"length": 1, "source_primary_index": 0}) == _P3_TUPLE


def test_l5_a08_max_cll_and_max_fall_resolved_separately():
    def cll(l6):
        return static_hdr_from_rpu_data(RpuSummary(l6=(l6,), l1=(758.0, 143.0)), RpuFrame())

    assert cll((0.0001, 1000.0, 1000.0, 0.0)).max_cll == "1000,143"
    estimate = cll((0.0001, 1000.0, 0.0, 400.0))
    assert estimate.max_cll == "758,400"
    assert (estimate.max_cll_source, estimate.max_fall_source) == ("rpu_l1", "rpu_l6")


def test_l5_a09_l9_sets_counted_in_stream_across_chunks(tmp_path, monkeypatch):
    import core.workflows.encode.runtime.dovi_static_hdr as module

    preset = '{"Level9":{"length":1,"source_primary_index":2}}'
    custom = (
        '{"Level9":{"length":17,"source_primary_index":255,"source_primary_red_x":22282,'
        '"source_primary_red_y":10485,"source_primary_green_x":8683,"source_primary_green_y":22609,'
        '"source_primary_blue_x":4915,"source_primary_blue_y":1966,"source_primary_white_x":10246,'
        '"source_primary_white_y":10780}}'
    )
    export = tmp_path / "rpu.json"
    export.write_text("[" + ",".join([preset] * 3 + [custom] * 40) + "]", encoding="utf-8")
    monkeypatch.setattr(module, "_EXPORT_CHUNK", 97)   # blocs coupés entre deux morceaux
    counts = module.count_l9_sets(export)
    assert sorted(counts.values()) == [3, 40]
    assert abs(max(counts.items(), key=lambda item: item[1])[0][2][0] - 34000) <= 2   # rouge P3 (0,68)


def _custom_l9(red_x: int, red_y: int, green_x: int, green_y: int) -> dict:
    return {
        "length": 17, "source_primary_index": 255,
        "source_primary_red_x": red_x, "source_primary_red_y": red_y,
        "source_primary_green_x": green_x, "source_primary_green_y": green_y,
        "source_primary_blue_x": 4915, "source_primary_blue_y": 1966,
        "source_primary_white_x": 10246, "source_primary_white_y": 10780,
    }


def test_l5_a09_custom_sets_under_one_label_trigger_full_count():
    """Deux jeux personnalisés (index 255) sous un seul libellé : BT.709 sur 1 trame, P3 sur 3."""
    import json as _json

    bt709 = _custom_l9(20971, 10813, 9830, 19660)
    p3 = _custom_l9(22282, 10485, 8683, 22609)
    frame0 = {"vdr_dm_data": {"source_max_pq": 3079, "cmv40_metadata": {"ext_metadata_blocks": [{"Level9": bt709}]}}}
    summary = "Summary:\n  Frames: 4\n  L9 MDP: DCI-P3 D65\n"
    calls: list[list[str]] = []

    def run_capture(cmd: list[str]) -> str:
        calls.append(cmd)
        return summary if "--summary" in cmd else "Parsing RPU file...\n" + _json.dumps(frame0)

    def count_l9(dovi, rpu, capture, check_cancelled):
        from core.workflows.encode.runtime.dovi_static_hdr import l9_primaries

        return {l9_primaries(bt709): 1, l9_primaries(p3): 3}

    estimate = estimate_static_hdr_from_rpu("dovi_tool", Path("rpu.bin"), run_capture, count_l9=count_l9)
    assert estimate.master_display.startswith("G(13250,34500)")   # vert P3 (0,265 ; 0,69), pas BT.709
    assert any("le plus fréquent" in warning for warning in estimate.warnings)

    # Présélection unique sur la première trame : aucun export complet (coût évité).
    preset = {"vdr_dm_data": {"cmv40_metadata": {"ext_metadata_blocks": [{"Level9": {"source_primary_index": 0}}]}}}
    exported: list[bool] = []
    estimate_static_hdr_from_rpu(
        "dovi_tool", Path("rpu.bin"),
        lambda cmd: summary if "--summary" in cmd else _json.dumps(preset),
        count_l9=lambda *args: exported.append(True) or {},
    )
    assert not exported


def test_l5_a12_l9_export_and_reading_are_cancellable(tmp_path):
    import threading
    import time

    from core.runner import TaskCancelledError, TaskSignals, ToolRunner
    from core.workflows.encode.runtime.dovi_static_hdr import count_l9_sets, export_l9_counts

    if sys.platform == "win32":
        pytest.skip("outil factice en script shell")
    slow = tmp_path / "dovi_tool"
    # Outil mono-processus comme dovi_tool : tué, il ferme son terminal.
    slow.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    slow.chmod(0o755)
    signals = TaskSignals()
    runner = ToolRunner()
    threading.Timer(0.3, signals.cancel).start()
    start = time.monotonic()
    with pytest.raises(TaskCancelledError):
        export_l9_counts(
            str(slow), tmp_path / "rpu.bin",
            lambda cmd: runner._run_cmd(cmd, cwd=tmp_path, label="t", signals=signals),
        )
    assert time.monotonic() - start < 5
    assert not [path for path in tmp_path.iterdir() if path.name.startswith("rpu_l9_")]

    # Lecture du JSON exporté : annulation vérifiée à chaque morceau.
    export = tmp_path / "rpu.json"
    export.write_text("[]", encoding="utf-8")

    def cancelled() -> None:
        raise TaskCancelledError()

    with pytest.raises(TaskCancelledError):
        count_l9_sets(export, cancelled)


# ---------------------------------------------------------------------------
# FFmpeg : libplacebo logiciel, un seul périphérique (L5-01)
# ---------------------------------------------------------------------------


def test_l5_01_p5_filter_is_software_and_keeps_encoder_device_alone():
    video = VideoEncodeSettings(codec="hevc_amf", p5_to_hdr10=True, force_10bit=True)
    callbacks = EncodeCodecDomainCallbacks(platform="win32", amf_device="0")
    args = hardware_input_args(video, callbacks=callbacks)
    assert args.count("-filter_hw_device") == 1 and "vulkan" not in " ".join(args)
    vf = build_encoder_vf(video, callbacks=callbacks)
    assert vf.startswith(P5_TO_HDR10_FILTER) and vf.endswith("hwupload")
    assert "hwdownload" not in vf and "format=yuv420p10le" in P5_TO_HDR10_FILTER
    vaapi = VideoEncodeSettings(codec="hevc_vaapi", p5_to_hdr10=True, force_10bit=True)
    vaapi_cb = EncodeCodecDomainCallbacks(platform="linux", vaapi_device="/dev/dri/renderD128")
    assert hardware_input_args(vaapi, callbacks=vaapi_cb) == ["-vaapi_device", "/dev/dri/renderD128"]


def test_p5_then_tonemap_chain_order():
    vf = build_vf(VideoEncodeSettings(codec="libx264", p5_to_hdr10=True, tonemap_to_sdr=True))
    assert vf.index("apply_dolbyvision") < vf.index("tonemap=")


# ---------------------------------------------------------------------------
# NVEncC : natif, pipe, état des images (L5-02, L5-04, L5-05)
# ---------------------------------------------------------------------------


def _after(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


def test_l5_04_native_p5_args_and_locked_user_overrides():
    video = VideoEncodeSettings(
        codec="nvencc_hevc", p5_to_hdr10=True, inject_hdr_meta=True, extra_params="--vpp-tweak gamma=2 --colorrange full",
    )
    cmd = build_nvencc_command("nvencc", video, "out.mkv", input_path="in.mkv", input_reader="avhw")
    assert _after(cmd, "--vpp-libplacebo-tonemapping") == NVENCC_P5_TONEMAP
    assert "src_max=10000,dst_max=10000" in NVENCC_P5_TONEMAP
    assert cmd.count("--vpp-tweak") == 1 and _after(cmd, "--vpp-tweak") == NVENCC_P5_RANGE_TWEAK
    assert cmd.count("--colorrange") == 1 and _after(cmd, "--colorrange") == "limited"
    assert (_after(cmd, "--colormatrix"), _after(cmd, "--transfer")) == ("bt2020nc", "smpte2084")
    assert _after(cmd, "--output-depth") == "10" and "--avhw" in cmd


def test_l5_02_pipe_consumes_conversion_and_describes_frames():
    hdr = nvencc_pipe_encode_video(VideoEncodeSettings(codec="nvencc_hevc", p5_to_hdr10=True, inject_hdr_meta=True))
    assert not hdr.p5_to_hdr10 and hdr.source_color_transfer == "smpte2084"
    cmd = build_nvencc_command("nvencc", hdr, "out.mkv")
    assert "--vpp-libplacebo-tonemapping" not in cmd and _after(cmd, "--transfer") == "smpte2084"
    sdr = nvencc_pipe_encode_video(VideoEncodeSettings(codec="nvencc_hevc", p5_to_hdr10=True, tonemap_to_sdr=True))
    sdr_cmd = build_nvencc_command("nvencc", sdr, "out.mkv")
    assert "--transfer" not in sdr_cmd
    assert sdr_cmd[sdr_cmd.index("--output-depth") + 1] == "8"


def test_l5_05_no_dovi_option_without_effective_copy():
    video = VideoEncodeSettings(codec="nvencc_hevc", p5_to_hdr10=True, copy_dv=False)
    cmd = build_nvencc_command("nvencc", video, "out.mkv", input_path="in.mkv")
    assert not any(token.startswith("--dolby-vision") for token in cmd)


def test_decode_pipe_frame_exact_only_on_request():
    assert "-fps_mode" not in build_decode_pipe_cmd("ffmpeg", "in.mkv")
    cmd = build_decode_pipe_cmd("ffmpeg", "in.mkv", frame_exact=True)
    assert _after(cmd, "-fps_mode") == "passthrough" and cmd.index("-fps_mode") < cmd.index("-f")


def _primary(config: EncodeConfig) -> VideoEncodeSettings:
    assert config.video is not None
    return config.video


def _router(ready: bool) -> NvenccInputRouter:
    return NvenccInputRouter(NvenccRoutingCallbacks(
        primary_video_settings=_primary,
        video_source_path=lambda config: Path(_primary(config).source_path or config.source),
        video_stream_index=lambda config: 0,
        video_codec_of=lambda path, index: "hevc",
        source_video_fps_expr=lambda path: "24000/1001",
        source_is_vfr=lambda path: False,
        nvencc_input_fps_hint=lambda source, path: None,
        nvencc_input_avsync_mode=lambda source, path: None,
        nvencc_dovi_rpu_prm=lambda video: None,
        p5_native_ready=lambda: ready,
    ))


def _route(ready: bool, source: str = "in.mkv", **kw):
    video = VideoEncodeSettings(codec="nvencc_hevc", source_path=Path(source), **kw)
    return _router(ready).resolve(EncodeConfig(source=Path(source), output=Path("out.mkv"), video=video))


def test_native_or_pipe_routing_for_p5():
    native = _route(True, p5_to_hdr10=True)
    assert native.p5_native and native.input_reader == "avhw"
    assert not _route(False, p5_to_hdr10=True).p5_native
    assert not _route(True, source="in.hevc", p5_to_hdr10=True).p5_native   # RPU d'un HEVC brut ignoré
    assert not _route(True, p5_to_hdr10=True, tonemap_to_sdr=True).p5_native
    assert not _route(True, p5_to_hdr10=True, filters=VideoFilterSettings(deblock_enabled=True)).p5_native
    rife = FrameInterpolationSettings(enabled=True, factor=2)
    assert not _route(True, p5_to_hdr10=True, interpolation=rife).p5_native
    # Playlist Blu-ray : toujours lue par FFmpeg (pipe), jamais de conversion native.
    assert not _route(True, source="BDMV/PLAYLIST/00800.mpls", p5_to_hdr10=True).p5_native


def test_l5_11_nvencc_output_profile_follows_source():
    assert _route(False, copy_dv=True, dovi_source_profile="p8_4").video.dovi_profile == "8.4"
    assert _route(False, copy_dv=True, dovi_source_profile="p7_fel").video.dovi_profile == "8.1"
    assert _route(False, copy_dv=True).video.dovi_profile == "8.1"


# ---------------------------------------------------------------------------
# Gardes : RPU manquant (L5-07), comptes stricts (L5-06)
# ---------------------------------------------------------------------------


def test_l5_07_missing_rpu_line_fails_even_with_exit_zero(tmp_path, qt_app):
    from core.runner import TaskSignals

    _ = qt_app
    monitor = NvenccDoviRpuMonitor()
    script = "import sys; sys.stdin.read(); print('avout: Failed to get dovi rpu for 40.'); print('encoded 48 frames')"
    NvenccPipeExecutor().run(
        decode_cmd=[sys.executable, "-c", "print('y4m')"],
        encode_cmd=[sys.executable, "-c", script],
        cwd=tmp_path,
        signals=TaskSignals(),
        monitor=monitor,
    )
    assert monitor.missing == 1 and monitor.encoded_frames == 48
    with pytest.raises(EncodeError, match="sans RPU"):
        monitor.raise_if_failed()
    clean = NvenccDoviRpuMonitor()
    clean.feed("encoded 47 frames, 70 fps")
    with pytest.raises(EncodeError, match="47 trames encodées pour 48"):
        clean.raise_if_failed(expected_frames=48)
    clean.raise_if_failed(expected_frames=47)


def _guard(source: int | None, exact: int | None, rpu: int | None) -> FrameCountGuard:
    guard = FrameCountGuard(mediainfo_bin="mediainfo", ffprobe_bin="ffprobe", dovi_tool_bin="dovi_tool")
    counts = {"rpu": rpu}
    guard._read_video_frame_count = lambda path: source  # type: ignore[method-assign]
    guard._recount_video_frames = lambda path: exact  # type: ignore[method-assign]
    guard._dovi_rpu_frame_count = lambda path: counts["rpu"]  # type: ignore[method-assign]

    def trim(path, *, target_frames, current_frames):
        counts["rpu"] = target_frames

    guard._trim_rpu = trim  # type: ignore[method-assign]
    return guard


def test_l5_06_external_rpu_strict_guard(tmp_path):
    rpu = tmp_path / "rpu.bin"
    # Statistique conteneur périmée : recomptage exact avant de conclure.
    assert _guard(1817, 312, 312).check_external_rpu(source=tmp_path / "s.mkv", stream_index=0, rpu_bin=rpu) == 312
    # Surplus final ≤ 4 retiré (même source, début aligné).
    assert _guard(312, 312, 314).check_external_rpu(source=tmp_path / "s.mkv", stream_index=0, rpu_bin=rpu) == 312
    with pytest.raises(FrameCountAuditError, match="plus court"):
        _guard(312, 312, 300).check_external_rpu(source=tmp_path / "s.mkv", stream_index=0, rpu_bin=rpu)
    with pytest.raises(FrameCountAuditError, match="illisible"):
        _guard(None, None, 312).check_external_rpu(source=tmp_path / "s.mkv", stream_index=0, rpu_bin=rpu)


# ---------------------------------------------------------------------------
# Workflow : résolution, validation, remux P7 sans conversion
# ---------------------------------------------------------------------------


def _workflow(sub: DoviSubProfile = P5) -> EncodeWorkflow:
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    wf._dovi_sub_profile = lambda *args, **kw: sub  # type: ignore[method-assign]
    wf._ffmpeg_libplacebo_ready = lambda: True  # type: ignore[method-assign]
    return wf


def _config(tmp_path: Path, **video_kw) -> EncodeConfig:
    source = tmp_path / "src.mkv"
    source.write_bytes(b"")
    video = VideoEncodeSettings(source_path=source, **video_kw)
    return EncodeConfig(source=source, output=tmp_path / "out.mkv", video=video, video_tracks=[video])


def test_resolution_marks_p5_conversion_for_any_encode(tmp_path, qt_app):
    _ = qt_app
    for kw in ({"codec": "libx265", "copy_dv": True}, {"codec": "hevc_nvenc"}, {"codec": "libx264"}):
        resolved = _workflow().resolve_dovi_sources(_config(tmp_path, **kw))
        assert resolved.video.p5_to_hdr10 and resolved.video.dovi_source_profile == "p5"


def test_copy_keep_p7_is_never_analysed_or_converted(tmp_path, qt_app):
    """Remux P7 sans conversion ni normalisation : aucune analyse, aucune commande dovi_tool."""
    _ = qt_app
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    wf._dovi_sub_profile = lambda *args, **kw: pytest.fail("Copy + Conserver ne doit pas être analysé")  # type: ignore[method-assign]
    config = _config(tmp_path, codec="copy", copy_dv=True, dovi_profile="0")
    assert wf.resolve_dovi_sources(config) is config
    assert wf._dovi_policy_errors(config) == []
    assert not EncodeWorkflow._needs_metadata_inject(config)
    with patch.object(wf._runner, "_run_cmd", side_effect=AssertionError("aucune commande attendue")):
        prepared = wf.prepare_dovi_sources(config, work_dir=tmp_path)
    assert prepared.video == config.video


def test_validation_refuses_impossible_dovi_combinations(tmp_path, qt_app):
    _ = qt_app
    errors = _workflow(P5)._dovi_policy_errors(
        _workflow(P5).resolve_dovi_sources(_config(tmp_path, codec="copy", copy_dv=True, dovi_profile="2"))
    )
    assert any(P5_COPY_NORMALIZE_ERROR in error for error in errors)
    wf = _workflow(DoviSubProfile.P8_4)
    errors = wf._dovi_policy_errors(wf.resolve_dovi_sources(_config(tmp_path, codec="copy", copy_dv=True, dovi_profile="2")))
    assert any(BASE_LAYER_NORMALIZE_ERROR in error for error in errors)
    # Cohérence profil / transfert : P8.4 réencodé exige une base HLG.
    wf = _workflow(DoviSubProfile.P8_4)
    config = wf.resolve_dovi_sources(
        _config(tmp_path, codec="libx265", copy_dv=True, source_color_transfer="smpte2084")
    )
    assert any("P8.4 exige une image de base HLG" in error for error in wf._dovi_policy_errors(config))


def test_p5_conversion_requires_libplacebo(tmp_path, qt_app):
    _ = qt_app
    wf = _workflow(P5)
    wf._ffmpeg_libplacebo_ready = lambda: False  # type: ignore[method-assign]
    wf._nvencc_p5_native_ready = lambda: False  # type: ignore[method-assign]
    config = wf.resolve_dovi_sources(_config(tmp_path, codec="hevc_nvenc"))
    assert any("conversion des couleurs impossible" in error for error in wf._dovi_policy_errors(config))
    wf._nvencc_p5_native_ready = lambda: True  # type: ignore[method-assign]
    nvencc = wf.resolve_dovi_sources(_config(tmp_path, codec="nvencc_hevc"))
    assert wf._dovi_policy_errors(nvencc) == []
    # L5-A06 : parcours imposé par le pipe (tone-mapping) → FFmpeg fonctionnel exigé.
    piped = wf.resolve_dovi_sources(_config(tmp_path, codec="nvencc_h264", tonemap_to_sdr=True))
    assert any("conversion native NVEncC non applicable" in error for error in wf._dovi_policy_errors(piped))


def test_stream_vfr_detection_combines_ffprobe_and_mediainfo(tmp_path, qt_app):
    _ = qt_app
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    streams = {"streams": [
        {"index": 0, "codec_type": "video", "r_frame_rate": "24000/1001", "avg_frame_rate": "24000/1001"},
        {"index": 1, "codec_type": "video", "r_frame_rate": "30/1", "avg_frame_rate": "2997/100"},
    ]}
    wf._ffprobe_streams_payload = lambda source: streams  # type: ignore[method-assign]
    modes = {0: {"FrameRate_Mode": "CFR"}}
    wf._load_mediainfo_video_track = lambda source, index=None: modes.get(index, {})  # type: ignore[method-assign]
    assert not wf._stream_is_vfr(tmp_path / "s.mkv", 0)
    assert wf._stream_is_vfr(tmp_path / "s.mkv", 1)   # 30 contre 29,97 : écart > 0,1 %
    modes[0] = {"FrameRate_Mode": "VFR"}
    assert wf._stream_is_vfr(tmp_path / "s.mkv", 0)


# ---------------------------------------------------------------------------
# Avertissement HDR → codec sans HDR (RV5-02), interpolation (C3)
# ---------------------------------------------------------------------------


def test_rv5_02_hdr_source_to_sdr_codec_without_tonemap_warns():
    for video in (
        VideoEncodeSettings(codec="libx264", source_color_transfer="smpte2084"),
        VideoEncodeSettings(codec="h264_nvenc", p5_to_hdr10=True),
    ):
        assert any("ne porte pas le HDR" in warning for warning in hdr_warnings(video))
    assert not any(
        "ne porte pas le HDR" in warning
        for warning in hdr_warnings(VideoEncodeSettings(codec="libx264", source_color_transfer="smpte2084",
                                                        tonemap_to_sdr=True))
    )


def test_c3_interpolation_colour_of_converted_p5():
    p5_stream = {"color_space": "unknown", "color_range": "pc", "height": 2160,
                 "r_frame_rate": "24000/1001", "avg_frame_rate": "24000/1001"}
    hdr = interpolation_source_from_probe(p5_stream, p5_to_hdr10=True)
    assert (hdr.matrix, hdr.color_range, hdr.transfer) == ("bt2020nc", "limited", "smpte2084")
    sdr = interpolation_source_from_probe(p5_stream, p5_to_hdr10=True, tonemap_to_sdr=True)
    assert (sdr.matrix, sdr.transfer) == ("bt709", "bt709")


def test_check_features_parsing():
    features = parse_check_features("Environment Info\n  nvrtc      : yes\n  libdovi    : yes\n  libplacebo : no\n")
    assert features == {"nvrtc": True, "libdovi": True, "libplacebo": False}


def test_frame_ratio_expected_frames_rounding():
    import math

    assert math.ceil(312 * Fraction(5, 2)) == 780


# ---------------------------------------------------------------------------
# Audit des modifications du lot 5 (L5-A01 à L5-A10)
# ---------------------------------------------------------------------------

_DOVI_RECORD_P5 = {"side_data_type": "DOVI configuration record", "dv_profile": 5, "dv_level": 6,
                   "dv_bl_signal_compatibility_id": 0}


def test_l5_a01_p5_detected_per_stream_without_mediainfo(tmp_path, qt_app):
    from core.dovi_profile_detector import DoviProfileDetector

    _ = qt_app
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", mediainfo_bin="/absent/mediainfo", generate_nfo=False)
    payload = {"streams": [
        {"index": 0, "codec_type": "video"},
        {"index": 1, "codec_type": "video", "side_data_list": [_DOVI_RECORD_P5]},
    ]}
    wf._ffprobe_streams_payload = lambda source: payload  # type: ignore[method-assign]
    wf._load_mediainfo_video_track = lambda source, index=None: None  # type: ignore[method-assign]
    source = tmp_path / "two.mkv"
    source.write_bytes(b"")
    assert wf._dovi_sub_profile(source, 1) is P5
    assert wf._dovi_sub_profile(source, 0) is DoviSubProfile.UNKNOWN
    video = VideoEncodeSettings(codec="libx265", source_path=source, stream_index=1)
    config = EncodeConfig(source=source, output=tmp_path / "o.mkv", video=video, video_tracks=[video])
    assert wf.resolve_dovi_sources(config).video.p5_to_hdr10
    # Repli dovi_tool ciblé : le flux choisi est extrait par FFmpeg.
    commands: list[list[str]] = []

    class _Proc:
        stdout = None

        def wait(self):
            return 0

    with patch("core.dovi_profile_detector.subprocess.Popen", side_effect=lambda cmd, **kw: commands.append(cmd) or _Proc()), \
         patch("core.dovi_profile_detector.subprocess.run",
               side_effect=lambda cmd, **kw: __import__("subprocess").CompletedProcess(cmd, 1, "", "")):
        DoviProfileDetector().detect_from_dovi_tool(source, stream_index=1)
    assert commands and commands[0][commands[0].index("-map") + 1] == "0:1"


def _a02_workflow(streams: list[dict]) -> EncodeWorkflow:
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False, nvencc_bin=sys.executable)
    wf._ffprobe_streams_payload = lambda source: {"streams": streams}  # type: ignore[method-assign]
    wf._load_mediainfo_video_track = lambda source, index=None: {}  # type: ignore[method-assign]
    wf._detect_source_dynamic_hdr_presence = lambda source, index=None: (True, False)  # type: ignore[method-assign]
    wf._ffmpeg_libplacebo_ready = lambda: True  # type: ignore[method-assign]
    return wf



def _p5_from_dovi_tool():
    from core.dovi_profile_detector import DoviDetectionResult

    return DoviDetectionResult(sub_profile=P5, profile=5, level=6, bl_signal_compat_id=0, raw_source="dovi_tool")


def test_l5_a02_probe_failure_revalidates_vfr_pipe(tmp_path, qt_app):
    from core.dovi_profile_detector import DoviProfileDetector
    from core.workflows.encode.runtime.nvencc_p5 import binary_signature

    _ = qt_app
    stream = {"index": 0, "codec_type": "video", "codec_name": "hevc", "r_frame_rate": "30/1",
              "avg_frame_rate": "2997/100", "width": 3840, "height": 2160}
    wf = _a02_workflow([stream])
    wf._nvencc_p5_features_ok = lambda: True  # type: ignore[method-assign]
    signature = binary_signature(sys.executable)

    def failed_probe(work_dir):
        wf.__dict__.setdefault("_nvencc_p5_probe_cache", {})[signature] = False

    wf._ensure_nvencc_p5_probe = failed_probe  # type: ignore[method-assign]
    config = _config(tmp_path, codec="nvencc_hevc", copy_dv=True)
    with patch.object(DoviProfileDetector, "detect_from_dovi_tool", return_value=_p5_from_dovi_tool()):
        with pytest.raises(EncodeError, match="cadence variable"):
            wf.prepare_dovi_sources(config, work_dir=tmp_path)


def test_l5_a02_late_p5_discovery_refuses_multi_track(tmp_path, qt_app):
    from core.dovi_profile_detector import DoviProfileDetector

    _ = qt_app
    streams = [{"index": i, "codec_type": "video", "codec_name": "hevc", "r_frame_rate": "24/1",
                "avg_frame_rate": "24/1", "width": 1920, "height": 1080} for i in (0, 1)]
    wf = _a02_workflow(streams)
    source = tmp_path / "src.mkv"
    source.write_bytes(b"")
    tracks = [VideoEncodeSettings(codec="libx265", copy_dv=True, source_path=source, stream_index=i,
                                  track_entry_id=f"t{i}") for i in (0, 1)]
    config = EncodeConfig(source=source, output=tmp_path / "o.mkv", video=tracks[0], video_tracks=tracks)
    assert not any("P5" in error for error in wf.validate(config))
    with patch.object(DoviProfileDetector, "detect_from_dovi_tool", return_value=_p5_from_dovi_tool()):
        with pytest.raises(EncodeError, match="une seule piste vidéo"):
            wf.prepare_dovi_sources(config, work_dir=tmp_path)


def test_l5_a03_record_compat_follows_matrix():
    from core.workflows.encode.runtime.metadata_inject import _resolve_dovi_compat_id
    from core.workflows.encode.runtime.dovi_p7_router import P7RoutingDecision

    def compat(sub: str, profile: str = "0", decision=None):
        video = VideoEncodeSettings(codec="libx265", copy_dv=True, dovi_profile=profile, dovi_source_profile=sub)
        return _resolve_dovi_compat_id(p7_router_decision=decision, user_dovi_profile=profile, video=video)

    assert (compat("p8_4"), compat("p8_2"), compat("p8_1"), compat("")) == (4, 2, 1, None)
    assert compat("p8_4", profile="2") == 1
    p84 = P7RoutingDecision(conversion_needed=True, sub_profile=DoviSubProfile.P8_4, convert_mode="4", reason="")
    assert compat("p8_4", decision=p84) == 4


def test_l5_a11_dovi_sdr_base_policy_and_warnings():
    from core.inspector import HDRType
    from core.workflows.encode.hdr_policy import default_static_hdr_checked, source_is_hdr

    # P8.2 (BT.709) : SDR ; P5 (VUI non spécifiée) et P8.1 : HDR ; HDR10 mal étiqueté inchangé.
    assert not default_static_hdr_checked(HDRType.DOLBY_VISION, "bt709")
    assert not source_is_hdr(HDRType.DOLBY_VISION, "bt709")
    for transfer in ("", "unknown", "smpte2084"):
        assert default_static_hdr_checked(HDRType.DOLBY_VISION, transfer)
        assert source_is_hdr(HDRType.DOLBY_VISION, transfer)
    assert default_static_hdr_checked(HDRType.HDR10, "bt709")
    # Avertissements alignés sur la sortie effective : copie DV d'une P8.2 = SDR, rien à signaler.
    p82 = VideoEncodeSettings(codec="libx265", copy_dv=True, source_color_transfer="bt709", dovi_source_profile="p8_2")
    assert hdr_warnings(p82) == []
    unknown = VideoEncodeSettings(codec="libx265", copy_dv=True, source_color_transfer="bt709")
    assert any("PQ" in warning for warning in hdr_warnings(unknown))


def test_l5_a04_p8_2_keeps_sdr_base():
    from core.workflows.encode.domain.codecs import output_hdr_transfer

    video = VideoEncodeSettings(codec="libx265", copy_dv=True, dovi_source_profile="p8_2", source_color_transfer="bt709")
    assert output_hdr_transfer(video) == ""
    assert build_encoder_vf(video, callbacks=EncodeCodecDomainCallbacks(platform="linux")) == ""
    hdr10 = VideoEncodeSettings(codec="libx265", copy_dv=True, inject_hdr_meta=True,
                                dovi_source_profile="p8_2", source_color_transfer="bt709")
    assert "P8.2 exige une image de base SDR" in dovi_transfer_error("8.2", output_hdr_transfer(hdr10))
    # Inchangé (lot 3) : DV sur une source au transfert inconnu reste signalé PQ.
    assert output_hdr_transfer(VideoEncodeSettings(codec="libx265", copy_dv=True)) == "pq"


def test_l5_a10_static_provenance_per_field(tmp_path, qt_app):
    from core.workflows.encode.runtime.dovi_static_hdr import RpuStaticHdr

    _ = qt_app
    estimate = RpuStaticHdr(
        master_display=f"{_P3}L(10000000,1)", max_cll="758,143", luminance_source="rpu_l6",
        primaries_source="default_p3_d65", max_cll_source="rpu_l1", max_fall_source="rpu_l1",
    )
    manual_md = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,50)"
    wf = _workflow(P5)
    config = wf.resolve_dovi_sources(_config(tmp_path, codec="libx265", inject_hdr_meta=True, master_display=manual_md))
    logs: list[str] = []
    wf.log_message.connect(lambda level, message: logs.append(message))
    with patch("core.workflows.encode.runtime.dovi_geometry.extract_dovi_rpu"), \
         patch("core.workflows.encode.workflow._estimate_static_hdr_from_rpu", return_value=estimate), \
         patch.object(wf, "validate", return_value=[]):
        prepared = wf.prepare_dovi_sources(config, work_dir=tmp_path).video
    assert prepared.master_display == manual_md and prepared.static_hdr_metadata_source == ""
    assert prepared.max_cll == "758,143" and prepared.static_hdr_light_level_source == "rpu_estimate"
    estimated = [line for line in logs if "estimé depuis les métadonnées du RPU" in line]
    assert estimated and "MaxCLL/MaxFALL 758,143" in estimated[0] and "Master Display" not in estimated[0]
