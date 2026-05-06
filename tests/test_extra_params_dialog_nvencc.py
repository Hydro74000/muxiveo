"""Tests pour les schémas NVEncC du dialog extra_params (style ``nvencc_flags``)."""

from __future__ import annotations

import os

import pytest

# QPA offscreen avant tout import Qt indirect via le module dialog.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.dialogs.extra_params_dialog import (  # noqa: E402
    _NVENCC_AV1,
    _NVENCC_H264,
    _NVENCC_HEVC,
    _parse_existing,
    _serialize,
    schema_for,
)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

class TestSchemaRegistration:
    def test_three_codecs_registered(self):
        assert schema_for("nvencc_hevc") is _NVENCC_HEVC
        assert schema_for("nvencc_h264") is _NVENCC_H264
        assert schema_for("nvencc_av1") is _NVENCC_AV1

    def test_all_use_nvencc_flags_style(self):
        for sch in (_NVENCC_HEVC, _NVENCC_H264, _NVENCC_AV1):
            assert sch.style == "nvencc_flags"

    def test_h264_has_no_hdr_group(self):
        titles = {g.title for g in _NVENCC_H264.groups}
        assert "HDR / Color" not in titles
        assert "H.264-specific" in titles

    def test_hevc_has_hdr_and_hevc_specific(self):
        titles = {g.title for g in _NVENCC_HEVC.groups}
        assert "HDR / Color" in titles
        assert "HEVC-specific" in titles

    def test_av1_has_hdr_and_av1_specific(self):
        titles = {g.title for g in _NVENCC_AV1.groups}
        assert "HDR / Color" in titles
        assert "AV1-specific" in titles

    def test_av1_has_no_multiref_l0_l1(self):
        keys = {p.key for g in _NVENCC_AV1.groups for p in g.params}
        assert "multiref-l0" not in keys
        assert "multiref-l1" not in keys
        # HEVC/H.264 doivent les avoir.
        for sch in (_NVENCC_HEVC, _NVENCC_H264):
            keys_other = {p.key for g in sch.groups for p in g.params}
            assert "multiref-l0" in keys_other
            assert "multiref-l1" in keys_other

    def test_workflow_owned_hdr_keys_are_hidden_from_structured_schema(self):
        keys = {p.key for g in _NVENCC_HEVC.groups for p in g.params}
        for hidden in (
            "colormatrix",
            "colorprim",
            "transfer",
            "chromaloc",
            "master-display",
            "max-cll",
            "dhdr10-info",
            "dolby-vision-profile",
            "dolby-vision-rpu",
            "vpp-colorspace",
            "vpp-libplacebo-tonemapping",
            "vpp-libplacebo-tonemapping-lut",
        ):
            assert hidden not in keys


# ---------------------------------------------------------------------------
# Sérialisation
# ---------------------------------------------------------------------------

