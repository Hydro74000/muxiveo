"""
tests/test_encode_panel_widgets.py — Tests des widgets du panneau d'encodage.

Plan de couverture :

    _AudioTable — flags éditables :
        - COL_TITLE a le flag ItemIsEditable
        - COL_LANG a le flag ItemIsEditable
        - COL_FORMAT n'a pas le flag ItemIsEditable (non-régression)
        - COL_IDX n'a pas le flag ItemIsEditable (non-régression)
        - COL_SOURCE n'a pas le flag ItemIsEditable (non-régression)

    _AudioTable — valeurs initiales :
        - Titre initial depuis AudioTrack.title
        - Langue initiale depuis AudioTrack.language
        - Titre vide si AudioTrack.title est None
        - Langue vide si AudioTrack.language est None

    _AudioTable — signal track_meta_changed :
        - Aucun signal émis pendant load_tracks (pas de spurious signals)
        - Signal émis quand le titre est modifié
        - Signal émis quand la langue est modifiée
        - Signal porte le bon stream_index
        - Signal porte le bon source_path
        - Signal porte la langue courante quand le titre change
        - Signal porte le titre courant quand la langue change
        - Aucun signal pour une colonne non-éditable (COL_IDX)
        - Signal émis indépendamment par ligne (plusieurs lignes)

    _AudioTable — add_custom_row :
        - COL_TITLE a le flag ItemIsEditable sur la nouvelle ligne
        - COL_LANG a le flag ItemIsEditable sur la nouvelle ligne
        - track_meta_changed émis sur modification après add_custom_row

    _AudioTable — persistance des réglages audio :
        - Le codec est conservé après reload avec ordre inversé
        - Le débit est conservé après reload avec ordre inversé
        - current_audio_settings expose bien codec, bitrate et flags TrueHD

Exécution :
    python -m pytest tests/test_encode_panel_widgets.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit

from core.inspector import AudioTrack
from ui.panels.encode_panel.widgets import _AudioTable


# ===========================================================================
# Helpers
# ===========================================================================

def _at(
    index: int = 1,
    codec: str = "eac3",
    codec_long: str | None = None,
    channels: int = 6,
    language: str | None = "fra",
    title: str | None = "Piste principale",
    raw: dict | None = None,
) -> AudioTrack:
    return AudioTrack(
        index=index, codec=codec, codec_long=codec_long or codec,
        channels=channels, channel_layout=None,
        sample_rate=48000, bit_rate=640_000,
        language=language, title=title,
        raw=raw or {},
    )


_PATH_A = Path("/tmp/film_a.mkv")
_PATH_B = Path("/tmp/film_b.mkv")
_COLOR  = "#4f6ef7"


@pytest.fixture
def table(qt_app) -> _AudioTable:
    t = _AudioTable()
    yield t
    t.close()


def _load_one(table: _AudioTable, track: AudioTrack | None = None, path: Path = _PATH_A) -> None:
    """Charge une seule piste dans la table."""
    at = track or _at()
    table.load_tracks([(at, _COLOR, path)])


def _codec_combo(table: _AudioTable, row: int) -> QComboBox:
    combo = table.cellWidget(row, _AudioTable.COL_CODEC)
    assert isinstance(combo, QComboBox)
    return combo


def _bitrate_editor(table: _AudioTable, row: int):
    editor = table.cellWidget(row, _AudioTable.COL_BITRATE)
    assert editor is not None
    return editor


def _set_codec(table: _AudioTable, row: int, codec_id: str) -> None:
    combo = _codec_combo(table, row)
    idx = next(i for i in range(combo.count()) if combo.itemData(i) == codec_id)
    combo.setCurrentIndex(idx)


def _set_bitrate(table: _AudioTable, row: int, value: int) -> None:
    editor = _bitrate_editor(table, row)
    if getattr(editor, "_combo").isHidden():
        line_edit = getattr(editor, "_edit")
        assert isinstance(line_edit, QLineEdit)
        line_edit.setText(str(value))
        return
    combo = getattr(editor, "_combo")
    assert isinstance(combo, QComboBox)
    idx = next(i for i in range(combo.count()) if combo.itemData(i) == value)
    combo.setCurrentIndex(idx)


def _bitrate_value(table: _AudioTable, row: int) -> int:
    return _bitrate_editor(table, row).value()


# ===========================================================================
# Flags éditables
# ===========================================================================

class TestAudioTableEditableFlags:

    def test_title_cell_has_editable_flag(self, table):
        _load_one(table)
        item = table.item(0, _AudioTable.COL_TITLE)
        assert item is not None
        assert item.flags() & Qt.ItemFlag.ItemIsEditable

    def test_lang_cell_has_editable_flag(self, table):
        _load_one(table)
        item = table.item(0, _AudioTable.COL_LANG)
        assert item is not None
        assert item.flags() & Qt.ItemFlag.ItemIsEditable

    def test_format_cell_not_editable(self, table):
        """COL_FORMAT est lecture seule (non-régression)."""
        _load_one(table)
        item = table.item(0, _AudioTable.COL_FORMAT)
        assert item is not None
        assert not (item.flags() & Qt.ItemFlag.ItemIsEditable)

    def test_idx_cell_not_editable(self, table):
        """COL_IDX est lecture seule (non-régression)."""
        _load_one(table)
        item = table.item(0, _AudioTable.COL_IDX)
        assert item is not None
        assert not (item.flags() & Qt.ItemFlag.ItemIsEditable)

    def test_source_cell_not_editable(self, table):
        """COL_SOURCE est lecture seule (non-régression)."""
        _load_one(table)
        item = table.item(0, _AudioTable.COL_SOURCE)
        assert item is not None
        assert not (item.flags() & Qt.ItemFlag.ItemIsEditable)


# ===========================================================================
# Valeurs initiales
# ===========================================================================

class TestAudioTableInitialValues:

    def test_title_populated_from_track(self, table):
        _load_one(table, _at(title="Dolby Atmos"))
        item = table.item(0, _AudioTable.COL_TITLE)
        assert item.text() == "Dolby Atmos"

    def test_lang_populated_from_track(self, table):
        _load_one(table, _at(language="jpn"))
        item = table.item(0, _AudioTable.COL_LANG)
        assert item.text() == "jpn"

    def test_title_empty_when_track_title_is_none(self, table):
        _load_one(table, _at(title=None))
        item = table.item(0, _AudioTable.COL_TITLE)
        assert item.text() == ""

    def test_lang_empty_when_track_language_is_none(self, table):
        _load_one(table, _at(language=None))
        item = table.item(0, _AudioTable.COL_LANG)
        assert item.text() == ""


# ===========================================================================
# Signal track_meta_changed
# ===========================================================================

class TestAudioTableTrackMetaChanged:

    def test_no_signal_during_load_tracks(self, table):
        """load_tracks ne doit pas émettre track_meta_changed (pas de spurious signals)."""
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        _load_one(table)
        assert emitted == []

    def test_signal_emitted_on_title_change(self, table):
        _load_one(table)
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        table.item(0, _AudioTable.COL_TITLE).setText("Nouveau titre")
        assert len(emitted) == 1

    def test_signal_emitted_on_lang_change(self, table):
        _load_one(table)
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        table.item(0, _AudioTable.COL_LANG).setText("ja")
        assert len(emitted) == 1

    def test_signal_carries_correct_stream_index(self, table):
        at = _at(index=7)
        _load_one(table, at)
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        table.item(0, _AudioTable.COL_TITLE).setText("X")
        stream_index, _, _, _ = emitted[0]
        assert stream_index == 7

    def test_signal_carries_correct_source_path(self, table):
        _load_one(table, path=_PATH_B)
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        table.item(0, _AudioTable.COL_TITLE).setText("X")
        _, source_path, _, _ = emitted[0]
        assert source_path == _PATH_B

    def test_signal_carries_current_lang_when_title_changes(self, table):
        _load_one(table, _at(language="fra", title="Original"))
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        table.item(0, _AudioTable.COL_TITLE).setText("Modifié")
        _, _, lang, title = emitted[0]
        assert lang == "fra"
        assert title == "Modifié"

    def test_signal_carries_current_title_when_lang_changes(self, table):
        _load_one(table, _at(language="fra", title="Mon titre"))
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        table.item(0, _AudioTable.COL_LANG).setText("ja")
        _, _, lang, title = emitted[0]
        assert lang == "ja"
        assert title == "Mon titre"

    def test_no_signal_for_non_editable_column(self, table):
        """Modifier une cellule lecture seule ne déclenche pas track_meta_changed."""
        _load_one(table)
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        # Modifie directement le texte de COL_IDX (lecture seule — pas d'édition utilisateur
        # en conditions normales, mais on simule via l'API pour vérifier le filtre)
        idx_item = table.item(0, _AudioTable.COL_IDX)
        idx_item.setText("99")
        assert emitted == []

    def test_signal_independent_per_row(self, table):
        """La modification de la ligne 1 n'émet pas de signal pour la ligne 0."""
        at0 = _at(index=1, title="Piste 1")
        at1 = _at(index=2, title="Piste 2")
        table.load_tracks([(at0, _COLOR, _PATH_A), (at1, _COLOR, _PATH_A)])

        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        table.item(1, _AudioTable.COL_TITLE).setText("Modifié")

        assert len(emitted) == 1
        stream_index, _, _, _ = emitted[0]
        assert stream_index == at1.index

    def test_load_tracks_resets_then_no_signal(self, table):
        """Un reload complet (nouvelles pistes) n'émet aucun signal."""
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        _load_one(table, _at(index=1))
        _load_one(table, _at(index=2))   # second load — réinitialise la table
        assert emitted == []


