"""
tests/test_encode_audio_bitrate_ui.py — Plan de test de l'encodage audio côté UI.

Plan de couverture :

    _AudioTable — valeurs par défaut adaptatives :
        - AC-3 utilise la table de bitrates standard Dolby et plafonne à 640 kbps
        - AAC utilise une combobox fixe basée sur la table AC-3 multipliée par les canaux
        - EAC-3 utilise une combobox fixe basée sur la table AC-3 multipliée par les canaux
        - Si le bitrate source est inférieur au défaut lossy, la valeur préselectionnée est arrondie sans dépasser la source
        - FLAC conserve un champ libre prérempli avec le bitrate source
        - FLAC retombe sur 96 kbps par canal si le bitrate source est absent

    _AudioTable — restitution de configuration :
        - La valeur choisie dans la combobox AAC/AC-3/EAC-3 est propagée dans current_audio_settings
        - Les métadonnées de canaux source restent présentes dans current_audio_settings

    _AudioSourceDialog — cohérence UX :
        - Le codec AAC bascule le contrôle vers une combobox
        - Le changement de piste recalcule la plage selon le nombre de canaux
        - Le codec FLAC préremplit le champ libre avec le bitrate de la piste sélectionnée

Exécution :
    python -m pytest tests/test_encode_audio_bitrate_ui.py -v
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QLineEdit

from core.inspector import AudioTrack
from ui.panels.encode_panel.widgets import _AudioSourceDialog, _AudioTable


_PATH_A = Path("/tmp/source_a.mkv")
_COLOR = "#4f6ef7"


class _Cfg:
    def __init__(self, *, aac: int = 96, eac3: int = 96) -> None:
        self.aac_bitrate_per_channel_kbps = aac
        self.eac3_bitrate_per_channel_kbps = eac3


def _at(
    *,
    index: int = 1,
    codec: str = "eac3",
    channels: int = 6,
    channel_layout: str | None = None,
    bit_rate: int | None = 640_000,
    title: str | None = "Piste principale",
) -> AudioTrack:
    return AudioTrack(
        index=index,
        codec=codec,
        codec_long=codec.upper(),
        channels=channels,
        channel_layout=channel_layout,
        sample_rate=48_000,
        bit_rate=bit_rate,
        language="fra",
        title=title,
        raw={},
    )


def _bitrate_editor(table: _AudioTable, row: int):
    editor = table.cellWidget(row, _AudioTable.COL_BITRATE)
    assert editor is not None
    return editor


def _codec_combo(table: _AudioTable, row: int) -> QComboBox:
    combo = table.cellWidget(row, _AudioTable.COL_CODEC)
    assert isinstance(combo, QComboBox)
    return combo


def _set_codec(table: _AudioTable, row: int, codec_id: str) -> None:
    combo = _codec_combo(table, row)
    idx = next(i for i in range(combo.count()) if combo.itemData(i) == codec_id)
    combo.setCurrentIndex(idx)


def _combo_values(editor) -> list[int]:
    combo = getattr(editor, "_combo")
    assert isinstance(combo, QComboBox)
    return [combo.itemData(i) for i in range(combo.count())]


def _combo_font_is_bold(editor, value: int) -> bool:
    combo = getattr(editor, "_combo")
    assert isinstance(combo, QComboBox)
    idx = next(i for i in range(combo.count()) if combo.itemData(i) == value)
    font = combo.itemData(idx, Qt.ItemDataRole.FontRole)
    return bool(font and font.bold())


class TestAudioTableAdaptiveBitrates:

    def test_ac3_uses_combobox_with_standard_dolby_range(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(channels=6), _COLOR, _PATH_A)], default_codec="ac3")

        editor = _bitrate_editor(table, 0)

        assert getattr(editor, "_combo").isHidden() is False
        assert getattr(editor, "_edit").isHidden() is True
        assert _combo_values(editor)[0] == 32
        assert _combo_values(editor)[-1] == 640
        assert 640 in _combo_values(editor)
        assert _combo_font_is_bold(editor, 640) is True
        assert editor.value() == 576
        table.close()

    def test_ac3_off_grid_source_bitrate_snaps_to_standard_value(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(channels=6, bit_rate=444_000), _COLOR, _PATH_A)], default_codec="ac3")

        editor = _bitrate_editor(table, 0)

        assert 444 not in _combo_values(editor)
        assert editor.value() == 384
        table.close()

    def test_ac3_mono_default_uses_source_channel_count(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(channels=1, channel_layout="mono"), _COLOR, _PATH_A)], default_codec="ac3")

        assert _bitrate_editor(table, 0).value() == 96
        table.close()

    def test_copy_mode_shows_source_bitrate_in_disabled_field(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(channels=6, bit_rate=640_000), _COLOR, _PATH_A)], default_codec="copy")

        editor = _bitrate_editor(table, 0)
        line_edit = getattr(editor, "_edit")

        assert getattr(editor, "_combo").isHidden() is True
        assert line_edit.isHidden() is False
        assert line_edit.isEnabled() is False
        assert line_edit.text() == "640"
        table.close()

    def test_aac_uses_combobox_with_channel_scaled_range(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(channels=6), _COLOR, _PATH_A)], default_codec="aac")

        editor = _bitrate_editor(table, 0)

        assert getattr(editor, "_combo").isHidden() is False
        assert getattr(editor, "_edit").isHidden() is True
        assert _combo_values(editor)[0] == 192
        assert _combo_values(editor)[-1] == 1728
        assert 640 not in _combo_values(editor)
        assert editor.value() == 576
        table.close()

    def test_eac3_uses_combobox_with_7_1_downmix_range(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(channels=8, channel_layout="7.1"), _COLOR, _PATH_A)], default_codec="eac3")

        editor = _bitrate_editor(table, 0)

        assert _combo_values(editor)[0] == 192
        assert _combo_values(editor)[-1] == 6144
        assert 640 not in _combo_values(editor)
        assert editor.value() == 576
        table.close()

    def test_eac3_stereo_range_uses_two_channel_output_limit(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(channels=2, channel_layout="stereo"), _COLOR, _PATH_A)], default_codec="eac3")

        editor = _bitrate_editor(table, 0)

        assert _combo_values(editor)[0] == 64
        assert _combo_values(editor)[-1] == 2048
        assert editor.value() == 192
        table.close()

    def test_codec_switch_uses_configured_aac_default_per_channel(self, qt_app):
        table = _AudioTable(config=_Cfg(aac=160, eac3=96))
        table.load_tracks([(_at(channels=6, bit_rate=2_000_000), _COLOR, _PATH_A)], default_codec="copy")

        _set_codec(table, 0, "aac")

        assert _bitrate_editor(table, 0).value() == 960
        table.close()

    def test_codec_switch_from_copy_ignores_source_floor_for_configured_aac_default(self, qt_app):
        table = _AudioTable(config=_Cfg(aac=160, eac3=96))
        table.load_tracks([(_at(channels=6, bit_rate=640_000), _COLOR, _PATH_A)], default_codec="copy")

        _set_codec(table, 0, "aac")

        assert _bitrate_editor(table, 0).value() == 960
        table.close()

    def test_codec_switch_uses_configured_eac3_default_per_channel(self, qt_app):
        table = _AudioTable(config=_Cfg(aac=96, eac3=224))
        table.load_tracks([(_at(channels=6, bit_rate=2_000_000), _COLOR, _PATH_A)], default_codec="copy")

        _set_codec(table, 0, "eac3")

        assert _bitrate_editor(table, 0).value() == 1344
        table.close()

    def test_codec_switch_from_copy_ignores_source_floor_for_configured_eac3_default(self, qt_app):
        table = _AudioTable(config=_Cfg(aac=96, eac3=224))
        table.load_tracks([(_at(channels=6, bit_rate=640_000), _COLOR, _PATH_A)], default_codec="copy")

        _set_codec(table, 0, "eac3")

        assert _bitrate_editor(table, 0).value() == 1344
        table.close()

    def test_flac_prefills_free_field_with_source_bitrate(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(codec="flac", bit_rate=1_537_000), _COLOR, _PATH_A)], default_codec="flac")

        editor = _bitrate_editor(table, 0)
        line_edit = getattr(editor, "_edit")

        assert getattr(editor, "_combo").isHidden() is True
        assert isinstance(line_edit, QLineEdit)
        assert line_edit.isHidden() is False
        assert line_edit.text() == "1537"
        assert editor.value() == 1537
        table.close()

    def test_flac_without_source_bitrate_falls_back_to_96_kbps_per_channel(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(codec="flac", channels=2, bit_rate=None), _COLOR, _PATH_A)], default_codec="flac")

        assert _bitrate_editor(table, 0).value() == 192
        table.close()

    def test_lossy_defaults_to_source_bitrate_when_source_is_lower(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(channels=2, channel_layout="stereo", bit_rate=160_000), _COLOR, _PATH_A)], default_codec="aac")

        editor = _bitrate_editor(table, 0)

        assert 160 in _combo_values(editor)
        assert _combo_font_is_bold(editor, 160) is True
        assert editor.value() == 160
        table.close()

    def test_lossy_source_bitrate_below_default_is_rounded_down_to_fixed_choice(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(channels=2, channel_layout="stereo", bit_rate=150_000), _COLOR, _PATH_A)], default_codec="aac")

        editor = _bitrate_editor(table, 0)
        values = _combo_values(editor)

        assert 150 not in values
        assert editor.value() == 128
        table.close()

    def test_current_audio_settings_propagates_selected_eac3_bitrate_and_input_channels(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(channels=6, channel_layout="5.1(side)"), _COLOR, _PATH_A)], default_codec="eac3")

        editor = _bitrate_editor(table, 0)
        combo = getattr(editor, "_combo")
        idx = next(i for i in range(combo.count()) if combo.itemData(i) == 960)
        combo.setCurrentIndex(idx)

        settings = table.current_audio_settings()

        assert len(settings) == 1
        assert settings[0].codec == "eac3"
        assert settings[0].bitrate_kbps == 960
        assert settings[0].input_channels == 6
        assert settings[0].input_channel_layout == "5.1(side)"
        table.close()

    def test_current_audio_settings_propagates_selected_ac3_bitrate(self, qt_app):
        table = _AudioTable()
        table.load_tracks([(_at(channels=2, channel_layout="stereo"), _COLOR, _PATH_A)], default_codec="ac3")

        editor = _bitrate_editor(table, 0)
        combo = getattr(editor, "_combo")
        idx = next(i for i in range(combo.count()) if combo.itemData(i) == 320)
        combo.setCurrentIndex(idx)

        settings = table.current_audio_settings()

        assert len(settings) == 1
        assert settings[0].codec == "ac3"
        assert settings[0].bitrate_kbps == 320
        table.close()


class TestAudioSourceDialogAdaptiveBitrates:

    def test_dialog_aac_recomputes_range_when_selected_track_changes(self, qt_app):
        stereo = _at(index=1, channels=2, channel_layout="stereo", bit_rate=192_000, title="Stereo")
        surround = _at(index=2, channels=6, channel_layout="5.1", bit_rate=640_000, title="Surround")
        dialog = _AudioSourceDialog([(stereo, _COLOR, _PATH_A), (surround, _COLOR, _PATH_A)])

        codec_idx = next(i for i in range(dialog._codec_combo.count()) if dialog._codec_combo.itemData(i) == "aac")
        dialog._codec_combo.setCurrentIndex(codec_idx)

        assert _combo_values(dialog._bitrate_edit)[0] == 64
        assert _combo_values(dialog._bitrate_edit)[-1] == 576
        assert _combo_font_is_bold(dialog._bitrate_edit, 192) is True
        assert dialog._bitrate_edit.value() == 192

        dialog._track_list.setCurrentRow(1)

        assert _combo_values(dialog._bitrate_edit)[0] == 192
        assert _combo_values(dialog._bitrate_edit)[-1] == 1728
        assert 640 not in _combo_values(dialog._bitrate_edit)
        assert dialog._bitrate_edit.value() == 576
        dialog.close()

    def test_dialog_flac_prefills_selected_track_source_bitrate(self, qt_app):
        stereo = _at(index=1, channels=2, channel_layout="stereo", bit_rate=888_000, title="Stereo")
        dialog = _AudioSourceDialog([(stereo, _COLOR, _PATH_A)])

        codec_idx = next(i for i in range(dialog._codec_combo.count()) if dialog._codec_combo.itemData(i) == "flac")
        dialog._codec_combo.setCurrentIndex(codec_idx)

        line_edit = getattr(dialog._bitrate_edit, "_edit")
        assert isinstance(line_edit, QLineEdit)
        assert line_edit.text() == "888"
        assert dialog.selected_bitrate() == 888
        dialog.close()

    def test_dialog_codec_switch_from_copy_uses_configured_defaults(self, qt_app):
        surround = _at(index=1, channels=6, channel_layout="5.1", bit_rate=640_000, title="Surround")
        dialog = _AudioSourceDialog([(surround, _COLOR, _PATH_A)], config=_Cfg(aac=160, eac3=224))

        aac_idx = next(i for i in range(dialog._codec_combo.count()) if dialog._codec_combo.itemData(i) == "aac")
        dialog._codec_combo.setCurrentIndex(aac_idx)
        assert dialog._bitrate_edit.value() == 960

        copy_idx = next(i for i in range(dialog._codec_combo.count()) if dialog._codec_combo.itemData(i) == "copy")
        dialog._codec_combo.setCurrentIndex(copy_idx)
        eac3_idx = next(i for i in range(dialog._codec_combo.count()) if dialog._codec_combo.itemData(i) == "eac3")
        dialog._codec_combo.setCurrentIndex(eac3_idx)
        assert dialog._bitrate_edit.value() == 1344
        dialog.close()
