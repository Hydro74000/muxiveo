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

import dataclasses
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QLineEdit,
    QPushButton, QSpinBox, QWidget,
)

from core.config import AppConfig
from core.i18n import current_language, set_current_language, translate_text
from core.inspector import AudioTrack, FileInfo, HDRType, VideoTrack
from core.workflows.encode import EncodePreset, ProfileManager
from core.workflows.remux_models import TrackEntry, clone_track_entry
from core.workflows.encode.domain.codecs import bit_depth_error, resolve_output_bit_depth
from core.workflows.encode.models import VideoFilterSettings
from ui.panels.encode_panel.panel import EncodePanel
from ui.panels.encode_panel.widgets import _AudioTable
from core.workflows.encode.runtime.static_hdr_estimator import (
    StaticHdrEstimate,
    StaticHdrEstimateService,
)


# ===========================================================================
# Helpers
# ===========================================================================

@pytest.fixture(autouse=True)
def _french_ui(monkeypatch):
    """Les libellés vérifiés ici ne dépendent pas de la locale du runner."""
    previous = current_language()
    monkeypatch.setattr("core.config._normalize_language_code", lambda _code: "fra")
    set_current_language("fra")
    yield
    set_current_language(previous)


@pytest.fixture(autouse=True)
def _no_real_hardware_probe(monkeypatch):
    """Tests de widgets : capacités injectées, sans démarrer 100 sondes GPU concurrentes.

    Les tests de capacités appellent _on_hw_detected avec leur propre résultat.
    Une sonde réelle prend jusqu'à 15 secondes et dépasse le délai de fermeture Qt.
    """
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)

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
def table(qt_app) -> Generator[_AudioTable, None, None]:
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
    return cast(Any, editor)


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


def _video_track(index: int, hdr_type: HDRType = HDRType.NONE, bit_depth: int = 10) -> VideoTrack:
    return VideoTrack(
        index=index,
        codec="hevc",
        codec_long="hevc",
        width=3840,
        height=2160,
        frame_rate="23.976",
        bit_depth=bit_depth,
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


def _select_codec(panel: EncodePanel, codec: str) -> None:
    idx = next(i for i in range(panel._codec_combo.count()) if panel._codec_combo.itemData(i) == codec)
    panel._codec_combo.setCurrentIndex(idx)


def test_lot6_depth_choice_survives_track_and_codec_changes(qt_app, monkeypatch):
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    first, second = _video_entry(0), _video_entry(1)
    first.entry_id, second.entry_id = "first", "second"
    info = _file_info(_PATH_A, [_video_track(0, bit_depth=10), _video_track(1, bit_depth=8)])
    panel.set_video_tracks([(info, first, _COLOR), (info, second, _COLOR)])
    try:
        _select_codec(panel, "libx264")
        # Présélection Auto : la source 10 bits donne High10 sans figer la valeur.
        assert panel._bit_depth_combo.currentData() == "auto"
        assert resolve_output_bit_depth(panel._current_video_settings()) == 10
        panel._set_combo_data(panel._bit_depth_combo, "8")
        panel._video_list.setCurrentRow(1)
        _select_codec(panel, "libx265")
        # Piste 2 : son propre Auto (source 8 bits), pas le choix de la piste 1.
        assert panel._bit_depth_combo.currentData() == "auto"
        assert resolve_output_bit_depth(panel._current_video_settings()) == 8
        panel._video_list.setCurrentRow(0)
        assert panel._bit_depth_combo.currentData() == "8"
        _select_codec(panel, "libx265")
        assert panel._bit_depth_combo.currentData() == "8"
        panel._set_combo_data(panel._bit_depth_combo, "auto")
        panel._video_list.setCurrentRow(1)
        panel._video_list.setCurrentRow(0)
        assert panel._bit_depth_combo.currentData() == "auto"
        panel._set_combo_data(panel._bit_depth_combo, "10")
        panel._on_hw_detected({"h264_nvenc"}, panel._sw_encoders, panel._config.tool_ffmpeg)
        _select_codec(panel, "h264_nvenc")
        assert panel._bit_depth_combo.currentData() == "auto"
        assert not panel._bit_depth_combo.model().item(panel._bit_depth_combo.findData("10")).isEnabled()
    finally:
        panel.close()


def test_lot6_profile_saves_and_restores_depth(qt_app, monkeypatch, tmp_path):
    from core.workflows.encode.profiles import ProfileManager

    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    panel._profiles = ProfileManager(tmp_path)
    panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0)]), _video_entry(0), _COLOR)])
    try:
        _select_codec(panel, "libx264")
        panel._set_combo_data(panel._bit_depth_combo, "10")
        panel._profile_name.setText("High10")
        panel._save_profile()
        assert panel._profiles.load_all()[0].bit_depth == "10"
        panel._set_combo_data(panel._bit_depth_combo, "8")
        panel._load_profile()
        assert panel._bit_depth_combo.currentData() == "10"
        _select_codec(panel, "libx265")
        panel._profile_name.setText("Auto")
        panel._set_combo_data(panel._bit_depth_combo, "auto")
        panel._save_profile()
        panel._set_combo_data(panel._bit_depth_combo, "10")
        panel._load_profile()
        assert panel._bit_depth_combo.currentData() == "auto"
    finally:
        panel.close()


def test_lot6_p82_auto_keeps_sdr_depth(qt_app, monkeypatch):
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    video = _video_track(0, HDRType.DOLBY_VISION, bit_depth=8)
    video.dovi_profile, video.dovi_compat_id, video.color_transfer = 8, 2, "bt709"
    panel.set_video_tracks([(_file_info(_PATH_A, [video]), _video_entry(0), _COLOR)])
    try:
        _select_codec(panel, "libx265")
        panel._set_combo_data(panel._bit_depth_combo, "auto")
        settings = panel._current_video_settings()
        assert settings.copy_dv and settings.dovi_source_profile == "p8_2"
        assert resolve_output_bit_depth(settings) == 8
        assert "[8-bit]" in panel._video_list.item(0).text()
    finally:
        panel.close()


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

    def test_truehd_atmos_copy_preserves_atmos_without_core_extraction(self, table):
        track = _at(codec="truehd", codec_long="TrueHD Atmos", title="VO Atmos")
        _load_one(table, track)

        settings = table.current_audio_settings()

        assert len(settings) == 1
        assert settings[0].codec == "copy"
        assert settings[0].extract_truehd_core is False

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


