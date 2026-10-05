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


def test_options_set_by_video_tab_stay_editable_and_prefilled(qt_app):
    """Arbitrage lot 2 : seules les options incompatibles sont grisées ; les autres
    sont pré-remplies avec la valeur de l'onglet Video et modifiables."""
    from PySide6.QtWidgets import QCheckBox, QSpinBox

    from ui.dialogs.extra_params_dialog import ExtraParamsDialog, option_owned_by_workflow

    _ = qt_app
    assert not option_owned_by_workflow("hevc_vaapi", "qp")
    assert not option_owned_by_workflow("hevc_vaapi", "compression_level")
    assert not option_owned_by_workflow("nvencc_hevc", "preset")
    assert not option_owned_by_workflow("nvencc_hevc", "cqp")
    assert option_owned_by_workflow("nvencc_hevc", "vpp-resize")
    dialog = ExtraParamsDialog("hevc_vaapi", "", workflow_values={"qp": "24", "rc_mode": "CQP"})
    row = dialog._rows["qp"]
    checkbox = row.findChild(QCheckBox)
    assert checkbox is not None and checkbox.isEnabled() and not checkbox.isChecked()
    assert isinstance(row._value_widget, QSpinBox) and row._value_widget.value() == 24
    assert "24" in checkbox.toolTip()
    assert dialog.result_text == ""
    dialog.close()
