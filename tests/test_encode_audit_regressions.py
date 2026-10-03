"""Non-régressions de l'audit encode (ordre des options FFmpeg, HDR10 statique,
entrées brutes, VAAPI, cadence NVEncC)."""

from __future__ import annotations

from pathlib import Path

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
        for cmd in built if isinstance(built[0], list) else [built]:
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
    payload = {"streams": [
        {"index": 0, "codec_type": "video", "avg_frame_rate": "25/1"},
        {"index": 1, "codec_type": "audio"},
        {"index": 2, "codec_type": "video", "avg_frame_rate": "24000/1001"},
    ]}
    probe = dict(ffprobe_streams_payload=lambda _p: payload, ffprobe_stream_dicts=lambda p: p["streams"],
                 mediainfo_fps_expr=lambda _p: None)
    assert source_video_fps_expr(Path("s.mkv"), stream_index=2, **probe) == "24000/1001"
    assert source_video_fps_expr(Path("s.mkv"), **probe) == "25/1"
    # index introuvable (flux brut) : premier flux vidéo
    assert source_video_fps_expr(Path("s.hevc"), stream_index=7, **probe) == "25/1"


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
    assert calls["calibration"] == offset.calibration
    assert str(rewritten) in cmd and "-itsoffset" not in cmd
