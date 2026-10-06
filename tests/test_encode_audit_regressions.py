"""Non-régressions de l'audit encode (ordre des options FFmpeg, HDR10 statique,
entrées brutes, VAAPI, cadence NVEncC)."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import cast

import pytest

from core.workflows.common import TrackTimeOffset
from core.workflows.encode import AudioTrackSettings, EncodeConfig, EncodeWorkflow, QualityMode, VideoEncodeSettings
from core.workflows.encode.domain import (
    EncodeCodecDomainCallbacks,
    build_encoder_vf,
    strips_source_static_hdr_side_data,
    svtav1_mastering_display,
    svtav1_params,
    video_codec_args,
)
from core.workflows.encode.models import VideoResizeSettings
from core.workflows.encode.runtime.nvencc_routing import source_video_fps_expr
from core.workflows.encode.runtime.video_preparation import raw_input_rate_args

_MD = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(40000000,50)"
_CB = EncodeCodecDomainCallbacks(platform="linux", vaapi_device="/dev/dri/renderD128")


def _hdr_video(codec: str, **kw) -> VideoEncodeSettings:
    return VideoEncodeSettings(codec=codec, inject_hdr_meta=True, master_display=_MD, max_cll="4000,1000", **kw)


def test_video_output_options_follow_every_input(qt_app, tmp_path):
    """-vf / -threads après la dernière entrée : sinon FFmpeg les applique à l'entrée
    auxiliaire de décalage et refuse la commande."""
    _ = qt_app
    src = tmp_path / "s.mkv"
    src.write_bytes(b"")
    video = VideoEncodeSettings(codec="libx264", quality_mode=QualityMode.CRF, crf=30, preset="ultrafast",
                                resize=VideoResizeSettings(enabled=True, mode="size", width=160, height=120))
    offsets = [TrackTimeOffset(track_type="video", source_path=src, stream_index=0, offset_ms=400),
               TrackTimeOffset(track_type="audio", source_path=src, stream_index=1, offset_ms=200)]
    for quality, extra in ((QualityMode.CRF, {}), (QualityMode.SIZE, {"target_size_mb": 10})):
        cfg = EncodeConfig(source=src, output=tmp_path / "o.mkv", video=VideoEncodeSettings(**{**video.__dict__,
                           "quality_mode": quality, **extra}), audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")],
                           copy_subtitles=False, keep_chapters=False, duration_s=10, track_time_offsets=offsets)
        built = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False).build_command(cfg)
        commands = cast(list[list[str]], built) if isinstance(built[0], list) else [cast(list[str], built)]
        for cmd in commands:
            last_input = max(i for i, token in enumerate(cmd) if token == "-i")
            assert cmd.index("-vf") > last_input and cmd.index("-threads") > last_input


def test_explicit_static_hdr_strips_source_side_data():
    for codec in ("libx265", "libsvtav1", "hevc_nvenc"):
        video = _hdr_video(codec)
        assert strips_source_static_hdr_side_data(video), codec
        assert "sidedata=mode=delete:type=MASTERING_DISPLAY_METADATA" in build_encoder_vf(video, callbacks=_CB)
    # V39 : QSV / AMF / VAAPI réinjectent aussi les valeurs saisies.
    assert strips_source_static_hdr_side_data(_hdr_video("hevc_qsv"))
    # HDR10 coché sans valeurs : celles de la source passent telles quelles.
    assert not strips_source_static_hdr_side_data(VideoEncodeSettings(codec="libx265", inject_hdr_meta=True))


def test_svtav1_static_hdr_params():
    assert svtav1_mastering_display(_MD) == (
        "G(0.26500,0.69000)B(0.15000,0.06000)R(0.68000,0.32000)WP(0.31270,0.32900)L(4000.0000,0.0050)"
    )
    assert svtav1_mastering_display("invalide") == ""
    params = svtav1_params(_hdr_video("libsvtav1", extra_params="tune=0"))
    assert params.split(":")[0] == "tune=0" and "content-light=4000,1000" in params
    args = video_codec_args(_hdr_video("libsvtav1", quality_mode=QualityMode.CRF, crf=30, preset="8"), 0, callbacks=_CB)
    assert "mastering-display=" in args[args.index("-svtav1-params") + 1]
    tonemapped = _hdr_video("libsvtav1", tonemap_to_sdr=True)
    assert "mastering-display" not in svtav1_params(tonemapped)


def test_vaapi_uploads_frames_from_inputs_without_hwaccel():
    video = VideoEncodeSettings(codec="hevc_vaapi", source_bit_depth=8)
    assert build_encoder_vf(video, callbacks=_CB) == ""
    assert build_encoder_vf(video, callbacks=_CB, hw_decoded=False) == "format=nv12,hwupload"


def test_raw_annexb_input_gets_source_frame_rate():
    video = VideoEncodeSettings(codec="libx265", input_frame_rate="24000/1001")
    assert raw_input_rate_args(video, Path("converted.hevc")) == ["-r", "24000/1001"]
    assert raw_input_rate_args(video, Path("source.mkv")) == []
    assert raw_input_rate_args(VideoEncodeSettings(codec="libx265"), Path("converted.hevc")) == []


def test_nvencc_frame_rate_of_selected_stream():
    payload: dict[str, object] = {"streams": [
        {"index": 0, "codec_type": "video", "avg_frame_rate": "25/1"},
        {"index": 1, "codec_type": "audio"},
        {"index": 2, "codec_type": "video", "avg_frame_rate": "24000/1001"},
    ]}
    def probe_streams(_source: Path) -> dict[str, object]:
        return payload

    def stream_dicts(result: dict[str, object]) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], result["streams"])

    fps_expr = partial(source_video_fps_expr, ffprobe_streams_payload=probe_streams,
                       ffprobe_stream_dicts=stream_dicts, mediainfo_fps_expr=lambda _source: None)
    assert fps_expr(Path("s.mkv"), stream_index=2) == "24000/1001"
    assert fps_expr(Path("s.mkv")) == "25/1"
    # index introuvable (flux brut) : premier flux vidéo
    assert fps_expr(Path("s.hevc"), stream_index=7) == "25/1"


def test_size_mode_single_video_split_encode_keeps_audio_budget(qt_app, tmp_path):
    """Encodage séparé mono-vidéo (réinjection HDR, RIFE) : budget audio déduit."""
    _ = qt_app
    video = _hdr_video("hevc_nvenc", quality_mode=QualityMode.SIZE, target_size_mb=1000)
    cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video, duration_s=3600,
                       audio_tracks=[AudioTrackSettings(stream_index=1, codec="aac", bitrate_kbps=320)])
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    assert wf._needs_split_video_encode(cfg)
    kbps = wf._size_to_bitrate_kbps_for_video(cfg, video)
    assert kbps == wf._size_to_bitrate_kbps(cfg) < int(1000 * 8 * 1024 * 1024 / 3600 / 1000)


def test_multi_video_assembly_materializes_calibrated_audio(qt_app, tmp_path, monkeypatch):
    """Pipeline multi-pistes : calibration multi-segments réécrite, pas de simple -itsoffset."""
    from unittest.mock import patch

    from core.runner import TaskSignals
    from core.workflows.common.sync_rewrite import SyncRewritePreparedInput
    from core.workflows.sync_calibration import SyncCalibration, SyncSegment

    _ = qt_app
    src, out, work = tmp_path / "s.mkv", tmp_path / "o.mkv", tmp_path / "work"
    src.write_bytes(b"src")
    work.mkdir()
    rewritten = work / "audio.rewrite.mka"
    video = VideoEncodeSettings(codec="libx265", source_path=src, stream_index=0)
    offset = TrackTimeOffset(track_type="audio", source_path=src, stream_index=1, offset_ms=256,
                             calibration=SyncCalibration((SyncSegment(0, 256.0), SyncSegment(90_000, 0.0))).to_dict())
    cfg = EncodeConfig(source=src, output=out, video=video, video_tracks=[video], work_dir=work, duration_s=10,
                       audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy", source_path=src)],
                       copy_subtitles=False, keep_chapters=False, track_time_offsets=[offset])
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    wf.set_sync_rewrite_enabled(True)
    calls: dict[str, object] = {}

    class _Rewrite:
        def __init__(self, **_kw):
            pass

        def maybe_materialize(self, **kw):
            calls["calibration"] = kw["calibration"]
            rewritten.write_bytes(b"a")
            return SyncRewritePreparedInput(path=rewritten, input_idx=int(kw["input_idx"]), track_type="audio",
                                            codec="eac3", mode_label="sync")

    def _run(cmd, **_kw):
        Path(cmd[-1]).write_bytes(b"x")
        return "ok"

    def _finalize(_cfg, cmd, *_a, **_kw):
        calls["cmd"] = list(cmd)
        return str(out)

    monkeypatch.setattr("core.workflows.encode.workflow.SyncRewriteService", _Rewrite)
    with patch.object(wf._runner, "_run_cmd", side_effect=_run), \
         patch.object(wf, "_prepare_multisource_sync", return_value=({}, [], None, False)), \
         patch.object(wf, "_stream_codec_of", return_value="eac3"), \
         patch.object(wf, "_finalize_ffmpeg_output", side_effect=_finalize):
        wf._run_multi_video_pipeline(cfg, cleanup_paths=[], prep_signals=TaskSignals())
    cmd = calls["cmd"]
    assert isinstance(cmd, list)
    assert calls["calibration"] == offset.calibration
    assert str(rewritten) in cmd and "-itsoffset" not in cmd


def test_v01_libx264_extra_params_reach_command():
    """Les paramètres avancés x264 (flags ffmpeg) sont transmis dans chaque mode."""
    extra = '-tune film -x264-params "aq-mode=3"'
    for mode in (QualityMode.CRF, QualityMode.CQ, QualityMode.BITRATE):
        video = VideoEncodeSettings(codec="libx264", quality_mode=mode, preset="slow", extra_params=extra)
        args = video_codec_args(video, 4000, callbacks=_CB)
        assert args[-4:] == ["-tune", "film", "-x264-params", "aq-mode=3"], mode


def test_v05_nvenc_crf_is_not_capped_by_default_bitrate():
    """-cq sans -b:v 0 laisse nvenc viser son débit par défaut (2 Mb/s)."""
    for codec in ("hevc_nvenc", "h264_nvenc", "av1_nvenc"):
        video = VideoEncodeSettings(codec=codec, quality_mode=QualityMode.CRF, crf=20, preset="p5")
        args = video_codec_args(video, 0, callbacks=_CB)
        assert args[args.index("-b:v") + 1] == "0", codec
        assert args[args.index("-cq:v") + 1] == "20", codec


# --- Lot 2 : garde-fous ------------------------------------------------------

_P7_MEDIAINFO = {
    "HDR_Format": "Dolby Vision, Version 1.0, Profile 7.6, dvhe.07.06, BL+EL+RPU",
    "HDR_Format_Profile": "dvhe.07 / 06",
    "HDR_Format_Settings": "BL+EL+RPU",
    "HDR_Format_Compatibility": "Blu-ray / HDR10",
}


def _two_track_config(tmp_path: Path, first: VideoEncodeSettings, second: VideoEncodeSettings) -> EncodeConfig:
    for name in ("a.mkv", "b.mkv"):
        (tmp_path / name).write_bytes(b"")
    first = VideoEncodeSettings(**{**first.__dict__, "source_path": tmp_path / "a.mkv"})
    second = VideoEncodeSettings(**{**second.__dict__, "source_path": tmp_path / "b.mkv"})
    return EncodeConfig(source=tmp_path / "a.mkv", output=tmp_path / "o.mkv", video=first,
                        video_tracks=[first, second], duration_s=10)


def test_v13_nvencc_track_is_validated_whatever_its_position(qt_app, tmp_path):
    _ = qt_app
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", nvencc_bin="nvencc", generate_nfo=False)
    copy = VideoEncodeSettings(codec="copy")
    nvencc = VideoEncodeSettings(codec="nvencc_hevc", quality_mode=QualityMode.CQ)
    for order in ((copy, nvencc), (nvencc, copy)):
        errors = wf.validate(_two_track_config(tmp_path, *order))
        assert any("NVEncC ne supporte pas le mode multi-pistes" in error for error in errors), order


def test_v11_v12_multi_track_dovi_routing_is_refused(qt_app, tmp_path, monkeypatch):
    _ = qt_app
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    monkeypatch.setattr(wf, "_load_mediainfo_video_track", lambda _path, _stream=None: dict(_P7_MEDIAINFO))
    p7_encode = VideoEncodeSettings(codec="libx265", copy_dv=True, inject_hdr_meta=True)
    errors = wf._dovi_multi_track_errors(_two_track_config(tmp_path, p7_encode, VideoEncodeSettings(codec="copy")))
    assert len(errors) == 1 and "P7 FEL" in errors[0]
    normalize_copy = VideoEncodeSettings(codec="copy", copy_dv=True, dovi_profile="2")
    errors = wf._dovi_multi_track_errors(_two_track_config(tmp_path, normalize_copy, VideoEncodeSettings(codec="copy")))
    assert len(errors) == 1 and "Normaliser en P8.1" in errors[0]
    # Piste unique : chemin mono-piste, conversion prise en charge.
    single = EncodeConfig(source=tmp_path / "a.mkv", output=tmp_path / "o.mkv", video=p7_encode,
                          video_tracks=[p7_encode], duration_s=10)
    assert wf._dovi_multi_track_errors(single) == []


def test_v02_v37_video_settings_are_validated():
    from core.workflows.encode.planning.validation import video_settings_errors

    errors = video_settings_errors([
        VideoEncodeSettings(codec="hevc_nvenc", extra_params="no-open-gop=1:hdr10=1"),
        VideoEncodeSettings(codec="libx265", extra_params="-tune grain"),
        VideoEncodeSettings(codec="libx264", quality_mode=QualityMode.BITRATE, bitrate_kbps=0),
        VideoEncodeSettings(codec="libx264", quality_mode=QualityMode.SIZE, target_size_mb=-5),
        VideoEncodeSettings(codec="libx264", extra_params='-x264-params "aq-mode=3'),
    ])
    assert [error.split(" — ")[0] for error in errors] == [
        "Piste vidéo #1", "Piste vidéo #2", "Piste vidéo #3", "Piste vidéo #4", "Piste vidéo #5",
    ]
    assert video_settings_errors([
        VideoEncodeSettings(codec="libx265", extra_params="no-open-gop=1:aq-mode=3"),
        VideoEncodeSettings(codec="hevc_nvenc", extra_params="-rc-lookahead 32 -spatial-aq 1"),
        VideoEncodeSettings(codec="nvencc_hevc", extra_params="--lookahead 32 --aq"),
    ]) == []


def test_v16_ffmpeg_extras_override_ui_settings_with_warning():
    """Saisie ajoutée après le workflow (la dernière occurrence l'emporte), sauf mapping / codec."""
    from core.workflows.encode.domain import ffmpeg_extra_params_report

    video = VideoEncodeSettings(codec="hevc_nvenc", quality_mode=QualityMode.CQ, preset="p5",
                                extra_params="-cq 30 -pix_fmt yuv420p -preset p1 -map 0:1 -c:v libx264 -spatial-aq 1")
    args = video_codec_args(video, 0, callbacks=_CB)
    assert args[-8:] == ["-cq", "30", "-pix_fmt", "yuv420p", "-preset", "p1", "-spatial-aq", "1"]
    assert "-map" not in args and args.count("-c:v") == 1
    report = ffmpeg_extra_params_report(video, callbacks=_CB)
    assert report.removed == ("-map", "0:1", "-c:v", "libx264")
    # Source inconnue : le workflow pose un format 8 bits, surcharge signalée.
    assert report.overriding == ("-cq", "30", "-pix_fmt", "yuv420p", "-preset", "p1")
    ten_bit = VideoEncodeSettings(**{**video.__dict__, "bit_depth": "10"})
    assert "-pix_fmt" in ffmpeg_extra_params_report(ten_bit, callbacks=_CB).overriding
    # Valeur par défaut du workflow (async_depth) : surchargeable sans message.
    qsv = VideoEncodeSettings(codec="hevc_qsv", quality_mode=QualityMode.CQ, preset="slow", extra_params="-async_depth 8")
    assert video_codec_args(qsv, 0, callbacks=_CB)[-2:] == ["-async_depth", "8"]
    assert ffmpeg_extra_params_report(qsv, callbacks=_CB).overriding == ()


def test_v16_user_filter_kept_only_without_workflow_filters():
    from core.workflows.encode.domain import ffmpeg_extra_params_report
    from core.workflows.encode.models import VideoCropSettings

    plain = VideoEncodeSettings(codec="libx264", extra_params="-vf hqdn3d")
    assert video_codec_args(plain, 0, callbacks=_CB)[-2:] == ["-vf", "hqdn3d"]
    cropped = VideoEncodeSettings(codec="libx264", extra_params="-vf hqdn3d",
                                  crop=VideoCropSettings(enabled=True, top=140, bottom=140))
    assert "-vf" not in video_codec_args(cropped, 0, callbacks=_CB)
    assert ffmpeg_extra_params_report(cropped, callbacks=_CB).removed == ("-vf", "hqdn3d")


def test_vaapi_none_preset_omits_compression_level():
    from core.workflows.encode.catalog import presets_for_codec

    assert presets_for_codec("hevc_vaapi")[0] == ""
    for mode in (QualityMode.CRF, QualityMode.CQ, QualityMode.BITRATE):
        args = video_codec_args(VideoEncodeSettings(codec="hevc_vaapi", quality_mode=mode, preset=""), 5000, callbacks=_CB)
        assert "-compression_level" not in args, mode
        args = video_codec_args(VideoEncodeSettings(codec="hevc_vaapi", quality_mode=mode, preset="4"), 5000, callbacks=_CB)
        assert args[args.index("-compression_level") + 1] == "4", mode


def test_v16_extra_params_warnings_are_reported(qt_app, tmp_path):
    _ = qt_app
    (tmp_path / "a.mkv").write_bytes(b"")
    video = VideoEncodeSettings(codec="libx264", extra_params="-crf 10 -map 0:1 -tune film",
                                source_path=tmp_path / "a.mkv")
    cfg = EncodeConfig(source=tmp_path / "a.mkv", output=tmp_path / "o.mkv", video=video, duration_s=10)
    warnings = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False).extra_params_warnings(cfg)
    assert warnings == [
        "Piste vidéo #1 — paramètres avancés ignorés (incompatibles avec le workflow) : -map 0:1",
        "Piste vidéo #1 — paramètres avancés qui remplacent les réglages de l'onglet Video : -crf 10",
    ]


