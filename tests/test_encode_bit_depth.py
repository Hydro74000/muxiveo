"""Lot 6 : résolution, migration et contrats matériels de profondeur."""
from dataclasses import replace
from unittest.mock import patch

import pytest

from core.workflows.encode.catalog import VIDEO_CODEC_SPECS
from core.workflows.encode.domain.codecs import (
    EncodeCodecDomainCallbacks, bit_depth_error, build_encoder_vf, depth_args,
    hardware_input_args, resolve_output_bit_depth, source_bit_depth_from_stream,
    supports_output_10bit, video_codec_args,
    ffmpeg_extra_params_report,
)
from core.workflows.encode.models import EncodePreset, VideoEncodeSettings
from core.workflows.encode.profiles import ProfileManager
from core.workflows.encode.runtime.nvencc import (
    _cached_10bit_codecs, _parse_10bit_codecs, build_nvencc_command,
    detect_nvencc_10bit_codecs, nvencc_pipe_encode_video,
    nvencc_extra_params_report,
)


@pytest.mark.parametrize("codec", list(VIDEO_CODEC_SPECS))
@pytest.mark.parametrize("source", [0, 8, 10, 12])
@pytest.mark.parametrize("choice", ["auto", "8", "10"])
def test_lot6_resolver_matrix(codec, source, choice):
    video = VideoEncodeSettings(codec=codec, source_bit_depth=source, bit_depth=choice)
    expected = source if codec == "copy" else int(choice) if choice != "auto" else 10 if source > 8 and supports_output_10bit(video) else 8
    assert resolve_output_bit_depth(video) == expected
    assert bool(bit_depth_error(video)) == (codec != "copy" and expected == 10 and not supports_output_10bit(video))


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"])
@pytest.mark.parametrize("codec", ["libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_vaapi", "libsvtav1", "nvencc_hevc", "nvencc_av1"])
def test_lot6_hdr_requires_10bit(codec, transfer):
    video = VideoEncodeSettings(codec=codec, source_color_transfer=transfer, source_bit_depth=8)
    assert resolve_output_bit_depth(video) == 10
    assert not bit_depth_error(video)
    assert "HDR" in bit_depth_error(replace(video, bit_depth="8"))
    assert resolve_output_bit_depth(replace(video, tonemap_to_sdr=True)) == 8


@pytest.mark.parametrize("model", [VideoEncodeSettings, EncodePreset])
def test_lot6_legacy_flags_and_explicit_auto(model):
    assert model().bit_depth == "auto"
    assert model(force_10bit=True).bit_depth == "10"
    assert model(force_8bit=True, force_10bit=True).bit_depth == "8"
    legacy = model(force_10bit=True)
    assert replace(legacy, bit_depth="auto").bit_depth == "auto"
    assert model(bit_depth="8", force_10bit=True).bit_depth == "8"


@pytest.mark.parametrize("depth", ["auto", "8", "10"])
def test_lot6_presets_round_trip(tmp_path, depth):
    manager = ProfileManager(tmp_path)
    preset = EncodePreset(name="depth", bit_depth=depth)
    manager.save(preset)
    assert "force_10bit" not in preset.to_json_dict()
    assert "force_8bit" not in preset.to_json_dict()
    assert manager.load_all()[0].to_video_settings().bit_depth == depth


@pytest.mark.parametrize("codec,filter_name", [("hevc_nvenc", "scale_cuda"), ("hevc_vaapi", "scale_vaapi")])
def test_lot6_gpu_decode_preserved_and_conversion_fallback(codec, filter_name):
    cb = EncodeCodecDomainCallbacks(platform="linux", vaapi_device="/dev/dri/renderD128")
    same = VideoEncodeSettings(codec=codec, bit_depth="10", source_bit_depth=10)
    assert "-hwaccel" in hardware_input_args(same, callbacks=cb)
    assert "hwupload" not in build_encoder_vf(same, callbacks=cb)
    assert "-pix_fmt" not in video_codec_args(same, 5000, callbacks=cb)
    conversion = replace(same, source_bit_depth=8)
    assert "-hwaccel" not in hardware_input_args(conversion, callbacks=cb)
    gpu_cb = replace(cb, depth_conversion_filters=frozenset({filter_name}))
    assert "-hwaccel" in hardware_input_args(conversion, callbacks=gpu_cb)
    assert f"{filter_name}=format=p010le" in build_encoder_vf(conversion, callbacks=gpu_cb)
    assert "-pix_fmt" not in video_codec_args(conversion, 5000, callbacks=gpu_cb)
    assert "-hwaccel" not in hardware_input_args(conversion, callbacks=gpu_cb, piped_frames=True)


