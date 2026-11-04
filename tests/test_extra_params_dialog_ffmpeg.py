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
    # Lot 4 : le mode de débit VAAPI est choisi dans la liste Mode → ligne grisée.
    assert checkbox is not None and not checkbox.isEnabled() and not checkbox.isChecked()
    assert isinstance(row._value_widget, QSpinBox) and row._value_widget.value() == 24
    assert "24" in checkbox.toolTip()
    row = dialog._rows["compression_level"]
    checkbox = row.findChild(QCheckBox)
    assert checkbox is not None and checkbox.isEnabled() and not checkbox.isChecked()
    assert dialog.result_text == ""
    dialog.close()


def test_review_workflow_rate_units_prefill_in_kbps(qt_app):
    from ui.dialogs.extra_params_dialog import ExtraParamsDialog

    dialog = ExtraParamsDialog("hevc_nvenc", "", workflow_values={"maxrate": "12000k", "bufsize": "16M"})
    try:
        for key, expected in (("maxrate", 12000), ("bufsize", 16000)):
            row = dialog._rows[key]
            assert row.value() == (False, expected)
            row._enabled_cb.setChecked(True)
        values = {key: row.value() for key, row in dialog._rows.items()}
        tokens = shlex.split(_serialize(dialog._schema, values))
        assert _pairs(tokens)["-maxrate"] == "12000k"
        assert _pairs(tokens)["-bufsize"] == "16000k"
    finally:
        dialog.close()


_X265_FILM = (
    "no-sao=1:psy-rd=1.2:psy-rdoq=1.5:rdoq-level=2:aq-mode=3:aq-strength=0.75:cbqpoffs=-2:crqpoffs=-2"
    ":qg-size=32:deblock=-1,-1:qcomp=0.65:b-adapt=2:b-intra=1:rc-lookahead=48:no-open-gop=1"
    ":keyint=240:min-keyint=24"
)


def _x265_dialog_roundtrip(text: str) -> tuple[dict[str, tuple[bool, object]], list[str]]:
    """Ouvre le dialogue libx265 sur ``text`` et relit ce qu'il écrirait (valeurs des widgets)."""
    from ui.dialogs.extra_params_dialog import ExtraParamsDialog

    dialog = ExtraParamsDialog("libx265", text)
    try:
        values = {key: row.value() for key, row in dialog._rows.items()}
        return values, _serialize(dialog._schema, values).split(":")
    finally:
        dialog.close()


def test_x265_film_params_each_have_a_widget(qt_app):
    _ = qt_app
    values, tokens = _x265_dialog_roundtrip(_X265_FILM)

    assert values["__free__"] == (False, "")  # tout est reconnu, rien en tokens libres
    assert values["sao"] == (True, False)  # no-sao=1 → case SAO décochée
    assert values["aq-strength"] == (True, 0.75)
    assert values["qcomp"] == (True, 0.65)
    assert values["cbqpoffs"] == (True, -2) and values["crqpoffs"] == (True, -2)
    assert values["b-intra"] == (True, True)
    assert set(tokens) == {
        "sao=0", "psy-rd=1.2", "psy-rdoq=1.5", "rdoq-level=2", "aq-mode=3", "aq-strength=0.75",
        "qg-size=32", "qcomp=0.65", "cbqpoffs=-2", "crqpoffs=-2", "rc-lookahead=48",
        "keyint=240", "min-keyint=24", "b-adapt=2", "b-intra=1", "no-open-gop=1", "deblock=-1,-1",
    }


def test_x265_negated_and_out_of_widget_values_are_kept(qt_app):
    _ = qt_app
    values, tokens = _x265_dialog_roundtrip("open-gop=0:no-b-intra:aq-mode=5:keyint=1000")

    assert values["no-open-gop"] == (True, True)
    assert values["b-intra"] == (True, False)
    # Hors des valeurs du widget : jamais ramené au défaut ni borné, recopié tel quel.
    assert values["aq-mode"][0] is False and values["keyint"][0] is False
    assert tokens[-2:] == ["aq-mode=5", "keyint=1000"]


def test_x265_deblock_colon_becomes_comma():
    schema = schema_for("libx265")
    assert schema is not None
    assert _serialize(schema, {"deblock": (True, "-2:-1")}) == "deblock=-2,-1"
