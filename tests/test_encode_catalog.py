from core.workflows.encode.catalog import (
    AudioCodecSpec,
    VideoCodecFamily,
    VideoCodecSpec,
    audio_codec_spec,
    encoder_badge,
    is_h264_video_codec,
    is_hardware_video_codec,
    supports_dynamic_hdr,
    supports_force_8bit,
    video_codec_family,
    video_codec_spec,
)


class TestVideoCodecSpecs:
    def test_libx264_spec_exposes_family_and_force_8bit(self):
        spec = video_codec_spec("libx264")
        assert isinstance(spec, VideoCodecSpec)
        assert spec.family is VideoCodecFamily.SOFTWARE
        assert spec.is_h264 is True
        assert spec.supports_force_8bit is True

    def test_hevc_nvenc_spec_exposes_hardware_family_and_hdr(self):
        spec = video_codec_spec("hevc_nvenc")
        assert isinstance(spec, VideoCodecSpec)
        assert spec.family is VideoCodecFamily.NVENC
        assert spec.is_hardware is True
        assert spec.supports_dynamic_hdr is True

    def test_unknown_video_codec_family_falls_back_to_other(self):
        assert video_codec_spec("unknown_codec") is None
        assert video_codec_family("unknown_codec") is VideoCodecFamily.OTHER

    def test_catalog_helpers_dispatch_from_specs(self):
        assert is_h264_video_codec("h264_qsv") is True
        assert is_hardware_video_codec("h264_qsv") is True
        assert supports_force_8bit("h264_qsv") is True
        assert supports_dynamic_hdr("hevc_qsv") is True
        assert encoder_badge("hevc_qsv") == "QSV"

    def test_copy_keeps_dynamic_hdr_passthrough_capability(self):
        assert supports_dynamic_hdr("copy") is True


class TestAudioCodecSpecs:
    def test_copy_audio_spec_is_passthrough(self):
        spec = audio_codec_spec("copy")
        assert isinstance(spec, AudioCodecSpec)
        assert spec.passthrough is True
        assert spec.supports_bitrate is False
        assert spec.supports_truehd_core_bsf is True

    def test_flac_audio_spec_is_lossless(self):
        spec = audio_codec_spec("flac")
        assert isinstance(spec, AudioCodecSpec)
        assert spec.lossless is True
        assert spec.supports_bitrate is False
