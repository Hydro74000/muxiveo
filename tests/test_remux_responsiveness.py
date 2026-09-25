"""Réactivité de la chaîne sources → remux → encodage, avec I/O lentes."""
from dataclasses import replace
from threading import Event, get_ident
import time
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication
from shiboken6 import Shiboken

from core.config import AppConfig
from core.inspector import HDRType
from ui.panels.encode_panel.panel import EncodePanel
from ui.panels.encode_panel.widgets import _AudioTable
from ui.panels.remux_panel.panel import RemuxPanel
from ui.panels.remux_panel.models import SourceFile
from tests.test_encode_panel_widgets import _at, _file_info, _video_entry, _video_track
from tests.test_remux import _track


def wait_until(app, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    assert predicate()


@pytest.fixture(autouse=True)
def no_hardware_detection(monkeypatch):
    monkeypatch.setattr(EncodePanel, "_detect_hw_encoders", lambda self: None)


@pytest.mark.parametrize("panel_class", [RemuxPanel, EncodePanel])
def test_preview_is_async_coalesced_and_copies_latest_snapshot(qt_app, monkeypatch, panel_class):
    panel = panel_class(AppConfig())
    entered, release = Event(), Event()
    ui_thread = get_ident()
    calls = []
    config = {"order": [1, 2]}

    def compile_preview(snapshot):
        calls.append((get_ident(), snapshot))
        entered.set()
        assert release.wait(3)
        return str(snapshot["order"])

    monkeypatch.setattr(panel, "_current_config", lambda: config)
    monkeypatch.setattr(panel._workflow, "preview_command", compile_preview)
    try:
        panel._rebuild_preview()
        panel._rebuild_preview()
        assert calls == []
        wait_until(qt_app, entered.is_set)
        assert len(calls) == 1
        assert calls[0][0] != ui_thread
        # La boucle Qt continue même si la compilation est bloquée.
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        wait_until(qt_app, lambda: bool(heartbeat))
        config["order"].reverse()
        panel._rebuild_preview()
        QApplication.clipboard().setText("ancienne commande")
        panel._copy_command()
        assert QApplication.clipboard().text() == "ancienne commande"
        assert calls[0][1]["order"] == [1, 2]
        release.set()
        wait_until(qt_app, lambda: QApplication.clipboard().text() == "[2, 1]")
        assert panel._cmd_preview.toPlainText() == "[2, 1]"
        assert len(calls) == 2
    finally:
        release.set()
        panel.close()


@pytest.mark.parametrize("panel_class", [RemuxPanel, EncodePanel])
def test_close_does_not_wait_for_command_io(qt_app, monkeypatch, panel_class):
    panel = panel_class(AppConfig())
    entered, release = Event(), Event()

    def compile_preview(config):
        entered.set()
        release.wait(3)
        return "late"

    monkeypatch.setattr(panel, "_current_config", lambda: object())
    monkeypatch.setattr(panel._workflow, "preview_command", compile_preview)
    try:
        panel._rebuild_preview()
        wait_until(qt_app, entered.is_set)
        started = time.monotonic()
        panel.close()
        assert time.monotonic() - started < 0.5
        assert panel._cmd_preview.toPlainText() != "late"
    finally:
        release.set()
        panel.close()


def test_multiple_sdr_sources_do_not_probe_hdr_in_ui(qt_app, monkeypatch, tmp_path):
    panel = EncodePanel(AppConfig())
    probe = MagicMock(side_effect=AssertionError("Unexpected HDR probe"))
    monkeypatch.setattr(panel, "_extract_hdr_meta_from_mediainfo", probe)
    monkeypatch.setattr(panel, "_extract_hdr_meta_from_ffprobe_frames", probe)
    try:
        tracks = [(_file_info(tmp_path / f"{i}.mkv", [_video_track(0)]), _video_entry(), "#fff")
                  for i in range(3)]
        panel.set_video_tracks(tracks)
        panel.set_video_tracks(list(reversed(tracks)))
        panel._current_video_settings_list()
        probe.assert_not_called()
    finally:
        panel.close()


def test_hdr_analysis_for_all_tracks_preserves_manual_fields(qt_app, monkeypatch, tmp_path):
    panel = EncodePanel(AppConfig())
    entered, release = Event(), Event()
    calls = []
    ui_thread = get_ident()

    def probe(path, stream_index=None):
        calls.append((get_ident(), stream_index))
        entered.set()
        assert release.wait(3)
        return f"master-{stream_index}", "1000,400"

    monkeypatch.setattr(panel, "_extract_hdr_meta_from_ffprobe_frames", probe)
    monkeypatch.setattr(panel, "_extract_hdr_meta_from_mediainfo", lambda path: ("", ""))
    info = _file_info(tmp_path / "hdr.mkv", [_video_track(0, HDRType.HDR10), _video_track(2, HDRType.HDR10)])
    first, second = _video_entry(0), _video_entry(2)
    try:
        panel.set_video_tracks([(info, first, "#fff"), (info, second, "#fff")])
        wait_until(qt_app, entered.is_set)
        panel._master_display.setText("manual")
        release.set()
        wait_until(qt_app, lambda: panel._video_settings_by_entry_id[second.entry_id].get("max_cll") == "1000,400")
        assert panel._master_display.text() == "manual"
        assert panel._max_cll.text() == "1000,400"
        assert panel._video_settings_by_entry_id[second.entry_id]["master_display"] == "master-2"
        assert all(thread != ui_thread for thread, _ in calls)
        assert [index for _, index in calls] == [0, 2]
        panel.set_video_tracks([(info, second, "#fff"), (info, first, "#fff")])
        assert len(calls) == 2
    finally:
        release.set()
        panel.close()


def test_audio_reorder_and_metadata_reuse_widgets_and_settings(qt_app):
    table = _AudioTable()
    first, second = _track(1), _track(2)
    tracks = [(_at(1), "#fff", None, first), (_at(2), "#fff", None, second)]
    try:
        table.load_tracks(tracks)
        combo = table.cellWidget(0, table.COL_CODEC)
        combo.setCurrentIndex(combo.findData("aac"))
        bitrate = table.cellWidget(0, table.COL_BITRATE)
        before = bitrate.value()
        table.load_tracks(list(reversed(tracks)))
        # Détecte aussi une destruction différée accidentelle des widgets Qt.
        # Flush ciblé : un flush global détruirait les objets laissés par
        # les autres tests et fait planter Qt.
        for widget in (combo, bitrate):
            QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
        assert Shiboken.isValid(combo) and Shiboken.isValid(bitrate)
        assert table.cellWidget(1, table.COL_CODEC) is combo
        assert table.cellWidget(1, table.COL_BITRATE) is bitrate
        changed = (replace(tracks[0][0], title="Edited", language="eng"), *tracks[0][1:])
        table.load_tracks([changed])
        assert table.cellWidget(0, table.COL_CODEC) is combo
        assert table.item(0, table.COL_TITLE).text() == "Edited"
        assert table.item(0, table.COL_LANG).text() == "eng"
        assert combo.currentData() == "aac"
        assert bitrate.value() == before
        assert table.current_audio_settings()[0].track_entry_id == first.entry_id
    finally:
        table.close()


def test_folder_scan_does_not_block_gui_and_ignores_result_after_close(qt_app, monkeypatch, tmp_path):
    panel = RemuxPanel(AppConfig())
    entered, release = Event(), Event()
    ui_thread = get_ident()
    threads = []

    def scan(folder):
        threads.append(get_ident())
        entered.set()
        release.wait(3)
        return [str(tmp_path / "late.mkv")], []

    monkeypatch.setattr(panel, "_collect_folder_drop_paths", scan)
    added = MagicMock()
    monkeypatch.setattr(panel, "_on_add_files", added)
    try:
        panel._route_dropped_paths([str(tmp_path)])
        wait_until(qt_app, entered.is_set)
        assert len(threads) == 1 and threads[0] != ui_thread
        panel.close()
        release.set()
        panel._path_executor.shutdown(wait=True)
        qt_app.processEvents()
        added.assert_not_called()
    finally:
        release.set()
        panel.close()


def test_select_all_updates_encode_and_filter_survives_insert(qt_app):
    panel = RemuxPanel(AppConfig())
    tracks = [_track(1, enabled=False), _track(2)]
    observed = []
    panel.audio_tracks_changed.connect(observed.append)
    try:
        panel._track_table.append_tracks("#fff", tracks)
        panel._track_table.set_filter_selected(True)
        assert panel._track_table.isRowHidden(0)
        panel._set_all_tracks(True)
        assert len(observed) == 1
        assert all(t.enabled for t in panel._track_table.current_tracks())
        panel._track_table.append_tracks("#fff", [_track(0, "video", enabled=False)])
        assert panel._track_table.isRowHidden(0)
        assert not panel._track_table.isRowHidden(1)
        assert panel._track_table._prev_lang[1] == tracks[0].language
    finally:
        panel.close()


def test_audio_encoding_feedback_does_not_reload_sources(qt_app, tmp_path):
    remux = RemuxPanel(AppConfig())
    encode = EncodePanel(AppConfig())
    info = _file_info(tmp_path / "source.mkv", [])
    info.audio_tracks = [_at(i) for i in range(8)]
    sf = SourceFile("source", info.path, "#fff")
    remux._source_files.append(sf)
    remux._source_colors[sf.id] = sf.color
    remux.audio_tracks_changed.connect(encode.set_audio_tracks)
    encode.audio_track_encoding_changed.connect(remux.update_audio_track_encoding)
    observed = []
    remux.audio_tracks_changed.connect(observed.append)
    try:
        remux._apply_inspection(sf.id, info)
        assert len(observed) == 1
        table = encode._audio_table
        combo = table.cellWidget(0, table.COL_CODEC)
        combo.setCurrentIndex(combo.findData("aac"))
        assert len(observed) == 1
        assert table.cellWidget(0, table.COL_CODEC) is combo
        assert sf.tracks[0].codec == "AAC"
        assert table.current_audio_settings()[0].codec == "aac"
    finally:
        encode.close()
        remux.close()


def test_removing_queued_source_cancels_inspection(qt_app, monkeypatch, tmp_path):
    from concurrent.futures import Future

    panel = RemuxPanel(AppConfig())
    future = Future()
    monkeypatch.setattr(panel._inspection_executor, "submit", lambda *args: future)
    try:
        panel.add_sources([tmp_path / "source.mkv"])
        sf = panel._source_files[0]
        panel._on_remove_file(sf.id)
        assert future.cancelled()
        # Un résultat déjà calculé et encore dans la file Qt doit être ignoré.
        panel._apply_inspection(sf.id, _file_info(sf.path, [_video_track(0)]))
        assert panel._track_table.rowCount() == 0
        assert not panel._source_files
    finally:
        panel.close()
