"""Panneau Conteneur : recalage d'une piste face à une autre piste du même fichier (Synchro Studio)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QDialog, QPushButton

from core.config import AppConfig
from core.i18n import translate_text
from core.workflows.remux_mapping import resolve_mapped_tracks
from core.workflows.remux_models import TrackEntry
from core.workflows.sync_calibration import SyncCalibration, SyncSegment
from tests.test_remux import _COLOR_A, _audio, _file_info, _subtitle, _track
from ui.panels.remux_panel import RemuxPanel, SourceFile
from ui.panels.remux_panel.functions import workflow as workflow_functions
from ui.panels.remux_panel.widgets import sync_studio_dialog as studio_module


def _add_source(panel, tmp_path, name, tracks):
    path = tmp_path / f"{name}.mkv"
    path.touch()
    audios = [_audio(index=track.mkv_tid) for track in tracks if track.track_type == "audio"]
    subs = [_subtitle(index=track.mkv_tid) for track in tracks if track.track_type == "subtitle"]
    info = _file_info(path=path, audios=audios, subs=subs)
    panel._source_files.append(SourceFile(id=name, path=path, color=_COLOR_A, info=info, tracks=tracks))
    panel._source_names[name] = f"{name}.mkv"
    panel._source_colors[name] = _COLOR_A
    panel._track_table.append_tracks(_COLOR_A, tracks)


@pytest.fixture
def films(qt_app, tmp_path):
    """A (maître) : une piste audio ; B : VF, VO et sous-titres."""
    panel = RemuxPanel(AppConfig())
    master = _track(1, "audio", file_id="A")
    vf = _track(1, "audio", file_id="B")
    vo = _track(2, "audio", file_id="B", language="eng")
    subtitle = _track(3, "subtitle", codec="SRT", file_id="B")
    _add_source(panel, tmp_path, "A", [master])
    _add_source(panel, tmp_path, "B", [vf, vo, subtitle])
    panel._refresh_audio_sync_buttons()
    panel._output_edit.setText(str(tmp_path / "output.mkv"))
    logs: list[tuple[str, str]] = []
    panel.log_message.connect(lambda level, text: logs.append((level, text)))
    yield panel, master, vf, vo, subtitle, logs
    panel.close()


class _Studio:
    """Synchro Studio validé avec `delta` ms de plus que la synchro affichée."""

    opened: list[dict] = []
    delta = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _Studio.opened.append(kwargs)

    def exec(self):
        return QDialog.DialogCode.Accepted

    def result_calibration(self):
        shown = self.kwargs["calibration"]
        changed = SyncCalibration.linear(shown.segments[0].shift_ms + _Studio.delta)
        return changed, round(changed.segments[0].shift_ms)


def _studio(panel, monkeypatch, target, reference, delta):
    """Studio ouvert sur `target`, `reference` choisie, validé avec `delta` ms."""
    offered: list[list] = []

    def _dialog(choices, **kwargs):
        offered.append(([choice for _label, choice in choices], kwargs.get("selected")))
        return SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted, selected_entry=lambda: reference)

    monkeypatch.setattr(studio_module, "SyncStudioDialog", _Studio)
    monkeypatch.setattr("ui.panels.remux_panel.panel._AudioSyncReferenceDialog", _dialog)
    _Studio.opened.clear()
    _Studio.delta = delta
    panel._on_sync_studio_requested(target)
    return offered


def _shifts(*tracks):
    return [track.time_shift_ms for track in tracks]


def test_single_file_offers_the_studio(qt_app, tmp_path):
    panel = RemuxPanel(AppConfig())
    vf = _track(1, "audio", file_id="B")
    subtitle = _track(3, "subtitle", codec="SRT", file_id="B")
    _add_source(panel, tmp_path, "B", [vf, subtitle])
    panel._refresh_audio_sync_buttons()
    tooltips = {
        translate_text("Ouvrir le Synchro Studio (forme d'onde, sous-titres, écoute, ajustement)"),
        translate_text("Ouvrir le Synchro Studio (forme d'onde, écoute, ajustement)"),
    }
    received = []
    panel._track_table.sync_studio_requested.disconnect()
    panel._track_table.sync_studio_requested.connect(received.append)
    for row, entry in enumerate((vf, subtitle)):
        for button in panel._track_table.cellWidget(row, panel._track_table.COL_EDIT).findChildren(QPushButton):
            if button.toolTip() in tooltips:
                button.click()
    assert received == [vf, subtitle]
    panel.close()


def test_track_is_realigned_alone_against_a_track_of_its_file(films, monkeypatch):
    panel, master, vf, vo, subtitle, _logs = films
    offered = _studio(panel, monkeypatch, vo, vf, 200)
    # Autre fichier d'abord, puis les pistes du même fichier (jamais la cible).
    assert offered == [([master, vf], None)]
    opened = _Studio.opened[0]
    assert opened["calibration"] == SyncCalibration.linear(0)
    assert opened["reference_source_path"] == opened["target_source_path"]
    assert opened["analysis_stream_index"] == vo.mkv_tid
    assert _shifts(master, vf, vo, subtitle) == [0, 0, 200, 0]
    assert panel._track_sync_links[vo.entry_id].reference_entry_id == vf.entry_id
    assert "B" not in panel._sync_links
    mapped = {item.track.entry_id: item.track.time_shift_ms for item in resolve_mapped_tracks(panel.collect_config())}
    assert (mapped[vf.entry_id], mapped[vo.entry_id]) == (0, 200)


@pytest.mark.parametrize("realign_first", [True, False])
def test_realigned_track_follows_its_source_sync(films, monkeypatch, realign_first):
    panel, master, vf, vo, subtitle, _logs = films
    if realign_first:
        _studio(panel, monkeypatch, vo, vf, 200)
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, -5466, .95, None)
    if not realign_first:
        _studio(panel, monkeypatch, vo, vf, 200)
        assert _Studio.opened[0]["calibration"] == SyncCalibration.linear(0)
    assert _shifts(master, vf, vo, subtitle) == [0, -5466, -5266, -5466]
    # Réouverture : écart relatif à la référence, référence présélectionnée.
    offered = _studio(panel, monkeypatch, vo, vf, 0)
    assert offered == [([master, vf], vf)]
    assert _Studio.opened[0]["calibration"].segments[0].shift_ms == 200
    assert _shifts(master, vf, vo, subtitle) == [0, -5466, -5266, -5466]


def test_cancel_or_entered_offset_releases_the_link(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    panel._apply_source_sync_offset("B", 1000)
    _studio(panel, monkeypatch, vo, vf, 200)
    assert _shifts(vf, vo) == [1000, 1200]
    panel._on_auto_sync_cancel_requested(vo)
    assert _shifts(vf, vo) == [1000, 1000]
    assert vo.entry_id not in panel._track_sync_links and vo.entry_id not in panel._own_sync_entry_ids

    _studio(panel, monkeypatch, vo, vf, 200)
    vo.time_shift_ms = 1500
    panel._track_table.time_shift_edited.emit(vo)
    assert vo.entry_id not in panel._track_sync_links
    panel._apply_source_sync_offset("B", 0)
    panel._propagate_sync({"B"})
    assert _shifts(vf, vo) == [0, 1500]


def test_loop_releases_the_former_realignment(films, monkeypatch):
    """VO recalée sur VF (+200), puis VF sur VO : VO suit de nouveau sa source, l'écart choisi prime."""
    panel, _master, vf, vo, _subtitle, logs = films
    _studio(panel, monkeypatch, vo, vf, 200)
    _studio(panel, monkeypatch, vf, vo, 100)
    assert _Studio.opened[0]["calibration"] == SyncCalibration.linear(-200)
    assert vo.entry_id not in panel._track_sync_links and vo.entry_id not in panel._own_sync_entry_ids
    assert panel._track_sync_links[vf.entry_id].reference_entry_id == vo.entry_id
    assert _shifts(vf, vo) == [-100, 0]
    assert ("INFO", translate_text(
        "La piste de référence #{reference} était recalée à partir de la piste #{target} : "
        "son recalage est retiré, le nouveau prime.", reference=vo.mkv_tid, target=vf.mkv_tid,
    )) in logs