def test_v16_workflow_option_values_for_dialog_prefill():
    from core.workflows.encode.domain import ffmpeg_workflow_option_values
    from core.workflows.encode.runtime.nvencc import nvencc_workflow_option_values

    vaapi = VideoEncodeSettings(codec="hevc_vaapi", quality_mode=QualityMode.CQ, cq=24, preset="4", extra_params="-qp 30")
    values = ffmpeg_workflow_option_values(vaapi, callbacks=_CB)
    assert (values["rc_mode"], values["qp"], values["compression_level"]) == ("CQP", "24", "4")
    nvencc = VideoEncodeSettings(codec="nvencc_hevc", quality_mode=QualityMode.CQ, cq=27, preset="P5")
    values = nvencc_workflow_option_values(nvencc)
    assert (values["qvbr"], values["preset"]) == ("27", "P5")


def test_v18a_resize_dimensions_match_ffmpeg_scale():
    """Valeurs relevées avec ffmpeg 8.1 (force_original_aspect_ratio=decrease:force_divisible_by=2)."""
    from core.workflows.encode.domain import resolve_resize_dimensions

    p720 = VideoResizeSettings(enabled=True, mode="preset", preset="720p")
    p1080 = VideoResizeSettings(enabled=True, mode="preset", preset="1080p")
    assert resolve_resize_dimensions(1920, 800, p720) == (1280, 534)
    assert resolve_resize_dimensions(1440, 1080, p720) == (960, 720)
    assert resolve_resize_dimensions(3840, 1600, p1080) == (1920, 800)
    assert resolve_resize_dimensions(1920, 800, p1080) == (1920, 800)  # pas d'agrandissement
    stretched = VideoResizeSettings(enabled=True, mode="size", width=1280, height=720, keep_aspect=False)
    assert resolve_resize_dimensions(1920, 800, stretched) == (1280, 720)