class TestEncodePanelCodecHdrPolicy:
    _MD = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"

    def test_h264_disables_hdr_options_and_restores_them_on_hevc(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-h264"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.DOLBY_VISION_HDR10PLUS)])
        panel.set_video_tracks([(info, entry, _COLOR)])
        _select_codec(panel, "libx265")
        assert panel._inject_hdr_cb.isChecked() is True

        for codec in ("libx264", "nvencc_h264"):
            if panel._codec_combo.findData(codec) < 0:
                continue
            _select_codec(panel, codec)
            assert panel._inject_hdr_cb.isEnabled() is False
            assert panel._inject_hdr_cb.isChecked() is False
            assert panel._copy_dv_cb.isChecked() is False
            assert panel._copy_hdr10plus_cb.isChecked() is False
            assert panel._tonemap_cb.isEnabled() is True
            video = panel._current_video_settings()
            assert (video.inject_hdr_meta, video.master_display, video.max_cll) == (False, "", "")

        _select_codec(panel, "libx265")
        assert panel._inject_hdr_cb.isEnabled() is True
        assert panel._inject_hdr_cb.isChecked() is True
        assert panel._copy_dv_cb.isChecked() is True
        panel.close()

    def test_unchecked_static_hdr_sends_no_values(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-static"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.HDR10)])
        panel.set_video_tracks([(info, entry, _COLOR)])
        _select_codec(panel, "libx265")
        panel._master_display.setText(self._MD)
        panel._max_cll.setText("1000,400")
        panel._inject_hdr_cb.setChecked(False)

        video = panel._current_video_settings()
        assert (video.inject_hdr_meta, video.master_display, video.max_cll) == (False, "", "")
        panel.close()

    def test_copy_locks_hdr_options_on_source(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-copy"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.HDR10PLUS)])
        panel.set_video_tracks([(info, entry, _COLOR)])
        _select_codec(panel, "copy")

        for checkbox in (panel._inject_hdr_cb, panel._copy_dv_cb, panel._copy_hdr10plus_cb, panel._tonemap_cb):
            assert checkbox.isEnabled() is False
        assert panel._inject_hdr_cb.isChecked() is True
        assert panel._copy_hdr10plus_cb.isChecked() is True
        assert panel._copy_dv_cb.isChecked() is False
        assert panel._tonemap_cb.isChecked() is False
        panel.close()

    def test_apply_all_keeps_hdr_metadata_per_source(self, qt_app):
        panel = EncodePanel(AppConfig())
        hdr_info = _file_info(_PATH_A, [_video_track(0, HDRType.HDR10)])
        sdr_info = _file_info(_PATH_B, [_video_track(0, HDRType.NONE)])
        hdr_entry = _video_entry(0)
        hdr_entry.entry_id = "video-hdr"
        sdr_entry = _video_entry(0)
        sdr_entry.entry_id = "video-sdr"
        panel.set_video_tracks([(hdr_info, hdr_entry, _COLOR), (sdr_info, sdr_entry, _COLOR)])
        panel._video_list.setCurrentRow(0)
        _select_codec(panel, "libx265")
        panel._master_display.setText(self._MD)
        panel._apply_all_video_cb.setChecked(True)

        sdr_state = panel._video_settings_by_entry_id["video-sdr"]
        assert sdr_state["codec"] == "libx265"
        assert sdr_state["inject_hdr_meta"] is False
        assert sdr_state["master_display"] == ""
        assert panel._video_settings_by_entry_id["video-hdr"]["master_display"] == self._MD
        panel.close()


class TestEncodePanelDynamicHdrDefaults:

    def test_sdr_source_never_runs_hdr_frame_probe_on_ui_thread(self, qt_app, tmp_path, monkeypatch):
        panel = EncodePanel(AppConfig())
        source = tmp_path / "sdr.mkv"
        source.touch()
        info = _file_info(source, [_video_track(0, HDRType.NONE)])
        frame_probe = MagicMock()
        monkeypatch.setattr(panel, "_extract_hdr_meta_from_ffprobe_frames", frame_probe)

        panel.set_video_tracks([(info, _video_entry(0), _COLOR)])

        frame_probe.assert_not_called()
        panel.close()

    def test_hdr_frame_probe_is_queued_and_preserves_user_edits(self, qt_app, tmp_path, monkeypatch):
        panel = EncodePanel(AppConfig())
        source = tmp_path / "hdr.mkv"
        source.touch()
        submit = MagicMock()
        monkeypatch.setattr(panel._hdr_meta_executor, "submit", submit)

        panel._prefill_hdr_meta({}, source, probe_frames=True)

        submit.assert_called_once_with(panel._probe_hdr_meta_from_frames, 0, source)
        panel._master_display.setText("manual")
        panel._on_hdr_meta_frame_probe_ready(0, "auto-master", "1000,400")

        assert panel._master_display.text() == "manual"
        assert panel._max_cll.text() == "1000,400"
        panel.close()

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

        # Copie : HDR source recopié tel quel, cases verrouillées.
        assert panel._copy_dv_cb.isEnabled() is False
        assert panel._copy_dv_cb.isChecked() is True
        assert panel._copy_hdr10plus_cb.isChecked() is False

        _select_codec(panel, "libx265")
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

    def test_apply_all_video_settings_is_disabled_by_default(self, qt_app):
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
        assert panel._apply_all_video_cb.isChecked() is False
        assert panel._copy_dv_cb.isChecked() is True

        panel._video_list.setCurrentRow(1)
        assert panel._copy_dv_cb.isChecked() is False
        assert panel._copy_dv_cb.isEnabled() is False

        panel._video_list.setCurrentRow(0)
        assert panel._copy_dv_cb.isChecked() is True
        panel.close()

    def test_video_row_shows_encoder_badge_and_bold_only_when_not_copy(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-encoder-badge"
        panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0)]), entry, _COLOR)])

        row_item = panel._video_list.item(0)
        assert row_item is not None
        assert "Enc:" not in row_item.text()
        assert "[x265]" not in row_item.text()
        assert row_item.font().bold() is False

        idx_x265 = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "libx265"
        )
        panel._codec_combo.setCurrentIndex(idx_x265)
        row_item = panel._video_list.item(0)
        assert row_item is not None
        assert "[x265]" in row_item.text()
        assert "Enc:" not in row_item.text()
        assert row_item.font().bold() is True

        idx_copy = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "copy"
        )
        panel._codec_combo.setCurrentIndex(idx_copy)
        row_item = panel._video_list.item(0)
        assert row_item is not None
        assert "[x265]" not in row_item.text()
        assert row_item.font().bold() is False
        panel.close()

    def test_video_row_shows_dv_and_hdr10plus_badges_from_track(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-with-hdr-badges"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.DOLBY_VISION_HDR10PLUS)])

        panel.set_video_tracks([(info, entry, _COLOR)])

        row_item = panel._video_list.item(0)
        assert row_item is not None
        text = row_item.text().lower()
        assert "[dv]" in text
        assert "[10+]" in text
        assert "enc:" not in text
        panel.close()

    def test_video_row_dv_badge_is_isolated_per_track_on_initial_load(self, qt_app):
        panel = EncodePanel(AppConfig())
        dv_entry = _video_entry(0)
        dv_entry.entry_id = "video-dv"
        sdr_entry = _video_entry(0)
        sdr_entry.entry_id = "video-sdr"
        dv_info = _file_info(_PATH_A, [_video_track(0, HDRType.DOLBY_VISION)])
        sdr_info = _file_info(_PATH_B, [_video_track(0, HDRType.NONE)])

        panel.set_video_tracks([
            (dv_info, dv_entry, _COLOR),
            (sdr_info, sdr_entry, _COLOR),
        ])

        first = panel._video_list.item(0)
        second = panel._video_list.item(1)
        assert first is not None
        assert second is not None
        assert "[dv]" in first.text().lower()
        assert "[dv]" not in second.text().lower()
        panel.close()

    def test_video_row_h264_codec_removes_dv_badge_for_dv_source(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-dv-h264"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.DOLBY_VISION)])
        panel.set_video_tracks([(info, entry, _COLOR)])

        idx_x264 = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "libx264"
        )
        panel._codec_combo.setCurrentIndex(idx_x264)
        row_item = panel._video_list.item(0)
        assert row_item is not None
        text = row_item.text().lower()
        assert "[dv]" not in text
        assert "[10+]" not in text
        panel.close()

    def test_collect_config_keeps_dynamic_hdr_flags_scoped_per_track(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
        dv_entry = _video_entry(0)
        dv_entry.entry_id = "video-dv"
        sdr_entry = _video_entry(0)
        sdr_entry.entry_id = "video-sdr"
        dv_info = _file_info(_PATH_A, [_video_track(0, HDRType.DOLBY_VISION)])
        sdr_info = _file_info(_PATH_B, [_video_track(0, HDRType.NONE)])
        panel.set_video_tracks([
            (dv_info, dv_entry, _COLOR),
            (sdr_info, sdr_entry, _COLOR),
        ])

        cfg = panel.collect_config()
        assert cfg is not None
        by_entry = {str(track.track_entry_id): track for track in cfg.video_tracks}
        assert by_entry["video-dv"].copy_dv is True
        assert by_entry["video-sdr"].copy_dv is False
        panel.close()

    def test_p5_to_p8_static_hdr_estimate_candidate_when_static_fields_missing(self, qt_app):
        panel = EncodePanel(AppConfig())
        video = _video_track(0, HDRType.DOLBY_VISION)
        video.dovi_profile = 5
        entry = _video_entry(0)
        entry.entry_id = "video-p5"

        panel.set_video_tracks([(_file_info(_PATH_A, [video]), entry, _COLOR)])
        panel._dovi_profile_choice = "2"
        panel._save_current_video_state()

        candidate = panel._static_hdr_estimate_candidate_current()

        assert candidate is not None
        assert candidate[0] == "video-p5"
        panel.close()

    def test_static_hdr_estimate_prompt_exposes_precise_fast_ignore(self, qt_app, monkeypatch):
        panel = EncodePanel(AppConfig())
        labels: list[str] = []

        class FakeMessageBox:
            class ButtonRole:
                AcceptRole = object()
                ActionRole = object()
                RejectRole = object()

            clicked_label = "Analyse rapide"

            def __init__(self, *_args, **_kwargs):
                self._buttons: dict[str, object] = {}

            def setWindowTitle(self, _title):  # noqa: ANN001
                pass

            def setText(self, _text):  # noqa: ANN001
                pass

            def addButton(self, label, _role):  # noqa: ANN001
                button = object()
                self._buttons[label] = button
                labels.append(label)
                return button

            def setDefaultButton(self, button):  # noqa: ANN001
                self.default_button = button

            def exec(self):  # noqa: A003, ANN201
                return 0

            def clickedButton(self):  # noqa: ANN201
                return self._buttons[self.clicked_label]

        monkeypatch.setattr("ui.panels.encode_panel.panel.QMessageBox", FakeMessageBox)

        mode = panel._ask_static_hdr_estimate_mode()

        assert labels == ["Analyse précise", "Analyse rapide", "Ignorer"]
        assert mode == StaticHdrEstimateService.FAST_MODE
        panel.close()

    def test_p5_to_p8_keeps_profile_and_schedules_precise_analysis(self, qt_app, monkeypatch):
        panel = EncodePanel(AppConfig())
        video = _video_track(0, HDRType.DOLBY_VISION)
        video.dovi_profile = 5
        entry = _video_entry(0)
        entry.entry_id = "video-p5"
        scheduled: list[tuple[str, str]] = []
        monkeypatch.setattr(
            panel,
            "_ask_static_hdr_estimate_mode",
            lambda: StaticHdrEstimateService.PRECISE_MODE,
        )
        monkeypatch.setattr(
            panel,
            "_schedule_static_hdr_estimate",
            lambda entry_id, _state, mode: scheduled.append((entry_id, mode)),
        )

        panel.set_video_tracks([(_file_info(_PATH_A, [video]), entry, _COLOR)])
        # V30 : une copie ne convertit jamais un P5 ; la normalisation suppose un réencodage.
        _select_codec(panel, "libx265")
        panel._dovi_p81_cb.click()

        assert scheduled == [("video-p5", StaticHdrEstimateService.PRECISE_MODE)]
        assert panel._dovi_profile_choice == "2" and panel._dovi_p81_cb.isChecked()
        assert panel._video_settings_by_entry_id["video-p5"]["dovi_profile"] == "2"
        panel.close()

    def test_p5_to_p8_can_schedule_fast_analysis(self, qt_app, monkeypatch):
        panel = EncodePanel(AppConfig())
        video = _video_track(0, HDRType.DOLBY_VISION)
        video.dovi_profile = 5
        entry = _video_entry(0)
        entry.entry_id = "video-p5"
        scheduled: list[str] = []
        monkeypatch.setattr(
            panel,
            "_ask_static_hdr_estimate_mode",
            lambda: StaticHdrEstimateService.FAST_MODE,
        )
        monkeypatch.setattr(
            panel,
            "_schedule_static_hdr_estimate",
            lambda _entry_id, _state, mode: scheduled.append(mode),
        )

        panel.set_video_tracks([(_file_info(_PATH_A, [video]), entry, _COLOR)])
        _select_codec(panel, "libx265")
        panel._dovi_p81_cb.click()

        assert scheduled == [StaticHdrEstimateService.FAST_MODE]
        assert panel._dovi_profile_choice == "2" and panel._dovi_p81_cb.isChecked()
        panel.close()

    def test_static_hdr_estimate_is_scheduled_for_workflow(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
        video = _video_track(0, HDRType.DOLBY_VISION)
        video.dovi_profile = 5
        entry = _video_entry(0)
        entry.entry_id = "video-p5"
        panel.set_video_tracks([(_file_info(_PATH_A, [video]), entry, _COLOR)])
        panel._dovi_profile_choice = "2"
        panel._save_current_video_state()
        state = panel._video_settings_by_entry_id["video-p5"]

        panel._schedule_static_hdr_estimate("video-p5", state, mode="precise")

        scheduled = panel._video_settings_by_entry_id["video-p5"]
        assert scheduled["static_hdr_metadata_analysis_request"] == "precise"
        assert scheduled["inject_hdr_meta"] is True
        assert scheduled["master_display"] == ""
        assert scheduled["max_cll"] == ""
        config = panel.collect_config()
        assert config is not None
        assert config.video is not None
        assert config.video.static_hdr_metadata_analysis_request == "precise"
        panel.close()

    def test_static_hdr_prefill_reuses_initial_mediainfo_json(self, qt_app, monkeypatch):
        panel = EncodePanel(AppConfig())
        video = _video_track(0, HDRType.HDR10)
        entry = _video_entry(0)
        entry.entry_id = "video-hdr"
        info = _file_info(_PATH_A, [video], hdr_type=HDRType.HDR10)
        info.mediainfo_json = {
            "media": {
                "track": [
                    {
                        "@type": "Video",
                        "MasteringDisplay_ColorPrimaries": "BT.2020",
                        "MasteringDisplay_Luminance_Min": "0.0001",
                        "MasteringDisplay_Luminance_Max": "1000",
                        "MaxCLL": "1000 cd/m2",
                        "MaxFALL": "400 cd/m2",
                    }
                ]
            }
        }

        def fail_subprocess_run(*_args, **_kwargs):
            raise AssertionError("mediainfo should be reused from FileInfo, not relaunched")

        monkeypatch.setattr("core.workflows.encode.planning.track_settings.subprocess.run", fail_subprocess_run)

        panel.set_video_tracks([(info, entry, _COLOR)])

        state = panel._video_settings_by_entry_id["video-hdr"]
        assert state["master_display"] == "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"
        assert state["max_cll"] == "1000,400"
        assert panel._master_display.text() == state["master_display"]
        assert panel._max_cll.text() == "1000,400"
        panel.close()

    def test_manual_hdr_values_cancel_scheduled_workflow_analysis(self, qt_app):
        panel = EncodePanel(AppConfig())
        video = _video_track(0, HDRType.DOLBY_VISION)
        video.dovi_profile = 5
        entry = _video_entry(0)
        entry.entry_id = "video-p5"
        panel.set_video_tracks([(_file_info(_PATH_A, [video]), entry, _COLOR)])
        panel._dovi_profile_choice = "2"
        panel._save_current_video_state()
        state = panel._video_settings_by_entry_id["video-p5"]
        panel._schedule_static_hdr_estimate("video-p5", state, mode="precise")

        panel._master_display.setText("MANUAL_MD")

        updated = panel._video_settings_by_entry_id["video-p5"]
        assert updated["master_display"] == "MANUAL_MD"
        assert updated["static_hdr_metadata_analysis_request"] == ""
        assert "video-p5:p5_to_p8" not in panel._static_hdr_estimate_prompted
        panel.close()

    def test_dovi_option_change_cancels_scheduled_workflow_analysis(self, qt_app):
        panel = EncodePanel(AppConfig())
        video = _video_track(0, HDRType.DOLBY_VISION)
        video.dovi_profile = 5
        entry = _video_entry(0)
        entry.entry_id = "video-p5"
        panel.set_video_tracks([(_file_info(_PATH_A, [video]), entry, _COLOR)])
        panel._dovi_profile_choice = "2"
        panel._save_current_video_state()
        state = panel._video_settings_by_entry_id["video-p5"]
        panel._schedule_static_hdr_estimate("video-p5", state, mode="fast")

        panel._on_dovi_p81_clicked(False)

        updated = panel._video_settings_by_entry_id["video-p5"]
        assert updated["dovi_profile"] == "0"
        assert updated["static_hdr_metadata_analysis_request"] == ""
        panel.close()

    def test_static_hdr_workflow_failure_shows_dialog(self, qt_app, monkeypatch):
        panel = EncodePanel(AppConfig())
        shown: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "ui.panels.encode_panel.panel.QMessageBox.critical",
            lambda _parent, title, message: shown.append((title, message)),
        )

        panel._on_static_hdr_estimate_failed("", "Quota insuffisant.")

        assert shown == [("Analyse HDR10 impossible", "Quota insuffisant.")]
        panel.close()

    def test_static_hdr_estimate_prefills_fields_and_marks_metadata_source(self, qt_app):
        panel = EncodePanel(AppConfig())
        video = _video_track(0, HDRType.DOLBY_VISION)
        video.dovi_profile = 5
        entry = _video_entry(0)
        entry.entry_id = "video-p5"
        panel.set_video_tracks([(_file_info(_PATH_A, [video]), entry, _COLOR)])
        panel._dovi_profile_choice = "2"
        panel._save_current_video_state()
        panel._video_settings_by_entry_id["video-p5"]["static_hdr_metadata_analysis_request"] = "precise"
        estimate = StaticHdrEstimate(
            master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
            max_cll="900,300",
            confidence="medium",
            sample_count=42,
            coverage=0.44,
            mode="precise",
            active_sample_count=40,
            ignored_sample_count=2,
            active_crop="1920:800:0:100",
        )

        panel._on_static_hdr_estimate_ready("video-p5", estimate)

        state = panel._video_settings_by_entry_id["video-p5"]
        assert state["master_display"] == estimate.master_display
        assert state["max_cll"] == "900,300"
        assert state["static_hdr_metadata_source"] == "estimated_p5_to_p8"
        assert state["static_hdr_metadata_analysis_mode"] == "precise"
        assert panel._master_display.text() == estimate.master_display
        assert panel._max_cll.text() == "900,300"
        panel.close()

    def test_static_hdr_estimate_does_not_overwrite_manual_fields(self, qt_app):
        panel = EncodePanel(AppConfig())
        video = _video_track(0, HDRType.DOLBY_VISION)
        video.dovi_profile = 5
        entry = _video_entry(0)
        entry.entry_id = "video-p5"
        panel.set_video_tracks([(_file_info(_PATH_A, [video]), entry, _COLOR)])
        state = panel._video_settings_by_entry_id["video-p5"]
        state["master_display"] = "MANUAL_MD"
        state["max_cll"] = "111,22"
        state["static_hdr_metadata_analysis_request"] = "precise"

        panel._on_static_hdr_estimate_ready(
            "video-p5",
            StaticHdrEstimate(
                master_display="AUTO_MD",
                max_cll="900,300",
                confidence="medium",
                sample_count=42,
                coverage=0.44,
            ),
        )

        assert panel._video_settings_by_entry_id["video-p5"]["master_display"] == "MANUAL_MD"
        assert panel._video_settings_by_entry_id["video-p5"]["max_cll"] == "111,22"
        panel.close()

    def test_new_video_track_does_not_inherit_previous_track_settings_when_apply_all_disabled(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
        first_entry = _video_entry(0)
        first_entry.entry_id = "video-first"
        second_entry = _video_entry(1)
        second_entry.entry_id = "video-second"
        info = _file_info(
            _PATH_A,
            [
                _video_track(0, HDRType.NONE),
                _video_track(1, HDRType.NONE),
            ],
        )

        panel.set_video_tracks([(info, first_entry, _COLOR)])
        panel._apply_all_video_cb.setChecked(False)
        idx_x265 = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "libx265"
        )
        panel._codec_combo.setCurrentIndex(idx_x265)

        panel.set_video_tracks([
            (info, first_entry, _COLOR),
            (info, second_entry, _COLOR),
        ])

        cfg = panel.collect_config()
        assert cfg is not None
        by_entry = {str(video.track_entry_id): video for video in cfg.video_tracks}
        assert by_entry["video-first"].codec == "libx265"
        assert by_entry["video-second"].codec == "copy"
        panel.close()

    def test_new_video_track_inherits_settings_only_when_apply_all_and_non_copy(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
        first_entry = _video_entry(0)
        first_entry.entry_id = "video-first"
        second_entry = _video_entry(1)
        second_entry.entry_id = "video-second"
        info = _file_info(
            _PATH_A,
            [
                _video_track(0, HDRType.NONE),
                _video_track(1, HDRType.NONE),
            ],
        )

        panel.set_video_tracks([(info, first_entry, _COLOR)])
        panel._apply_all_video_cb.setChecked(True)
        idx_x265 = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "libx265"
        )
        panel._codec_combo.setCurrentIndex(idx_x265)

        panel.set_video_tracks([
            (info, first_entry, _COLOR),
            (info, second_entry, _COLOR),
        ])

        cfg = panel.collect_config()
        assert cfg is not None
        by_entry = {str(video.track_entry_id): video for video in cfg.video_tracks}
        assert by_entry["video-first"].codec == "libx265"
        assert by_entry["video-second"].codec == "libx265"
        panel.close()

    def test_video_row_shows_sdr_badge_when_tonemap_enabled(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-tonemap"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.HDR10)])
        panel.set_video_tracks([(info, entry, _COLOR)])

        assert panel._tonemap_cb.isEnabled() is False  # copie : tone-mapping impossible
        _select_codec(panel, "libx265")
        panel._tonemap_cb.setChecked(True)
        row_item = panel._video_list.item(0)
        assert row_item is not None
        text = row_item.text().lower()
        assert "[sdr]" in text
        assert "[dv]" not in text
        assert "[10+]" not in text
        panel.close()

    def test_video_row_shows_hdr_badge_when_metadata_injection_enabled(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-hdr-injection"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.NONE)])
        panel.set_video_tracks([(info, entry, _COLOR)])

        _select_codec(panel, "libx265")
        panel._inject_hdr_cb.setChecked(True)
        row_item = panel._video_list.item(0)
        assert row_item is not None
        text = row_item.text().lower()
        assert "[hdr]" in text
        panel.close()

    def test_encode_panel_tabs_keep_hdr_inside_video_tab(self, qt_app):
        panel = EncodePanel(AppConfig())

        raw_labels = [panel._tabs.tabText(i) for i in range(panel._tabs.count())]
        labels = [label.replace("&&", "&") for label in raw_labels]
        assert panel._tabs.count() == 4
        assert raw_labels[0] == "Sources && Audio"
        assert labels[0] == "Sources & Audio"
        assert labels[1] == "Video"
        assert labels[2] in {"Géométrie / Filtres", "Geometry / Filters"}
        assert labels[3] in {"Preview / Commande", "Preview / Command"}
        assert not {"HDR", "Géométrie", "Filtres", "Geometry", "Filters"}.intersection(labels)
        video_tab = panel._tabs.widget(1)
        assert video_tab is not None
        assert panel._inject_hdr_cb in video_tab.findChildren(QCheckBox)
        geometry_filters_tab = panel._tabs.widget(2)
        assert geometry_filters_tab is not None
        assert panel._geometry_controls in geometry_filters_tab.findChildren(QWidget)
        assert panel._filters_controls in geometry_filters_tab.findChildren(QWidget)
        panel.close()

    def test_video_source_list_height_is_capped_at_ten_rows(self, qt_app):
        panel = EncodePanel(AppConfig())
        tracks = []
        for index in range(12):
            path = Path(f"/tmp/film_{index}.mkv")
            entry = _video_entry(index)
            entry.entry_id = f"video-{index}"
            tracks.append((_file_info(path, [_video_track(index)]), entry, _COLOR))

        panel.set_video_tracks(tracks)

        row_h = panel._video_list.sizeHintForRow(0)
        assert row_h > 0
        frame_h = panel._video_list.frameWidth() * 2
        expected_ten_rows = 10 * row_h + frame_h + 2
        assert panel._video_list.maximumHeight() == expected_ten_rows
        assert panel._video_list.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded

        panel.set_video_tracks(tracks[:3])

        expected_three_rows = 3 * row_h + frame_h + 2
        assert panel._video_list.maximumHeight() == expected_three_rows
        panel.close()

    def test_preview_tab_exposes_real_preview_controls(self, qt_app):
        panel = EncodePanel(AppConfig())

        assert isinstance(panel._preview_mode_combo, QComboBox)
        assert panel._preview_mode_combo.itemData(0) == "image"
        assert panel._preview_mode_combo.itemData(1) == "video"
        assert isinstance(panel._preview_time_edit, QLineEdit)
        assert isinstance(panel._preview_duration_spin, QSpinBox)
        assert panel._preview_duration_spin.minimum() == 5
        assert panel._preview_duration_spin.maximum() == 30
        assert isinstance(panel._preview_generate_btn, QPushButton)
        assert isinstance(panel._preview_cancel_btn, QPushButton)
        assert panel._preview_cancel_btn.isEnabled() is False
        assert panel._preview_image.isHidden() is False
        assert panel._preview_video_result_row.isHidden() is True
        panel.close()

    def test_preview_mode_video_shows_duration_and_video_result(self, qt_app):
        panel = EncodePanel(AppConfig())

        panel._preview_mode_combo.setCurrentIndex(1)

        assert panel._preview_duration_spin.isEnabled() is True
        assert panel._preview_time_edit.isEnabled() is True
        assert panel._preview_random_btn.isEnabled() is True
        assert panel._preview_video_result_row.isHidden() is False
        panel.close()

    def test_preview_image_mode_disables_video_only_inputs(self, qt_app):
        panel = EncodePanel(AppConfig())

        panel._preview_mode_combo.setCurrentIndex(0)

        assert panel._preview_duration_spin.isEnabled() is False
        assert panel._preview_time_edit.isEnabled() is False
        assert panel._preview_random_btn.isEnabled() is False
        assert panel._preview_video_result_row.isHidden() is True
        panel.close()

    def test_preview_carousel_navigation_cycles_through_captures(self, qt_app, tmp_path):
        panel = EncodePanel(AppConfig())
        captures = [
            {"image_path": str(tmp_path / f"img_{i}.png"), "scene_time_s": float(i), "label": ""}
            for i in range(3)
        ]
        panel._preview_captures = captures
        panel._show_preview_capture(0)

        assert panel._preview_current_index == 0
        panel._on_preview_next()
        assert panel._preview_current_index == 1
        panel._on_preview_next()
        panel._on_preview_next()
        assert panel._preview_current_index == 0
        panel._on_preview_prev()
        assert panel._preview_current_index == 2
        panel.close()

    def test_preview_zoom_slider_updates_percentage(self, qt_app):
        panel = EncodePanel(AppConfig())

        panel._preview_zoom_slider.setValue(250)

        assert panel._preview_zoom_percent == 250
        assert panel._preview_zoom_value_lbl.text() == "250 %"
        assert panel._preview_zoom_slider.minimum() == 10
        assert panel._preview_zoom_slider.maximum() == 400
        panel.close()

    def test_preview_progress_bar_responds_to_progress_pct(self, qt_app):
        panel = EncodePanel(AppConfig())

        panel._on_preview_progress_pct(40)
        assert panel._preview_progress.value() == 40

        panel._on_preview_progress_pct(150)
        assert panel._preview_progress.value() == 100

        panel._on_preview_progress_pct(-5)
        assert panel._preview_progress.value() == 0
        panel.close()

    def test_preview_timecode_parse_and_format(self, qt_app):
        assert EncodePanel._parse_preview_timecode("01:02:03.450") == pytest.approx(3723.45)
        assert EncodePanel._parse_preview_timecode("02:03") == pytest.approx(123.0)
        assert EncodePanel._parse_preview_timecode("12,5") == pytest.approx(12.5)
        assert EncodePanel._format_preview_timecode(3723.45) == "01:02:03.450"

    def test_copy_mode_keeps_geometry_and_filters_visible_with_clear_message(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-copy-transforms"
        panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0)]), entry, _COLOR)])

        from core.i18n import translate_text

        # Attendu dans la langue active (config utilisateur éventuellement non française).
        expected = translate_text(
            "Options indisponibles en mode Copy. Choisissez un codec d'encodage "
            "dans l'onglet Video pour activer la géométrie et les filtres."
        )
        assert panel._geometry_copy_msg.text() == expected
        assert panel._filters_copy_msg.text() == expected
        assert panel._geometry_copy_msg.isHidden() is False
        assert panel._filters_copy_msg.isHidden() is False
        assert panel._geometry_controls.isEnabled() is False
        assert panel._filters_controls.isEnabled() is False

        idx_x265 = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "libx265"
        )
        panel._codec_combo.setCurrentIndex(idx_x265)
        assert panel._geometry_controls.isEnabled() is True
        assert panel._filters_controls.isEnabled() is True
        assert panel._geometry_copy_msg.isHidden() is True
        assert panel._filters_copy_msg.isHidden() is True
        panel.close()

    def test_resize_mode_switches_visible_controls_and_presets_update_dimensions(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-resize-ui"
        panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0)]), entry, _COLOR)])

        panel._set_combo_data(panel._resize_preset_combo, "1080p")
        assert panel._resize_width_spin.value() == 1920
        assert panel._resize_height_spin.value() == 1080
        assert panel._resize_value_stack.currentIndex() == 0

        panel._set_combo_data(panel._resize_mode_combo, "percent")
        assert panel._resize_value_stack.currentIndex() == 1

        panel._set_combo_data(panel._resize_mode_combo, "size")
        assert panel._resize_value_stack.currentIndex() == 2
        panel.close()

    def test_video_row_shows_geometry_and_filter_badges(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-filter-badges"
        panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0)]), entry, _COLOR)])

        idx_x265 = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "libx265"
        )
        panel._codec_combo.setCurrentIndex(idx_x265)
        panel._resize_enabled_cb.setChecked(True)
        panel._crop_enabled_cb.setChecked(True)
        panel._crop_left_spin.setValue(8)
        panel._yadif_cb.setChecked(True)
        panel._deblock_cb.setChecked(True)
        panel._denoise_cb.setChecked(True)  # MVO-R2 absent des tests : NLMeans
        panel._chroma_cb.setChecked(True)

        row_item = panel._video_list.item(0)
        assert row_item is not None
        text = row_item.text()
        for badge in ("720p", "Crop", "Yadif", "Deblock", "NLMeans", "Chroma"):
            assert f"[{badge}]" in text
        panel.close()

    def test_filter_labels_show_features_and_filter_names_stay_visible(self, qt_app):
        panel = EncodePanel(AppConfig())

        assert panel._yadif_cb.text() in {"Désentrelacement", "Deinterlacing"}
        assert panel._deblock_cb.text() == "Deblock"
        assert panel._denoise_cb.text() in {"Débruitage", "Denoise"}
        assert panel._chroma_cb.text() == "Color Smooth"
        assert panel._yadif_filter_combo.itemText(0) == "Yadif"
        assert panel._deblock_filter_combo.itemText(0) == "deblock"
        assert [panel._denoise_filter_combo.itemText(i) for i in range(2)] == ["MVO-R2", "NLMeans"]
        assert panel._chroma_filter_combo.itemText(0) == "chromanr"
        panel.close()

    def test_qsv_manual_hdr_metadata_fields_are_editable(self, qt_app):
        """V39 : valeurs saisies réinjectées en SEI, comme pour hevc_nvenc."""
        panel = EncodePanel(AppConfig())
        panel._hw_encoders = {"hevc_qsv"}
        panel._populate_codec_combo()
        entry = _video_entry(0)
        entry.entry_id = "video-qsv-hdr-lock"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.HDR10)])
        panel.set_video_tracks([(info, entry, _COLOR)])

        idx_qsv = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "hevc_qsv"
        )
        panel._codec_combo.setCurrentIndex(idx_qsv)

        assert panel._master_display.isReadOnly() is False
        assert panel._max_cll.isReadOnly() is False
        panel.close()

    def test_x265_keeps_manual_hdr_metadata_fields_editable(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-x265-hdr-edit"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.HDR10)])
        panel.set_video_tracks([(info, entry, _COLOR)])

        idx_x265 = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "libx265"
        )
        panel._codec_combo.setCurrentIndex(idx_x265)

        assert panel._master_display.isReadOnly() is False
        assert panel._max_cll.isReadOnly() is False
        panel.close()

    def test_nvencc_hevc_keeps_manual_hdr_metadata_fields_editable(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel._hw_encoders = {"nvencc_hevc"}
        panel._populate_codec_combo()
        entry = _video_entry(0)
        entry.entry_id = "video-nvencc-hevc-hdr-edit"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.HDR10)])
        panel.set_video_tracks([(info, entry, _COLOR)])

        idx_nvencc = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "nvencc_hevc"
        )
        panel._codec_combo.setCurrentIndex(idx_nvencc)

        assert panel._master_display.isReadOnly() is False
        assert panel._max_cll.isReadOnly() is False
        panel.close()

    def test_nvencc_h264_disables_dynamic_hdr_and_offers_size_mode(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel._hw_encoders = {"nvencc_h264"}
        panel._populate_codec_combo()
        entry = _video_entry(0)
        entry.entry_id = "video-nvencc-h264-dv"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.DOLBY_VISION_HDR10PLUS)])
        panel.set_video_tracks([(info, entry, _COLOR)])

        idx_nvencc = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "nvencc_h264"
        )
        panel._codec_combo.setCurrentIndex(idx_nvencc)

        mode_values = [panel._mode_combo.itemData(i) for i in range(panel._mode_combo.count())]
        assert panel._copy_dv_cb.isEnabled() is False
        assert panel._copy_hdr10plus_cb.isEnabled() is False
        assert panel._copy_dv_cb.isChecked() is False
        assert panel._copy_hdr10plus_cb.isChecked() is False
        # V33 : taille cible NVEncC en une passe VBR plafonnée.
        assert mode_values == ["qvbr", "cqp", "vbr_quality", "vbr", "cbr", "size"]
        panel.close()

    def test_nvencc_av1_enables_dynamic_hdr_passthrough(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel._hw_encoders = {"nvencc_av1"}
        panel._populate_codec_combo()
        entry = _video_entry(0)
        entry.entry_id = "video-nvencc-av1-dv"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.DOLBY_VISION_HDR10PLUS)])
        panel.set_video_tracks([(info, entry, _COLOR)])

        idx_nvencc = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "nvencc_av1"
        )
        panel._codec_combo.setCurrentIndex(idx_nvencc)

        # AV1 ne supporte pas le passthrough Dolby Vision (incompatible décodeurs TV),
        # mais supporte le passthrough dynamique HDR10+.
        assert panel._copy_dv_cb.isEnabled() is False
        assert panel._copy_hdr10plus_cb.isEnabled() is True
        assert panel._copy_dv_cb.isChecked() is False
        assert panel._copy_hdr10plus_cb.isChecked() is True
        panel.close()

    def test_nvencc_hevc_enables_both_dovi_and_hdr10plus_passthrough(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel._hw_encoders = {"nvencc_hevc"}
        panel._populate_codec_combo()
        entry = _video_entry(0)
        entry.entry_id = "video-nvencc-hevc-dv"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.DOLBY_VISION_HDR10PLUS)])
        panel.set_video_tracks([(info, entry, _COLOR)])

        idx_nvencc = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "nvencc_hevc"
        )
        panel._codec_combo.setCurrentIndex(idx_nvencc)

        assert panel._copy_dv_cb.isEnabled() is True
        assert panel._copy_hdr10plus_cb.isEnabled() is True
        assert panel._copy_dv_cb.isChecked() is True
        assert panel._copy_hdr10plus_cb.isChecked() is True
        assert panel._dovi_warning_widget.isHidden() is True
        panel.close()

    @pytest.mark.parametrize("codec", ["hevc_nvenc", "hevc_qsv", "hevc_vaapi", "hevc_amf"])
    def test_hardware_hevc_keeps_dovi_available(self, qt_app, codec):
        """Encodeurs HEVC matériels FFmpeg : RPU réinjecté par dovi_tool, copie DV disponible."""
        panel = EncodePanel(AppConfig())
        panel._hw_encoders = {codec}
        panel._populate_codec_combo()
        entry = _video_entry(0)
        entry.entry_id = f"video-{codec}-dv"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.DOLBY_VISION_HDR10PLUS)])
        panel.set_video_tracks([(info, entry, _COLOR)])
        panel._set_combo_data(panel._codec_combo, codec)

        assert panel._copy_dv_cb.isEnabled() is True
        assert panel._dovi_warning_widget.isHidden() is True
        panel.close()

    def test_av1_shows_dovi_warning_banner_and_disables_cb(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel._hw_encoders = {"av1_nvenc", "nvencc_hevc"}
        panel._populate_codec_combo()
        entry = _video_entry(0)
        entry.entry_id = "video-av1-dv-warn"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.DOLBY_VISION_HDR10PLUS)])
        panel.set_video_tracks([(info, entry, _COLOR)])

        panel._set_combo_data(panel._codec_combo, "av1_nvenc")

        # DoVi checkbox désactivée et décochée
        assert panel._copy_dv_cb.isEnabled() is False
        assert panel._copy_dv_cb.isChecked() is False
        # Bannière d'alerte visible avec picto et encodeurs HEVC proposés
        assert panel._dovi_warning_widget.isHidden() is False
        assert "⚠️" in panel._dovi_warning_icon.text()
        assert "av1_nvenc" in panel._dovi_warning_text.text()
        assert "x265" in panel._dovi_warning_text.text()

        # Bascule vers nvencc_hevc : la bannière d'alerte doit disparaître
        panel._set_combo_data(panel._codec_combo, "nvencc_hevc")
        assert panel._copy_dv_cb.isEnabled() is True
        assert panel._dovi_warning_widget.isHidden() is True
        panel.close()

    def test_x264_preserves_source_10bit_without_8bit_precheck(self, qt_app):
        cfg = AppConfig()
        cfg.language = "fra"
        panel = EncodePanel(cfg)
        entry = _video_entry(0)
        entry.entry_id = "video-h264-8bit"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.NONE, bit_depth=10)])
        logs: list[tuple[str, str]] = []
        panel.log_message.connect(lambda lvl, msg: logs.append((str(lvl), str(msg))))
        panel.set_video_tracks([(info, entry, _COLOR)])

        idx_x264 = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "libx264"
        )
        panel._codec_combo.setCurrentIndex(idx_x264)

        row_item = panel._video_list.item(0)
        assert row_item is not None
        assert "[10-bit]" in row_item.text()
        settings = panel._current_video_settings()
        assert settings.bit_depth == "auto" and resolve_output_bit_depth(settings) == 10
        assert not any("bascule auto en 8-bit" in message for _lvl, message in logs)

        idx_x265 = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "libx265"
        )
        panel._codec_combo.setCurrentIndex(idx_x265)
        settings = panel._current_video_settings()
        assert settings.bit_depth == "auto" and resolve_output_bit_depth(settings) == 10
        assert not any("retour au mode source" in message for _lvl, message in logs)
        panel.close()

    def test_x264_source_depth_is_preserved_per_track(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
        first_entry = _video_entry(0)
        first_entry.entry_id = "video-10bit"
        second_entry = _video_entry(1)
        second_entry.entry_id = "video-8bit"
        info = _file_info(
            _PATH_A,
            [
                _video_track(0, HDRType.NONE, bit_depth=10),
                _video_track(1, HDRType.NONE, bit_depth=8),
            ],
        )

        panel.set_video_tracks([(info, first_entry, _COLOR), (info, second_entry, _COLOR)])
        panel._apply_all_video_cb.setChecked(False)

        idx_x264 = next(
            i for i in range(panel._codec_combo.count())
            if panel._codec_combo.itemData(i) == "libx264"
        )
        panel._video_list.setCurrentRow(0)
        panel._codec_combo.setCurrentIndex(idx_x264)
        panel._video_list.setCurrentRow(1)
        panel._codec_combo.setCurrentIndex(idx_x264)

        cfg = panel.collect_config()
        assert cfg is not None
        by_entry = {str(video.track_entry_id): video for video in cfg.video_tracks}
        assert resolve_output_bit_depth(by_entry["video-10bit"]) == 10
        assert resolve_output_bit_depth(by_entry["video-8bit"]) == 8
        panel.close()

    def test_lot6_auto_preselection_does_not_leak_to_hdr_track(self, qt_app):
        """« Appliquer à toutes » depuis une piste SDR 8 bits : la piste HDR10 reste en 10 bits."""
        panel = EncodePanel(AppConfig())
        sdr = dataclasses.replace(_video_track(0, HDRType.NONE, bit_depth=8), color_transfer="bt709")
        hdr = dataclasses.replace(_video_track(0, HDRType.HDR10, bit_depth=10), color_transfer="smpte2084")
        sdr_entry, hdr_entry = _video_entry(0), _video_entry(0)
        sdr_entry.entry_id, hdr_entry.entry_id = "video-sdr", "video-hdr"
        hdr_info = _file_info(_PATH_B, [hdr], HDRType.HDR10)
        panel.set_video_tracks([(_file_info(_PATH_A, [sdr]), sdr_entry, _COLOR), (hdr_info, hdr_entry, _COLOR)])
        panel._video_list.setCurrentRow(0)
        _select_codec(panel, "libx265")
        assert panel._bit_depth_combo.currentData() == "auto"
        assert resolve_output_bit_depth(panel._current_video_settings()) == 8
        panel._apply_all_video_cb.setChecked(True)
        state = panel._video_settings_by_entry_id["video-hdr"]
        settings = panel._video_settings_from_state(file_info=hdr_info, track=hdr_entry, state=state)
        assert resolve_output_bit_depth(settings) == 10 and not bit_depth_error(settings)
        panel.close()

    def test_dynamic_hdr_settings_are_independent_per_video_entry_when_apply_all_is_disabled(self, qt_app):
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
        panel._apply_all_video_cb.setChecked(False)
        _select_codec(panel, "libx265")
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
                copy_dv=True,
                copy_hdr10plus=True,
                dovi_profile="0",
            ),
            audio_tracks=[],
            copy_dv=True,
            copy_hdr10plus=True,
        )

        assert panel.is_pure_copy(cast(Any, config)) is True
        panel.close()

    def test_dovi_normalization_forces_encode_route_when_video_is_copy(self, qt_app):
        panel = EncodePanel(AppConfig())
        config = SimpleNamespace(
            video=SimpleNamespace(
                codec="copy",
                inject_hdr_meta=False,
                tonemap_to_sdr=False,
                copy_dv=True,
                copy_hdr10plus=False,
                dovi_profile="2",
            ),
            audio_tracks=[],
        )

        assert panel.is_pure_copy(cast(Any, config)) is False
        panel.close()

    def test_is_pure_copy_is_false_if_any_video_track_requires_encode(self, qt_app):
        panel = EncodePanel(AppConfig())
        config = SimpleNamespace(
            video=SimpleNamespace(codec="copy", inject_hdr_meta=False, tonemap_to_sdr=False),
            video_tracks=[
                SimpleNamespace(codec="copy", inject_hdr_meta=False, tonemap_to_sdr=False),
                SimpleNamespace(codec="libx265", inject_hdr_meta=False, tonemap_to_sdr=False),
            ],
            audio_tracks=[SimpleNamespace(codec="copy")],
        )

        assert panel.is_pure_copy(cast(Any, config)) is False
        panel.close()

    def test_collect_config_keeps_all_video_tracks_after_addition_in_per_track_mode(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel.set_output_provider(lambda: Path("/tmp/out.mkv"))

        info = _file_info(
            _PATH_A,
            [
                _video_track(0, HDRType.NONE),
                _video_track(1, HDRType.NONE),
            ],
        )
        first_entry = _video_entry(0)
        first_entry.entry_id = "video-first"
        second_entry = _video_entry(1)
        second_entry.entry_id = "video-second"

        panel.set_video_tracks([(info, first_entry, _COLOR)])
        panel._apply_all_video_cb.setChecked(False)
        panel.set_video_tracks([
            (info, first_entry, _COLOR),
            (info, second_entry, _COLOR),
        ])

        cfg = panel.collect_config()
        assert cfg is not None
        assert [video.track_entry_id for video in cfg.video_tracks] == [
            "video-first",
            "video-second",
        ]
        panel.close()


class TestEncodePanelRunOperation:

    def test_collect_config_uses_shared_mux_backend_provider(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
        panel.set_mux_backend_provider(lambda: "native")
        entry = _video_entry(0)
        entry.entry_id = "video-mux-provider"
        panel.set_video_tracks([(
            _file_info(_PATH_A, [_video_track(0)]), entry, _COLOR,
        )])

        config = panel.collect_config()

        assert config is not None
        assert config.mux_backend == "native"
        panel.close()

    def test_run_operation_skips_duplicate_workflow_validation(self, qt_app, monkeypatch):
        panel = EncodePanel(AppConfig())
        config = object()
        captured: dict[str, object] = {}
        expected_signals = object()

        def fake_run(cfg, *, validate=True, retain_failed_work_dir=False):
            captured["config"] = cfg
            captured["validate"] = validate
            captured["retain_failed_work_dir"] = retain_failed_work_dir
            return expected_signals

        monkeypatch.setattr(panel._workflow, "run", fake_run)

        assert panel.run_operation(cast(Any, config)) is expected_signals
        # Échec : dossier de travail gardé pour la popup de l'interface.
        assert captured == {"config": config, "validate": False, "retain_failed_work_dir": True}
        panel.close()


class TestEncodePanelInterpolationTta:
    def test_tta_combo_round_trip_badge_and_enabling(self, qt_app, monkeypatch):
        from core.workflows.encode import FrameInterpolationSettings

        panel = EncodePanel(AppConfig())
        monkeypatch.setattr(panel, "_interpolation_tool_available", lambda: True)
        panel._sync_interpolation_availability()

        panel._apply_interpolation_settings(FrameInterpolationSettings(enabled=True, factor=2, tta=4))
        tta = panel._interp_tta_combo
        # état propre de la liste (les parents restent désactivés tant que le codec est « copy »)
        parent = tta.parentWidget()
        assert parent is not None
        assert tta.isEnabledTo(parent)
        assert panel._current_interpolation_settings().tta == 4
        badges = panel._video_filter_badges_from_state(
            {"interpolation": FrameInterpolationSettings(enabled=True, factor=2, tta=4)}
        )
        assert any(b.startswith("MVO-RIFE x2") and "TTA ×4" in b for b in badges)

        panel._interp_cb.setChecked(False)
        parent = tta.parentWidget()
        assert parent is not None
        assert not tta.isEnabledTo(parent)
        # valeur hors liste (preset édité à la main) : retour à « Désactivé »
        panel._apply_interpolation_settings(FrameInterpolationSettings(enabled=True, tta=3))
        assert panel._current_interpolation_settings().tta == 1
        panel.close()


class TestEncodePanelAuditLot1:

    def test_v28_preset_change_updates_track_state(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-preset"
        panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0)]), entry, _COLOR)])
        _select_codec(panel, "libx265")
        panel._preset_combo.setCurrentIndex(panel._preset_combo.findData("veryfast"))
        assert panel._video_settings_by_entry_id["video-preset"]["preset"] == "veryfast"
        panel.close()

    def test_v08_job_duration_follows_primary_track_not_selection(self, qt_app, tmp_path):
        panel = EncodePanel(AppConfig())
        panel.set_output_provider(lambda: tmp_path / "out.mkv")
        first = _video_entry(0)
        first.entry_id = "video-main"
        second = _video_entry(0)
        second.entry_id = "video-other"
        short = dataclasses.replace(_file_info(_PATH_B, [_video_track(0)]), duration_s=3600.0)
        panel.set_video_tracks([
            (_file_info(_PATH_A, [_video_track(0)]), first, _COLOR),
            (short, second, _COLOR),
        ])
        panel._video_list.setCurrentRow(1)
        assert panel.get_duration_s() == 7200.0
        config = panel.collect_config()
        assert config is not None and config.duration_s == 7200.0
        panel.close()

    def test_v29b_incompatible_profile_is_not_applied(self, qt_app, tmp_path):
        panel = EncodePanel(AppConfig())
        panel._profiles = ProfileManager(tmp_path)
        _select_codec(panel, "libx265")
        panel._crf_spin.setValue(20)
        warnings: list[str] = []
        panel.log_message.connect(lambda level, msg: warnings.append(msg) if level == "WARN" else None)
        for name, preset in (
            ("absent", EncodePreset(name="absent", codec="libfoo", crf=30)),
            ("preset", EncodePreset(name="preset", codec="libx265", preset="p7", crf=30)),
        ):
            panel._profiles.save(preset)
            panel._refresh_profiles(select=name)
            panel._load_profile()
            assert panel._codec_combo.currentData() == "libx265"
            assert panel._crf_spin.value() == 20
        assert len(warnings) == 2
        panel.close()


