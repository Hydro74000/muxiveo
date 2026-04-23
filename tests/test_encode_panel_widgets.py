"""
tests/test_encode_panel_widgets.py — Tests des widgets du panneau d'encodage.

Plan de couverture :

    _AudioTable — flags éditables :
        - COL_TITLE a le flag ItemIsEditable
        - COL_LANG a le flag ItemIsEditable
        - COL_FORMAT n'a pas le flag ItemIsEditable (non-régression)
        - COL_SRC_BR n'a pas le flag ItemIsEditable (non-régression)
        - COL_IDX n'a pas le flag ItemIsEditable (non-régression)
        - COL_SOURCE n'a pas le flag ItemIsEditable (non-régression)

    _AudioTable — valeurs initiales :
        - Titre initial depuis AudioTrack.title
        - Langue initiale depuis AudioTrack.language
        - Bitrate source initial depuis AudioTrack.bit_rate
        - Bitrate source affiche un fallback visuel si AudioTrack.bit_rate est absent
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
        - suppression d'une ligne NEW possible même si la source n'est plus listée
        - suppression d'une ligne source ne demande pas sa suppression au RemuxPanel

    _AudioTable — plan d'encodage remonté au RemuxPanel :
        - changement de codec émet track_encoding_changed(entry_id, codec, bitrate)
        - changement de bitrate émet track_encoding_changed(entry_id, codec, bitrate)

    EncodePanel — sources de nouvelles pistes :
        - seules les pistes d'origine peuvent servir de source à add_custom_row
        - une piste NEW reste éditable/supprimable mais n'active pas le bouton Ajouter

    _AudioTable — persistance des réglages audio :
        - Le codec est conservé après reload avec ordre inversé
        - Le débit est conservé après reload avec ordre inversé
        - current_audio_settings expose bien codec, bitrate et flags TrueHD

Exécution :
    python -m pytest tests/test_encode_panel_widgets.py -v
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLineEdit

from core.config import AppConfig
from core.inspector import AudioTrack, FileInfo, HDRType, VideoTrack
from core.workflows.remux_models import TrackEntry, clone_track_entry
from ui.panels.encode_panel.panel import EncodePanel
from ui.panels.encode_panel.widgets import _AudioTable


# ===========================================================================
# Helpers
# ===========================================================================

def _at(
    index: int = 1,
    codec: str = "eac3",
    codec_long: str | None = None,
    channels: int = 6,
    bit_rate: int | None = 640_000,
    language: str | None = "fra",
    title: str | None = "Piste principale",
    raw: dict | None = None,
) -> AudioTrack:
    return AudioTrack(
        index=index, codec=codec, codec_long=codec_long or codec,
        channels=channels, channel_layout=None,
        sample_rate=48000, bit_rate=bit_rate,
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


def _remux_entry(entry_id: str = "entry-a") -> TrackEntry:
    return TrackEntry(
        mkv_tid=1,
        track_type="audio",
        codec="EAC3",
        display_info="5.1  640 kbps",
        language="fra",
        title="",
        entry_id=entry_id,
    )


def _video_track(index: int, hdr_type: HDRType = HDRType.NONE) -> VideoTrack:
    return VideoTrack(
        index=index,
        codec="hevc",
        codec_long="hevc",
        width=3840,
        height=2160,
        frame_rate="23.976",
        bit_depth=10,
        color_space=None,
        color_primaries=None,
        color_transfer=None,
        color_matrix=None,
        hdr_type=hdr_type,
        raw={},
    )


def _file_info(path: Path, videos: list[VideoTrack], hdr_type: HDRType = HDRType.NONE) -> FileInfo:
    return FileInfo(
        path=path,
        format="matroska",
        duration_s=7200.0,
        size_bytes=20_000_000_000,
        bit_rate=22_000_000,
        video_tracks=videos,
        hdr_type=hdr_type,
    )


def _video_entry(mkv_tid: int = 0) -> TrackEntry:
    return TrackEntry(
        mkv_tid=mkv_tid,
        track_type="video",
        codec="HEVC",
        display_info="3840x2160",
        language="",
        title="",
    )


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

    def test_source_bitrate_cell_not_editable(self, table):
        """COL_SRC_BR est lecture seule (non-régression)."""
        _load_one(table)
        item = table.item(0, _AudioTable.COL_SRC_BR)
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

    def test_source_bitrate_populated_from_track(self, table):
        _load_one(table, _at())
        item = table.item(0, _AudioTable.COL_SRC_BR)
        assert item.text() == "640"

    def test_source_bitrate_uses_visual_fallback_when_missing(self, table):
        _load_one(table, _at(raw={}, title="Sans bitrate", bit_rate=None))
        item = table.item(0, _AudioTable.COL_SRC_BR)
        assert item.text() == "—"

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
        stream_index, _, _, _, _ = emitted[0]
        assert stream_index == 7

    def test_signal_carries_correct_source_path(self, table):
        _load_one(table, path=_PATH_B)
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        table.item(0, _AudioTable.COL_TITLE).setText("X")
        _, source_path, _, _, _ = emitted[0]
        assert source_path == _PATH_B

    def test_signal_carries_current_lang_when_title_changes(self, table):
        _load_one(table, _at(language="fra", title="Original"))
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        table.item(0, _AudioTable.COL_TITLE).setText("Modifié")
        _, _, lang, title, _ = emitted[0]
        assert lang == "fra"
        assert title == "Modifié"

    def test_signal_carries_current_title_when_lang_changes(self, table):
        _load_one(table, _at(language="fra", title="Mon titre"))
        emitted: list = []
        table.track_meta_changed.connect(lambda *a: emitted.append(a))
        table.item(0, _AudioTable.COL_LANG).setText("ja")
        _, _, lang, title, _ = emitted[0]
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
        stream_index, _, _, _, _ = emitted[0]
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
        _set_bitrate(table, 0, 960)
        _set_codec(table, 1, "eac3")
        _set_bitrate(table, 1, 960)

        table.load_tracks([(at2, _COLOR, _PATH_B), (at1, _COLOR, _PATH_A)])

        assert table.item(0, _AudioTable.COL_IDX).text() == "2"
        assert table.item(1, _AudioTable.COL_IDX).text() == "1"
        assert _codec_combo(table, 0).currentData() == "eac3"
        assert _bitrate_value(table, 0) == 960
        assert _codec_combo(table, 1).currentData() == "aac"
        assert _bitrate_value(table, 1) == 960


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


class TestAudioTableTrackEncodingChanged:

    def test_codec_change_emits_track_encoding_plan(self, table):
        entry = _remux_entry("entry-codec")
        table.load_tracks([(_at(index=1), _COLOR, _PATH_A, entry)])
        emitted: list = []
        table.track_encoding_changed.connect(lambda *a: emitted.append(a))

        _set_codec(table, 0, "aac")

        assert emitted
        assert emitted[-1][0] == "entry-codec"
        assert emitted[-1][1] == "aac"

    def test_bitrate_change_emits_track_encoding_plan(self, table):
        entry = _remux_entry("entry-bitrate")
        table.load_tracks([(_at(index=1), _COLOR, _PATH_A, entry)])
        _set_codec(table, 0, "eac3")
        emitted: list = []
        table.track_encoding_changed.connect(lambda *a: emitted.append(a))

        _set_bitrate(table, 0, 960)

        assert emitted
        assert emitted[-1] == ("entry-bitrate", "eac3", 960)


class TestAudioTableTrackRemoval:

    def test_new_track_can_be_deleted_when_source_is_not_present(self, table):
        source_entry = _remux_entry("source-entry")
        new_entry = clone_track_entry(source_entry, entry_id="new-entry")
        table.load_tracks([(_at(index=1), _COLOR, _PATH_A, new_entry)])
        emitted: list = []
        table.track_removed.connect(lambda entry_id: emitted.append(entry_id))

        assert table._can_delete(0) is True
        table._delete_row(0)

        assert emitted == ["new-entry"]
        assert table.rowCount() == 0

    def test_deleting_source_row_does_not_request_remux_removal(self, table):
        source_entry = _remux_entry("source-entry")
        new_entry = clone_track_entry(source_entry, entry_id="new-entry")
        track = _at(index=1)
        table.load_tracks([
            (track, _COLOR, _PATH_A, source_entry),
            (track, _COLOR, _PATH_A, new_entry),
        ])
        emitted: list = []
        table.track_removed.connect(lambda entry_id: emitted.append(entry_id))

        assert table._can_delete(0) is True
        table._delete_row(0)

        assert emitted == []
        assert table.rowCount() == 1


class TestEncodePanelNewTrackSources:

    def test_new_track_does_not_enable_add_button_when_it_is_the_only_audio(self, qt_app):
        panel = EncodePanel(AppConfig())
        source_entry = _remux_entry("source-entry")
        new_entry = clone_track_entry(source_entry, entry_id="new-entry")

        panel.set_audio_tracks([(_at(index=1), _COLOR, _PATH_A, new_entry)])

        assert panel._add_audio_btn.isEnabled() is False
        panel.close()

    def test_add_dialog_receives_only_original_tracks(self, qt_app, monkeypatch):
        panel = EncodePanel(AppConfig())
        source_entry = _remux_entry("source-entry")
        new_entry = clone_track_entry(source_entry, entry_id="new-entry")
        original = (_at(index=1, title="Original"), _COLOR, _PATH_A, source_entry)
        new_track = (_at(index=1, title="New"), _COLOR, _PATH_A, new_entry)
        panel.set_audio_tracks([original, new_track])
        captured: dict[str, list] = {}

        class FakeDialog:
            DialogCode = QDialog.DialogCode

            def __init__(self, tracks, *args, **kwargs):
                captured["tracks"] = tracks

            def exec(self):
                return QDialog.DialogCode.Rejected

        monkeypatch.setattr("ui.panels.encode_panel.panel._AudioSourceDialog", FakeDialog)

        panel._on_add_audio_track()

        assert captured["tracks"] == [original]
        panel.close()


class TestEncodePanelDynamicHdrDefaults:

    def test_selected_video_track_drives_dolby_vision_default(self, qt_app):
        panel = EncodePanel(AppConfig())
        first_source = _file_info(_PATH_A, [_video_track(0, HDRType.NONE)])
        second_source = _file_info(
            _PATH_B,
            [_video_track(0, HDRType.DOLBY_VISION)],
            hdr_type=HDRType.NONE,
        )
        panel._file_info = first_source

        panel.set_video_tracks([(second_source, _video_entry(0), _COLOR)])

        assert panel._copy_dv_cb.isEnabled() is True
        assert panel._copy_dv_cb.isChecked() is True
        assert panel._copy_hdr10plus_cb.isChecked() is False
        panel.close()

    def test_selected_track_hdr10plus_is_used_even_when_container_hdr_is_sdr(self, qt_app):
        panel = EncodePanel(AppConfig())
        info = _file_info(
            _PATH_A,
            [
                _video_track(0, HDRType.NONE),
                _video_track(3, HDRType.DOLBY_VISION_HDR10PLUS),
            ],
            hdr_type=HDRType.NONE,
        )

        panel.set_video_tracks([(info, _video_entry(3), _COLOR)])

        assert panel._copy_dv_cb.isChecked() is True
        assert panel._copy_hdr10plus_cb.isChecked() is True
        panel.close()

    def test_dynamic_hdr_settings_are_independent_per_video_entry(self, qt_app):
        panel = EncodePanel(AppConfig())
        info = _file_info(
            _PATH_A,
            [
                _video_track(0, HDRType.DOLBY_VISION),
                _video_track(1, HDRType.NONE),
            ],
        )
        dovi_entry = _video_entry(0)
        dovi_entry.entry_id = "video-dv"
        sdr_entry = _video_entry(1)
        sdr_entry.entry_id = "video-sdr"

        panel.set_video_tracks([
            (info, dovi_entry, _COLOR),
            (info, sdr_entry, _COLOR),
        ])
        assert panel._copy_dv_cb.isChecked() is True

        panel._video_list.setCurrentRow(1)
        assert panel._copy_dv_cb.isChecked() is False
        assert panel._copy_dv_cb.isEnabled() is False

        panel._video_list.setCurrentRow(0)
        assert panel._copy_dv_cb.isChecked() is True
        panel.close()

    def test_manual_dynamic_hdr_choice_is_kept_per_video_entry(self, qt_app):
        panel = EncodePanel(AppConfig())
        info = _file_info(
            _PATH_A,
            [
                _video_track(0, HDRType.DOLBY_VISION),
                _video_track(1, HDRType.DOLBY_VISION),
            ],
        )
        first_entry = _video_entry(0)
        first_entry.entry_id = "video-dv-1"
        second_entry = _video_entry(1)
        second_entry.entry_id = "video-dv-2"

        panel.set_video_tracks([
            (info, first_entry, _COLOR),
            (info, second_entry, _COLOR),
        ])
        panel._copy_dv_cb.setChecked(False)

        panel._video_list.setCurrentRow(1)
        assert panel._copy_dv_cb.isChecked() is True

        panel._video_list.setCurrentRow(0)
        assert panel._copy_dv_cb.isChecked() is False
        panel.close()

    def test_removed_video_entry_is_removed_from_encode_panel(self, qt_app):
        panel = EncodePanel(AppConfig())
        info = _file_info(
            _PATH_A,
            [
                _video_track(0, HDRType.DOLBY_VISION),
                _video_track(1, HDRType.NONE),
            ],
        )
        kept_entry = _video_entry(0)
        kept_entry.entry_id = "video-kept"
        removed_entry = _video_entry(1)
        removed_entry.entry_id = "video-removed"

        panel.set_video_tracks([
            (info, kept_entry, _COLOR),
            (info, removed_entry, _COLOR),
        ])
        assert panel._video_list.count() == 2

        panel.set_video_tracks([(info, kept_entry, _COLOR)])

        assert panel._video_list.count() == 1
        assert panel._current_video_entry_id == "video-kept"
        assert "video-removed" not in panel._video_settings_by_entry_id
        panel.close()

    def test_current_video_settings_carries_selected_track_identity(self, qt_app):
        panel = EncodePanel(AppConfig())
        info = _file_info(
            _PATH_B,
            [
                _video_track(0, HDRType.NONE),
                _video_track(4, HDRType.DOLBY_VISION),
            ],
        )
        entry = _video_entry(4)
        entry.entry_id = "video-selected"

        panel.set_video_tracks([(info, entry, _COLOR)])
        settings = panel._current_video_settings()

        assert settings.source_path == _PATH_B
        assert settings.stream_index == 4
        assert settings.track_entry_id == "video-selected"
        panel.close()

    def test_dynamic_hdr_flags_do_not_force_encode_when_video_is_copy(self, qt_app):
        panel = EncodePanel(AppConfig())
        config = SimpleNamespace(
            video=SimpleNamespace(
                codec="copy",
                inject_hdr_meta=False,
                tonemap_to_sdr=False,
            ),
            audio_tracks=[],
            copy_dv=True,
            copy_hdr10plus=True,
        )

        assert panel.is_pure_copy(config) is True
        panel.close()


class TestEncodePanelRunOperation:

    def test_run_operation_skips_duplicate_workflow_validation(self, qt_app, monkeypatch):
        panel = EncodePanel(AppConfig())
        config = object()
        captured: dict[str, object] = {}
        expected_signals = object()

        def fake_run(cfg, *, validate=True):
            captured["config"] = cfg
            captured["validate"] = validate
            return expected_signals

        monkeypatch.setattr(panel._workflow, "run", fake_run)

        assert panel.run_operation(config) is expected_signals
        assert captured == {"config": config, "validate": False}
        panel.close()
