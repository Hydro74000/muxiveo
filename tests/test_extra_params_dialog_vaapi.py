"""Tests pour les schémas VAAPI du dialog extra_params (style ``ffmpeg_flags``)."""

from __future__ import annotations

import os

# QPA offscreen avant tout import Qt indirect via le module dialog.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.dialogs.extra_params_dialog import (  # noqa: E402
    _VAAPI_AV1,
    _VAAPI_H264,
    _VAAPI_HEVC,
    _parse_existing,
    _serialize,
    schema_for,
)


class TestSchemaRegistration:
    def test_three_codecs_registered(self):
        assert schema_for("hevc_vaapi") is _VAAPI_HEVC
        assert schema_for("h264_vaapi") is _VAAPI_H264
        assert schema_for("av1_vaapi") is _VAAPI_AV1

    def test_all_use_ffmpeg_flags_style(self):
        for schema in (_VAAPI_HEVC, _VAAPI_H264, _VAAPI_AV1):
            assert schema.style == "ffmpeg_flags"

    def test_common_vaapi_params_are_exposed(self):
        common_keys = {
            "g",
            "bf",
            "idr_interval",
            "b_depth",
            "rc_mode",
            "async_depth",
            "compression_level",
            "b",
            "maxrate",
            "bufsize",
            "rc_init_occupancy",
            "q",
            "qmin",
            "qmax",
            "i_qfactor",
            "i_qoffset",
            "b_qfactor",
            "b_qoffset",
            "blbrc",
            "max_frame_size",
            "slices",
            "low_power",
        }
        for schema in (_VAAPI_HEVC, _VAAPI_H264, _VAAPI_AV1):
            keys = {param.key for group in schema.groups for param in group.params}
            assert common_keys.issubset(keys)

    def test_qp_is_only_exposed_on_hevc_and_h264(self):
        hevc_keys = {param.key for group in _VAAPI_HEVC.groups for param in group.params}
        h264_keys = {param.key for group in _VAAPI_H264.groups for param in group.params}
        av1_keys = {param.key for group in _VAAPI_AV1.groups for param in group.params}
        assert "qp" in hevc_keys
        assert "qp" in h264_keys
        assert "qp" not in av1_keys

    def test_quality_is_h264_specific(self):
        h264_keys = {param.key for group in _VAAPI_H264.groups for param in group.params}
        hevc_keys = {param.key for group in _VAAPI_HEVC.groups for param in group.params}
        av1_keys = {param.key for group in _VAAPI_AV1.groups for param in group.params}
        assert "quality" in h264_keys
        assert "quality" not in hevc_keys
        assert "quality" not in av1_keys


class TestSerialize:
    def test_hevc_common_and_specific_flags(self):
        out = _serialize(_VAAPI_HEVC, {
            "g": (True, 240),
            "rc_mode": (True, "QVBR"),
            "async_depth": (True, 8),
            "compression_level": (True, 6),
            "b": (True, "18M"),
            "qp": (True, 23),
            "i_qoffset": (True, -0.8),
            "tiles": (True, "2x2"),
            "low_power": (True, True),
        })
        assert out == (
            "-g 240 -rc_mode QVBR -async_depth 8 -compression_level 6 -b 18M "
            "-qp 23 -i_qoffset -0.8 -tiles 2x2 -low_power 1"
        )

    def test_av1_common_flags_exclude_qp(self):
        out = _serialize(_VAAPI_AV1, {
            "rc_mode": (True, "ICQ"),
            "async_depth": (True, 4),
            "tile_groups": (True, 8),
        })
        assert out == "-rc_mode ICQ -async_depth 4 -tile_groups 8"


class TestParseExisting:
    def test_h264_parser_accepts_negative_numeric_values(self):
        parsed = _parse_existing(
            _VAAPI_H264,
            "-quality -1 -i_qoffset -0.8 -b_qoffset -1.25 -b 18M -async_depth 6",
        )
        assert parsed["quality"] == (True, "-1")
        assert parsed["i_qoffset"] == (True, "-0.8")
        assert parsed["b_qoffset"] == (True, "-1.25")
        assert parsed["b"] == (True, "18M")
        assert parsed["async_depth"] == (True, "6")

    def test_unknown_flags_are_preserved_in_free(self):
        parsed = _parse_existing(_VAAPI_HEVC, "-async_depth 5 -some-driver-flag yes")
        assert parsed["async_depth"] == (True, "5")
        assert parsed["__free__"] == (True, "-some-driver-flag yes")


class TestRoundTrip:
    def test_hevc_roundtrip_preserves_common_vaapi_flags(self):
        original = {
            "rc_mode": (True, "CQP"),
            "qp": (True, 19),
            "b": (True, "14M"),
            "i_qoffset": (True, -0.8),
            "b_qfactor": (True, 1.25),
            "slices": (True, 4),
            "__free__": (True, "-vendor_opt enabled"),
        }
        serialized = _serialize(_VAAPI_HEVC, original)
        parsed = _parse_existing(_VAAPI_HEVC, serialized)
        assert parsed["rc_mode"] == (True, "CQP")
        assert parsed["qp"] == (True, "19")
        assert parsed["b"] == (True, "14M")
        assert parsed["i_qoffset"] == (True, "-0.8")
        assert parsed["b_qfactor"] == (True, "1.25")
        assert parsed["slices"] == (True, "4")
        assert parsed["__free__"] == (True, "-vendor_opt enabled")