def test_v18a_nvencc_native_resize_keeps_aspect_ratio():
    from core.workflows.encode.models import VideoCropSettings
    from core.workflows.encode.runtime.dovi_geometry import nvencc_dovi_resize_changes_scale
    from core.workflows.encode.runtime.nvencc import build_nvencc_command

    video = VideoEncodeSettings(codec="nvencc_hevc", quality_mode=QualityMode.CQ,
                                resize=VideoResizeSettings(enabled=True, mode="preset", preset="720p"),
                                crop=VideoCropSettings(enabled=True, top=140, bottom=140))
    cmd = build_nvencc_command("nvencc", video, "/tmp/o.mkv", input_path="/in.mkv", source_dimensions=(1920, 1080))
    assert cmd[cmd.index("--output-res") + 1] == "1280x534"
    # Preset égal à l'image : aucun rééchantillonnage, la copie DoVi reste possible.
    same = VideoEncodeSettings(codec="nvencc_hevc", copy_dv=True,
                               resize=VideoResizeSettings(enabled=True, mode="preset", preset="2160p"))
    assert nvencc_dovi_resize_changes_scale(same, (3840, 1600)) is False


# --- Lot 3 : intégrité HDR ---------------------------------------------------

def test_lot3_output_hdr_transfer_follows_source_not_checkbox():
    from core.workflows.encode.domain import output_hdr_transfer

    pq = dict(codec="libx265", source_color_transfer="smpte2084")
    assert output_hdr_transfer(VideoEncodeSettings(**pq, inject_hdr_meta=False)) == "pq"
    assert output_hdr_transfer(VideoEncodeSettings(**pq, tonemap_to_sdr=True)) == ""
    assert output_hdr_transfer(VideoEncodeSettings(codec="libx264", source_color_transfer="smpte2084")) == ""
    assert output_hdr_transfer(VideoEncodeSettings(codec="libx265", source_color_transfer="arib-std-b67", copy_dv=True)) == "hlg"
    assert output_hdr_transfer(VideoEncodeSettings(codec="libx265", source_color_transfer="bt709")) == ""
    # Demande HDR explicite sur une source étiquetée SDR (PQ mal étiquetée) : PQ.
    assert output_hdr_transfer(VideoEncodeSettings(codec="libx265", source_color_transfer="bt709",
                                                   inject_hdr_meta=True)) == "pq"
    # Transfert inconnu : une demande HDR vaut PQ (comportement historique).
    assert output_hdr_transfer(VideoEncodeSettings(codec="libx265", inject_hdr_meta=True)) == "pq"
    assert output_hdr_transfer(VideoEncodeSettings(codec="libx265")) == ""