def test_lot6_qsv_depth_conversion_stays_in_software():
    """vpp_qsv format= échoue (UHD 630, Windows) : conversion logicielle, même si le filtre existe."""
    cb = EncodeCodecDomainCallbacks(platform="win32", qsv_device="1", depth_conversion_filters=frozenset({"vpp_qsv"}))
    conversion = VideoEncodeSettings(codec="hevc_qsv", bit_depth="8", source_bit_depth=10, source_pix_fmt="yuv420p10le")
    assert "-hwaccel" not in hardware_input_args(conversion, callbacks=cb)
    assert "vpp_qsv" not in build_encoder_vf(conversion, callbacks=cb)
    assert "-pix_fmt" in video_codec_args(conversion, 5000, callbacks=cb)


@pytest.mark.parametrize("codec", ["hevc_vaapi", "hevc_amf"])
@pytest.mark.parametrize("depth,fmt", [("8", "nv12"), ("10", "p010")])
def test_lot6_upload_uses_target_depth(codec, depth, fmt):
    cb = EncodeCodecDomainCallbacks(platform="win32", vaapi_device="gpu", amf_device="0")
    video = VideoEncodeSettings(codec=codec, bit_depth=depth, source_bit_depth=10)
    assert build_encoder_vf(video, callbacks=cb, piped_frames=True).endswith(f"format={fmt},hwupload")
    assert "-pix_fmt" not in video_codec_args(video, 5000, callbacks=cb)


def test_lot6_x264_high10_and_tonemapping_conversion():
    video = VideoEncodeSettings(codec="libx264", bit_depth="10", source_bit_depth=10)
    assert depth_args(video) == ["-profile:v", "high10"]
    assert depth_args(replace(video, source_bit_depth=8)) == ["-pix_fmt", "yuv420p10le", "-profile:v", "high10"]
    assert "yuv420p10le" in depth_args(replace(video, tonemap_to_sdr=True))


def test_lot6_high10_source_falls_back_before_gpu_filter():
    cb = EncodeCodecDomainCallbacks(platform="linux", vaapi_device="gpu", depth_conversion_filters=frozenset({"scale_cuda", "scale_vaapi"}))
    for codec in ("hevc_nvenc", "h264_nvenc", "hevc_vaapi"):
        video = VideoEncodeSettings(codec=codec, bit_depth="8", source_bit_depth=10, source_codec="h264")
        assert "-hwaccel" not in hardware_input_args(video, callbacks=cb)
        assert "scale_cuda" not in build_encoder_vf(video, callbacks=cb)
        assert "scale_vaapi" not in build_encoder_vf(video, callbacks=cb)


@pytest.mark.parametrize("tonemap", [False, True])
def test_lot6_nvencc_pipe_preserves_resolved_target(tonemap):
    video = VideoEncodeSettings(codec="nvencc_hevc", source_bit_depth=10, p5_to_hdr10=True, tonemap_to_sdr=tonemap)
    piped = nvencc_pipe_encode_video(video)
    assert piped.bit_depth == "10"
    assert piped.source_bit_depth == (8 if tonemap else 10)
    assert not piped.p5_to_hdr10 and not piped.tonemap_to_sdr
    cmd = build_nvencc_command("nvencc", piped, "out.mkv")
    assert cmd[cmd.index("--output-depth") + 1] == "10"


def test_lot6_nvencc_h264_capability_is_codec_specific():
    output = "NVEnc features\nCodec: H.264/AVC\n10bit depth no\nCodec: H.265/HEVC\n10bit depth yes\nNVDec features\nCodec: H.264/AVC\n10bit depth yes"
    assert _parse_10bit_codecs(output) == frozenset({"nvencc_hevc"})
    assert _parse_10bit_codecs(output.replace("10bit depth no", "10bit depth yes")) == frozenset({"nvencc_hevc", "nvencc_h264"})
    video = VideoEncodeSettings(codec="nvencc_h264", source_bit_depth=10)
    assert resolve_output_bit_depth(video) == 8
    assert resolve_output_bit_depth(replace(video, encoder_supports_10bit=True)) == 10
    assert bit_depth_error(replace(video, bit_depth="10"))


def test_lot6_extra_depth_guards_and_override_warning():
    cb = EncodeCodecDomainCallbacks(platform="linux")
    hdr = VideoEncodeSettings(codec="hevc_nvenc", source_bit_depth=10, source_color_transfer="smpte2084", extra_params="-pix_fmt yuv420p")
    assert ffmpeg_extra_params_report(hdr, callbacks=cb).removed == ("-pix_fmt", "yuv420p")
    sdr = replace(hdr, source_color_transfer="bt709", extra_params="-pix_fmt yuv420p10le")
    assert ffmpeg_extra_params_report(sdr, callbacks=cb).overriding == ("-pix_fmt", "yuv420p10le")
    assert "-hwaccel" not in hardware_input_args(sdr, callbacks=cb)
    h264 = VideoEncodeSettings(codec="nvencc_h264", extra_params="--output-depth 10")
    assert nvencc_extra_params_report(h264).removed == ("--output-depth", "10")
    assert not nvencc_extra_params_report(replace(h264, encoder_supports_10bit=True)).removed
    vaapi = replace(sdr, codec="hevc_vaapi")
    assert ffmpeg_extra_params_report(vaapi, callbacks=cb).removed == ("-pix_fmt", "yuv420p10le")