class TestSerialize:
    def test_empty_returns_empty_string(self):
        out = _serialize(_NVENCC_HEVC, {})
        assert out == ""

    def test_int_param(self):
        out = _serialize(_NVENCC_HEVC, {"aq-strength": (True, 12)})
        assert out == "--aq-strength 12"

    def test_disabled_param_is_skipped(self):
        out = _serialize(_NVENCC_HEVC, {"aq-strength": (False, 12)})
        assert out == ""

    def test_bool_flag_alone_when_on(self):
        # --aq utilise bool_repr=(None, "") → flag seul si activé.
        out = _serialize(_NVENCC_HEVC, {"aq": (True, True)})
        assert out == "--aq"

    def test_bool_flag_alone_off_emits_nothing(self):
        out = _serialize(_NVENCC_HEVC, {"aq": (True, False)})
        assert out == ""

    def test_bool_with_no_variant_on(self):
        # H.264 --deblock / --no-deblock pair.
        out = _serialize(_NVENCC_H264, {"deblock": (True, True)})
        assert out == "--deblock"

    def test_bool_with_no_variant_off(self):
        out = _serialize(_NVENCC_H264, {"deblock": (True, False)})
        assert out == "--no-deblock"

    def test_enum_param(self):
        out = _serialize(_NVENCC_HEVC, {"multipass": (True, "2pass-full")})
        assert out == "--multipass 2pass-full"

    def test_text_param_passthrough(self):
        out = _serialize(_NVENCC_HEVC, {"vpp-knn": (True, "radius=4,strength=0.08")})
        assert out == "--vpp-knn radius=4,strength=0.08"

    def test_combined_flags(self):
        out = _serialize(_NVENCC_HEVC, {
            "aq": (True, True),
            "aq-temporal": (True, True),
            "aq-strength": (True, 12),
            "multipass": (True, "2pass-full"),
            "lookahead": (True, 32),
        })
        # L'ordre suit l'ordre des groupes dans le schéma.
        assert "--aq" in out
        assert "--aq-temporal" in out
        assert "--aq-strength 12" in out
        assert "--multipass 2pass-full" in out
        assert "--lookahead 32" in out

    def test_free_tokens_appended(self):
        out = _serialize(_NVENCC_HEVC, {
            "aq": (True, True),
            "__free__": (True, "--option-file /tmp/opts.txt"),
        })
        assert out.endswith("--option-file /tmp/opts.txt")

    @pytest.mark.parametrize("key", ["cqp", "qp-init", "qp-min", "qp-max"])
    def test_qp_triplet_single_value_is_normalized(self, key):
        out = _serialize(_NVENCC_HEVC, {key: (True, "18")})
        assert out == f"--{key} 18:18:18"

    @pytest.mark.parametrize("key", ["cqp", "qp-init", "qp-min", "qp-max"])
    def test_qp_triplet_full_value_is_preserved(self, key):
        out = _serialize(_NVENCC_HEVC, {key: (True, "18:20:22")})
        assert out == f"--{key} 18:20:22"

    def test_parallel_all_is_normalized_to_auto(self):
        out = _serialize(_NVENCC_HEVC, {"parallel": (True, "all")})
        assert out == "--parallel auto"

    def test_split_enc_enum_serializes(self):
        out = _serialize(_NVENCC_HEVC, {"split-enc": (True, "forced_2")})
        assert out == "--split-enc forced_2"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParseExisting:
    def test_empty_returns_empty(self):
        assert _parse_existing(_NVENCC_HEVC, "") == {}
        assert _parse_existing(_NVENCC_HEVC, "   ") == {}

    def test_int_value(self):
        v = _parse_existing(_NVENCC_HEVC, "--aq-strength 12")
        assert v["aq-strength"] == (True, "12")

    def test_bool_flag_alone(self):
        v = _parse_existing(_NVENCC_HEVC, "--aq")
        assert v["aq"] == (True, True)

    def test_bool_no_variant_off(self):
        v = _parse_existing(_NVENCC_H264, "--no-deblock")
        assert v["deblock"] == (True, False)

    def test_bool_no_variant_on(self):
        v = _parse_existing(_NVENCC_H264, "--deblock")
        assert v["deblock"] == (True, True)

    def test_enum_value(self):
        v = _parse_existing(_NVENCC_HEVC, "--multipass 2pass-full")
        assert v["multipass"] == (True, "2pass-full")

    def test_text_with_commas(self):
        v = _parse_existing(_NVENCC_HEVC, "--vpp-knn radius=4,strength=0.08")
        assert v["vpp-knn"] == (True, "radius=4,strength=0.08")

    def test_parse_single_value_cqp_and_reemit_normalized(self):
        parsed = _parse_existing(_NVENCC_HEVC, "--cqp 18")
        assert parsed["cqp"] == (True, "18")
        assert _serialize(_NVENCC_HEVC, parsed) == "--cqp 18:18:18"

    def test_parse_parallel_all_and_reemit_auto(self):
        parsed = _parse_existing(_NVENCC_HEVC, "--parallel all")
        assert parsed["parallel"] == (True, "all")
        assert _serialize(_NVENCC_HEVC, parsed) == "--parallel auto"

    def test_workflow_owned_hdr_flags_are_ignored_in_parser(self):
        md = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
        parsed = _parse_existing(
            _NVENCC_HEVC,
            f'--master-display "{md}" --vpp-colorspace "matrix=bt2020nc:bt709,hdr2sdr=hable"',
        )
        assert parsed == {}

    def test_unknown_flag_goes_to_free(self):
        v = _parse_existing(_NVENCC_HEVC, "--unknown-flag value-x")
        assert "__free__" in v
        assert "--unknown-flag" in v["__free__"][1]

    def test_combined(self):
        v = _parse_existing(
            _NVENCC_HEVC,
            "--aq --aq-temporal --aq-strength 12 --multipass 2pass-full --lookahead 32",
        )
        assert v["aq"] == (True, True)
        assert v["aq-temporal"] == (True, True)
        assert v["aq-strength"] == (True, "12")
        assert v["multipass"] == (True, "2pass-full")
        assert v["lookahead"] == (True, "32")


# ---------------------------------------------------------------------------
# Round-trip serialize → parse → serialize
# ---------------------------------------------------------------------------

class TestRoundTrip:
    @pytest.mark.parametrize("schema", [_NVENCC_HEVC, _NVENCC_H264, _NVENCC_AV1])
    def test_basic_combo_roundtrip(self, schema):
        original = {
            "aq": (True, True),
            "aq-strength": (True, 12),
            "multipass": (True, "2pass-full"),
            "lookahead": (True, 32),
        }
        serialized = _serialize(schema, original)
        parsed = _parse_existing(schema, serialized)
        # Toutes les clés cochées doivent être retrouvées.
        for key, (enabled, _val) in original.items():
            assert key in parsed
            assert parsed[key][0] is enabled

    def test_h264_no_deblock_roundtrip(self):
        serialized = _serialize(_NVENCC_H264, {"deblock": (True, False)})
        assert serialized == "--no-deblock"
        parsed = _parse_existing(_NVENCC_H264, serialized)
        assert parsed["deblock"] == (True, False)

    def test_qp_triplet_roundtrip_normalizes_to_canonical_form(self):
        serialized = _serialize(_NVENCC_HEVC, {"qp-max": (True, "18")})
        assert serialized == "--qp-max 18:18:18"
        parsed = _parse_existing(_NVENCC_HEVC, serialized)
        assert parsed["qp-max"] == (True, "18:18:18")

    def test_vpp_knn_compound_roundtrip(self):
        val = "radius=4,strength=0.08,lerp=0.2"
        serialized = _serialize(_NVENCC_HEVC, {"vpp-knn": (True, val)})
        parsed = _parse_existing(_NVENCC_HEVC, serialized)
        assert parsed["vpp-knn"] == (True, val)

    def test_free_tokens_preserved(self):
        original = {
            "aq": (True, True),
            "__free__": (True, "--input-option fflags=+genpts"),
        }
        serialized = _serialize(_NVENCC_HEVC, original)
        # Le free passe directement (les tokens n'ont pas de spec connue).
        assert "--input-option" in serialized
        parsed = _parse_existing(_NVENCC_HEVC, serialized)
        assert parsed["aq"] == (True, True)
        # Les tokens inconnus sont remontés dans __free__ du parser.
        assert "__free__" in parsed
        assert "--input-option" in parsed["__free__"][1]