def test_lot3_v09_v10_vf_tags_hdr_and_strips_unchecked_static_metadata():
    strip = "sidedata=mode=delete:type=MASTERING_DISPLAY_METADATA"
    for codec in ("libx265", "hevc_nvenc", "hevc_qsv", "av1_nvenc"):
        unchecked = VideoEncodeSettings(codec=codec, source_color_transfer="smpte2084")
        vf = build_encoder_vf(unchecked, callbacks=_CB)
        assert vf.startswith("setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc"), codec
        assert strip in vf, codec
        kept = VideoEncodeSettings(codec=codec, source_color_transfer="smpte2084", inject_hdr_meta=True)
        assert strip not in build_encoder_vf(kept, callbacks=_CB), codec
    hlg = VideoEncodeSettings(codec="libx265", source_color_transfer="arib-std-b67")
    assert "color_trc=arib-std-b67" in build_encoder_vf(hlg, callbacks=_CB)
    assert build_encoder_vf(VideoEncodeSettings(codec="libx265", source_color_transfer="bt709"), callbacks=_CB) == ""


def test_lot3_vaapi_metadata_filters_keep_hardware_frames():
    """setparams / sidedata acceptent les images VAAPI : pas d'upload logiciel ajouté."""
    video = VideoEncodeSettings(codec="hevc_vaapi", source_color_transfer="smpte2084", source_bit_depth=10)
    vf = build_encoder_vf(video, callbacks=_CB)
    assert "hwupload" not in vf and vf.startswith("setparams=")