class TestEncodePanelAuditLot2:

    def test_v13_single_track_codecs_disabled_with_several_video_tracks(self, qt_app):
        from PySide6.QtGui import QStandardItemModel

        panel = EncodePanel(AppConfig())
        panel._hw_encoders = {"nvencc_hevc"}
        panel._populate_codec_combo()
        row = panel._codec_combo.findData("nvencc_hevc")
        model = panel._codec_combo.model()
        assert isinstance(model, QStandardItemModel)
        first = _video_entry(0)
        first.entry_id = "v1"
        second = _video_entry(1)
        second.entry_id = "v2"
        info = _file_info(_PATH_A, [_video_track(0), _video_track(1)])
        panel.set_video_tracks([(info, first, _COLOR), (info, second, _COLOR)])
        assert model.item(row).isEnabled() is False
        panel.set_video_tracks([(info, first, _COLOR)])
        assert model.item(row).isEnabled() is True
        panel.close()

    def test_v02_extra_params_are_kept_per_codec(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-extras"
        panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0)]), entry, _COLOR)])
        _select_codec(panel, "libx265")
        panel._extra_params.setText("no-open-gop=1")
        _select_codec(panel, "libx264")
        assert panel._extra_params.text() == ""
        panel._extra_params.setText("-tune film")
        _select_codec(panel, "libx265")
        assert panel._extra_params.text() == "no-open-gop=1"
        _select_codec(panel, "libx264")
        assert panel._extra_params.text() == "-tune film"
        assert panel._current_video_settings().extra_params == "-tune film"
        panel.close()

    def test_v37_invalid_rate_is_not_replaced_silently(self, qt_app):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = "video-rate"
        panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0)]), entry, _COLOR)])
        for edit in (panel._bitrate_edit, panel._size_edit):
            validator = edit.validator()
            # Arbitrage : entiers > 0, sans borne haute applicative.
            assert validator is not None and validator.bottom() == 1 and validator.top() == 2**31 - 1
        panel._bitrate_edit.setText("")
        assert panel._current_video_settings().bitrate_kbps == -1
        panel.close()

    def test_vaapi_offers_no_preset_entry(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel._hw_encoders = {"hevc_vaapi"}
        panel._populate_codec_combo()
        _select_codec(panel, "hevc_vaapi")
        assert panel._preset_combo.itemData(0) == ""
        assert panel._preset_combo.itemText(0) == "Aucun (défaut pilote)"
        panel._preset_combo.setCurrentIndex(0)
        assert panel._current_video_settings().preset == ""
        panel.close()

    def test_vaapi_mesa_presets_are_labelled_and_default_to_quality_preencode(self, qt_app):
        from core.workflows.encode.catalog import MESA_VAAPI_PRESETS

        panel = EncodePanel(AppConfig())
        try:
            panel._on_hw_detected({"hevc_vaapi"}, panel._sw_encoders, panel._config.tool_ffmpeg, {},
                                  None, {"hevc_vaapi": frozenset(MESA_VAAPI_PRESETS)}, {"hevc_vaapi": "mesa"})
            _select_codec(panel, "hevc_vaapi")
            items = {panel._preset_combo.itemData(i): panel._preset_combo.itemText(i)
                     for i in range(panel._preset_combo.count())}
            assert set(items) == set(MESA_VAAPI_PRESETS)
            assert items[""] == "Speed (défaut pilote)"
            assert items["29"] == "29 — Quality + pré-encodage + VBAQ"
            assert panel._current_video_settings().preset == "13"
        finally:
            panel.close()

    def test_vaapi_intel_presets_keep_driver_default(self, qt_app):
        panel = EncodePanel(AppConfig())
        try:
            levels = frozenset({"", *(str(i) for i in range(8))})
            panel._on_hw_detected({"hevc_vaapi"}, panel._sw_encoders, panel._config.tool_ffmpeg, {},
                                  None, {"hevc_vaapi": levels}, {"hevc_vaapi": "intel"})
            _select_codec(panel, "hevc_vaapi")
            items = [panel._preset_combo.itemText(i) for i in range(panel._preset_combo.count())]
            assert items[:3] == ["Aucun (défaut pilote)", "0", "1 — qualité max"] and "13" not in items
            assert panel._current_video_settings().preset == ""
        finally:
            panel.close()


class TestEncodePanelAuditLot3:
    _MD = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"

    def _panel(self, hdr_type: HDRType, transfer: str | None = "smpte2084", entry_id: str = "video-lot3"):
        panel = EncodePanel(AppConfig())
        entry = _video_entry(0)
        entry.entry_id = entry_id
        track = dataclasses.replace(_video_track(0, hdr_type), color_transfer=transfer)
        panel.set_video_tracks([(_file_info(_PATH_A, [track]), entry, _COLOR)])
        _select_codec(panel, "libx265")
        return panel

    def test_v24_d2_unchecking_static_hdr_keeps_hdr_output_and_dolby_vision(self, qt_app):
        panel = self._panel(HDRType.DOLBY_VISION)
        assert panel._inject_hdr_cb.isChecked() and panel._copy_dv_cb.isChecked()
        panel._inject_hdr_cb.setChecked(False)
        assert panel._tonemap_cb.isChecked() is False
        assert panel._copy_dv_cb.isChecked() and panel._copy_dv_cb.isEnabled()
        video = panel._current_video_settings()
        assert (video.inject_hdr_meta, video.copy_dv, video.source_color_transfer) == (False, True, "smpte2084")
        panel.close()

    def test_tonemap_greys_hdr_options_without_forgetting_them(self, qt_app):
        panel = self._panel(HDRType.DOLBY_VISION)
        panel._tonemap_cb.setChecked(True)
        assert not panel._inject_hdr_cb.isEnabled() and not panel._copy_dv_cb.isEnabled()
        assert panel._inject_hdr_cb.isChecked() and panel._copy_dv_cb.isChecked()
        video = panel._current_video_settings()
        assert (video.tonemap_to_sdr, video.inject_hdr_meta, video.copy_dv) == (True, False, False)
        panel._tonemap_cb.setChecked(False)
        assert panel._inject_hdr_cb.isEnabled() and panel._copy_dv_cb.isEnabled()
        assert panel._current_video_settings().copy_dv is True
        panel.close()

    def test_v24_v25_manual_values_kept_and_source_values_restorable(self, qt_app):
        panel = self._panel(HDRType.HDR10)
        state = panel._video_settings_by_entry_id["video-lot3"]
        state["default_master_display"], state["default_max_cll"] = self._MD, "1000,400"
        panel._master_display.setText("G(1,1)B(1,1)R(1,1)WP(1,1)L(1,1)")
        assert panel._hdr_meta_provenance.text() == "Saisie manuelle."
        panel._inject_hdr_cb.setChecked(False)
        panel._inject_hdr_cb.setChecked(True)
        assert panel._master_display.text() == "G(1,1)B(1,1)R(1,1)WP(1,1)L(1,1)"
        panel._hdr_meta_source_btn.click()
        assert (panel._master_display.text(), panel._max_cll.text()) == (self._MD, "1000,400")
        assert panel._hdr_meta_provenance.text() == "Valeurs de la source."
        panel.close()

    def test_d3_hlg_dolby_vision_static_hdr_unchecked_but_available(self, qt_app):
        panel = self._panel(HDRType.DOLBY_VISION, transfer="arib-std-b67")
        assert panel._inject_hdr_cb.isChecked() is False and panel._inject_hdr_cb.isEnabled()
        assert panel._current_video_settings().source_color_transfer == "arib-std-b67"
        panel.close()

    def test_d1_tonemap_available_on_sdr_source_with_warning(self, qt_app):
        panel = self._panel(HDRType.NONE, transfer="bt709")
        assert panel._tonemap_cb.isEnabled()
        assert "pas détectée HDR" in panel._tonemap_cb.toolTip()
        panel.close()

    def test_track_badge_follows_hdr_output_without_static_metadata(self, qt_app):
        panel = self._panel(HDRType.HDR10)
        panel._inject_hdr_cb.setChecked(False)
        state = panel._video_settings_by_entry_id["video-lot3"]
        source_video = panel._video_track_for_entry(*panel._video_tracks[0][:2])
        assert panel._video_hdr_badges_from_state(state, source_video=source_video) == ("HDR",)
        panel.close()
        hlg = self._panel(HDRType.HLG, transfer="arib-std-b67", entry_id="video-hlg")
        state = hlg._video_settings_by_entry_id["video-hlg"]
        source_video = hlg._video_track_for_entry(*hlg._video_tracks[0][:2])
        assert hlg._video_hdr_badges_from_state(state, source_video=source_video) == ("HLG",)
        hlg.close()

    @pytest.mark.parametrize("unsupported", ["av1_nvenc", "libx264"])
    def test_review_hdr_preferences_survive_track_navigation(self, qt_app, unsupported):
        panel = self._panel(HDRType.DOLBY_VISION_HDR10PLUS)
        panel._on_hw_detected({"av1_nvenc"}, panel._sw_encoders, panel._config.tool_ffmpeg, {})
        first = panel._video_tracks[0]
        other = _video_entry(0)
        other.entry_id = "other-video"
        panel.set_video_tracks([first, (_file_info(_PATH_B, [_video_track(0, HDRType.NONE)]), other, _COLOR)])
        assert panel._copy_dv_cb.isChecked()
        _select_codec(panel, unsupported)
        assert not panel._copy_dv_cb.isChecked()
        panel._video_list.setCurrentRow(1)
        assert not panel._copy_dv_cb.isChecked()
        panel._video_list.setCurrentRow(0)
        _select_codec(panel, "libx265")
        assert panel._copy_dv_cb.isChecked() and panel._copy_hdr10plus_cb.isChecked()
        assert panel._inject_hdr_cb.isChecked()
        panel.close()


class TestEncodePanelAuditLot4:
    """Lot 4 : modes de débit par codec (V19 / V20), presets par défaut (V41)."""

    def _panel(self, hw: set[str], rate_controls: dict[str, frozenset[str]] | None = None, tracks: int = 1):
        panel = EncodePanel(AppConfig())
        panel._on_hw_detected(hw, panel._sw_encoders, panel._config.tool_ffmpeg, rate_controls or {})
        info = _file_info(_PATH_A, [_video_track(i, HDRType.NONE) for i in range(tracks)])
        entries = []
        for i in range(tracks):
            entry = _video_entry(i)
            entry.entry_id = f"video-lot4-{i}"
            entries.append((info, entry, _COLOR))
        panel.set_video_tracks(entries)
        return panel

    @staticmethod
    def _modes(panel: EncodePanel) -> list[str]:
        return [panel._mode_combo.itemData(i) for i in range(panel._mode_combo.count())]

    @pytest.mark.parametrize(("codec", "mode"), [
        *((codec, mode) for codec in ("nvencc_hevc", "nvencc_h264", "nvencc_av1") for mode in ("vbr", "vbr_quality")),
        ("hevc_nvenc", "vbr"), ("h264_nvenc", "vbr"), ("av1_nvenc", "vbr"),
    ])
    def test_vbr_zero_accepts_and_preserves_zero(self, qt_app, codec, mode):
        from core.workflows.encode.planning.validation import video_settings_errors

        panel = self._panel({codec})
        try:
            _select_codec(panel, codec)
            panel._set_combo_data(panel._mode_combo, mode)
            panel._bitrate_edit.setText("0")
            assert panel._bitrate_edit.hasAcceptableInput()
            assert video_settings_errors([panel._current_video_settings()]) == []
            state = panel._current_video_state()
            state["bitrate_kbps"] = 0  # anciens profils / état numérique
            panel._apply_video_state(state)
            assert panel._bitrate_edit.text() == "0"
            assert panel._current_video_settings().bitrate_kbps == 0
            assert "0 kbps" in panel._rate_control_summary(state)
            panel._bitrate_edit.clear()
            assert video_settings_errors([panel._current_video_settings()])
            panel._set_combo_data(panel._mode_combo, "cbr")
            panel._bitrate_edit.setText("0")
            assert not panel._bitrate_edit.hasAcceptableInput()
            assert video_settings_errors([panel._current_video_settings()])
        finally:
            panel.close()

    def test_v19_mode_list_follows_codec_and_driver(self, qt_app):
        panel = self._panel({"hevc_vaapi"}, {"hevc_vaapi": frozenset({"cqp", "qvbr", "vbr", "size"})})
        _select_codec(panel, "hevc_vaapi")
        assert self._modes(panel) == ["cqp", "qvbr", "vbr", "size"]
        assert panel._preset_combo.currentData() == ""
        panel._set_combo_data(panel._mode_combo, "qvbr")
        assert not panel._quality_value_label.isHidden() and panel._quality_value_label.text() == "Qualité"
        assert not panel._bitrate_widget.isHidden() and panel._size_widget.isHidden()
        panel._cq_spin.setValue(28)
        panel._bitrate_edit.setText("9000")
        video = panel._current_video_settings()
        assert (video.rate_control, video.quality_mode.value, video.cq, video.bitrate_kbps) == (
            "qvbr", "cq", 28, 9000,
        )
        plan = panel._video_plan_from_state(
            entry_id="video-lot4-0", state=panel._current_video_state(), source_video=None,
        )
        assert plan.codec_summary == "hevc_vaapi - Qualité VBR (QVBR) (Qualité 28, 9000 kbps)"
        panel._set_combo_data(panel._mode_combo, "size")
        assert panel._quality_value_label.isHidden() and panel._bitrate_widget.isHidden()
        assert not panel._size_widget.isHidden()
        panel.close()

    def test_quality_scale_follows_mode(self, qt_app):
        panel = self._panel({"hevc_nvenc", "av1_nvenc"})
        _select_codec(panel, "libx265")
        panel._crf_spin.setValue(20)
        _select_codec(panel, "hevc_nvenc")
        assert panel._mode_combo.currentData() == "vbr_cq"
        assert (panel._cq_spin.minimum(), panel._cq_spin.maximum(), panel._cq_spin.value()) == (1, 51, 26)
        assert panel._preset_combo.currentData() == "p5"
        panel._cq_spin.setValue(30)
        panel._set_combo_data(panel._mode_combo, "constqp")
        assert panel._quality_value_label.text() == "QP" and panel._cq_spin.value() == 24
        _select_codec(panel, "av1_nvenc")
        assert panel._mode_combo.currentData() == "constqp"
        assert (panel._cq_spin.maximum(), panel._cq_spin.value()) == (255, 96)
        _select_codec(panel, "libx265")
        assert panel._mode_combo.currentData() == "crf" and panel._crf_spin.value() == 20
        panel.close()

    def test_rate_control_restored_per_track(self, qt_app):
        panel = self._panel({"hevc_vaapi"}, tracks=2)
        _select_codec(panel, "hevc_vaapi")
        panel._set_combo_data(panel._mode_combo, "icq")
        panel._cq_spin.setValue(33)
        panel._video_list.setCurrentRow(1)
        assert panel._codec_combo.currentData() == "copy"
        panel._video_list.setCurrentRow(0)
        assert panel._mode_combo.currentData() == "icq" and panel._cq_spin.value() == 33
        panel.close()

    def test_review_profile_cannot_select_disabled_multi_video_codec(self, qt_app, tmp_path):
        panel = self._panel({"nvencc_hevc"}, tracks=2)
        _select_codec(panel, "libx265")
        panel._profiles = ProfileManager(tmp_path)
        panel._profiles.save(EncodePreset(name="nv", codec="nvencc_hevc", preset="default", rate_control="qvbr"))
        panel._refresh_profiles(select="nv")
        before = panel._current_video_state()
        panel._load_profile()
        assert panel._current_video_state() == before
        panel.close()

    def test_v41_hw_detection_keeps_codec_preset(self, qt_app):
        panel = self._panel(set())
        _select_codec(panel, "libx265")
        panel._preset_combo.setCurrentIndex(panel._preset_combo.findData("veryslow"))
        panel._on_hw_detected({"hevc_nvenc"}, panel._sw_encoders, panel._config.tool_ffmpeg, {})
        assert panel._codec_combo.currentData() == "libx265"
        assert panel._preset_combo.currentData() == "veryslow"
        panel.close()

    def test_legacy_profile_migrates_quality_value(self, qt_app, tmp_path):
        panel = self._panel({"hevc_nvenc"})
        panel._profiles = ProfileManager(tmp_path)
        for name, preset, mode, spin in (
            ("x265-cq", EncodePreset(name="x265-cq", codec="libx265", quality_mode="cq", cq=24), "crf", "_crf_spin"),
            ("nvenc-crf", EncodePreset(name="nvenc-crf", codec="hevc_nvenc", quality_mode="crf", crf=21,
                                       preset="p7"), "vbr_cq", "_cq_spin"),
        ):
            panel._profiles.save(preset)
            panel._refresh_profiles(select=name)
            panel._load_profile()
            assert panel._mode_combo.currentData() == mode
            assert getattr(panel, spin).value() == (24 if mode == "crf" else 21)
        assert panel._preset_combo.currentData() == "p7"
        panel.close()

    def test_driver_refused_mode_profile_is_not_applied(self, qt_app, tmp_path):
        panel = self._panel({"hevc_vaapi"}, {"hevc_vaapi": frozenset({"cqp", "vbr", "size"})})
        panel._profiles = ProfileManager(tmp_path)
        _select_codec(panel, "libx265")
        warnings: list[str] = []
        panel.log_message.connect(lambda level, msg: warnings.append(msg) if level == "WARN" else None)
        panel._profiles.save(EncodePreset(name="icq", codec="hevc_vaapi", rate_control="icq", preset=""))
        panel._refresh_profiles(select="icq")
        panel._load_profile()
        assert panel._codec_combo.currentData() == "libx265" and len(warnings) == 1
        panel.close()


class TestEncodePanelAuditLot5:
    """Lot 5 : matrice Dolby Vision V30 et tone-mapping vers un codec sans HDR (RV5-02)."""

    def _panel(self, hdr_type: HDRType, *, dovi_profile: int | None = None, compat: int | None = None,
               transfer: str | None = "smpte2084"):
        panel = EncodePanel(AppConfig())
        panel._hw_encoders = {"h264_nvenc"}
        panel._populate_codec_combo()
        video = dataclasses.replace(_video_track(0, hdr_type), color_transfer=transfer)
        video.dovi_profile = dovi_profile
        video.dovi_compat_id = compat
        entry = _video_entry(0)
        entry.entry_id = "video-lot5"
        panel.set_video_tracks([(_file_info(_PATH_A, [video]), entry, _COLOR)])
        return panel

    @staticmethod
    def _normalize_item(panel: EncodePanel):
        return panel._dovi_p81_cb, 0

    def test_p5_copy_cannot_normalize_but_reencode_can(self, qt_app, monkeypatch):
        panel = self._panel(HDRType.DOLBY_VISION, dovi_profile=5, transfer=None)
        # Fenêtre « Analyse HDR10 estimée » (modale) proposée à la normalisation P5.
        monkeypatch.setattr(panel, "_ask_static_hdr_estimate_mode", lambda: "")
        _select_codec(panel, "copy")
        item, _index = self._normalize_item(panel)
        assert not item.isEnabled()
        assert "réencodée" in item.toolTip()
        _select_codec(panel, "libx265")
        item, _index = self._normalize_item(panel)
        assert item.isEnabled()
        assert "P5" in panel._dovi_p81_note.text() and not panel._dovi_p81_note.isHidden()
        panel._dovi_p81_cb.click()
        _select_codec(panel, "copy")
        # Normaliser devenu impossible en copie : retour à « Conserver ».
        assert panel._dovi_profile_choice == "0" and not panel._dovi_p81_cb.isChecked()
        panel.close()

    def test_p8_4_never_offers_normalize(self, qt_app):
        panel = self._panel(HDRType.DOLBY_VISION, dovi_profile=8, compat=4, transfer="arib-std-b67")
        for codec in ("copy", "libx265"):
            _select_codec(panel, codec)
            item, _index = self._normalize_item(panel)
            assert not item.isEnabled()
        panel.close()

    def test_rv5_02_tonemap_prefilled_for_sdr_only_codec(self, qt_app):
        panel = self._panel(HDRType.HDR10)
        _select_codec(panel, "libx265")
        assert not panel._tonemap_cb.isChecked()
        _select_codec(panel, "libx264")
        assert panel._tonemap_cb.isChecked()
        assert panel._current_video_settings().tonemap_to_sdr
        _select_codec(panel, "libx265")
        assert not panel._tonemap_cb.isChecked()
        # Décoché à la main sur H.264 : le choix de l'utilisateur est conservé.
        _select_codec(panel, "h264_nvenc")
        assert panel._tonemap_cb.isChecked()
        panel._tonemap_cb.setChecked(False)
        # L5-A05 : le décochage manuel n'est pas annulé par la synchronisation qui suit.
        assert not panel._tonemap_cb.isChecked()
        assert not panel._current_video_settings().tonemap_to_sdr
        _select_codec(panel, "libx265")
        _select_codec(panel, "libx264")
        assert panel._tonemap_cb.isChecked()
        panel.close()

    def test_rv5_02_sdr_source_is_not_tonemapped(self, qt_app):
        panel = self._panel(HDRType.NONE, transfer="bt709")
        _select_codec(panel, "libx264")
        assert not panel._tonemap_cb.isChecked()
        panel.close()

    def test_l5_a11_p8_2_sdr_base_keeps_sdr_defaults(self, qt_app):
        """P8.2 (base BT.709) : ni HDR10 statique par défaut, ni tone-mapping d'office ; DV copié."""
        panel = self._panel(HDRType.DOLBY_VISION, dovi_profile=8, compat=2, transfer="bt709")
        _select_codec(panel, "libx265")
        assert not panel._inject_hdr_cb.isChecked()
        assert panel._copy_dv_cb.isChecked()
        settings = panel._current_video_settings()
        assert not settings.inject_hdr_meta and settings.copy_dv
        _select_codec(panel, "libx264")
        assert not panel._tonemap_cb.isChecked()
        panel.close()

    def test_p7_copy_keep_stays_pure_copy(self, qt_app):
        panel = self._panel(HDRType.DOLBY_VISION, dovi_profile=7, transfer="smpte2084")
        panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
        _select_codec(panel, "copy")
        assert panel._dovi_profile_choice == "0" and not panel._dovi_p81_cb.isChecked()
        config = panel.collect_config()
        assert config is not None and config.video is not None
        assert config.video.codec == "copy" and config.video.dovi_profile == "0" and config.video.copy_dv
        assert not config.video.p5_to_hdr10 and not config.video.dovi_source_profile
        # Remux pur si aucune autre transformation n'est demandée (HDR10 statique source non réécrit).
        pure = dataclasses.replace(config.video, inject_hdr_meta=False)
        assert panel.is_pure_copy(dataclasses.replace(config, video=pure, video_tracks=[pure], audio_tracks=[]))
        panel.close()

    def test_p7_p81_checkbox_follows_copy_and_reencode(self, qt_app):
        """Case P8.1 sous la copie du RPU : libre en copie, cochée et figée en réencodage (P7 → P8.1)."""
        panel = self._panel(HDRType.DOLBY_VISION, dovi_profile=7, transfer="smpte2084")
        layout = panel._copy_dv_cb.parentWidget().layout()
        assert layout.indexOf(panel._dovi_profile_widget) == layout.indexOf(panel._copy_dv_cb) + 1
        _select_codec(panel, "copy")
        assert panel._dovi_p81_cb.isEnabled() and not panel._dovi_p81_cb.isChecked()
        panel._dovi_p81_cb.click()
        assert panel._current_video_settings().dovi_profile == "2"
        assert "sans réencodage" in panel._dovi_p81_note.text() and not panel._dovi_plan_label.text()
        panel._dovi_p81_cb.click()
        _select_codec(panel, "libx265")
        assert panel._dovi_p81_cb.isChecked() and not panel._dovi_p81_cb.isEnabled()
        assert "toujours P8.1" in panel._dovi_p81_cb.toolTip()
        _select_codec(panel, "copy")
        # Choix de copie conservé (décoché) après le passage en réencodage.
        assert panel._dovi_p81_cb.isEnabled() and not panel._dovi_p81_cb.isChecked()
        assert panel._current_video_settings().dovi_profile == "0"
        panel.close()


class TestEncodePanelAuditLot7:
    """Lot 7 : moteur des filtres NVEncC (V18b) et règle de propagation unique (V43)."""

    def test_v18b_badge_shows_nvencc_filter_engine(self, qt_app):
        panel = EncodePanel(AppConfig())
        panel._hw_encoders = {"nvencc_hevc"}
        panel._populate_codec_combo()
        entry = _video_entry(0)
        entry.entry_id = "video-filters"
        panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0, HDRType.NONE, bit_depth=8)]), entry, _COLOR)])
        _select_codec(panel, "nvencc_hevc")
        state = dict(panel._video_settings_by_entry_id["video-filters"])
        state["filters"] = VideoFilterSettings(nlmeans_enabled=True)
        assert "Filtres NVEncC" in panel._video_filter_badges_from_state(state)
        state["filters"] = VideoFilterSettings(deblock_enabled=True)
        assert "Filtres FFmpeg" in panel._video_filter_badges_from_state(state)
        _select_codec(panel, "libx265")
        state = dict(panel._video_settings_by_entry_id["video-filters"])
        state["filters"] = VideoFilterSettings(nlmeans_enabled=True)
        assert not any(badge.startswith("Filtres") for badge in panel._video_filter_badges_from_state(state))
        panel.close()

    def test_v43_copy_is_never_propagated_by_apply_all(self, qt_app):
        panel = EncodePanel(AppConfig())
        first, second = _video_entry(0), _video_entry(1)
        first.entry_id, second.entry_id = "video-1", "video-2"
        info = _file_info(_PATH_A, [_video_track(0, HDRType.NONE), _video_track(1, HDRType.NONE)])
        panel.set_video_tracks([(info, first, _COLOR), (info, second, _COLOR)])
        panel._video_list.setCurrentRow(0)
        panel._apply_all_video_cb.setChecked(True)
        _select_codec(panel, "libx265")
        assert panel._video_settings_by_entry_id["video-2"]["codec"] == "libx265"
        # Retour en Copy sur la piste 1 : la piste 2 garde son encodage (même règle partout).
        _select_codec(panel, "copy")
        assert panel._video_settings_by_entry_id["video-1"]["codec"] == "copy"
        assert panel._video_settings_by_entry_id["video-2"]["codec"] == "libx265"
        panel.close()


