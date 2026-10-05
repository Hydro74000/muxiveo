"""Éditeur de paramètres avancés, style flags ffmpeg : relecture et sérialisation."""

from __future__ import annotations

import shlex

from ui.dialogs.extra_params_dialog import _parse_existing, _serialize, schema_for


def _roundtrip(codec: str, text: str) -> list[str]:
    schema = schema_for(codec)
    assert schema is not None
    return shlex.split(_serialize(schema, _parse_existing(schema, text)))


def _pairs(tokens: list[str]) -> dict[str, str]:
    return {tokens[i]: tokens[i + 1] for i in range(0, len(tokens) - 1) if tokens[i].startswith("-")}


def test_v03_bool_value_following_flag_is_kept():
    tokens = _roundtrip("hevc_nvenc", "-spatial-aq 0")
    assert tokens == ["-spatial-aq", "0"]


def test_v03_bool_flag_without_value_means_enabled():
    tokens = _roundtrip("hevc_nvenc", "-spatial-aq -maxrate 80000k")
    assert _pairs(tokens)["-spatial-aq"] == "1"


def test_v04_nvenc_rates_are_written_in_kbps():
    assert _pairs(_roundtrip("hevc_nvenc", "-maxrate 80000k"))["-maxrate"] == "80000k"
    assert _pairs(_roundtrip("hevc_nvenc", "-bufsize 8M"))["-bufsize"] == "8000k"
    # nombre nu = bit/s, comme ffmpeg le lit
    assert _pairs(_roundtrip("hevc_nvenc", "-maxrate 80000"))["-maxrate"] == "80k"


def test_v34_quoted_values_survive_roundtrip():
    tokens = _roundtrip("libx264", '-metadata title="Mon film" -x264-params "aq-mode=3:psy-rd=1.0,0.15"')
    assert tokens[tokens.index("-metadata") + 1] == "title=Mon film"
    assert tokens[tokens.index("-x264-params") + 1] == "aq-mode=3:psy-rd=1.0,0.15"


def test_v35_amf_and_qsv_option_names_match_ffmpeg():
    def keys(codec: str) -> set[str]:
        schema = schema_for(codec)
        assert schema is not None
        return {spec.key for group in schema.groups for spec in group.params}

    assert "profile_tier" in keys("hevc_amf") and "tier" not in keys("hevc_amf")
    assert "look_ahead" not in keys("hevc_qsv")
    assert "look_ahead" in keys("h264_qsv")