def test_lot3_hlg_vui_in_output_options():
    from core.workflows.encode.domain import hdr_meta_args

    args = hdr_meta_args(VideoEncodeSettings(codec="libx265", source_color_transfer="arib-std-b67", copy_dv=True))
    assert args[args.index("-color_trc") + 1] == "arib-std-b67"


def test_lot3_n1_nvencc_hdr_output_without_static_metadata_stays_10bit_tagged():
    from core.workflows.encode.runtime.nvencc import build_nvencc_command

    video = VideoEncodeSettings(codec="nvencc_hevc", quality_mode=QualityMode.CQ, source_color_transfer="smpte2084")
    cmd = build_nvencc_command("nvencc", video, "/tmp/o.mkv", input_path="/in.mkv")
    assert cmd[cmd.index("--output-depth") + 1] == "10"
    assert cmd[cmd.index("--transfer") + 1] == "auto"
    assert "--master-display" not in cmd and "--max-cll" not in cmd
    piped = build_nvencc_command(
        "nvencc", VideoEncodeSettings(codec="nvencc_hevc", source_color_transfer="arib-std-b67"), "/tmp/o.mkv",
    )
    assert piped[piped.index("--transfer") + 1] == "arib-std-b67"


def test_lot3_v17_dynamic_hdr_detection_is_per_stream():
    from core.workflows.encode.runtime.hdr_metadata import HdrMetadataProbeService, select_mediainfo_video_track

    payload = {"streams": [
        {"index": 0, "codec_type": "video", "side_data_list": [{"side_data_type": "DOVI configuration record"}]},
        {"index": 1, "codec_type": "video", "side_data_list": []},
    ]}
    service = HdrMetadataProbeService.__new__(HdrMetadataProbeService)
    common = dict(
        ffprobe_streams_payload=lambda _source: payload,
        ffprobe_stream_dicts=lambda data: list(data["streams"]),
        mediainfo_hdr_flags=lambda _source, _stream=None: (False, False),
        ffprobe_frame_dynamic_hdr_flags=lambda _source, **_kw: (False, False),
    )
    assert service.detect_source_dynamic_hdr_presence(Path("/x.mkv"), stream_index=0, **common) == (True, False)
    assert service.detect_source_dynamic_hdr_presence(Path("/x.mkv"), stream_index=1, **common) == (False, False)
    tracks = [{"StreamOrder": "0", "HDR_Format": "Dolby Vision"}, {"StreamOrder": "1", "HDR_Format": ""}]
    assert select_mediainfo_video_track(tracks, 1) is tracks[1]
    assert select_mediainfo_video_track(tracks, 5) is None


def test_lot3_hdr_policy_defaults_and_warnings():
    from core.inspector import HDRType
    from core.workflows.encode.hdr_policy import default_static_hdr_checked, hdr_warnings

    assert default_static_hdr_checked(HDRType.HDR10, "smpte2084")
    assert not default_static_hdr_checked(HDRType.DOLBY_VISION, "arib-std-b67")  # D3 : P8.4
    assert not default_static_hdr_checked(HDRType.HLG, "arib-std-b67")
    assert not default_static_hdr_checked(HDRType.NONE, "bt709")
    tonemap_sdr = hdr_warnings(VideoEncodeSettings(codec="libx265", tonemap_to_sdr=True, source_color_transfer="bt709"))
    assert len(tonemap_sdr) == 1 and "source détectée SDR" in tonemap_sdr[0]  # D1
    dv_only = hdr_warnings(VideoEncodeSettings(codec="libx265", copy_dv=True, source_color_transfer="smpte2084"))
    assert len(dv_only) == 1 and "Dolby Vision sans HDR10 statique" in dv_only[0]  # D2
    assert hdr_warnings(VideoEncodeSettings(codec="libx265", copy_dv=True, inject_hdr_meta=True)) == []
    mistagged = hdr_warnings(VideoEncodeSettings(codec="libx265", inject_hdr_meta=True, source_color_transfer="bt709"))
    assert len(mistagged) == 1 and "HDR demandé sur une source détectée SDR" in mistagged[0]


def test_lot3_config_warnings_and_source_transfer_resolution(qt_app, tmp_path, monkeypatch):
    _ = qt_app
    (tmp_path / "a.mkv").write_bytes(b"")
    video = VideoEncodeSettings(codec="libx265", copy_dv=True, source_path=tmp_path / "a.mkv")
    cfg = EncodeConfig(source=tmp_path / "a.mkv", output=tmp_path / "o.mkv", video=video, duration_s=10)
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    monkeypatch.setattr(wf, "_ffprobe_streams_payload", lambda _src: {
        "streams": [{"index": 0, "codec_type": "video", "color_transfer": "smpte2084"}],
    })
    resolved = wf.resolve_source_color_transfer(cfg)
    assert resolved.video.source_color_transfer == "smpte2084"
    warnings = wf.config_warnings(resolved)
    assert warnings and warnings[0].startswith("Piste vidéo #1 — Dolby Vision sans HDR10 statique")


def test_lot3_v44_av1_nvenc_requires_ada_or_newer(monkeypatch):
    from core.workflows.encode.hardware import HardwareEncoderDetector

    detector = HardwareEncoderDetector()
    monkeypatch.setattr("core.workflows.encode.hardware.sys.platform", "linux")
    monkeypatch.setattr(HardwareEncoderDetector, "_nvidia_ok", staticmethod(lambda: True))
    compiled = {"hevc_nvenc", "av1_nvenc"}
    for caps, expected in (([8.6], {"hevc_nvenc"}), ([8.9], compiled), ([12.0], compiled), ([7.5, 8.9], compiled)):
        monkeypatch.setattr(HardwareEncoderDetector, "_nvidia_compute_capabilities", staticmethod(lambda c=caps: c))
        assert detector._detect_nvenc("ffmpeg", set(compiled)) == expected, caps
    # nvidia-smi sans compute_cap : sonde réelle 10 bits.
    monkeypatch.setattr(HardwareEncoderDetector, "_nvidia_compute_capabilities", staticmethod(lambda: []))
    probes: list[list[str]] = []
    monkeypatch.setattr(HardwareEncoderDetector, "_probe_encoder", staticmethod(lambda cmd: probes.append(cmd) or False))
    assert detector._detect_nvenc("ffmpeg", set(compiled)) == {"hevc_nvenc"}
    assert "format=p010le" in probes[0] and "av1_nvenc" in probes[0]