def test_file_target_size_is_shared_by_all_video_tracks(qt_app):
    """A09 : la taille cible porte sur le fichier, une seule valeur pour toutes les pistes."""
    from core.workflows.encode.models import QualityMode, VideoEncodeSettings

    panel = EncodePanel(AppConfig())
    try:
        panel._video_settings_by_entry_id = {"a": {"target_size_mb": "4000"}, "b": {"target_size_mb": "900"}}
        panel._size_edit.setText("1234")
        assert {state["target_size_mb"] for state in panel._video_settings_by_entry_id.values()} == {"1234"}
        sized = VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.SIZE, target_size_mb=900)
        crf = VideoEncodeSettings(codec="libx265", quality_mode=QualityMode.CRF)
        assert panel._file_target_size_mb([crf, sized]) == 1234
        assert panel._file_target_size_mb([crf]) is None
    finally:
        panel.deleteLater()


def test_interpolation_fractional_factor_and_legacy_target(qt_app, monkeypatch):
    from core.workflows.encode import FrameInterpolationSettings

    panel = EncodePanel(AppConfig())
    try:
        monkeypatch.setattr(panel, "_interpolation_tool_available", lambda: True)
        panel._sync_interpolation_availability()
        combo = panel._interp_factor_combo
        assert [combo.itemData(i) for i in range(combo.count())] == ["2", "2.5", "3", "4"]
        panel._apply_interpolation_settings(FrameInterpolationSettings(enabled=True, factor=2.5))
        assert combo.currentText() == "x2,5"
        settings = panel._current_interpolation_settings()
        assert settings.factor == 2.5
        assert settings.target_fps == ""
        badges = panel._video_filter_badges_from_state(
            {"interpolation": FrameInterpolationSettings(enabled=True, factor=2.5)}
        )
        assert any("MVO-RIFE x2,5" in badge for badge in badges)
        panel._apply_interpolation_settings(FrameInterpolationSettings(enabled=True, target_fps="60000/1001"))
        assert panel._current_interpolation_settings().target_fps == "60000/1001"
    finally:
        panel.close()