def test_session_round_trips_track_links(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 200)
    session = workflow_functions._session_job(panel)["_muxiveo_session"]
    config = SimpleNamespace(sources=[SimpleNamespace(tracks=source.tracks) for source in panel._source_files])
    restored = workflow_functions._restored_track_sync_links(panel, config, session["track_sync_links"])
    assert restored == panel._track_sync_links
    assert workflow_functions._restored_track_sync_links(panel, config, None) == {}


def test_references_have_the_type_of_the_target(films, monkeypatch, tmp_path):
    """Audio ↔ audio, sous-titres ↔ sous-titres : autres fichiers, même fichier et repli."""
    panel, master, vf, vo, subtitle, _logs = films
    other_sub = _track(2, "subtitle", codec="SRT", file_id="A")
    second_sub = _track(4, "subtitle", codec="SRT", file_id="B", language="eng")
    panel._track_table.append_tracks(_COLOR_A, [other_sub, second_sub])
    offered = _studio(panel, monkeypatch, subtitle, second_sub, 0)
    assert offered == [([other_sub, second_sub], None)]
    offered = _studio(panel, monkeypatch, vo, vf, 0)
    assert offered == [([master, vf], None)]
    # Fichier seul, aucun autre sous-titre : le studio s'ouvre sans référence (jamais l'audio).
    alone = RemuxPanel(AppConfig())
    lone_audio = _track(1, "audio", file_id="C")
    lone_sub = _track(2, "subtitle", codec="SRT", file_id="C")
    _add_source(alone, tmp_path, "C", [lone_audio, lone_sub])
    _studio(alone, monkeypatch, lone_sub, None, 0)
    assert _Studio.opened[0]["reference_entry"] is None
    alone.close()