# ---------------------------------------------------------------------------
# Lot 4 — qualité, débit, taille cible
# ---------------------------------------------------------------------------


def _rc_args(codec: str, rate_control: str, bitrate_kbps: int = 8000, **kw) -> list[str]:
    video = VideoEncodeSettings(codec=codec, rate_control=rate_control, preset="", **kw)
    return video_codec_args(video, bitrate_kbps, callbacks=_CB)


def _after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_lot4_catalog_rate_controls_ranges_and_defaults():
    from core.workflows.encode.catalog import (
        default_preset_for_codec,
        presets_for_codec,
        rate_control_spec,
        rate_controls_for_codec,
        resolve_rate_control,
    )

    for codec in ("libx265", "hevc_nvenc", "hevc_amf", "hevc_qsv", "hevc_vaapi", "nvencc_hevc"):
        controls = rate_controls_for_codec(codec)
        assert controls and controls[-1].rc_id == "size"
        for spec in controls:
            low, high = spec.quality_range
            assert not spec.uses_quality or low <= spec.quality_default <= high
        assert default_preset_for_codec(codec) in presets_for_codec(codec)
    assert rate_control_spec("libsvtav1", "crf").quality_range == (0, 63)
    assert rate_control_spec("av1_nvenc", "constqp").quality_range == (0, 255)
    assert rate_control_spec("av1_vaapi", "cqp").quality_range == (0, 255)
    assert rate_control_spec("hevc_vaapi", "cqp").quality_range == (0, 52)
    assert [s.rc_id for s in rate_controls_for_codec("hevc_vaapi")] == [
        "cqp", "icq", "qvbr", "vbr", "cbr", "avbr", "size",
    ]
    assert rate_controls_for_codec("copy") == ()
    # Anciens modes → mode équivalent du codec.
    assert resolve_rate_control("hevc_nvenc", "", "crf").rc_id == "vbr_cq"
    assert resolve_rate_control("libx265", "", "cq").rc_id == "crf"
    assert resolve_rate_control("nvencc_hevc", "", "cq").rc_id == "qvbr"
    assert resolve_rate_control("hevc_vaapi", "", "bitrate").rc_id == "vbr"
    assert resolve_rate_control("libx265", "qvbr", "size").rc_id == "size"
    assert default_preset_for_codec("hevc_nvenc") == "p5"
    assert default_preset_for_codec("hevc_vaapi") == ""


def test_lot4_v19_v20_hw_rate_control_args():
    args = _rc_args("hevc_nvenc", "vbr_cq", cq=28)
    assert args[args.index("-rc:v"):args.index("-rc:v") + 6] == ["-rc:v", "vbr", "-b:v", "0", "-cq:v", "28"]
    args = _rc_args("av1_nvenc", "constqp", cq=120)
    assert _after(args, "-rc:v") == "constqp" and _after(args, "-qp") == "120"
    args = _rc_args("hevc_nvenc", "size", quality_mode=QualityMode.SIZE)
    assert _after(args, "-maxrate:v") == "12000k" and _after(args, "-bufsize:v") == "16000k"
    assert _after(args, "-multipass") == "fullres"
    args = _rc_args("hevc_amf", "cqp", cq=22)
    assert _after(args, "-qp_i") == "22" and "-qp_b" not in args
    assert "-qp_b" in _rc_args("h264_amf", "cqp")
    args = _rc_args("hevc_amf", "qvbr", cq=20)
    assert _after(args, "-rc") == "qvbr" and _after(args, "-qvbr_quality_level") == "20" and "-b:v" in args
    args = _rc_args("hevc_qsv", "cqp", cq=23)
    assert _after(args, "-global_quality") == "23" and "-b:v" not in args
    args = _rc_args("hevc_qsv", "cbr")
    assert _after(args, "-maxrate:v") == _after(args, "-b:v") == "8000k"
    args = _rc_args("hevc_vaapi", "icq", cq=30)
    assert _after(args, "-rc_mode") == "ICQ" and _after(args, "-global_quality") == "30"
    args = _rc_args("hevc_vaapi", "qvbr", cq=24)
    assert _after(args, "-rc_mode") == "QVBR" and _after(args, "-b:v") == "8000k"
    args = _rc_args("av1_vaapi", "cqp", cq=100)
    assert _after(args, "-global_quality") == "100" and "-qp" not in args
    # Valeur hors plage du mode : bornée.
    assert _after(_rc_args("hevc_vaapi", "cqp", cq=90), "-qp") == "52"


def test_lot4_preset_outside_codec_list_is_not_sent():
    """Preset hérité d'un autre codec : ni -compression_level (VAAPI), ni -quality (AMF), ni -preset (QSV)."""
    for codec, flag, preset in (
        ("hevc_vaapi", "-compression_level", "slow"), ("hevc_amf", "-quality", "slow"), ("hevc_qsv", "-preset", "p5"),
    ):
        video = VideoEncodeSettings(codec=codec, rate_control="cqp", preset=preset)
        assert flag not in video_codec_args(video, 8000, callbacks=_CB)
    video = VideoEncodeSettings(codec="hevc_amf", rate_control="cqp", preset="quality")
    assert _after(video_codec_args(video, 8000, callbacks=_CB), "-quality") == "quality"


def test_lot4_nvencc_rate_control_args():
    from core.workflows.encode.runtime.nvencc import build_nvencc_command

    def cmd(rate_control: str, **kw) -> list[str]:
        video = VideoEncodeSettings(codec="nvencc_hevc", rate_control=rate_control, **kw)
        return build_nvencc_command("nvencc", video, "out.mkv")

    assert _after(cmd("cqp", cq=50), "--cqp") == "50:51:51"
    assert _after(cmd("qvbr", cq=27), "--qvbr") == "27"
    vbr_q = cmd("vbr_quality", cq=25, bitrate_kbps=9000)
    assert _after(vbr_q, "--vbr") == "9000" and _after(vbr_q, "--vbr-quality") == "25"
    size = cmd("size", quality_mode=QualityMode.SIZE, bitrate_kbps=6000)
    assert _after(size, "--vbr") == "6000" and _after(size, "--max-bitrate") == "9000"