@pytest.mark.parametrize("target", ["60", "59.94", "60000/1001", "2", "invalide", "0/0"])
def test_interpolation_target_restoration_keeps_fps_distinct_from_multiplier(qt_app, monkeypatch, target):
    from core.workflows.encode import FrameInterpolationSettings

    panel = EncodePanel(AppConfig())
    try:
        panel._codec_combo.setCurrentIndex(panel._codec_combo.findData("libx265"))
        monkeypatch.setattr(panel, "_interpolation_tool_available", lambda: True)
        panel._sync_interpolation_availability()
        panel._apply_interpolation_settings(FrameInterpolationSettings(enabled=True, target_fps=target))
        restored = panel._current_interpolation_settings()
        assert restored.target_fps == target
        assert restored.factor == 2
        assert panel._interp_factor_combo.currentText().endswith(" i/s")
        # Le badge doit aussi accepter un profil invalide, que la validation
        # du workflow refusera ensuite sans interrompre le chargement de l'UI.
        assert panel._video_filter_badges_from_state({"interpolation": restored})
        panel._apply_interpolation_settings(FrameInterpolationSettings(enabled=True, factor=2))
        assert panel._current_interpolation_settings().target_fps == ""
        assert panel._interp_factor_combo.currentText() == "x2"
    finally:
        panel.close()


def test_legacy_integer_target_fps_survives_profile_loading(qt_app, tmp_path, monkeypatch):
    from core.workflows.encode import FrameInterpolationSettings

    panel = EncodePanel(AppConfig())
    try:
        monkeypatch.setattr(panel, "_interpolation_tool_available", lambda: True)
        panel._sync_interpolation_availability()
        panel._profiles = ProfileManager(tmp_path / "profiles")
        panel._profiles.save(EncodePreset(name="60 fps", interpolation=FrameInterpolationSettings(enabled=True, target_fps="60")))
        panel._refresh_profiles(select="60 fps")
        panel._load_profile()
        restored = panel._current_interpolation_settings()
        assert restored.enabled
        assert restored.target_fps == "60"
        assert restored.factor == 2
    finally:
        panel.close()


def test_apply_all_codec_round_trip_through_sdr_restores_dynamic_hdr(qt_app, monkeypatch):
    """« Appliquer à toutes » : HEVC → H.264 → HEVC rétablit HDR10, Dolby Vision et HDR10+ sur chaque piste."""
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    hdr = HDRType.DOLBY_VISION_HDR10PLUS
    panel = EncodePanel(AppConfig())
    first, second = _video_entry(0), _video_entry(1)
    first.entry_id, second.entry_id = "first", "second"
    info = _file_info(_PATH_A, [_video_track(0, hdr), _video_track(1, hdr)], hdr_type=hdr)
    panel.set_video_tracks([(info, first, _COLOR), (info, second, _COLOR)])
    try:
        _select_codec(panel, "libx265")
        panel._on_apply_all_video_toggled(True)
        _select_codec(panel, "libx264")
        _select_codec(panel, "libx265")
        assert (panel._inject_hdr_cb.isChecked(), panel._copy_dv_cb.isChecked(),
                panel._copy_hdr10plus_cb.isChecked()) == (True, True, True)
        for entry_id in ("first", "second"):
            state = panel._video_settings_by_entry_id[entry_id]
            assert (state["inject_hdr_meta"], state["copy_dv"], state["copy_hdr10plus"]) == (True, True, True), entry_id
    finally:
        panel.close()


def test_restored_state_with_list_flags_restores_dynamic_hdr(qt_app, monkeypatch):
    """État relu d'un JSON (marqueurs en liste) : Dolby Vision et HDR10+ rétablis au retour sur HEVC."""
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    hdr = HDRType.DOLBY_VISION_HDR10PLUS
    panel = EncodePanel(AppConfig())
    entry = _video_entry(0)
    entry.entry_id = "only"
    panel.set_video_tracks([(_file_info(_PATH_A, [_video_track(0, hdr)], hdr_type=hdr), entry, _COLOR)])
    try:
        _select_codec(panel, "libx264")
        state = dict(panel._video_settings_by_entry_id["only"])
        state.update(hdr_disabled_by_codec=True, dynamic_hdr_disabled_by_codec=[True, True])
        panel._apply_video_state(state)
        _select_codec(panel, "libx265")
        assert (panel._inject_hdr_cb.isChecked(), panel._copy_dv_cb.isChecked(),
                panel._copy_hdr10plus_cb.isChecked()) == (True, True, True)
    finally:
        panel.close()


def test_p5_scheduled_analysis_survives_a_round_trip_through_an_sdr_codec(qt_app, monkeypatch):
    """P5 → P8.1 : l'analyse HDR10 programmée, déprogrammée par H.264, est reprise au retour sur HEVC."""
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    asked: list[str] = []
    monkeypatch.setattr(panel, "_ask_static_hdr_estimate_mode", lambda: asked.append("ask") or "")
    panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
    video = _video_track(0, HDRType.DOLBY_VISION)
    video.dovi_profile = 5
    entry = _video_entry(0)
    entry.entry_id = "video-p5"
    panel.set_video_tracks([(_file_info(_PATH_A, [video]), entry, _COLOR)])
    try:
        _select_codec(panel, "libx265")
        panel._dovi_profile_choice = "2"
        panel._save_current_video_state()
        panel._schedule_static_hdr_estimate("video-p5", panel._video_settings_by_entry_id["video-p5"],
                                            mode="precise")
        _select_codec(panel, "libx264")
        assert panel._video_settings_by_entry_id["video-p5"]["static_hdr_metadata_analysis_request"] == ""
        asked.clear()
        _select_codec(panel, "libx265")
        state = panel._video_settings_by_entry_id["video-p5"]
        assert state["static_hdr_metadata_analysis_request"] == "precise" and state["inject_hdr_meta"]
        assert asked == []  # choix repris, pas redemandé
        assert "programmée" in panel._hdr_meta_provenance.text() or "scheduled" in panel._hdr_meta_provenance.text()
        # Valeurs connues entre-temps (état restauré d'une session) : l'analyse mémorisée est oubliée.
        _select_codec(panel, "libx264")
        state = dict(panel._video_settings_by_entry_id["video-p5"])
        assert state["static_hdr_analysis_disabled_by_codec"] == "precise"
        state["master_display"] = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
        normalized, _cancelled = panel._normalize_pending_static_hdr_analysis("video-p5", state)
        assert not normalized["static_hdr_analysis_disabled_by_codec"]
        assert not normalized["static_hdr_metadata_analysis_request"]
    finally:
        panel.close()


def test_apply_all_with_different_sources_keeps_the_common_hdr_intent(qt_app, monkeypatch):
    """« Appliquer à toutes », piste DV+HDR10+ et piste HDR10 : HEVC → H.264, piste HDR10 affichée, retour HEVC."""
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    first, second = _video_entry(0), _video_entry(1)
    panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
    first.entry_id, second.entry_id = "dv", "hdr10"
    dv = _file_info(_PATH_A, [_video_track(0, HDRType.DOLBY_VISION_HDR10PLUS)], hdr_type=HDRType.DOLBY_VISION_HDR10PLUS)
    hdr10 = _file_info(_PATH_B, [_video_track(0, HDRType.HDR10)], hdr_type=HDRType.HDR10)
    panel.set_video_tracks([(dv, first, _COLOR), (hdr10, second, _COLOR)])
    try:
        _select_codec(panel, "libx265")
        panel._on_apply_all_video_toggled(True)
        _select_codec(panel, "libx264")
        panel._video_list.setCurrentRow(1)  # piste HDR10 affichée pendant l'intervalle SDR
        _select_codec(panel, "libx265")
        state = panel._video_settings_by_entry_id["dv"]
        assert (state["codec"], state["inject_hdr_meta"], state["copy_dv"], state["copy_hdr10plus"]) == (
            "libx265", True, True, True)
        config = panel.collect_config()
        assert config is not None
        dv_track = next(video for video in config.video_tracks if video.source_path == _PATH_A)
        assert dv_track.copy_dv and dv_track.copy_hdr10plus  # copie effective sur la piste DV+HDR10+
    finally:
        panel.close()


def _apply_all_panel(monkeypatch, *sources):
    """Panneau « Appliquer à toutes » : une piste par (chemin, type HDR, transfert)."""
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
    tracks = []
    for index, (path, hdr, transfer) in enumerate(sources):
        video = _video_track(0, hdr)
        video.color_transfer = transfer
        entry = _video_entry(index)
        entry.entry_id = f"t{index}"
        tracks.append((_file_info(path, [video], hdr_type=hdr), entry, _COLOR))
    panel.set_video_tracks(tracks)
    _select_codec(panel, "libx265")
    panel._on_apply_all_video_toggled(True)
    return panel


def _settings_by_path(panel) -> dict:
    config = panel.collect_config()
    assert config is not None
    return {video.source_path: video for video in config.video_tracks}


def test_apply_all_sdr_track_displayed_never_gets_hdr10(qt_app, monkeypatch):
    from core.workflows.encode.domain.codecs import output_hdr_transfer

    path_sdr = Path("/tmp/film_sdr.mkv")
    panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.HDR10, "smpte2084"), (path_sdr, HDRType.NONE, "bt709"))
    try:
        _select_codec(panel, "libx264")
        panel._video_list.setCurrentRow(1)  # piste SDR affichée pendant l'intervalle SDR
        _select_codec(panel, "libx265")
        for _round in range(2):  # navigation répétée, collectes répétées, autre réglage modifié sur la piste SDR
            settings = _settings_by_path(panel)
            sdr, hdr10 = settings[path_sdr], settings[_PATH_A]
            assert not sdr.inject_hdr_meta and output_hdr_transfer(sdr) != "pq"
            assert hdr10.inject_hdr_meta and not hdr10.tonemap_to_sdr
            panel._video_list.setCurrentRow(0)
            assert panel._inject_hdr_cb.isChecked()
            panel._video_list.setCurrentRow(1)
            assert not panel._inject_hdr_cb.isChecked()
            panel._crf_spin.setValue(panel._crf_spin.value() + 1)
    finally:
        panel.close()


def test_apply_all_explicitly_unchecked_hdr10_is_never_restored(qt_app, monkeypatch):
    path_sdr = Path("/tmp/film_sdr.mkv")
    panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.HDR10, "smpte2084"), (path_sdr, HDRType.NONE, "bt709"))
    try:
        panel._inject_hdr_cb.click()  # choix commun (décoché) depuis la piste HDR10
        panel._video_list.setCurrentRow(1)
        _select_codec(panel, "libx264")
        _select_codec(panel, "libx265")
        panel._crf_spin.setValue(panel._crf_spin.value() + 1)
        assert not _settings_by_path(panel)[_PATH_A].inject_hdr_meta
        panel._video_list.setCurrentRow(0)
        assert not panel._inject_hdr_cb.isChecked()
    finally:
        panel.close()


def test_apply_all_hlg_track_keeps_hlg_and_never_inherits_hdr10(qt_app, monkeypatch):
    from core.workflows.encode.domain.codecs import output_hdr_transfer

    path_hlg = Path("/tmp/film_hlg.mkv")
    panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.HDR10, "smpte2084"), (path_hlg, HDRType.HLG, "arib-std-b67"))
    try:
        _select_codec(panel, "libx264")
        panel._video_list.setCurrentRow(1)
        _select_codec(panel, "libx265")
        settings = _settings_by_path(panel)
        assert not settings[path_hlg].inject_hdr_meta and output_hdr_transfer(settings[path_hlg]) == "hlg"
        assert settings[_PATH_A].inject_hdr_meta
    finally:
        panel.close()