def test_subtitle_without_mirror_is_realigned_alone_and_cancelled_to_zero(films, monkeypatch):
    """Sous-titres hors miroir : jamais déplacés par leur source, recalage annulé → 0."""
    panel, _master, _vf, _vo, subtitle, _logs = films
    panel._workflow_options["sync_subtitles"] = "off"
    second_sub = _track(4, "subtitle", codec="SRT", file_id="B", language="eng")
    panel._track_table.append_tracks(_COLOR_A, [second_sub])
    panel._apply_source_sync_offset("B", 1000)
    assert _shifts(subtitle, second_sub) == [0, 0]
    _studio(panel, monkeypatch, subtitle, second_sub, 300)
    assert subtitle.entry_id not in panel._own_sync_entry_ids
    assert _shifts(subtitle, second_sub) == [300, 0]
    panel._on_auto_sync_cancel_requested(subtitle)
    assert _shifts(subtitle, second_sub) == [0, 0]


def test_restored_session_keeps_following_the_reference(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 200)
    session = workflow_functions._session_job(panel)["_muxiveo_session"]
    config = panel.collect_config()
    workflow_functions.restore(panel, config, [source.info for source in panel._source_files], session)
    a_id, b_id = (source.id for source in panel._source_files)
    tracks = {(track.file_id, track.mkv_tid): track for track in panel._track_table.current_tracks()}
    vf, vo, sub = tracks[(b_id, 1)], tracks[(b_id, 2)], tracks[(b_id, 3)]
    assert panel._track_sync_links[vo.entry_id].reference_entry_id == vf.entry_id
    assert vo.entry_id in panel._own_sync_entry_ids
    assert _shifts(vf, vo, sub) == [0, 200, 0]
    panel._on_audio_sync_done(vf.entry_id, tracks[(a_id, 1)].entry_id, -1000, .95, None)
    assert _shifts(vf, vo, sub) == [-1000, -800, -1000]