def test_lot4_legacy_settings_migrate_to_codec_rate_control(tmp_path):
    from core.workflows.encode import EncodePreset
    from core.workflows.encode.domain.codecs import rate_control_values

    spec, value = rate_control_values(VideoEncodeSettings(codec="hevc_nvenc", quality_mode=QualityMode.CRF, crf=20))
    assert (spec.rc_id, value) == ("vbr_cq", 20)
    spec, value = rate_control_values(VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.CQ, cq=24))
    assert (spec.rc_id, value) == ("crf", 24)
    # Mode explicite : famille synchronisée.
    video = VideoEncodeSettings(codec="hevc_vaapi", rate_control="qvbr")
    assert video.quality_mode == QualityMode.CQ
    from core.workflows.encode import ProfileManager

    manager = ProfileManager(tmp_path)
    manager.save(EncodePreset(name="p", codec="hevc_vaapi", quality_mode="cq", rate_control="icq", cq=31))
    restored = manager.load_all()[0].to_video_settings()
    assert (restored.rate_control, restored.quality_mode, restored.cq) == ("icq", QualityMode.CQ, 31)
    assert EncodePreset(name="s", codec="hevc_nvenc", preset="safe").preset == "p5"


def test_lot4_v33_two_pass_only_for_software_codecs(qt_app, tmp_path):
    from core.workflows.encode.domain import uses_two_pass_video

    _ = qt_app
    assert uses_two_pass_video(VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.SIZE))
    assert uses_two_pass_video(VideoEncodeSettings(codec="libsvtav1", quality_mode=QualityMode.SIZE))
    for codec in ("hevc_nvenc", "hevc_qsv", "hevc_vaapi", "hevc_amf", "nvencc_hevc"):
        assert not uses_two_pass_video(VideoEncodeSettings(codec=codec, quality_mode=QualityMode.SIZE))
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    video = VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.SIZE, target_size_mb=1000)
    cmds = wf.build_command(EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv",
                                         video=video, duration_s=3600))
    assert isinstance(cmds[0], list) and len(cmds) == 2


def _size_workflow(monkeypatch, streams: list[dict]) -> EncodeWorkflow:
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    monkeypatch.setattr(wf, "_ffprobe_streams_payload", lambda _src: {"streams": streams})
    return wf


def test_lot4_v07_size_budget_deducts_copied_streams(qt_app, tmp_path, monkeypatch):
    _ = qt_app
    streams = [
        {"index": 0, "codec_type": "video", "width": 3840, "height": 2160, "avg_frame_rate": "24/1"},
        {"index": 1, "codec_type": "audio", "tags": {"BPS": "4000000"}},
        {"index": 2, "codec_type": "subtitle", "bit_rate": "40000"},
    ]
    wf = _size_workflow(monkeypatch, streams)
    video = VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.SIZE, target_size_mb=4000)
    cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=video, duration_s=3600,
                       audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")], copy_subtitles=True)
    target_bps = 4000 * 8 * 1024 * 1024 / 3600
    expected_kbps = int((target_bps * 0.995 - 4_000_000 - 40_000) / 1000)
    assert abs(wf._size_to_bitrate_kbps(cfg) - expected_kbps) <= 1
    assert wf.size_target_errors(cfg) == []
    small = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", duration_s=3600,
                         video=VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.SIZE,
                                                   target_size_mb=1500),
                         audio_tracks=[AudioTrackSettings(stream_index=1, codec="copy")])
    errors = wf.size_target_errors(small)
    assert errors and "inatteignable" in errors[0]


def test_lot4_v07_size_budget_split_by_pixel_rate(qt_app, tmp_path, monkeypatch):
    _ = qt_app
    streams = [
        {"index": 0, "codec_type": "video", "width": 3840, "height": 2160, "avg_frame_rate": "24/1"},
        {"index": 1, "codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "24/1"},
    ]
    wf = _size_workflow(monkeypatch, streams)
    first = VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.SIZE, target_size_mb=5000,
                                stream_index=0, source_path=tmp_path / "s.mkv")
    second = VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.SIZE, target_size_mb=5000,
                                 stream_index=1, source_path=tmp_path / "s.mkv")
    cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=first,
                       video_tracks=[first, second], duration_s=3600)
    big = wf._size_to_bitrate_kbps_for_video(cfg, first)
    small = wf._size_to_bitrate_kbps_for_video(cfg, second)
    assert abs(big - 4 * small) <= 4


def test_lot4_vaapi_rate_controls_probed_from_driver(monkeypatch):
    import subprocess

    from core.workflows.encode.hardware import HardwareEncoderDetector

    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        stderr = "[hevc_vaapi] Driver does not support ICQ RC mode (supported modes: CQP, CBR, VBR, QVBR).\n"
        return subprocess.CompletedProcess(cmd, 1, "", stderr)

    detector = HardwareEncoderDetector()
    monkeypatch.setattr(detector, "_cached_vaapi_device", lambda: "/dev/dri/renderD128")
    monkeypatch.setattr("core.workflows.encode.hardware.subprocess.run", fake_run)
    assert detector._probe_vaapi_rate_controls("ffmpeg", "hevc_vaapi") == frozenset(
        {"cqp", "cbr", "vbr", "qvbr", "size"}
    )
    assert len(calls) == 1 and _after(calls[0], "-rc_mode") == "ICQ"


@pytest.mark.parametrize(("codec", "rate_control"), [
    ("hevc_amf", "qvbr"), ("hevc_vaapi", "qvbr"), ("nvencc_hevc", "vbr_quality"),
])
@pytest.mark.parametrize("bitrate", [0, -1])
def test_review_quality_with_bitrate_rejects_invalid_bitrate(codec, rate_control, bitrate):
    from core.workflows.encode.planning.validation import video_settings_errors

    video = VideoEncodeSettings(codec=codec, rate_control=rate_control, bitrate_kbps=bitrate)
    assert video_settings_errors([video]) == [
        "Piste vidéo #1 — débit vidéo invalide (kbps > 0 attendu).",
    ]
    video.bitrate_kbps = 8000
    assert video_settings_errors([video]) == []


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"])
@pytest.mark.parametrize("p5", [False, True])
def test_review_nvencc_tonemapped_pipe_is_sdr(transfer, p5):
    from core.workflows.encode.runtime.nvencc import build_nvencc_command, nvencc_pipe_encode_video

    source = VideoEncodeSettings(codec="nvencc_hevc", source_color_transfer=transfer,
                                 p5_to_hdr10=p5, tonemap_to_sdr=True)
    cmd = build_nvencc_command("nvencc", nvencc_pipe_encode_video(source), "out.mkv")
    assert "--transfer" not in cmd
    assert cmd[cmd.index("--output-depth") + 1] == "8"
    assert "--vpp-libplacebo-tonemapping" not in cmd and "--vpp-colorspace" not in cmd
    assert source.tonemap_to_sdr and source.p5_to_hdr10 == p5