def test_lot6_nvencc_probe_failure_and_binary_cache(tmp_path):
    binary = tmp_path / "nvencc"
    binary.write_bytes(b"one")
    _cached_10bit_codecs.cache_clear()
    with patch("core.workflows.encode.runtime.nvencc._run_nvencc_probe", return_value=None) as probe:
        assert detect_nvencc_10bit_codecs(str(binary)) == frozenset()
        assert detect_nvencc_10bit_codecs(str(binary)) == frozenset()
        assert probe.call_count == 1
        binary.write_bytes(b"changed")
        detect_nvencc_10bit_codecs(str(binary))
        assert probe.call_count == 2


@pytest.mark.parametrize("fmt,expected", [("yuv420p", 8), ("yuv420p10le", 10), ("p010le", 10), ("yuv444p12le", 12), ("cuda", 0), ("", 0)])
def test_lot6_source_depth_from_ffprobe(fmt, expected):
    assert source_bit_depth_from_stream({"pix_fmt": fmt}) == expected


def test_lot6_workflow_resolves_depth_and_hdr_before_validation(qt_app, tmp_path, monkeypatch):
    from core.workflows.encode import EncodeConfig, EncodeWorkflow

    source = tmp_path / "source.mkv"
    source.touch()
    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    monkeypatch.setattr(wf, "_ffprobe_streams_payload", lambda source: {"streams": [
        {"index": 0, "codec_type": "video", "pix_fmt": "yuv420p10le", "color_transfer": "bt709"},
        {"index": 1, "codec_type": "video", "pix_fmt": "yuv420p10le", "color_transfer": "smpte2084"},
    ]})
    tracks = [VideoEncodeSettings(stream_index=0), VideoEncodeSettings(stream_index=1, bit_depth="8")]
    config = EncodeConfig(source=source, output=tmp_path / "out.mkv", video=tracks[0], video_tracks=tracks, mux_backend="ffmpeg")
    resolved = wf.resolve_source_color_transfer(config)
    assert [v.source_bit_depth for v in resolved.video_tracks] == [10, 10]
    assert resolve_output_bit_depth(resolved.video_tracks[0]) == 10
    assert any("#2" in error and "HDR" in error for error in wf.validate(config))


def test_lot6_copy_depth_never_requires_a_probe(qt_app, tmp_path, monkeypatch):
    from core.workflows.encode import EncodeConfig, EncodeWorkflow

    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", generate_nfo=False)
    def unexpected_probe(*args):
        pytest.fail("Copy ne doit pas sonder ni convertir la profondeur")
    monkeypatch.setattr(wf, "_ffprobe_streams_payload", unexpected_probe)
    config = EncodeConfig(source=tmp_path / "source.mkv", output=tmp_path / "out.mkv", video=VideoEncodeSettings(codec="copy", bit_depth="10"))
    assert wf.resolve_source_color_transfer(config) is config


@pytest.mark.parametrize("codec,expected", [
    ("libx264", ["-pix_fmt", "yuv420p10le", "-profile:v", "high10"]),
    ("libx265", ["-pix_fmt", "yuv420p10le"]),
    ("hevc_nvenc", ["-pix_fmt", "p010le", "-profile:v", "main10"]),
])
@pytest.mark.parametrize("fmt", ["yuv422p10le", "yuv444p10le"])
def test_lot6_non_420_source_is_converted_to_420(codec, expected, fmt):
    """Même profondeur, autre sous-échantillonnage : 4:2:0 explicite (High10, Main10 matériel, TV)."""
    video = VideoEncodeSettings(codec=codec, bit_depth="auto", source_bit_depth=10, source_pix_fmt=fmt)
    assert depth_args(video) == expected
    # Source 4:2:0 de même profondeur : aucune conversion demandée.
    same = VideoEncodeSettings(codec=codec, bit_depth="auto", source_bit_depth=10, source_pix_fmt="yuv420p10le")
    assert "-pix_fmt" not in depth_args(same)


def test_lot6_non_420_source_is_decoded_in_software():
    cb = EncodeCodecDomainCallbacks(platform="linux", depth_conversion_filters=frozenset({"scale_cuda"}))
    video = VideoEncodeSettings(codec="hevc_nvenc", bit_depth="8", source_bit_depth=10,
                                source_codec="prores", source_pix_fmt="yuv422p10le")
    assert "-hwaccel" not in hardware_input_args(video, callbacks=cb)
    assert "-pix_fmt" in depth_args(video)