def test_track_rejoining_a_synced_source_stays_cancellable(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    panel._apply_source_sync_offset("B", 1000)
    _studio(panel, monkeypatch, vo, vf, 200)
    panel._on_auto_sync_cancel_requested(vo)
    assert _shifts(vf, vo) == [1000, 1000]
    assert vo.entry_id in panel._auto_sync_entry_ids


def test_realigned_track_does_not_hold_the_source_sync(films, monkeypatch):
    """Annuler toutes les pistes qui suivent la source retire sa synchro, malgré une piste recalée."""
    panel, master, vf, vo, subtitle, _logs = films
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, -5466, .95, None)
    _studio(panel, monkeypatch, vo, vf, 200)
    panel._on_auto_sync_cancel_requested(vf)
    panel._on_auto_sync_cancel_requested(subtitle)
    assert "B" not in panel._sync_links and "B" not in panel._source_sync_offsets_ms
    assert _shifts(vf, vo, subtitle) == [0, 200, 0]
    panel._on_auto_sync_cancel_requested(vo)
    assert _shifts(vf, vo, subtitle) == [0, 0, 0]


@pytest.mark.parametrize("source_offset,delta,expected", [(0, 3, 3), (-200, 200, 0)])
def test_studio_setting_is_applied_without_negligible_threshold(films, monkeypatch, source_offset, delta, expected):
    panel, _master, vf, vo, _subtitle, logs = films
    panel._apply_source_sync_offset("B", source_offset)
    _studio(panel, monkeypatch, vo, vf, delta)
    assert vo.time_shift_ms == expected
    assert not any("non appliquée" in text or "not applied" in text for _level, text in logs)
    panel._apply_source_sync_offset("B", -5466)
    panel._propagate_sync({"B"})
    assert vo.time_shift_ms == -5466 + delta


def test_studio_against_a_realigned_track_of_the_linked_source(films, monkeypatch):
    """B suit a1 ; a2 recalée sur a1 (+300) : face à a2, le studio montre la position réelle de B."""
    panel, master, vf, _vo, _subtitle, _logs = films
    a2 = _track(2, "audio", file_id="A", language="eng")
    panel._track_table.append_tracks(_COLOR_A, [a2])
    _studio(panel, monkeypatch, a2, master, 300)
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, -5000, .95, None)
    _studio(panel, monkeypatch, vf, a2, 0)
    assert _Studio.opened[0]["calibration"].segments[0].shift_ms == -5300
    assert vf.time_shift_ms == -5000


def test_unrecognised_layout_still_offers_the_other_files(films, monkeypatch):
    panel, master, _vf, vo, _subtitle, _logs = films
    mono = TrackEntry(4, "audio", "AAC", "1.0  48 kHz", "fra", "Mono", file_id="B")
    panel._track_table.append_tracks(_COLOR_A, [mono])
    offered = _studio(panel, monkeypatch, mono, master, 0)
    assert offered[0][0][0] is master and vo in offered[0][0]


def test_source_following_a_realigned_track_is_a_loop(films, monkeypatch):
    panel, master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 200)
    panel._on_audio_sync_done(master.entry_id, vo.entry_id, 100, .95, None)
    assert panel._sync_links["A"].reference_entry_id == vo.entry_id
    assert panel._sync_follows_track("A", vf)