def test_review_size_target_comes_from_sized_track_with_copy_primary(qt_app, tmp_path, monkeypatch):
    wf = _size_workflow(monkeypatch, [
        {"index": 0, "codec_type": "video", "bit_rate": "2000000"},
        {"index": 1, "codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "24/1"},
    ])
    copied = VideoEncodeSettings(codec="copy", stream_index=0)
    sized = VideoEncodeSettings(codec="libx265", stream_index=1, rate_control="size", target_size_mb=500)
    cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv",
                       video=copied, video_tracks=[copied, sized], duration_s=1000)
    expected = int((500 * 8 * 1024 * 1024 / 1000 * 0.995 - 2_000_000) / 1000)
    assert wf._size_to_bitrate_kbps_for_video(cfg, sized) == expected
    assert wf.size_target_errors(cfg) == []


def test_review_size_budget_reserves_fixed_bitrate_video(qt_app, tmp_path, monkeypatch):
    wf = _size_workflow(monkeypatch, [
        {"index": index, "codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "24/1"}
        for index in (0, 1)
    ])
    sized = VideoEncodeSettings(codec="libx265", stream_index=0, rate_control="size", target_size_mb=500)
    fixed = VideoEncodeSettings(codec="libx265", stream_index=1, rate_control="abr", bitrate_kbps=2000)
    cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv",
                       video=sized, video_tracks=[sized, fixed], duration_s=1000)
    expected = int((500 * 8 * 1024 * 1024 / 1000 * 0.995 - 2_000_000) / 1000)
    assert wf._size_to_bitrate_kbps_for_video(cfg, sized) == expected


def test_review_size_budget_accounts_for_each_track_duration(qt_app, tmp_path, monkeypatch):
    wf = _size_workflow(monkeypatch, [
        {"index": 0, "codec_type": "video", "width": 1920, "height": 1080,
         "avg_frame_rate": "24/1", "tags": {"DURATION": "00:16:40.000000000"}},
        {"index": 1, "codec_type": "video", "width": 1920, "height": 1080,
         "avg_frame_rate": "24/1", "duration": "500"},
        {"index": 2, "codec_type": "audio", "bit_rate": "2000000", "duration": "500"},
    ])
    videos = [VideoEncodeSettings(codec="libx265", stream_index=index, rate_control="size", target_size_mb=500)
              for index in (0, 1)]
    cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv",
                       video=videos[0], video_tracks=videos, duration_s=1000,
                       audio_tracks=[AudioTrackSettings(stream_index=2, codec="copy")])
    rates = [wf._size_to_bitrate_kbps_for_video(cfg, video) for video in videos]
    # Deux vidéos de même définition/cadence ont le même débit ; la courte occupe moins d'octets.
    assert abs(rates[0] - rates[1]) <= 1
    total_bits = rates[0] * 1000 * 1000 + rates[1] * 1000 * 500 + 2_000_000 * 500
    assert 0 <= 500 * 8 * 1024 * 1024 * 0.995 - total_bits < 1_500_000


def test_review_size_budget_marks_quality_track_estimate(qt_app, tmp_path, monkeypatch):
    wf = _size_workflow(monkeypatch, [
        {"index": 0, "codec_type": "video"},
        {"index": 1, "codec_type": "video", "bit_rate": "2000000"},
    ])
    sized = VideoEncodeSettings(codec="libx265", stream_index=0, rate_control="size", target_size_mb=500)
    quality = VideoEncodeSettings(codec="libx265", stream_index=1, rate_control="crf")
    cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv",
                       video=sized, video_tracks=[sized, quality], duration_s=1000)
    expected = int((500 * 8 * 1024 * 1024 / 1000 * 0.995 - 2_000_000) / 1000)
    assert wf._size_to_bitrate_kbps_for_video(cfg, sized) == expected
    assert any("estim" in message and "#2" in message for message in wf.size_target_warnings(cfg))


@pytest.mark.parametrize(("rate_control", "blocked"), [("crf", False), ("abr", True)])
def test_review_size_target_blocks_known_budget_not_quality_estimate(qt_app, tmp_path, monkeypatch,
                                                                    rate_control, blocked):
    wf = _size_workflow(monkeypatch, [
        {"index": 0, "codec_type": "video"},
        {"index": 1, "codec_type": "video", "bit_rate": "6000000"},
    ])
    sized = VideoEncodeSettings(codec="libx265", stream_index=0, rate_control="size", target_size_mb=500)
    other = VideoEncodeSettings(codec="libx265", stream_index=1, rate_control=rate_control, bitrate_kbps=6000)
    cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv",
                       video=sized, video_tracks=[sized, other], duration_s=1000)
    assert bool(wf.size_target_errors(cfg)) is blocked
    if not blocked:
        assert any("non garantie" in message for message in wf.size_target_warnings(cfg))


def test_review_size_warning_identifies_track_after_copied_primary(qt_app, tmp_path, monkeypatch):
    wf = _size_workflow(monkeypatch, [
        {"index": 0, "codec_type": "video", "bit_rate": "100000"},
        {"index": 1, "codec_type": "video"},
    ])
    copied = VideoEncodeSettings(codec="copy", stream_index=0)
    sized = VideoEncodeSettings(codec="libx265", stream_index=1, rate_control="size", target_size_mb=50)
    cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv",
                       video=copied, video_tracks=[copied, sized], duration_s=1000)
    warnings = wf.size_target_warnings(cfg)
    assert len(warnings) == 1 and "#2" in warnings[0]
