from core.workflows.encode.catalog import (
    AudioCodecSpec,
    StaticHdrMetadataMode,
    VideoCodecFamily,
    VideoCodecSpec,
    audio_codec_spec,
    encoder_badge,
    is_h264_video_codec,
    is_hardware_video_codec,
    needs_static_hdr_bitstream_patch_codec,
    static_hdr_metadata_mode,
    supports_manual_static_hdr_metadata,
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

    def test_static_hdr_modes_follow_codec_capabilities(self):
        assert static_hdr_metadata_mode("libx265") is StaticHdrMetadataMode.X265_PARAMS
        assert static_hdr_metadata_mode("hevc_vaapi") is StaticHdrMetadataMode.VAAPI_SEI
        assert static_hdr_metadata_mode("hevc_nvenc") is StaticHdrMetadataMode.BITSTREAM_PATCH
        assert static_hdr_metadata_mode("hevc_amf") is StaticHdrMetadataMode.FRAME_SIDE_DATA
        assert static_hdr_metadata_mode("hevc_qsv") is StaticHdrMetadataMode.FRAME_SIDE_DATA
        assert static_hdr_metadata_mode("libsvtav1") is StaticHdrMetadataMode.NONE

    def test_only_nvenc_needs_static_hdr_bitstream_patch(self):
        assert needs_static_hdr_bitstream_patch_codec("hevc_nvenc") is True
        assert needs_static_hdr_bitstream_patch_codec("hevc_amf") is False
        assert needs_static_hdr_bitstream_patch_codec("hevc_qsv") is False
        assert needs_static_hdr_bitstream_patch_codec("libx265") is False
        assert needs_static_hdr_bitstream_patch_codec("hevc_vaapi") is False

    def test_manual_static_hdr_edit_is_reserved_for_codecs_with_real_path(self):
        assert supports_manual_static_hdr_metadata("libx265") is True
        assert supports_manual_static_hdr_metadata("hevc_nvenc") is True
        assert supports_manual_static_hdr_metadata("hevc_amf") is False
        assert supports_manual_static_hdr_metadata("hevc_qsv") is False
        assert supports_manual_static_hdr_metadata("hevc_vaapi") is False

    def test_nvencc_codecs_are_registered(self):
        from core.workflows.encode.catalog import (
            CQ_CAPABLE_VIDEO_CODECS,
            DYNAMIC_HDR_VIDEO_CODECS,
            H264_VIDEO_CODECS,
            HARDWARE_VIDEO_CODECS,
            NVENCC_VIDEO_CODECS,
            VIDEO_ENCODER_BADGES,
        )

        assert NVENCC_VIDEO_CODECS == frozenset({
            "nvencc_hevc", "nvencc_h264", "nvencc_av1",
        })
        ids_in_catalog = {cid for cid, _ in HARDWARE_VIDEO_CODECS}
        assert NVENCC_VIDEO_CODECS <= ids_in_catalog
        for codec in NVENCC_VIDEO_CODECS:
            assert VIDEO_ENCODER_BADGES[codec] == "NVEncC"
            assert codec in CQ_CAPABLE_VIDEO_CODECS
        assert "nvencc_h264" in H264_VIDEO_CODECS
        assert "nvencc_hevc" in DYNAMIC_HDR_VIDEO_CODECS
        assert "nvencc_av1" in DYNAMIC_HDR_VIDEO_CODECS
        # H.264 ne porte pas de HDR, on s'assure qu'il n'a pas glissé dedans.
        assert "nvencc_h264" not in DYNAMIC_HDR_VIDEO_CODECS

    def test_nvencc_specs_expose_family_and_capabilities(self):
        spec_hevc = video_codec_spec("nvencc_hevc")
        assert spec_hevc is not None
        assert spec_hevc.family is VideoCodecFamily.NVENCC
        assert spec_hevc.is_hardware is True
        assert spec_hevc.supports_dynamic_hdr is True
        assert spec_hevc.supports_10bit is True

        spec_h264 = video_codec_spec("nvencc_h264")
        assert spec_h264 is not None
        assert spec_h264.is_h264 is True
        assert spec_h264.supports_force_8bit is True

        spec_av1 = video_codec_spec("nvencc_av1")
        assert spec_av1 is not None
        assert spec_av1.supports_dynamic_hdr is True
        assert spec_av1.is_h264 is False


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