def test_restored_link_keeps_the_source_cuts(films, monkeypatch):
    """Recalage ramené à 0 sur une source à coupures : synchro de la piste égale à celle de la source,
    absente du job, recalculée à la restauration."""
    panel, master, vf, vo, _subtitle, _logs = films
    cuts = SyncCalibration((SyncSegment(0, -100), SyncSegment(60000, -300)))
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, -100, .95, cuts)
    _studio(panel, monkeypatch, vo, vf, 200)
    _studio(panel, monkeypatch, vo, vf, -200)
    assert panel._track_sync_links[vo.entry_id].relative == SyncCalibration.linear(0)
    session = workflow_functions._session_job(panel)["_muxiveo_session"]
    config = panel.collect_config()
    workflow_functions.restore(panel, config, [source.info for source in panel._source_files], session)
    b_id = panel._source_files[1].id
    tracks = {(track.file_id, track.mkv_tid): track for track in panel._track_table.current_tracks()}
    vf, vo = tracks[(b_id, 1)], tracks[(b_id, 2)]
    assert vo.entry_id in panel._track_sync_links
    assert panel._track_calibration(vo).segments == panel._track_calibration(vf).segments == cuts.segments


def test_subtitle_studio_without_reference_has_no_automatic_alignment(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    subtitle = TrackEntry(2, "subtitle", "SubRip", "", "fre", "VF", file_id="B")
    dialog = studio_module.SyncStudioDialog(
        target_entry=subtitle, target_source_path=tmp_path / "b.mkv", target_stream_index=2,
        reference_entry=None, reference_source_path=None, reference_stream_index=None,
    )
    assert not hasattr(dialog, "btn_auto_sub_sync")
    dialog._run_auto_subtitle_sync()
    assert dialog._is_sub_sync_running is False
    dialog.close()


def _reloaded(panel, monkeypatch):
    """Session sérialisée comme sur disque (job exact), relue puis restaurée dans le panneau."""
    from cli.logging import Logger
    from core.jobs.options import CommonOptions
    from core.jobs.remux_config import build_remux_config

    infos = {source.path: source.info for source in panel._source_files}
    monkeypatch.setattr("core.inspector.FileInspector.inspect", lambda _self, path, *_a, **_k: infos[Path(path)])
    job = workflow_functions._session_job(panel)
    config = build_remux_config(job, panel._config, CommonOptions(), Logger())
    workflow_functions.restore(panel, config, [source.info for source in panel._source_files],
                               job["_muxiveo_session"])
    b_id = panel._source_files[1].id
    tracks = {(track.file_id, track.mkv_tid): track for track in panel._track_table.current_tracks()}
    return tracks[(b_id, 1)], tracks[(b_id, 2)], tracks[(b_id, 3)]


def test_analysis_uses_a_track_on_the_source_timeline(films, monkeypatch):
    panel, master, vf, vo, _subtitle, _logs = films
    english = TrackEntry(1, "audio", "EAC3", "5.1  48 kHz", "eng", "VO", file_id="A")
    assert panel._audio_sync_measure_entry(vf, english) is vo
    _studio(panel, monkeypatch, vo, vf, -2000)
    # VO recalée : hors de la chronologie de B, jamais mesurée pour toute la source.
    assert panel._audio_sync_measure_entry(vf, english) is vf
    # Studio de VO face à une référence française : l'analyse porte sur VO, recalée seule.
    _studio(panel, monkeypatch, vo, master, 0)
    assert _Studio.opened[0]["analysis_stream_index"] == vo.mkv_tid


def test_followed_track_becomes_cancellable(films, monkeypatch):
    panel, master, vf, vo, _subtitle, _logs = films
    panel._apply_source_sync_offset("B", -200)
    _studio(panel, monkeypatch, vo, vf, 200)
    assert vo.time_shift_ms == 0 and vo.sync_calibration is not None
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, -1000, .95, None)
    assert vo.time_shift_ms == -800
    assert vo.entry_id in panel._track_table._auto_sync_cancelable_entry_ids