# ===========================================================================
# add_custom_row — drapeaux éditables
# ===========================================================================

class TestAudioTableAddCustomRow:

    def test_custom_row_title_has_editable_flag(self, table, qt_app):
        at = _at(index=5)
        table.add_custom_row(at, _COLOR, source_path=_PATH_A)
        item = table.item(0, _AudioTable.COL_TITLE)
        assert item is not None
        assert item.flags() & Qt.ItemFlag.ItemIsEditable

    def test_custom_row_lang_has_editable_flag(self, table, qt_app):
        at = _at(index=5)
        table.add_custom_row(at, _COLOR, source_path=_PATH_A)
        item = table.item(0, _AudioTable.COL_LANG)
        assert item is not None
        assert item.flags() & Qt.ItemFlag.ItemIsEditable

    def test_custom_row_track_meta_changed_on_title_edit(self, table, qt_app):
        at = _at(index=5)
        table.add_custom_row(at, _COLOR, source_path=_PATH_A)
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        table.item(0, _AudioTable.COL_TITLE).setText("Custom")
        assert len(emitted) == 1
        assert emitted[0][0] == 5


class TestAudioTableReloadPreservesSettings:

    def test_load_tracks_preserves_codec_and_bitrate_when_order_changes(self, table):
        at1 = _at(index=1, title="VF")
        at2 = _at(index=2, title="VO")
        table.load_tracks([(at1, _COLOR, _PATH_A), (at2, _COLOR, _PATH_B)])

        _set_codec(table, 0, "aac")
        _set_bitrate(table, 0, 640)
        _set_codec(table, 1, "eac3")
        _set_bitrate(table, 1, 640)

        table.load_tracks([(at2, _COLOR, _PATH_B), (at1, _COLOR, _PATH_A)])

        assert table.item(0, _AudioTable.COL_IDX).text() == "2"
        assert table.item(1, _AudioTable.COL_IDX).text() == "1"
        assert _codec_combo(table, 0).currentData() == "eac3"
        assert _bitrate_value(table, 0) == 640
        assert _codec_combo(table, 1).currentData() == "aac"
        assert _bitrate_value(table, 1) == 640


class TestAudioTableCurrentAudioSettings:

    def test_truehd_atmos_copy_enables_truehd_core_extraction(self, table):
        track = _at(codec="truehd", codec_long="TrueHD Atmos", title="VO Atmos")
        _load_one(table, track)

        settings = table.current_audio_settings()

        assert len(settings) == 1
        assert settings[0].codec == "copy"
        assert settings[0].extract_truehd_core is True

    def test_truehd_atmos_transcode_disables_truehd_core_extraction(self, table):
        track = _at(codec="truehd", codec_long="TrueHD Atmos", title="VO Atmos")
        _load_one(table, track)
        _set_codec(table, 0, "eac3")

        settings = table.current_audio_settings()

        assert len(settings) == 1
        assert settings[0].codec == "eac3"
        assert settings[0].extract_truehd_core is False
