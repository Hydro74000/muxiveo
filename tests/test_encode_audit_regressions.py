"""Non-régressions de l'audit encode (ordre des options FFmpeg, HDR10 statique,
entrées brutes, VAAPI, cadence NVEncC)."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import cast

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
    # encodeurs « side data » sans valeurs éditables : la source passe telle quelle
    assert not strips_source_static_hdr_side_data(_hdr_video("hevc_qsv"))
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
    video = VideoEncodeSettings(codec="hevc_vaapi")
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
    # -pix_fmt : le workflow n'en pose pas (case 10-Bits décochée), pas de remplacement.
    assert report.overriding == ("-cq", "30", "-preset", "p1")
    ten_bit = VideoEncodeSettings(**{**video.__dict__, "force_10bit": True})
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