def test_subtitle_without_mirror_linked_at_zero_stays_cancellable(films, monkeypatch):
    panel, _master, _vf, _vo, subtitle, _logs = films
    panel._workflow_options["sync_subtitles"] = "off"
    second_sub = _track(4, "subtitle", codec="SRT", file_id="B", language="eng")
    panel._track_table.append_tracks(_COLOR_A, [second_sub])
    second_sub.time_shift_ms = -200
    _studio(panel, monkeypatch, subtitle, second_sub, 100)
    assert subtitle.time_shift_ms == 100
    _studio(panel, monkeypatch, subtitle, second_sub, -100)
    assert subtitle.time_shift_ms == 0 and subtitle.sync_calibration is not None
    panel._on_auto_sync_cancel_requested(subtitle)
    assert subtitle.entry_id not in panel._track_sync_links and subtitle.sync_calibration is None


def test_proposed_sync_through_a_loop_on_a_realigned_track(films, monkeypatch):
    panel, master, vf, vo, _subtitle, _logs = films
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 150, .95, None)
    _studio(panel, monkeypatch, vo, vf, 200)
    assert vo.time_shift_ms == 350
    proposed = panel._proposed_sync(master, vo, SyncCalibration.linear(10))
    assert proposed.segments[0].shift_ms == 210
    panel._on_audio_sync_done(master.entry_id, vo.entry_id, 10, .95, None)
    assert _shifts(master, vf, vo) == [210, 0, 200]


def test_reloaded_session_keeps_the_link_on_an_imported_source_calibration(films, monkeypatch):
    """Source à calibration linéaire importée, recalage ramené à 0 : la piste garde sa calibration
    après relecture du job ; un décalage saisi ensuite est bien celui du remux."""
    panel, _master, vf, vo, _subtitle, _logs = films
    panel._workflow_options["sync_calibrations"] = {"1": SyncCalibration.linear(-1000).to_dict()}
    panel._sync_entry_calibrations()
    _studio(panel, monkeypatch, vo, vf, 200)
    _studio(panel, monkeypatch, vo, vf, -200)
    vf, vo, _sub = _reloaded(panel, monkeypatch)
    assert vo.entry_id in panel._track_sync_links and vo.sync_calibration is not None
    assert panel._track_calibration(vo) == SyncCalibration.linear(-1000)
    vo.time_shift_ms = 300
    panel._track_table.time_shift_edited.emit(vo)
    mapped = {item.track.entry_id: item.track.time_shift_ms for item in resolve_mapped_tracks(panel.collect_config())}
    assert mapped[vo.entry_id] == 300 and mapped[vf.entry_id] == -1000


def test_reloaded_session_keeps_the_source_cuts(films, monkeypatch):
    panel, master, vf, vo, _subtitle, _logs = films
    panel._workflow_options["sync_mode"] = "physical"
    cuts = SyncCalibration((SyncSegment(0, -100), SyncSegment(60000, -300)))
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, -100, .95, cuts)
    _studio(panel, monkeypatch, vo, vf, 200)
    _studio(panel, monkeypatch, vo, vf, -200)
    vf, vo, _sub = _reloaded(panel, monkeypatch)
    assert vo.entry_id in panel._track_sync_links
    assert panel._track_calibration(vo).segments == panel._track_calibration(vf).segments == cuts.segments


@pytest.mark.parametrize("case", ["entered", "cancelled"])
def test_studio_shows_the_real_gap_between_tracks_of_a_file(films, monkeypatch, case):
    """Pistes d'une même source à décalages différents (saisie, annulation d'une seule) : écart réel."""
    panel, _master, vf, vo, _subtitle, _logs = films
    if case == "entered":
        vo.time_shift_ms = 500
        panel._track_table.time_shift_edited.emit(vo)
        expected = 500
    else:
        panel._apply_source_sync_offset("B", 1000)
        panel._track_table.update_time_shift(vo.entry_id, 0)
        expected = -1000
    before = _shifts(vf, vo)
    _studio(panel, monkeypatch, vo, vf, 0)
    assert _Studio.opened[0]["calibration"].segments[0].shift_ms == expected
    assert _shifts(vf, vo) == before
    _studio(panel, monkeypatch, vo, vf, -expected)
    assert vo.time_shift_ms == vf.time_shift_ms