def test_apply_all_editing_from_a_track_without_dv_keeps_dv_on_the_others(qt_app, monkeypatch):
    """Défaut antérieur : modifier un réglage depuis la piste HDR10 retirait DV / HDR10+ de la piste DV."""
    path_hdr10 = Path("/tmp/film_hdr10.mkv")
    panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.DOLBY_VISION_HDR10PLUS, "smpte2084"),
                             (path_hdr10, HDRType.HDR10, "smpte2084"))
    try:
        for _round in range(2):
            panel._video_list.setCurrentRow(1)
            panel._crf_spin.setValue(panel._crf_spin.value() + 1)
            settings = _settings_by_path(panel)
            assert settings[_PATH_A].copy_dv and settings[_PATH_A].copy_hdr10plus
            assert not settings[path_hdr10].copy_dv and not settings[path_hdr10].copy_hdr10plus
            panel._video_list.setCurrentRow(0)
            assert panel._copy_dv_cb.isChecked() and panel._copy_hdr10plus_cb.isChecked()
        # Choix commun changé depuis la piste DV : suivi, même en revenant éditer depuis l'autre piste.
        panel._copy_dv_cb.click()
        panel._video_list.setCurrentRow(1)
        panel._crf_spin.setValue(panel._crf_spin.value() + 1)
        settings = _settings_by_path(panel)
        assert not settings[_PATH_A].copy_dv and settings[_PATH_A].copy_hdr10plus
    finally:
        panel.close()


def test_apply_all_sdr_round_trip_with_an_sdr_track_displayed_keeps_dv(qt_app, monkeypatch):
    from core.workflows.encode.domain.codecs import output_hdr_transfer

    path_sdr = Path("/tmp/film_sdr.mkv")
    panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.DOLBY_VISION_HDR10PLUS, "smpte2084"),
                             (path_sdr, HDRType.NONE, "bt709"))
    try:
        _select_codec(panel, "libx264")
        panel._video_list.setCurrentRow(1)
        _select_codec(panel, "libx265")
        for _round in range(2):
            settings = _settings_by_path(panel)
            dv, sdr = settings[_PATH_A], settings[path_sdr]
            assert dv.inject_hdr_meta and dv.copy_dv and dv.copy_hdr10plus and not dv.tonemap_to_sdr
            assert not sdr.inject_hdr_meta and output_hdr_transfer(sdr) != "pq"
            panel._video_list.setCurrentRow(0)
            panel._video_list.setCurrentRow(1)
            panel._crf_spin.setValue(panel._crf_spin.value() + 1)
    finally:
        panel.close()


def test_manual_hdr10_values_on_an_sdr_source_are_kept(qt_app, monkeypatch):
    """Valeurs HDR10 saisies sur une source SDR : gardées une fois la piste quittée (et non propagées)."""
    path_sdr = Path("/tmp/film_sdr.mkv")
    md = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
    for apply_all in (False, True):
        panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.HDR10, "smpte2084"), (path_sdr, HDRType.NONE, "bt709"))
        try:
            if not apply_all:
                panel._on_apply_all_video_toggled(False)
            panel._video_list.setCurrentRow(1)
            panel._inject_hdr_cb.setChecked(True)
            panel._master_display.setText(md)
            panel._max_cll.setText("1000,400")
            panel._save_current_video_state()
            panel._video_list.setCurrentRow(0)  # piste SDR quittée : normalisée comme les autres
            settings = _settings_by_path(panel)
            assert settings[path_sdr].inject_hdr_meta and settings[path_sdr].master_display == md, apply_all
            assert settings[_PATH_A].master_display != md  # valeurs propres à leur source
        finally:
            panel.close()


def _sdr_track_with_source_cll(index: int, path: Path):
    video = _video_track(0, HDRType.NONE)
    video.color_transfer = "bt709"
    video.raw = {"side_data_list": [{"side_data_type": "Content light level metadata", "max_content": 1000,
                                     "max_average": 400}]}
    entry = _video_entry(index)
    entry.entry_id = f"sdr{index}"
    return _file_info(path, [video]), entry, _COLOR


def test_hdr10_provenance_on_an_sdr_source_follows_user_actions(qt_app, monkeypatch):
    """Source SDR à MaxCLL hérité : jamais injecté seul (case cochée par le programme compris) ; case cochée ou
    valeur saisie par l'utilisateur : gardé."""
    from PySide6.QtTest import QTest

    from core.workflows.encode.domain.codecs import output_hdr_transfer

    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    path_a, path_b = Path("/tmp/sdr_a.mkv"), Path("/tmp/sdr_b.mkv")
    for action in ("programmatic", "click", "type"):
        panel = EncodePanel(AppConfig())
        panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
        panel.set_video_tracks([_sdr_track_with_source_cll(0, path_a), _sdr_track_with_source_cll(1, path_b)])
        try:
            _select_codec(panel, "libx265")
            if action == "programmatic":
                panel._inject_hdr_cb.setChecked(True)  # changement programmatique : aucune saisie explicite
                assert panel._inject_hdr_cb.isChecked() and panel._max_cll.text() == "1000,400"
            elif action == "click":
                panel._inject_hdr_cb.click()
            else:
                panel._inject_hdr_cb.click()
                panel._max_cll.clear()
                QTest.keyClicks(panel._max_cll, "2000,500")
            panel._save_current_video_state()
            panel._video_list.setCurrentRow(1)  # piste quittée : normalisée comme les autres
            settings = {v.source_path: v for v in panel.collect_config().video_tracks}
            a = settings[path_a]
            assert a.inject_hdr_meta == (action != "programmatic"), action
            assert (output_hdr_transfer(a) == "pq") == (action != "programmatic"), action
            if action == "type":
                assert a.max_cll == "2000,500" and a.static_hdr_light_level_source == "manual"
            assert not settings[path_b].inject_hdr_meta  # marqueurs propres à la piste
        finally:
            panel.close()


def test_first_displayed_track_keeps_source_hdr10_values_classified_as_source(qt_app, monkeypatch):
    """Défaut antérieur : la première piste affichée tenait ses valeurs HDR10 de la source pour des saisies."""
    from core.fel.preparation import measures_light_levels

    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
    sdr_path = Path("/tmp/sdr_a.mkv")
    hdr = _video_track(0, HDRType.HDR10)
    hdr.color_transfer = "smpte2084"
    hdr.raw = {"side_data_list": [{"side_data_type": "Content light level metadata", "max_content": 1000,
                                   "max_average": 400}]}
    entry = _video_entry(0)
    entry.entry_id = "hdr"
    panel.set_video_tracks([(_file_info(_PATH_A, [hdr], hdr_type=HDRType.HDR10), entry, _COLOR),
                            _sdr_track_with_source_cll(1, sdr_path)])
    try:
        _select_codec(panel, "libx265")
        settings = {v.source_path: v for v in panel.collect_config().video_tracks}
        assert settings[_PATH_A].max_cll == "1000,400" and settings[_PATH_A].static_hdr_light_level_source == "source"
        assert measures_light_levels(settings[_PATH_A])  # FEL : MaxCLL de la source remplacé par la mesure
    finally:
        panel.close()


def test_programmatic_hdr10_on_an_sdr_track_is_never_injected_once_left(qt_app, monkeypatch):
    """Case HDR10 cochée par le programme sur une piste SDR à MaxCLL hérité, piste quittée : pas d'injection."""
    from core.workflows.encode.domain.codecs import output_hdr_transfer

    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
    sdr_path = Path("/tmp/sdr_a.mkv")
    hdr = _video_track(0, HDRType.HDR10)
    hdr.color_transfer = "smpte2084"
    entry = _video_entry(0)
    entry.entry_id = "hdr"
    panel.set_video_tracks([(_file_info(_PATH_A, [hdr], hdr_type=HDRType.HDR10), entry, _COLOR),
                            _sdr_track_with_source_cll(1, sdr_path)])
    try:
        _select_codec(panel, "libx265")
        panel._video_list.setCurrentRow(1)
        panel._inject_hdr_cb.setChecked(True)
        panel._save_current_video_state()
        for row in (1, 0):  # piste affichée, puis quittée
            panel._video_list.setCurrentRow(row)
            sdr = {v.source_path: v for v in panel.collect_config().video_tracks}[sdr_path]
            assert not sdr.inject_hdr_meta and output_hdr_transfer(sdr) != "pq"
    finally:
        panel.close()


def test_apply_all_with_only_sdr_sources_keeps_each_track_own_hdr10_choice(qt_app, monkeypatch):
    from PySide6.QtTest import QTest

    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    path_a, path_b = Path("/tmp/sdr_a.mkv"), Path("/tmp/sdr_b.mkv")
    panel = EncodePanel(AppConfig())
    panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
    a, b = _sdr_track_with_source_cll(0, path_a), _sdr_track_with_source_cll(1, path_b)
    for track in (a, b):
        track[0].video_tracks[0].raw = {}
    panel.set_video_tracks([a, b])
    try:
        _select_codec(panel, "libx265")
        panel._on_apply_all_video_toggled(True)
        panel._inject_hdr_cb.click()
        QTest.keyClicks(panel._max_cll, "1000,400")
        panel._save_current_video_state()
        panel._video_list.setCurrentRow(1)
        for _round in range(2):
            settings = {v.source_path: v for v in panel.collect_config().video_tracks}
            assert settings[path_a].inject_hdr_meta and settings[path_a].max_cll == "1000,400"
            assert not settings[path_b].inject_hdr_meta
            panel._crf_spin.setValue(panel._crf_spin.value() + 1)
    finally:
        panel.close()


def test_apply_all_first_hdr_track_added_to_an_sdr_list_keeps_its_hdr(qt_app, monkeypatch):
    """Défaut antérieur : une première piste DV+HDR10+ ajoutée à une liste SDR en mode global perdait son HDR."""
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    path_sdr = Path("/tmp/film_sdr.mkv")
    panel = _apply_all_panel(monkeypatch, (path_sdr, HDRType.NONE, "bt709"))
    try:
        dv = _video_track(0, HDRType.DOLBY_VISION_HDR10PLUS)
        dv.color_transfer = "smpte2084"
        entry = _video_entry(1)
        entry.entry_id = "dv"
        sdr_track = panel._video_tracks[0]
        panel.set_video_tracks([sdr_track, (_file_info(_PATH_A, [dv], hdr_type=HDRType.DOLBY_VISION_HDR10PLUS),
                                            entry, _COLOR)])
        settings = {v.source_path: v for v in panel.collect_config().video_tracks}
        assert settings[_PATH_A].codec == "libx265"
        assert settings[_PATH_A].inject_hdr_meta and settings[_PATH_A].copy_dv and settings[_PATH_A].copy_hdr10plus
    finally:
        panel.close()


def test_apply_all_copy_imposed_boxes_never_become_the_common_intent(qt_app, monkeypatch):
    """A DV+HDR10+ (options décochées), B SDR ; A en Copy, liste rafraîchie, B affichée : A garde ses options retirées."""
    path_sdr = Path("/tmp/film_sdr.mkv")
    panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.DOLBY_VISION_HDR10PLUS, "smpte2084"),
                             (path_sdr, HDRType.NONE, "bt709"))
    try:
        for box in (panel._inject_hdr_cb, panel._copy_dv_cb, panel._copy_hdr10plus_cb):
            box.click()  # retirées par l'utilisateur
        _select_codec(panel, "copy")
        panel.set_video_tracks(list(panel._video_tracks))  # rafraîchissement de la liste
        panel._video_list.setCurrentRow(1)
        panel._crf_spin.setValue(panel._crf_spin.value() + 1)
        dv = {v.source_path: v for v in panel.collect_config().video_tracks}[_PATH_A]
        assert dv.codec == "libx265" and not (dv.inject_hdr_meta or dv.copy_dv or dv.copy_hdr10plus)
        # Copy → HEVC sur la piste affichée : ses cases suivent l'intention, pas les cases imposées par Copy.
        panel._video_list.setCurrentRow(0)
        _select_codec(panel, "copy")
        _select_codec(panel, "libx265")
        assert not (panel._inject_hdr_cb.isChecked() or panel._copy_dv_cb.isChecked()
                    or panel._copy_hdr10plus_cb.isChecked())
    finally:
        panel.close()


def test_apply_all_activation_takes_the_displayed_track_memory(qt_app, monkeypatch):
    """A HDR10 décochée ; B HDR10 cochée puis H.264 ; mode global activé depuis B : HDR10 voulu sur les deux pistes."""
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    path_b = Path("/tmp/film_b_hdr10.mkv")
    panel = EncodePanel(AppConfig())
    panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
    tracks = []
    for index, path in enumerate((_PATH_A, path_b)):
        video = _video_track(0, HDRType.HDR10)
        video.color_transfer = "smpte2084"
        entry = _video_entry(index)
        entry.entry_id = f"t{index}"
        tracks.append((_file_info(path, [video], hdr_type=HDRType.HDR10), entry, _COLOR))
    panel.set_video_tracks(tracks)
    try:
        _select_codec(panel, "libx265")
        panel._inject_hdr_cb.click()  # A : HDR10 retiré (mode piste par piste)
        panel._video_list.setCurrentRow(1)
        _select_codec(panel, "libx265")
        assert panel._inject_hdr_cb.isChecked()
        _select_codec(panel, "libx264")  # B : HDR10 mémorisé par le codec sans HDR
        panel._on_apply_all_video_toggled(True)
        panel._video_list.setCurrentRow(0)
        _select_codec(panel, "libx265")
        settings = {v.source_path: v for v in panel.collect_config().video_tracks}
        assert settings[_PATH_A].inject_hdr_meta and settings[path_b].inject_hdr_meta
        # Désactivation puis réactivation depuis une piste en H.264 : sa mémoire fait encore foi.
        panel._on_apply_all_video_toggled(False)
        panel._inject_hdr_cb.click()  # A : retiré, mode piste par piste
        _select_codec(panel, "libx264")
        panel._on_apply_all_video_toggled(True)
        _select_codec(panel, "libx265")
        settings = {v.source_path: v for v in panel.collect_config().video_tracks}
        assert not settings[_PATH_A].inject_hdr_meta and not settings[path_b].inject_hdr_meta
    finally:
        panel.close()


def test_apply_all_intent_survives_list_changes_and_is_cleared_by_reset(qt_app, monkeypatch):
    path_sdr = Path("/tmp/film_sdr.mkv")
    panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.HDR10, "smpte2084"), (path_sdr, HDRType.NONE, "bt709"))
    try:
        hdr_track, sdr_track = panel._video_tracks
        panel._inject_hdr_cb.click()  # intention commune : HDR10 retiré
        panel.set_video_tracks([sdr_track])  # retrait de la dernière piste HDR10
        panel.set_video_tracks([sdr_track, hdr_track])  # ajout ultérieur
        assert not {v.source_path: v for v in panel.collect_config().video_tracks}[_PATH_A].inject_hdr_meta
        other = _video_track(0, HDRType.HDR10)
        other.color_transfer = "smpte2084"
        entry = _video_entry(5)
        entry.entry_id = "nouveau"
        new_path = Path("/tmp/autre_film.mkv")
        panel.set_video_tracks([(_file_info(new_path, [other], hdr_type=HDRType.HDR10), entry, _COLOR)])  # remplacées
        assert not {v.source_path: v for v in panel.collect_config().video_tracks}[new_path].inject_hdr_meta
        panel.reset()
        assert panel._hdr_common_intent == {}
    finally:
        panel.close()


def test_apply_all_explicit_tonemap_survives_an_sdr_round_trip_unlike_the_automatic_one(qt_app, monkeypatch):
    path_sdr = Path("/tmp/film_sdr.mkv")
    panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.HDR10, "smpte2084"), (path_sdr, HDRType.NONE, "bt709"))
    try:
        _select_codec(panel, "libx264")  # tone mapping coché d'office (source HDR vers codec sans HDR)
        assert panel._tonemap_cb.isChecked()
        _select_codec(panel, "libx265")
        assert not panel._tonemap_cb.isChecked()  # retiré au retour : il était automatique
        panel._tonemap_cb.click()  # choix explicite
        _select_codec(panel, "libx264")
        _select_codec(panel, "libx265")
        settings = {v.source_path: v for v in panel.collect_config().video_tracks}
        assert panel._tonemap_cb.isChecked() and settings[_PATH_A].tonemap_to_sdr
    finally:
        panel.close()


def test_restored_track_states_with_and_without_explicit_markers(qt_app, monkeypatch):
    """État d'une session antérieure (sans marqueurs) : provenance par comparaison ; avec marqueurs : saisie gardée."""
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
    path_a, path_b = Path("/tmp/sdr_a.mkv"), Path("/tmp/sdr_b.mkv")
    panel.set_video_tracks([_sdr_track_with_source_cll(0, path_a), _sdr_track_with_source_cll(1, path_b)])
    try:
        _select_codec(panel, "libx265")
        panel._video_list.setCurrentRow(1)  # état de la piste 0 remplacé hors affichage
        base = dict(panel._video_settings_by_entry_id["sdr0"])
        for markers, expected in (({}, False), ({"static_hdr_manual": True}, True),
                                  ({"static_hdr_max_cll_manual": True}, True)):
            state = {**base, "inject_hdr_meta": True, "dynamic_hdr_disabled_by_codec": [False, False], **markers}
            for key in ("static_hdr_manual", "static_hdr_max_cll_manual", "static_hdr_master_display_manual"):
                state.setdefault(key, False) if markers else state.pop(key, None)
            panel._video_settings_by_entry_id["sdr0"] = state
            panel._video_list.setCurrentRow(1)
            panel._video_list.setCurrentRow(0)  # état restauré relu par le panneau
            panel._video_list.setCurrentRow(1)
            a = {v.source_path: v for v in panel.collect_config().video_tracks}[path_a]
            assert a.inject_hdr_meta is expected, markers
    finally:
        panel.close()


# --- Huitième audit : aperçu = lancement, provenance liée à la valeur, tone mapping par source ---------------

_CLL_RAW = {"side_data_list": [{"side_data_type": "Content light level metadata", "max_content": 1000,
                                "max_average": 400}]}


def _hdr_entry(index: int, path: Path, hdr: HDRType, transfer: str, raw: dict | None = None):
    video = _video_track(0, hdr)
    video.color_transfer = transfer
    video.raw = dict(raw or {})
    entry = _video_entry(index)
    entry.entry_id = f"v{index}"
    return _file_info(path, [video], hdr_type=hdr), entry, _COLOR


_HDR_COMPARED = ("inject_hdr_meta", "master_display", "max_cll", "static_hdr_light_level_source",
                 "static_hdr_master_display_source", "tonemap_to_sdr", "copy_dv", "copy_hdr10plus")


@pytest.mark.parametrize("apply_all", [False, True])
@pytest.mark.parametrize("hdr, transfer, raw, codec, action, injected", [
    (HDRType.NONE, "bt709", None, "libx265", "click", False),        # case cochée, champs vides : rien
    (HDRType.NONE, "bt709", _CLL_RAW, "libx265", "click", True),     # case cochée, valeur de la source
    (HDRType.NONE, "bt709", None, "libx265", "type", True),          # valeur saisie
    (HDRType.HLG, "arib-std-b67", None, "libx265", "click", False),  # HLG : transfert HLG gardé
    (HDRType.HDR10, "smpte2084", None, "libx265", "", True),         # HDR10 aux champs vides : complété au lancement
    (HDRType.HDR10, "smpte2084", _CLL_RAW, "libx264", "", False),    # tone mapping automatique
    (HDRType.HDR10, "smpte2084", _CLL_RAW, "copy", "", True),
])
def test_preview_and_launch_give_the_displayed_track_the_same_hdr_settings(
        qt_app, monkeypatch, apply_all, hdr, transfer, raw, codec, action, injected):
    """Défaut de l'audit 8 : l'aperçu marquait PQ une source SDR à champs vides que le lancement n'injectait pas."""
    from PySide6.QtTest import QTest

    from core.workflows.encode.domain.codecs import output_hdr_transfer

    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
    path = Path("/tmp/displayed.mkv")
    panel.set_video_tracks([_hdr_entry(0, path, hdr, transfer, raw),
                            _hdr_entry(1, Path("/tmp/other.mkv"), HDRType.NONE, "bt709")])
    try:
        panel._apply_all_video_cb.setChecked(apply_all)
        _select_codec(panel, codec)
        if action in ("click", "type"):
            panel._inject_hdr_cb.click()
        if action == "type":
            panel._max_cll.clear()
            QTest.keyClicks(panel._max_cll, "2000,500")
        preview_config = panel._current_preview_config()
        assert preview_config is not None
        preview = preview_config.video_tracks[0]
        launch = {v.source_path: v for v in panel.collect_config().video_tracks}[path]
        assert {k: getattr(preview, k) for k in _HDR_COMPARED} == {k: getattr(launch, k) for k in _HDR_COMPARED}
        assert output_hdr_transfer(preview) == output_hdr_transfer(launch)
        assert launch.inject_hdr_meta is injected
        if hdr == HDRType.HLG:
            assert output_hdr_transfer(launch) == "hlg"
    finally:
        panel.close()


def _hdr_panel(monkeypatch, *entries):
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)
    panel = EncodePanel(AppConfig())
    panel.set_output_provider(lambda: Path("/tmp/out.mkv"))
    panel.set_video_tracks(list(entries))
    return panel


def _launched(panel, path: Path):
    return {v.source_path: v for v in panel.collect_config().video_tracks}[path]


def _type_cll(panel, text: str) -> None:
    from PySide6.QtTest import QTest

    panel._max_cll.clear()
    QTest.keyClicks(panel._max_cll, text)


_HDR10_PATH = Path("/tmp/hdr10.mkv")


def test_replaced_sources_never_inherit_the_values_typed_on_the_previous_ones(qt_app, monkeypatch):
    from core.fel.preparation import measures_light_levels

    panel = _hdr_panel(monkeypatch, _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW))
    try:
        _select_codec(panel, "libx265")
        _type_cll(panel, "2000,500")
        new_path = Path("/tmp/new.mkv")
        panel.set_video_tracks([_hdr_entry(5, new_path, HDRType.HDR10, "smpte2084", _CLL_RAW)])
        _select_codec(panel, "libx265")
        settings = _launched(panel, new_path)
        assert settings.max_cll == "1000,400" and settings.static_hdr_light_level_source == "source"
        assert measures_light_levels(settings)  # FEL : la mesure peut remplacer la valeur de la source
        assert panel._hdr_meta_provenance.text() == translate_text("Valeurs de la source.")
    finally:
        panel.close()


@pytest.mark.parametrize("typed", ["2000,500", "1000,400"])  # différente ou identique à la source
def test_copy_round_trip_returns_typed_values_to_the_source(qt_app, monkeypatch, typed):
    panel = _hdr_panel(monkeypatch, _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW))
    try:
        _select_codec(panel, "libx265")
        _type_cll(panel, typed)
        assert _launched(panel, _HDR10_PATH).static_hdr_light_level_source == "manual"
        assert panel._hdr_meta_provenance.text() == translate_text("Saisie manuelle.")  # égale : saisie quand même
        _select_codec(panel, "copy")
        _select_codec(panel, "libx265")
        settings = _launched(panel, _HDR10_PATH)
        assert settings.max_cll == "1000,400" and settings.static_hdr_light_level_source == "source"
        assert panel._hdr_meta_provenance.text() == translate_text("Valeurs de la source.")
    finally:
        panel.close()


def test_source_values_button_and_redisplay_follow_the_value_shown(qt_app, monkeypatch):
    other = Path("/tmp/other.mkv")
    panel = _hdr_panel(monkeypatch, _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW),
                       _hdr_entry(1, other, HDRType.HDR10, "smpte2084", _CLL_RAW))
    try:
        _select_codec(panel, "libx265")
        _type_cll(panel, "1000,400")
        panel._video_list.setCurrentRow(1)
        panel._video_list.setCurrentRow(0)  # réaffichage : saisie gardée
        assert _launched(panel, _HDR10_PATH).static_hdr_light_level_source == "manual"
        panel._restore_source_hdr_metadata()  # « Valeurs source »
        assert _launched(panel, _HDR10_PATH).static_hdr_light_level_source == "source"
        _type_cll(panel, "")  # champ vidé : complété par la source, plus une saisie
        panel._video_list.setCurrentRow(1)
        panel._video_list.setCurrentRow(0)
        assert panel._max_cll.text() == "1000,400"
        assert _launched(panel, _HDR10_PATH).static_hdr_light_level_source == "source"
        panel._video_list.setCurrentRow(1)  # état de la piste 0 remplacé hors affichage
        panel._video_settings_by_entry_id["v0"].update(max_cll="", static_hdr_max_cll_manual=True)
        panel._video_list.setCurrentRow(0)  # état relu : marqueur d'un champ vide, valeur complétée par la source
        assert panel._max_cll.text() == "1000,400" and not panel._static_hdr_field_manual["max_cll"]
        assert _launched(panel, _HDR10_PATH).static_hdr_light_level_source == "source"
    finally:
        panel.close()


def test_hdr_studio_choice_is_an_explicit_value_even_when_equal_to_the_source(qt_app, monkeypatch):
    from core.fel.preparation import measures_light_levels

    sdr = Path("/tmp/sdr.mkv")
    panel = _hdr_panel(monkeypatch, _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW),
                       _hdr_entry(1, sdr, HDRType.NONE, "bt709", _CLL_RAW))
    try:
        _select_codec(panel, "libx265")
        assert panel.apply_static_light_levels(_HDR10_PATH, 0, 1000, 400)
        settings = _launched(panel, _HDR10_PATH)
        assert settings.static_hdr_light_level_source == "manual" and not measures_light_levels(settings)
        panel._video_list.setCurrentRow(1)
        _select_codec(panel, "libx265")
        assert not _launched(panel, sdr).inject_hdr_meta  # MaxCLL hérité : jamais injecté seul
        assert panel.apply_static_light_levels(sdr, 1, 1000, 400)  # SDR : demande explicite → injection
        assert _launched(panel, sdr).inject_hdr_meta
    finally:
        panel.close()


def test_typed_values_take_precedence_over_a_p5_estimate_in_the_label(qt_app, monkeypatch):
    video = _video_track(0, HDRType.DOLBY_VISION)
    video.dovi_profile = 5
    entry = _video_entry(0)
    entry.entry_id = "video-p5"
    panel = _hdr_panel(monkeypatch, (_file_info(_PATH_A, [video]), entry, _COLOR))
    try:
        _select_codec(panel, "libx265")
        panel._save_current_video_state()
        panel._video_settings_by_entry_id["video-p5"]["static_hdr_metadata_analysis_request"] = "precise"
        panel._on_static_hdr_estimate_ready("video-p5", StaticHdrEstimate(
            master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
            max_cll="900,300", confidence="medium", sample_count=42, coverage=0.44, mode="precise"))
        assert panel._hdr_meta_provenance.text().startswith(translate_text(
            "Valeurs estimées (analyse P5→P8.1, confiance {confidence}).", confidence="medium")[:15])
        assert _launched(panel, _PATH_A).static_hdr_light_level_source == "source"
        _type_cll(panel, "900,300")  # même valeur, saisie par l'utilisateur
        assert panel._hdr_meta_provenance.text() == translate_text("Saisie manuelle.")
        assert _launched(panel, _PATH_A).static_hdr_light_level_source == "manual"
    finally:
        panel.close()


def _tonemaps(panel) -> dict:
    return {v.source_path: v.tonemap_to_sdr for v in panel.collect_config().video_tracks}


def test_apply_all_automatic_tonemap_follows_each_source_whatever_track_is_displayed(qt_app, monkeypatch):
    """Défaut antérieur (audit 8) : la piste HDR recevait le tone mapping de la piste SDR affichée."""
    path_sdr = Path("/tmp/film_sdr.mkv")
    panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.HDR10, "smpte2084"), (path_sdr, HDRType.NONE, "bt709"))
    try:
        panel._video_list.setCurrentRow(1)  # piste SDR affichée
        _select_codec(panel, "libx264")
        assert _tonemaps(panel) == {_PATH_A: True, path_sdr: False}
        panel._video_list.setCurrentRow(0)
        panel._video_list.setCurrentRow(1)
        assert _tonemaps(panel) == {_PATH_A: True, path_sdr: False}
        _select_codec(panel, "libx265")
        assert _tonemaps(panel) == {_PATH_A: False, path_sdr: False}
        _select_codec(panel, "libx264")
        panel._tonemap_cb.click()  # choix explicite sur la piste affichée : commun à toutes
        assert _tonemaps(panel) == {_PATH_A: True, path_sdr: True}
        panel._tonemap_cb.click()
        panel._video_list.setCurrentRow(0)
        _select_codec(panel, "libx265")
        _select_codec(panel, "libx264")
        assert _tonemaps(panel) == {_PATH_A: False, path_sdr: False}  # refus commun gardé
    finally:
        panel.close()


@pytest.mark.parametrize("apply_all", [False, True])
def test_explicit_tonemap_choices_survive_codec_and_copy_cycles(qt_app, monkeypatch, apply_all):
    other = Path("/tmp/other_hdr10.mkv")
    panel = _hdr_panel(monkeypatch, _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW),
                       _hdr_entry(1, other, HDRType.HDR10, "smpte2084", _CLL_RAW))

    def same_on_both(expected: bool) -> None:  # mode global : piste affichée et autre piste identiques
        values = _tonemaps(panel)
        assert values[_HDR10_PATH] is expected
        if apply_all:
            assert values[other] is expected

    try:
        panel._apply_all_video_cb.setChecked(apply_all)
        _select_codec(panel, "libx264")
        assert panel._tonemap_cb.isChecked()  # automatique
        for codec in ("copy", "libx264"):  # Copy impose « sans » : jamais un refus de l'utilisateur
            _select_codec(panel, codec)
        assert panel._tonemap_cb.isChecked()
        panel._tonemap_cb.click()  # refus explicite
        for codec in ("libx265", "copy", "libx264"):
            _select_codec(panel, codec)
        assert not panel._tonemap_cb.isChecked()
        same_on_both(False)
        _select_codec(panel, "libx265")
        panel._tonemap_cb.click()  # choix explicite sur un codec HDR
        for codec in ("copy", "libx265"):
            _select_codec(panel, codec)
        assert panel._tonemap_cb.isChecked()
        same_on_both(True)
    finally:
        panel.close()


@pytest.mark.parametrize("legacy, codec_after, expected", [
    ({"codec": "libx264", "tonemap_to_sdr": False, "tonemap_enabled_by_codec": False}, "libx264", False),  # refus
    ({"codec": "libx264", "tonemap_to_sdr": True, "tonemap_enabled_by_codec": True}, "libx265", False),   # auto
    ({"codec": "libx265", "tonemap_to_sdr": True, "tonemap_enabled_by_codec": False}, "libx264", True),   # choix
    ({"codec": "libx265", "tonemap_to_sdr": False, "tonemap_enabled_by_codec": False}, "libx264", True),  # défaut
])
def test_restored_states_without_a_tonemap_choice_keep_their_meaning(qt_app, monkeypatch, legacy, codec_after,
                                                                     expected):
    other = Path("/tmp/other.mkv")
    panel = _hdr_panel(monkeypatch, _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW),
                       _hdr_entry(1, other, HDRType.HDR10, "smpte2084", _CLL_RAW))
    try:
        _select_codec(panel, "libx265")
        panel._video_list.setCurrentRow(1)  # état de la piste 0 remplacé hors affichage
        state = dict(panel._video_settings_by_entry_id["v0"])
        state.pop("tonemap_choice", None)
        panel._video_settings_by_entry_id["v0"] = {**state, **legacy}  # session antérieure
        panel._video_list.setCurrentRow(0)
        assert panel._codec_combo.currentData() == legacy["codec"]  # état relu tel quel
        if codec_after != legacy["codec"]:
            _select_codec(panel, codec_after)
        else:
            _select_codec(panel, "libx265")
            _select_codec(panel, codec_after)
        assert _tonemaps(panel)[_HDR10_PATH] is expected
    finally:
        panel.close()


# --- Neuvième audit : choix commun du tone mapping, sondes asynchrones, codec sans édition HDR10 -------------


def test_apply_all_common_tonemap_choice_reaches_the_displayed_track_after_copy_and_new_sources(qt_app, monkeypatch):
    other = Path("/tmp/other_hdr10.mkv")
    panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.HDR10, "smpte2084"), (other, HDRType.HDR10, "smpte2084"))
    try:
        panel._tonemap_cb.click()  # choix commun : tone mapping sur codec HDR
        _select_codec(panel, "copy")
        new_a, new_b = Path("/tmp/new_a.mkv"), Path("/tmp/new_b.mkv")
        panel.set_video_tracks([_hdr_entry(5, new_a, HDRType.HDR10, "smpte2084"),
                                _hdr_entry(6, new_b, HDRType.HDR10, "smpte2084")])
        _select_codec(panel, "libx265")
        assert panel._tonemap_cb.isChecked() and _tonemaps(panel) == {new_a: True, new_b: True}
        _select_codec(panel, "copy")
        added = Path("/tmp/added.mkv")  # piste ajoutée pendant que la piste affichée est en Copy
        panel.set_video_tracks([_hdr_entry(5, new_a, HDRType.HDR10, "smpte2084"),
                                _hdr_entry(6, new_b, HDRType.HDR10, "smpte2084"),
                                _hdr_entry(7, added, HDRType.HDR10, "smpte2084")])
        panel._video_list.setCurrentRow(2)
        _select_codec(panel, "libx265")
        assert panel._tonemap_cb.isChecked() and _tonemaps(panel)[added]
    finally:
        panel.close()


_SOURCE_MD = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"


@pytest.mark.parametrize("retyped", [True, False])
@pytest.mark.parametrize("path", ["track", "frames"])
def test_late_source_probe_never_reclassifies_an_explicit_entry(qt_app, monkeypatch, retyped, path):
    """Sonde des valeurs de la source en cours ; saisie identique pendant ce temps : elle reste une saisie."""
    from core.fel.preparation import measures_light_levels

    entry = _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW)  # Master Display manquant
    panel = _hdr_panel(monkeypatch, entry)
    try:
        _select_codec(panel, "libx265")
        assert panel._max_cll.text() == "1000,400" and panel._master_display.text() == ""
        if path == "frames":
            panel._hdr_meta_probe_expected = (panel._hdr_meta_probe_generation, "", "1000,400")
        if retyped:
            _type_cll(panel, "1000,400")
        if path == "track":
            panel._track_hdr_requests["v0"] = entry[0]
            panel._apply_track_hdr("v0", entry[0], {"master_display": "", "max_cll": "1000,400"}, _SOURCE_MD,
                                   "1000,400")
        else:
            panel._on_hdr_meta_frame_probe_ready(panel._hdr_meta_probe_generation, _SOURCE_MD, "1000,400")
        settings = _launched(panel, _HDR10_PATH)
        assert settings.master_display == _SOURCE_MD and settings.static_hdr_master_display_source == "source"
        assert settings.max_cll == "1000,400"
        assert settings.static_hdr_light_level_source == ("manual" if retyped else "source")
        assert measures_light_levels(settings) is not retyped  # mesure FEL : jamais à la place d'une saisie
    finally:
        panel.close()


@pytest.mark.parametrize("apply_all", [False, True])
@pytest.mark.parametrize("typed", ["1000,400", "2000,500"])
def test_codec_without_hdr10_editing_returns_every_track_to_the_source_values(qt_app, monkeypatch, apply_all, typed):
    """Même règle pour la piste affichée et les autres : un codec sans édition HDR10 rend les valeurs de la source."""
    other = Path("/tmp/other_hdr10.mkv")
    panel = _hdr_panel(monkeypatch, _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW),
                       _hdr_entry(1, other, HDRType.HDR10, "smpte2084", _CLL_RAW))
    try:
        for row in (0, 1):
            panel._video_list.setCurrentRow(row)
            _select_codec(panel, "libx265")
            _type_cll(panel, typed)
        panel._video_list.setCurrentRow(0)
        panel._apply_all_video_cb.setChecked(apply_all)
        assert {_launched(panel, p).static_hdr_light_level_source for p in (_HDR10_PATH, other)} == {"manual"}
        rows = (0,) if apply_all else (0, 1)
        for row in rows:
            panel._video_list.setCurrentRow(row)
            for codec in ("libx264", "libx265"):
                _select_codec(panel, codec)
        for p in (_HDR10_PATH, other):
            settings = _launched(panel, p)
            assert (settings.max_cll, settings.static_hdr_light_level_source) == ("1000,400", "source"), p
    finally:
        panel.close()


def test_apply_all_common_tonemap_choice_overrides_a_contradicting_restored_state(qt_app, monkeypatch):
    other = Path("/tmp/other_hdr10.mkv")
    panel = _apply_all_panel(monkeypatch, (_PATH_A, HDRType.HDR10, "smpte2084"), (other, HDRType.HDR10, "smpte2084"))
    try:
        panel._tonemap_cb.click()  # choix commun : vrai
        panel._video_list.setCurrentRow(1)
        _select_codec(panel, "copy")  # piste affichée en Copy : plus aucune propagation vers la piste 0
        first = panel._video_entry_id(panel._video_tracks[0][1])
        panel._video_settings_by_entry_id[first].update(tonemap_choice=False, tonemap_to_sdr=False)  # état relu
        panel._video_list.setCurrentRow(0)
        assert panel._codec_combo.currentData() == "libx265"  # état de la piste 0 relu tel quel
        assert panel._tonemap_cb.isChecked() and panel._tonemap_choice is True
        preview = panel._current_preview_config().video_tracks[0]
        assert preview.tonemap_to_sdr and _tonemaps(panel)[_PATH_A]
    finally:
        panel.close()


def test_late_probe_keeps_a_master_display_typed_identical_to_the_value_it_finds(qt_app, monkeypatch):
    from PySide6.QtTest import QTest

    entry = _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW)
    panel = _hdr_panel(monkeypatch, entry)
    try:
        _select_codec(panel, "libx265")
        panel._master_display.clear()
        QTest.keyClicks(panel._master_display, _SOURCE_MD)  # saisi avant que la sonde ne le trouve
        panel._track_hdr_requests["v0"] = entry[0]
        panel._apply_track_hdr("v0", entry[0], {"master_display": "", "max_cll": "1000,400"}, _SOURCE_MD, "1000,400")
        settings = _launched(panel, _HDR10_PATH)
        assert settings.master_display == _SOURCE_MD and settings.static_hdr_master_display_source == "manual"
        assert settings.static_hdr_light_level_source == "source"
    finally:
        panel.close()


def test_late_probe_of_a_hidden_track_or_of_replaced_sources(qt_app, monkeypatch):
    other = Path("/tmp/other_hdr10.mkv")
    first, second = (_hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW),
                     _hdr_entry(1, other, HDRType.HDR10, "smpte2084", _CLL_RAW))
    panel = _hdr_panel(monkeypatch, first, second)
    try:
        _select_codec(panel, "libx265")
        _type_cll(panel, "1000,400")  # saisie identique sur la piste 0
        panel._video_list.setCurrentRow(1)  # piste 0 cachée au retour de sa sonde
        panel._track_hdr_requests["v0"] = first[0]
        panel._apply_track_hdr("v0", first[0], {"master_display": "", "max_cll": "1000,400"}, _SOURCE_MD, "1000,400")
        assert _launched(panel, _HDR10_PATH).static_hdr_light_level_source == "manual"
        assert panel._video_settings_by_entry_id["v0"]["default_master_display"] == _SOURCE_MD
        new = Path("/tmp/new.mkv")
        stale_info = second[0]
        panel.set_video_tracks([_hdr_entry(1, new, HDRType.HDR10, "smpte2084", _CLL_RAW)])  # sources remplacées
        panel._apply_track_hdr("v1", stale_info, {"master_display": "", "max_cll": "1000,400"}, _SOURCE_MD, "9,9")
        assert panel._video_settings_by_entry_id["v1"].get("max_cll") != "9,9"  # résultat périmé ignoré
    finally:
        panel.close()


def _p5_panel(monkeypatch):
    video = _video_track(0, HDRType.DOLBY_VISION)
    video.dovi_profile = 5
    entry = _video_entry(0)
    entry.entry_id = "video-p5"
    panel = _hdr_panel(monkeypatch, (_file_info(_PATH_A, [video]), entry, _COLOR))
    _select_codec(panel, "libx265")
    panel._save_current_video_state()
    return panel


_P5_ESTIMATE = dict(master_display="G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
                    max_cll="900,300", confidence="medium", sample_count=42, coverage=0.44, mode="precise")


def test_late_p5_estimate_never_replaces_an_entry_but_completes_empty_fields(qt_app, monkeypatch):
    panel = _p5_panel(monkeypatch)
    try:
        state = panel._video_settings_by_entry_id["video-p5"]
        state.update(static_hdr_metadata_analysis_request="precise", static_hdr_metadata_source="estimated_p5_to_p8",
                     max_cll="800,200", default_max_cll="900,300", static_hdr_max_cll_manual=True)
        panel._on_static_hdr_estimate_ready("video-p5", StaticHdrEstimate(**_P5_ESTIMATE))
        assert panel._video_settings_by_entry_id["video-p5"]["max_cll"] == "800,200"  # saisie gardée
        state = panel._video_settings_by_entry_id["video-p5"]
        state.update(static_hdr_metadata_analysis_request="precise", static_hdr_metadata_source="",
                     master_display="", max_cll="", static_hdr_master_display_manual=True,
                     static_hdr_max_cll_manual=True)  # champs vides, anciens marqueurs : jamais manuels
        panel._on_static_hdr_estimate_ready("video-p5", StaticHdrEstimate(**_P5_ESTIMATE))
        assert panel._video_settings_by_entry_id["video-p5"]["max_cll"] == "900,300"
    finally:
        panel.close()


# --- Dixième audit : anciens états face aux sondes, libellé après sonde, estimation P5 par signal -------------

_MANUAL_MD = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(40000000,50)"


@pytest.mark.parametrize("probe", ["track", "frames"])
@pytest.mark.parametrize("field", ["max_cll", "master_display"])
def test_late_probe_keeps_a_manual_value_of_an_old_state_without_markers(qt_app, monkeypatch, field, probe):
    """État d'une version antérieure : saisie reconnue par comparaison à la source, jamais par marqueur."""
    from core.fel.preparation import measures_light_levels

    raw = _CLL_RAW if field == "max_cll" else None  # l'autre champ manque : la sonde est lancée
    entry = _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", raw)
    other = _hdr_entry(1, Path("/tmp/other_hdr10.mkv"), HDRType.HDR10, "smpte2084", _CLL_RAW)
    panel = _hdr_panel(monkeypatch, entry, other)
    try:
        _select_codec(panel, "libx265")
        panel._video_list.setCurrentRow(1)  # état de la piste 0 remplacé hors affichage
        manual = "2000,500" if field == "max_cll" else _MANUAL_MD
        state = panel._video_settings_by_entry_id["v0"]
        state[field] = manual
        for marker in ("static_hdr_manual", "static_hdr_master_display_manual", "static_hdr_max_cll_manual"):
            state.pop(marker, None)
        panel._video_list.setCurrentRow(0)
        assert _launched(panel, _HDR10_PATH).static_hdr_light_level_source == (
            "manual" if field == "max_cll" else "source")
        expected = {"master_display": state.get("master_display", ""), "max_cll": state.get("max_cll", "")}
        if probe == "track":
            panel._track_hdr_requests["v0"] = entry[0]
            panel._apply_track_hdr("v0", entry[0], expected, _SOURCE_MD, "1000,400")
        else:
            panel._hdr_meta_probe_expected = (panel._hdr_meta_probe_generation, expected["master_display"],
                                              expected["max_cll"])
            panel._on_hdr_meta_frame_probe_ready(panel._hdr_meta_probe_generation, _SOURCE_MD, "1000,400")
        settings = _launched(panel, _HDR10_PATH)
        assert getattr(settings, field) == manual
        source_field = "static_hdr_light_level_source" if field == "max_cll" else "static_hdr_master_display_source"
        assert getattr(settings, source_field) == "manual"
        # FEL : décision sur MaxCLL seul — saisi : jamais mesuré ; Master Display seul saisi : MaxCLL mesurable.
        assert measures_light_levels(settings) is (field == "master_display")
        assert panel._hdr_meta_provenance.text() == translate_text("Saisie manuelle.")
    finally:
        panel.close()


def test_label_and_source_button_follow_values_completed_by_the_track_probe(qt_app, monkeypatch):
    entry = _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084")  # aucune valeur connue
    panel = _hdr_panel(monkeypatch, entry)
    try:
        _select_codec(panel, "libx265")
        assert not panel._hdr_meta_source_btn.isEnabled()
        panel._track_hdr_requests["v0"] = entry[0]
        panel._apply_track_hdr("v0", entry[0], {"master_display": "", "max_cll": ""}, _SOURCE_MD, "1000,400")
        assert panel._hdr_meta_provenance.text() == translate_text("Valeurs de la source.")
        assert panel._hdr_meta_source_btn.isEnabled()
        assert _launched(panel, _HDR10_PATH).static_hdr_light_level_source == "source"
    finally:
        panel.close()


def test_p5_estimate_delivered_by_the_workflow_signal_respects_entries(qt_app, monkeypatch):
    from PySide6.QtCore import QCoreApplication

    panel = _p5_panel(monkeypatch)
    try:
        state = panel._video_settings_by_entry_id["video-p5"]
        state.update(static_hdr_metadata_analysis_request="precise", static_hdr_metadata_source="estimated_p5_to_p8",
                     max_cll="800,200", default_max_cll="900,300", static_hdr_max_cll_manual=True)
        panel._workflow.static_hdr_estimate_ready.emit("video-p5", StaticHdrEstimate(**_P5_ESTIMATE))
        QCoreApplication.processEvents()  # connexion en file : livrée par la boucle Qt
        assert panel._video_settings_by_entry_id["video-p5"]["max_cll"] == "800,200"
        state = panel._video_settings_by_entry_id["video-p5"]
        state.update(static_hdr_metadata_analysis_request="precise", static_hdr_metadata_source="",
                     master_display="", max_cll="")
        panel._workflow.static_hdr_estimate_ready.emit("video-p5", StaticHdrEstimate(**_P5_ESTIMATE))
        QCoreApplication.processEvents()
        assert panel._video_settings_by_entry_id["video-p5"]["max_cll"] == "900,300"
        assert panel._max_cll.text() == "900,300"
    finally:
        panel.close()


def _hide_first_and_set_old_state(panel, **values) -> None:
    """État d'une version antérieure posé sur la piste 0 pendant que la piste 1 est affichée (sans marqueurs)."""
    panel._video_list.setCurrentRow(1)
    state = panel._video_settings_by_entry_id["v0"]
    state.update(values)
    for marker in ("static_hdr_manual", "static_hdr_master_display_manual", "static_hdr_max_cll_manual"):
        state.pop(marker, None)


def _hdr_sources(settings) -> tuple[str, str]:
    return settings.static_hdr_master_display_source, settings.static_hdr_light_level_source


@pytest.mark.parametrize("probe", ["track", "frames"])
@pytest.mark.parametrize("field", ["max_cll", "master_display"])
def test_old_value_identical_to_the_probe_result_keeps_its_provenance_through_redisplay(qt_app, monkeypatch, field,
                                                                                       probe):
    """Cas décisif : référence absente, ancienne valeur égale au résultat — la provenance survit au complément."""
    entry = _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084")  # aucune valeur de la source connue
    panel = _hdr_panel(monkeypatch, entry, _hdr_entry(1, Path("/tmp/other_hdr10.mkv"), HDRType.HDR10, "smpte2084"))
    try:
        _select_codec(panel, "libx265")
        old = "1000,400" if field == "max_cll" else _SOURCE_MD  # égale à ce que la sonde va trouver
        _hide_first_and_set_old_state(panel, **{field: old, f"default_{field}": ""})
        panel._video_list.setCurrentRow(0)
        expected = {"master_display": panel._master_display.text(), "max_cll": panel._max_cll.text()}
        assert expected[field] == old
        if probe == "track":
            panel._track_hdr_requests["v0"] = entry[0]
            panel._apply_track_hdr("v0", entry[0], expected, _SOURCE_MD, "1000,400")
        else:
            panel._hdr_meta_probe_expected = (panel._hdr_meta_probe_generation, expected["master_display"],
                                              expected["max_cll"])
            panel._on_hdr_meta_frame_probe_ready(panel._hdr_meta_probe_generation, _SOURCE_MD, "1000,400")
        assert panel._video_settings_by_entry_id["v0"][f"default_{field}"] == old
        panel._video_list.setCurrentRow(1)
        panel._video_list.setCurrentRow(0)  # sauvegarde puis réaffichage
        preview = panel._current_preview_config().video_tracks[0]
        launch = _launched(panel, _HDR10_PATH)
        wanted = ("source", "manual") if field == "max_cll" else ("manual", "source")
        assert _hdr_sources(preview) == _hdr_sources(launch) == wanted
        assert panel._hdr_meta_provenance.text() == translate_text("Saisie manuelle.")
    finally:
        panel.close()


def test_apply_all_old_state_of_a_hidden_track_never_contaminates_the_other_track(qt_app, monkeypatch):
    from core.fel.preparation import measures_light_levels

    entry = _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW)
    other_path = Path("/tmp/other_hdr10.mkv")
    panel = _hdr_panel(monkeypatch, entry, _hdr_entry(1, other_path, HDRType.HDR10, "smpte2084", _CLL_RAW))
    try:
        _select_codec(panel, "libx265")
        panel._apply_all_video_cb.setChecked(True)
        _hide_first_and_set_old_state(panel, max_cll="2000,500")
        panel._track_hdr_requests["v0"] = entry[0]
        panel._apply_track_hdr("v0", entry[0], {"master_display": "", "max_cll": "2000,500"}, _SOURCE_MD, "1000,400")
        hidden, shown = _launched(panel, _HDR10_PATH), _launched(panel, other_path)
        assert (hidden.max_cll, hidden.static_hdr_light_level_source) == ("2000,500", "manual")
        assert not measures_light_levels(hidden)
        assert (shown.max_cll, shown.static_hdr_light_level_source) == ("1000,400", "source")
        assert measures_light_levels(shown)
    finally:
        panel.close()


def test_partial_probe_keeps_the_manual_label_and_copy_keeps_the_read_only_one(qt_app, monkeypatch):
    entry = _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW)  # Master Display manquant
    panel = _hdr_panel(monkeypatch, entry)
    try:
        _select_codec(panel, "libx265")
        _type_cll(panel, "2000,500")
        panel._track_hdr_requests["v0"] = entry[0]
        panel._apply_track_hdr("v0", entry[0], {"master_display": "", "max_cll": "2000,500"}, _SOURCE_MD, "")
        assert panel._master_display.text() == _SOURCE_MD
        assert panel._hdr_meta_provenance.text() == translate_text("Saisie manuelle.")
        assert _hdr_sources(_launched(panel, _HDR10_PATH)) == ("source", "manual")
    finally:
        panel.close()
    entry = _hdr_entry(0, _HDR10_PATH, HDRType.HDR10, "smpte2084")
    panel = _hdr_panel(monkeypatch, entry)  # Copy ordinaire : valeurs de la source, non modifiables
    try:
        assert panel._codec_combo.currentData() == "copy"
        assert panel._max_cll.isReadOnly()  # défaut antérieur : piste sans état affichée en Copy modifiable
        panel._track_hdr_requests["v0"] = entry[0]
        panel._apply_track_hdr("v0", entry[0], {"master_display": "", "max_cll": ""}, _SOURCE_MD, "1000,400")
        assert panel._hdr_meta_provenance.text() == translate_text(
            "Valeurs de la source, non modifiables avec ce codec.")
        assert not panel._hdr_meta_source_btn.isEnabled() and panel._max_cll.isReadOnly()
    finally:
        panel.close()


def test_hdr10_fields_editability_follows_the_shared_rule_in_the_panel(qt_app, monkeypatch):
    video = _video_track(0, HDRType.DOLBY_VISION)
    video.dovi_profile, video.dovi_compat_id, video.color_transfer = 7, 6, "smpte2084"
    video.raw = dict(_CLL_RAW)
    entry = _video_entry(0)
    entry.entry_id = "dv"
    panel = _hdr_panel(monkeypatch, (_file_info(_PATH_A, [video], hdr_type=HDRType.DOLBY_VISION), entry, _COLOR))
    try:
        _select_codec(panel, "libx265")
        assert not panel._max_cll.isReadOnly()  # codec éditable
        _select_codec(panel, "copy")
        assert panel._max_cll.isReadOnly()  # Copy ordinaire
        assert panel._copy_dv_cb.isChecked() and panel._inject_hdr_cb.isChecked()
        panel._dovi_p81_cb.click()  # Copy + DV normalisé P8.1 + HDR10 injecté : exception
        assert panel._dovi_profile_choice == "2" and not panel._max_cll.isReadOnly()
        _type_cll(panel, "1000,400")
        assert _launched(panel, _PATH_A).static_hdr_light_level_source == "manual"
    finally:
        panel.close()


def test_p5_estimate_delivered_after_a_user_entry_never_replaces_it(qt_app, monkeypatch):
    from PySide6.QtCore import QCoreApplication

    panel = _p5_panel(monkeypatch)
    try:
        panel._video_settings_by_entry_id["video-p5"]["static_hdr_metadata_analysis_request"] = "precise"
        panel._workflow.static_hdr_estimate_ready.emit("video-p5", StaticHdrEstimate(**_P5_ESTIMATE))  # en file
        _type_cll(panel, "800,200")  # saisie avant la livraison du résultat
        QCoreApplication.processEvents()
        assert panel._max_cll.text() == "800,200"
        assert _launched(panel, _PATH_A).static_hdr_light_level_source == "manual"
    finally:
        panel.close()
    panel = _p5_panel(monkeypatch)
    try:  # succès : valeurs estimées, provenance automatique
        panel._video_settings_by_entry_id["video-p5"]["static_hdr_metadata_analysis_request"] = "precise"
        panel._workflow.static_hdr_estimate_ready.emit("video-p5", StaticHdrEstimate(**_P5_ESTIMATE))
        QCoreApplication.processEvents()
        settings = _launched(panel, _PATH_A)
        assert settings.max_cll == "900,300" and _hdr_sources(settings) == ("source", "source")
        assert settings.static_hdr_metadata_source == "estimated_p5_to_p8"
    finally:
        panel.close()


# --- Onzième audit : restauration atomique d'un état Copy + Dolby Vision normalisé P8.1 -------------------------


def _dv_p7_entry(index: int, path: Path):
    video = _video_track(0, HDRType.DOLBY_VISION)
    video.dovi_profile, video.dovi_compat_id, video.color_transfer = 7, 6, "smpte2084"
    video.raw = dict(_CLL_RAW)
    entry = _video_entry(index)
    entry.entry_id = f"v{index}"
    return _file_info(path, [video], hdr_type=HDRType.DOLBY_VISION), entry, _COLOR


@pytest.mark.parametrize("apply_all", [False, True])
def test_copy_p81_entries_survive_displaying_a_track_without_dovi(qt_app, monkeypatch, apply_all):
    from PySide6.QtTest import QTest

    dv_path = Path("/tmp/dv_p7.mkv")
    panel = _hdr_panel(monkeypatch, _dv_p7_entry(0, dv_path),
                       _hdr_entry(1, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW))
    try:
        panel._apply_all_video_cb.setChecked(apply_all)
        assert panel._codec_combo.currentData() == "copy"
        panel._dovi_p81_cb.click()  # Copy + DV normalisé P8.1 + HDR10 injecté : HDR10 modifiable
        assert not panel._max_cll.isReadOnly()
        panel._master_display.clear()
        QTest.keyClicks(panel._master_display, _MANUAL_MD)
        _type_cll(panel, "2000,500")
        panel._video_list.setCurrentRow(1)  # piste HDR10 sans DV, en Copy
        panel._video_list.setCurrentRow(0)
        assert (panel._master_display.text(), panel._max_cll.text()) == (_MANUAL_MD, "2000,500")
        assert not panel._max_cll.isReadOnly() and panel._dovi_p81_cb.isChecked()
        assert panel._hdr_meta_provenance.text() == translate_text("Saisie manuelle.")
        state = panel._video_settings_by_entry_id["v0"]
        assert state["static_hdr_master_display_manual"] and state["static_hdr_max_cll_manual"]
        preview = panel._current_preview_config().video_tracks[0]
        launch = _launched(panel, dv_path)
        for settings in (preview, launch):
            assert (settings.master_display, settings.max_cll) == (_MANUAL_MD, "2000,500")
            assert _hdr_sources(settings) == ("manual", "manual")
    finally:
        panel.close()


def test_apply_all_reencoding_propagates_without_losing_copy_p81_entries(qt_app, monkeypatch):
    from PySide6.QtTest import QTest

    dv_path = Path("/tmp/dv_p7.mkv")
    panel = _hdr_panel(monkeypatch, _dv_p7_entry(0, dv_path),
                       _hdr_entry(1, _HDR10_PATH, HDRType.HDR10, "smpte2084", _CLL_RAW))
    try:
        panel._dovi_p81_cb.click()  # Copy + P8.1 : HDR10 modifiable
        panel._master_display.clear()
        QTest.keyClicks(panel._master_display, _MANUAL_MD)
        _type_cll(panel, "2000,500")
        panel._apply_all_video_cb.setChecked(True)
        panel._video_list.setCurrentRow(1)
        _select_codec(panel, "libx265")  # réencodage : propagé à la piste P7
        launch = _launched(panel, dv_path)
        assert launch.codec == "libx265"  # propagation effective
        assert (launch.master_display, launch.max_cll) == (_MANUAL_MD, "2000,500")
        assert _hdr_sources(launch) == ("manual", "manual")
        panel._video_list.setCurrentRow(0)
        preview = panel._current_preview_config().video_tracks[0]
        assert (preview.master_display, preview.max_cll) == (_MANUAL_MD, "2000,500")
        assert _hdr_sources(preview) == ("manual", "manual")
        assert panel._hdr_meta_provenance.text() == translate_text("Saisie manuelle.")
    finally:
        panel.close()
