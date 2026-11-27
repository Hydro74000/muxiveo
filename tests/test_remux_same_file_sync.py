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


class _CutStudio(_Studio):
    """Synchro Studio validé avec une coupure (+200 ms puis +400 ms à 60 s)."""

    def result_calibration(self):
        changed = SyncCalibration((SyncSegment(0, 200), SyncSegment(60000, 400)))
        return changed, 200


def _cut_realign(panel, monkeypatch, target, reference):
    """Recalage à coupure de `target` face à `reference` (choisie dans la fenêtre de choix)."""
    monkeypatch.setattr(studio_module, "SyncStudioDialog", _CutStudio)
    monkeypatch.setattr("ui.panels.remux_panel.panel._AudioSyncReferenceDialog",
                        lambda choices, **_k: SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted,
                                                              selected_entry=lambda: reference))
    panel._on_sync_studio_requested(target)
    assert target.entry_id in panel._track_sync_links


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


@pytest.mark.parametrize("sync_mode", ["physical", "container"])
def test_reloaded_session_keeps_the_source_cuts(films, monkeypatch, sync_mode):
    """Coupures de la source 2 en mode container : job exact relu sans refus (point 2 de l'audit)."""
    panel, master, vf, vo, _subtitle, _logs = films
    panel._workflow_options["sync_mode"] = sync_mode
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


# --- Audit Codex du commit 904ffe4 : un test par point. ---


def _uncheck(panel, *entries):
    """Pistes décochées dans la table (état relu par la configuration)."""
    from PySide6.QtCore import Qt

    table = panel._track_table
    for entry in entries:
        table.item(table._find_row_for_entry(entry), table.COL_CHECK).setCheckState(Qt.CheckState.Unchecked)


def _prepared_shifts(panel, tmp_path):
    """Pistes transformées par la synchro physique : piste → décalage de départ."""
    from core.workflows.physical_sync import preparation_commands

    config = panel.collect_config()
    return {item.track.entry_id: calibration.segments[0].shift_ms
            for item, _path, _command, calibration in preparation_commands(config, tmp_path, "ffmpeg")}


@pytest.mark.parametrize("reference_enabled", [True, False])
def test_physical_mirror_ignores_a_realigned_audio_track(films, monkeypatch, tmp_path, reference_enabled):
    """Point 1 : VO recalée seule (+300) ; le sous-titre en miroir garde la synchro de la source."""
    panel, _master, vf, vo, subtitle, _logs = films
    panel._workflow_options["sync_mode"] = "physical"
    _studio(panel, monkeypatch, vo, vf, 300)
    if not reference_enabled:
        _uncheck(panel, vf)
    shifts = _prepared_shifts(panel, tmp_path)
    assert shifts[vo.entry_id] == 300
    assert shifts.get(subtitle.entry_id, 0) == 0


def test_physical_mirror_respects_a_subtitle_realigned_to_zero(films, monkeypatch, tmp_path):
    """Point 1 : sous-titre recalé à 0 ms sur une source à +1000 ms : jamais remplacé par le miroir."""
    panel, _master, _vf, _vo, subtitle, _logs = films
    panel._workflow_options["sync_mode"] = "physical"
    second_sub = _track(4, "subtitle", codec="SRT", file_id="B", language="eng")
    panel._track_table.append_tracks(_COLOR_A, [second_sub])
    panel._apply_source_sync_offset("B", 1000)
    _studio(panel, monkeypatch, subtitle, second_sub, -1000)
    assert subtitle.time_shift_ms == 0
    assert _prepared_shifts(panel, tmp_path).get(subtitle.entry_id, 0) == 0


def test_frozen_track_keeps_its_own_sync_after_reload(films, monkeypatch):
    """Point 3 : référence (variante) retirée, piste figée égale à la synchro de sa source : reste
    une synchro propre après relecture du job."""
    panel, master, vf, vo, _subtitle, _logs = films
    panel._workflow_options["sync_mode"] = "physical"
    cuts = SyncCalibration((SyncSegment(0, -100), SyncSegment(60000, -300)))
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, -100, .95, cuts)
    panel.add_audio_track_variant(vf, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    _studio(panel, monkeypatch, vo, variant, 200)
    _studio(panel, monkeypatch, vo, variant, -200)
    panel.remove_audio_track_variant("variant")
    assert vo.entry_id in panel._own_sync_entry_ids and vo.entry_id not in panel._track_sync_links
    _vf, vo, _sub = _reloaded(panel, monkeypatch)
    assert vo.entry_id in panel._own_sync_entry_ids
    assert panel._track_calibration(vo).segments == cuts.segments


def test_profile_offsets_on_a_reference_and_an_own_track(films, monkeypatch):
    """Point 4 : B suit A (+100) ; un profil règle la référence de A (+500) et VO de B (+800)."""
    panel, master, vf, vo, subtitle, _logs = films
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 100, .95, None)
    _studio(panel, monkeypatch, vo, vf, 200)
    master.time_shift_ms, vo.time_shift_ms = 500, 800
    panel._take_entered_offsets([master, vo])
    assert _shifts(master, vf, vo, subtitle) == [500, 600, 800, 600]


def test_independent_own_track_is_not_a_loop(films, monkeypatch):
    """Point 5 : VO rendue indépendante (saisie) ; A synchronisée sur VO ne retire pas la synchro de B."""
    panel, master, vf, vo, subtitle, _logs = films
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 100, .95, None)
    _studio(panel, monkeypatch, vo, vf, 200)
    vo.time_shift_ms = 700
    panel._track_table.time_shift_edited.emit(vo)
    assert not panel._track_depends_on(vo, "A")
    panel._on_audio_sync_done(master.entry_id, vo.entry_id, 50, .95, None)
    # Aucune boucle : A suit VO (+50), B garde son lien et suit A (+100) ; VO reste à sa saisie.
    assert "B" in panel._sync_links
    assert _shifts(master, vf, vo, subtitle) == [750, 850, 700, 850]
    assert panel._track_depends_on(vf, "A")


def test_removed_own_reference_drops_the_source_link(films, monkeypatch):
    """Point 6 : A suit une variante de B recalée (+300) ; variante retirée → A garde sa synchro."""
    panel, master, vf, _vo, _subtitle, logs = films
    panel.add_audio_track_variant(vf, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    _studio(panel, monkeypatch, variant, vf, 300)
    panel._on_audio_sync_done(master.entry_id, "variant", 0, .95, None)
    assert master.time_shift_ms == 300
    panel.remove_audio_track_variant("variant")
    assert "A" not in panel._sync_links
    panel._apply_source_sync_offset("B", 50)
    panel._propagate_sync({"B"})
    assert master.time_shift_ms == 300
    assert any("ne suit plus sa référence" in text or "no longer follows" in text for _level, text in logs)


def test_variant_of_a_realigned_track_follows_like_it(films, monkeypatch):
    """Point 7 : variante d'une piste recalée : même synchro propre et même lien, avant et après reprise."""
    panel, _master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 300)
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    assert variant.entry_id in panel._own_sync_entry_ids and variant.entry_id in panel._track_sync_links
    panel._apply_source_sync_offset("B", 100)
    panel._propagate_sync({"B"})
    assert _shifts(vf, vo, variant) == [100, 400, 400]
    vf, vo, _sub = _reloaded(panel, monkeypatch)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    panel._apply_source_sync_offset(vf.file_id, 0)
    panel._propagate_sync({vf.file_id})
    assert _shifts(vf, vo, variant) == [0, 300, 300]


def test_last_chosen_reference_is_preselected(films, monkeypatch):
    """Point 8 : référence choisie puis studio validé sans changement → présélectionnée ensuite ;
    même chose pour la synchro automatique des sous-titres."""
    panel, master, vf, vo, subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 0)
    assert panel._track_sync_links == {}
    offered = _studio(panel, monkeypatch, vo, master, 0)
    assert offered == [([master, vf], vf)]

    first_sub = _track(2, "subtitle", codec="SRT", file_id="A")
    second_sub = _track(4, "subtitle", codec="SRT", file_id="A", language="eng")
    panel._track_table.append_tracks(_COLOR_A, [first_sub, second_sub])
    monkeypatch.setattr(panel._executor, "submit", lambda *_a, **_k: None)
    selected: list = []

    def _dialog(choices, **kwargs):
        selected.append(kwargs.get("selected"))
        return SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted, selected_entry=lambda: second_sub)

    monkeypatch.setattr("ui.panels.remux_panel.panel._AudioSyncReferenceDialog", _dialog)
    panel._on_subtitle_sync_requested(subtitle)
    panel._on_subtitle_sync_requested(subtitle)
    assert selected == [None, second_sub]


# --- Seconde relecture Codex (commit 605585d). ---


def _independent(panel, monkeypatch, track, reference, offset_ms):
    """Piste rendue indépendante : recalée sur `reference`, puis décalage saisi à la main."""
    _studio(panel, monkeypatch, track, reference, 1)
    track.time_shift_ms = offset_ms
    panel._track_table.time_shift_edited.emit(track)
    assert track.entry_id in panel._own_sync_entry_ids and track.entry_id not in panel._track_sync_links


def test_physical_mirror_without_selected_audio(films, tmp_path):
    """Audios de B décochés (+300 et 0 saisis) : le SRT seul sort, sans miroir ambigu."""
    panel, _master, vf, vo, subtitle, _logs = films
    panel._workflow_options["sync_mode"] = "physical"
    vf.time_shift_ms = 300
    _uncheck(panel, vf, vo)
    assert _prepared_shifts(panel, tmp_path).get(subtitle.entry_id, 0) == 0


def test_frozen_track_on_an_imported_linear_source_stays_explicit(films, monkeypatch):
    """Piste figée à la calibration linéaire importée de B ; après reprise, les coupures données
    ensuite à B ne la touchent pas."""
    from core.workflows.remux_mapping import track_sync_payload

    panel, _master, vf, vo, _subtitle, _logs = films
    panel._workflow_options["sync_calibrations"] = {"1": SyncCalibration.linear(-1000).to_dict()}
    panel._sync_entry_calibrations()
    panel.add_audio_track_variant(vf, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    _studio(panel, monkeypatch, vo, variant, 200)
    _studio(panel, monkeypatch, vo, variant, -200)
    panel.remove_audio_track_variant("variant")
    _vf, vo, _sub = _reloaded(panel, monkeypatch)
    assert vo.entry_id in panel._own_sync_entry_ids and vo.sync_calibration is not None
    cuts = SyncCalibration((SyncSegment(0, -1000), SyncSegment(60000, -1300)))
    panel._set_source_calibration(vo.file_id, cuts)
    config = panel.collect_config()
    assert track_sync_payload(config, vo, 1) == SyncCalibration.linear(-1000).to_dict()


def test_profile_moving_an_independent_reference_and_an_own_track(films, monkeypatch):
    """B suit a2, piste indépendante de A (+300, B +100) ; un profil règle a2 (+500) et VO (+800)."""
    panel, master, vf, vo, subtitle, _logs = films
    a2 = _track(2, "audio", file_id="A", language="eng")
    panel._track_table.append_tracks(_COLOR_A, [a2])
    _independent(panel, monkeypatch, a2, master, 300)
    panel._on_audio_sync_done(vf.entry_id, a2.entry_id, 100, .95, None)
    assert _shifts(vf, subtitle) == [400, 400]
    _independent(panel, monkeypatch, vo, vf, 600)
    a2.time_shift_ms, vo.time_shift_ms = 500, 800
    panel._take_entered_offsets([a2, vo])
    assert _shifts(a2, vf, vo, subtitle) == [500, 600, 800, 600]


def _reciprocal(panel, monkeypatch, master, vf, vo):
    """B suit le master de A (+100) ; A suit VO, piste indépendante de B (+50)."""
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 100, .95, None)
    _independent(panel, monkeypatch, vo, vf, 700)
    panel._on_audio_sync_done(master.entry_id, vo.entry_id, 50, .95, None)


def test_reciprocal_links_follow_an_edited_independent_track(films, monkeypatch):
    panel, master, vf, vo, subtitle, _logs = films
    _reciprocal(panel, monkeypatch, master, vf, vo)
    assert _shifts(master, vf, vo, subtitle) == [750, 850, 700, 850]
    vo.time_shift_ms = 900
    panel._track_table.time_shift_edited.emit(vo)
    assert _shifts(master, vf, vo, subtitle) == [950, 1050, 900, 1050]


def test_rejoining_its_source_breaks_the_loop(films, monkeypatch):
    """VO rejoint B qui suit A, lui-même lié à VO : le lien de A est abandonné (boucle)."""
    panel, master, vf, vo, _subtitle, logs = films
    _reciprocal(panel, monkeypatch, master, vf, vo)
    panel._on_auto_sync_cancel_requested(vo)
    assert "A" not in panel._sync_links and "B" in panel._sync_links
    assert _shifts(master, vf, vo) == [750, 850, 850]
    assert any("(boucle)" in text or "(loop)" in text for _level, text in logs)


def test_track_realigned_on_an_independent_track_keeps_the_other_source(films, monkeypatch):
    """B suit a2 (piste indépendante de A) ; a2 recalée seule sur VO, indépendante de B : pas de boucle."""
    panel, master, vf, vo, subtitle, _logs = films
    a2 = _track(2, "audio", file_id="A", language="eng")
    panel._track_table.append_tracks(_COLOR_A, [a2])
    _independent(panel, monkeypatch, a2, master, 300)
    panel._on_audio_sync_done(vf.entry_id, a2.entry_id, 100, .95, None)
    _independent(panel, monkeypatch, vo, vf, 600)
    _studio(panel, monkeypatch, a2, vo, 10)
    assert "B" in panel._sync_links
    assert _shifts(a2, vf, vo, subtitle) == [310, 410, 600, 410]


def test_preselection_follows_every_selector_and_the_session(films, monkeypatch):
    panel, master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 0)
    monkeypatch.setattr(panel._executor, "submit", lambda *_a, **_k: None)
    monkeypatch.setattr("ui.panels.remux_panel.panel._AudioSyncReferenceDialog",
                        lambda choices, **_k: SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted,
                                                              selected_entry=lambda: master))
    panel._on_audio_sync_requested(vo)
    offered = _studio(panel, monkeypatch, vo, master, 0)
    assert offered == [([master, vf], master)]
    _studio(panel, monkeypatch, vo, vf, 0)
    _vf, vo, _sub = _reloaded(panel, monkeypatch)
    offered = _studio(panel, monkeypatch, vo, _vf, 0)
    assert offered[0][1] is _vf


# --- Troisième relecture Codex (commit 3bb6157). ---


def test_realigning_in_its_file_cannot_close_a_loop(films, monkeypatch):
    """B suit le master de A, A suit VO (indépendante) : recaler VO sur VF (B) fermerait la boucle ;
    le lien de A est abandonné, VO prend la place réglée."""
    panel, master, vf, vo, _subtitle, logs = films
    _reciprocal(panel, monkeypatch, master, vf, vo)
    _studio(panel, monkeypatch, vo, vf, 10)
    assert _Studio.opened[0]["calibration"] == SyncCalibration.linear(-150)
    assert "A" not in panel._sync_links and "B" in panel._sync_links
    assert _shifts(master, vf, vo) == [750, 850, 710]
    assert any("(boucle)" in text or "(loop)" in text for _level, text in logs)


def test_inverted_realignment_moves_the_sources_linked_to_the_released_track(films, monkeypatch):
    """VO suit VF (+300), A suit VO (+100) ; VF recalée sur VO libère VO (0) : A suit (+100)."""
    panel, master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 300)
    panel._on_audio_sync_done(master.entry_id, vo.entry_id, 100, .95, None)
    assert master.time_shift_ms == 400
    _studio(panel, monkeypatch, vf, vo, 50)
    assert vo.entry_id not in panel._track_sync_links
    assert _shifts(master, vf, vo) == [100, -250, 0]


def test_new_reference_choice_triggers_the_autosave(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    panel._autosave_dirty = False
    _studio(panel, monkeypatch, vo, vf, 0)
    assert panel._autosave_dirty is True
    panel._autosave_dirty = False
    _studio(panel, monkeypatch, vo, vf, 0)
    assert panel._autosave_dirty is False


# --- Quatrième relecture Codex (commit eb4eeff). ---


def test_indirect_loop_breaks_the_link_closest_to_the_modified_track(films, monkeypatch):
    """B suit A ; le commentaire de B suit VO (indépendante) ; A suit le commentaire. Recaler VO sur
    VF ferme la boucle : le lien de A (le plus proche de VO) est abandonné, B suit toujours A."""
    panel, master, vf, vo, _subtitle, _logs = films
    comm = _track(5, "audio", file_id="B", language="eng", title="Commentaire")
    panel._track_table.append_tracks(_COLOR_A, [comm])
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 100, .95, None)
    _independent(panel, monkeypatch, vo, vf, 700)
    _studio(panel, monkeypatch, comm, vo, 50)
    assert comm.time_shift_ms == 150 and panel._track_sync_links[comm.entry_id].reference_entry_id == vo.entry_id
    panel._on_audio_sync_done(master.entry_id, comm.entry_id, 50, .95, None)
    # A suit le commentaire (150 + 50) ; B suit A (+100).
    assert _shifts(master, vf, vo, comm) == [200, 300, 700, 150]
    _studio(panel, monkeypatch, vo, vf, 10)
    assert "A" not in panel._sync_links and "B" in panel._sync_links
    assert _shifts(master, vf, vo, comm) == [200, 300, 710, 160]


def test_removed_reference_is_followed_through_its_source(films, monkeypatch, tmp_path):
    """A suit c2 (indépendante, C) ; B suit une variante de A ; C suit VF. Variante retirée puis VO
    recalée sur VF : la chaîne passe par c2, indépendante, aucune boucle (C garde son lien)."""
    panel, master, vf, vo, _subtitle, _logs = films
    c1, c2 = _track(1, "audio", file_id="C"), _track(2, "audio", file_id="C", language="eng")
    _add_source(panel, tmp_path, "C", [c1, c2])
    panel.add_audio_track_variant(master, entry_id="variant", codec="aac", bitrate_kbps=192)
    _independent(panel, monkeypatch, c2, c1, 300)
    panel._on_audio_sync_done(master.entry_id, c2.entry_id, 50, .95, None)
    panel._on_audio_sync_done(vf.entry_id, "variant", 100, .95, None)
    panel._on_audio_sync_done(c1.entry_id, vf.entry_id, 20, .95, None)
    assert _shifts(master, vf, c1, c2) == [350, 450, 470, 300]
    panel.remove_audio_track_variant("variant")
    _studio(panel, monkeypatch, vo, vf, 10)
    assert set(panel._sync_links) == {"A", "B", "C"}
    assert _shifts(master, vf, c1, c2) == [350, 450, 470, 300]


def test_realignment_back_to_zero_stays_cancellable(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 300)
    _studio(panel, monkeypatch, vo, vf, -300)
    assert vo.time_shift_ms == 0 and vo.entry_id in panel._track_sync_links
    assert panel._track_table._has_cancelable_auto_sync(vo)
    panel._on_auto_sync_cancel_requested(vo)
    assert vo.entry_id not in panel._track_sync_links
    assert not panel._track_table._has_cancelable_auto_sync(vo)


# --- Cinquième relecture Codex (commit caa50cf). ---


def test_physical_remux_leaves_a_track_realigned_back_to_zero_untouched(films, monkeypatch, tmp_path):
    """Calibration explicite sans effet (0 ms) : pas de préparation, horodatages d'origine conservés."""
    panel, _master, vf, vo, _subtitle, _logs = films
    panel._workflow_options["sync_mode"] = "physical"
    _studio(panel, monkeypatch, vo, vf, 300)
    assert _prepared_shifts(panel, tmp_path)[vo.entry_id] == 300
    _studio(panel, monkeypatch, vo, vf, -300)
    assert vo.sync_calibration is not None and vo.entry_id in panel._track_sync_links
    assert vo.entry_id not in _prepared_shifts(panel, tmp_path)


def test_global_cancel_stays_offered_for_a_link_at_zero(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 300)
    _studio(panel, monkeypatch, vo, vf, -300)
    assert not panel._global_sync_cancel_btn.isHidden()
    panel._on_global_sync_cancel_requested()
    assert panel._track_sync_links == {} and panel._global_sync_cancel_btn.isHidden()


# --- Sixième relecture Codex (commit f97b457). ---


def test_profile_variant_keeps_the_offset_written_by_the_profile(films, monkeypatch):
    """VO recalée (+300) ; un profil la règle à +120 et recrée sa variante à +120 : les deux à +120."""
    import copy

    from core.workflows.remux_models import clone_track_entry

    panel, _master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 300)
    tracks = [copy.deepcopy(track) for track in panel._track_table.current_tracks()]
    new_vo = next(track for track in tracks if track.entry_id == vo.entry_id)
    variant = clone_track_entry(new_vo, entry_id="variant")
    new_vo.time_shift_ms = variant.time_shift_ms = 120
    tracks.append(variant)
    panel._commit_decision_profile_result(tracks, {"written_fields": {
        vo.entry_id: ("time_shift_ms",), "variant": ("time_shift_ms",),
    }})
    assert (new_vo.time_shift_ms, variant.time_shift_ms) == (120, 120)
    assert variant.entry_id in panel._own_sync_entry_ids and variant.entry_id not in panel._track_sync_links
    panel._apply_source_sync_offset(vf.file_id, 50)
    panel._propagate_sync({vf.file_id})
    assert (new_vo.time_shift_ms, variant.time_shift_ms) == (120, 120)


def test_global_cancel_offered_while_a_link_between_files_exists(films, monkeypatch):
    """A suit VO (recalée sur VF) ; VO ramenée à 0 puis annulée : A suit encore VO à 0 ms."""
    panel, master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 300)
    panel._on_audio_sync_done(master.entry_id, vo.entry_id, 0, .95, None)
    assert master.time_shift_ms == 300
    _studio(panel, monkeypatch, vo, vf, -300)
    panel._on_auto_sync_cancel_requested(vo)
    assert _shifts(master, vf, vo) == [0, 0, 0] and "A" in panel._sync_links
    assert not panel._global_sync_cancel_btn.isHidden()
    panel._on_global_sync_cancel_requested()
    assert panel._sync_links == {} and panel._global_sync_cancel_btn.isHidden()


def test_removed_variant_with_its_own_offset_freezes_its_dependents(films, monkeypatch):
    """Variante de B décalée seule (+300) ; A calée dessus (+100). Variante retirée : A reste à +400,
    même quand VF bouge ensuite."""
    panel, master, vf, _vo, _subtitle, _logs = films
    panel.add_audio_track_variant(vf, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    variant.time_shift_ms = 300
    panel._track_table.time_shift_edited.emit(variant)
    panel._on_audio_sync_done(master.entry_id, "variant", 100, .95, None)
    assert master.time_shift_ms == 400
    panel.remove_audio_track_variant("variant")
    assert "A" not in panel._sync_links
    vf.time_shift_ms = 50
    panel._track_table.time_shift_edited.emit(vf)
    assert master.time_shift_ms == 400


# --- Septième relecture Codex (commit 7f92b36). ---


def test_profile_replays_the_own_offset_of_a_variant(qt_app, monkeypatch, tmp_path):
    """VO recalée (+300), sa variante AAC décalée seule (+120) ; profil capturé, variante retirée,
    profil réappliqué : la variante recréée sort à +120."""
    from core.profiles.decision import remux_config_to_decision_profile

    panel = RemuxPanel(AppConfig())
    panel._output_edit.setText(str(tmp_path / "output.mkv"))
    vf = _track(1, "audio", file_id="B", title="VF")
    vo = _track(2, "audio", file_id="B", title="VO", language="eng", orig_language="eng", orig_title="VO")
    _add_source(panel, tmp_path, "B", [vf, vo])
    _studio(panel, monkeypatch, vo, vf, 300)
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    variant.time_shift_ms = 120
    panel._track_table.time_shift_edited.emit(variant)
    profile = remux_config_to_decision_profile(panel.collect_config())
    variant_actions = [action for rule in profile["rules"] for action in rule["actions"]
                       if action["type"] == "create_audio_variant"]
    assert [action.get("time_shift_ms") for action in variant_actions] == [120]
    panel.remove_audio_track_variant("variant")
    panel._apply_decision_profile(profile)
    tracks = panel._track_table.current_tracks()
    recreated = next(track for track in tracks if track.is_new)
    vo = next(track for track in tracks if track.entry_id == vo.entry_id)
    assert (vo.time_shift_ms, recreated.time_shift_ms) == (300, 120)
    mapped = {item.track.entry_id: item.track.time_shift_ms for item in resolve_mapped_tracks(panel.collect_config())}
    assert mapped[recreated.entry_id] == 120
    panel.close()


def test_physical_validation_accepts_an_aac_variant_of_a_dts_track(films, monkeypatch):
    """Variante AAC d'une VO DTS recalée avec une coupure, VO décochée : la préparation encode l'AAC,
    la validation l'accepte."""
    from core.workflows.remux import RemuxWorkflow

    panel, _master, vf, _vo, _subtitle, _logs = films
    panel._workflow_options["sync_mode"] = "physical"
    dts = TrackEntry(5, "audio", "DTS", "5.1  1509 kbps", "eng", "VO DTS", file_id="B")
    panel._track_table.append_tracks(_COLOR_A, [dts])
    panel._find_source("B").tracks.append(dts)
    panel.add_audio_track_variant(dts, entry_id="variant", codec="aac", bitrate_kbps=384)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    _cut_realign(panel, monkeypatch, variant, vf)
    _uncheck(panel, dts)
    errors = RemuxWorkflow().validate(panel.collect_config())
    assert not any("incompatible" in error for error in errors), errors


# --- Huitième relecture Codex (commit 23d4ab7). ---


def test_profile_variant_offset_on_a_source_with_cuts(qt_app, monkeypatch, tmp_path):
    """B à coupures (+300) ; variante AAC de VO recalée sur VF (+120) ; profil capturé, variante
    retirée, profil réappliqué : la variante sort à +120 (synchro propre), pas à la synchro de B."""
    from core.profiles.decision import remux_config_to_decision_profile
    from core.workflows.remux_mapping import track_sync_payload

    panel = RemuxPanel(AppConfig())
    panel._output_edit.setText(str(tmp_path / "output.mkv"))
    master = _track(1, "audio", file_id="A", title="Master", language="ger", orig_language="ger", orig_title="Master")
    vf = _track(1, "audio", file_id="B", title="VF")
    vo = _track(2, "audio", file_id="B", title="VO", language="eng", orig_language="eng", orig_title="VO")
    _add_source(panel, tmp_path, "A", [master])
    _add_source(panel, tmp_path, "B", [vf, vo])
    cuts = SyncCalibration((SyncSegment(0, 300), SyncSegment(60000, 500)))
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 300, .95, cuts)
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    _studio(panel, monkeypatch, variant, vf, 120)
    assert variant.time_shift_ms == 420
    profile = remux_config_to_decision_profile(panel.collect_config())
    panel.remove_audio_track_variant("variant")
    panel._apply_decision_profile(profile)
    recreated = next(track for track in panel._track_table.current_tracks() if track.is_new)
    assert recreated.time_shift_ms == 420 and recreated.entry_id in panel._own_sync_entry_ids
    payload = track_sync_payload(panel.collect_config(), recreated, 1)
    assert SyncCalibration.from_dict(payload).segments[0].shift_ms == 420
    panel.close()


def test_offset_toggle_never_hides_cuts(films, monkeypatch, tmp_path):
    """Calibration à coupures : bascule « Sync offset » indisponible ; un mode offset restauré
    n'empêche pas la préparation physique."""
    from core.workflows.common.sync_rewrite import SYNC_REWRITE_MODE_OFFSET

    panel, _master, vf, vo, _subtitle, _logs = films
    panel._workflow_options["sync_mode"] = "physical"
    _cut_realign(panel, monkeypatch, vo, vf)
    assert not panel._track_table._can_toggle_sync_rewrite(vo)
    vo.sync_rewrite_mode = SYNC_REWRITE_MODE_OFFSET
    assert _prepared_shifts(panel, tmp_path)[vo.entry_id] == 200


def test_container_validation_checks_the_variant_target_codec(films, monkeypatch):
    """Mode container : variante FLAC d'une piste E-AC-3 recalée avec une coupure, seule en sortie :
    refusée dès la validation (la réécriture container ne produit pas de FLAC)."""
    from core.workflows.remux import RemuxWorkflow

    panel, _master, vf, vo, _subtitle, _logs = films
    panel.add_audio_track_variant(vo, entry_id="variant", codec="flac")
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    _cut_realign(panel, monkeypatch, variant, vf)
    _uncheck(panel, vo)
    errors = RemuxWorkflow().validate(panel.collect_config())
    assert any("incompatible" in error and "flac" in error for error in errors), errors


# --- Neuvième relecture Codex (commit bcdcef9). ---


def _distinct_panel(tmp_path, *, with_subtitle=False):
    """A (master allemand) ; B : VF, VO (pistes distinguables par un profil), sous-titres en option."""
    panel = RemuxPanel(AppConfig())
    panel._output_edit.setText(str(tmp_path / "output.mkv"))
    master = _track(1, "audio", file_id="A", title="Master", language="ger", orig_language="ger", orig_title="Master")
    vf = _track(1, "audio", file_id="B", title="VF")
    vo = _track(2, "audio", file_id="B", title="VO", language="eng", orig_language="eng", orig_title="VO")
    b_tracks = [vf, vo]
    subtitle = None
    if with_subtitle:
        subtitle = _track(3, "subtitle", codec="SRT", file_id="B", title="VF")
        b_tracks.append(subtitle)
    _add_source(panel, tmp_path, "A", [master])
    _add_source(panel, tmp_path, "B", b_tracks)
    return panel, master, vf, vo, subtitle


def test_profile_offset_applies_to_a_reused_variant_on_a_source_with_cuts(qt_app, monkeypatch, tmp_path):
    """Profil capturé avec une variante à +420 ; variante recréée à la main (suit B, +300), profil
    réappliqué : la variante reprise sort à +420."""
    from core.profiles.decision import remux_config_to_decision_profile
    from core.workflows.remux_mapping import track_sync_payload

    panel, master, vf, vo, _sub = _distinct_panel(tmp_path)
    cuts = SyncCalibration((SyncSegment(0, 300), SyncSegment(60000, 500)))
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 300, .95, cuts)
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    _studio(panel, monkeypatch, variant, vf, 120)
    profile = remux_config_to_decision_profile(panel.collect_config())
    panel.remove_audio_track_variant("variant")
    panel.add_audio_track_variant(vo, entry_id="manual", codec="aac", bitrate_kbps=192)
    panel._apply_decision_profile(profile)
    reused = next(track for track in panel._track_table.current_tracks() if track.entry_id == "manual")
    assert reused.time_shift_ms == 420 and reused.entry_id in panel._own_sync_entry_ids
    payload = track_sync_payload(panel.collect_config(), reused, 1)
    assert SyncCalibration.from_dict(payload).segments == cuts.with_start_offset(420).segments
    panel.close()


def test_profile_constant_variant_offset_is_not_a_mirror_ambiguity(qt_app, tmp_path):
    """Source sans coupure : variante à +120 recréée par un profil, VO et sous-titres gardés :
    le miroir physique des sous-titres ignore la variante (synchro propre)."""
    from core.profiles.decision import remux_config_to_decision_profile

    panel, _master, _vf, vo, subtitle = _distinct_panel(tmp_path, with_subtitle=True)
    panel._workflow_options["sync_mode"] = "physical"
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    variant.time_shift_ms = 120
    panel._track_table.time_shift_edited.emit(variant)
    profile = remux_config_to_decision_profile(panel.collect_config())
    panel.remove_audio_track_variant("variant")
    panel._apply_decision_profile(profile)
    recreated = next(track for track in panel._track_table.current_tracks() if track.is_new)
    shifts = _prepared_shifts(panel, tmp_path)
    assert shifts[recreated.entry_id] == 120 and shifts.get(subtitle.entry_id, 0) == 0
    panel.close()


def _pal_auto(offset_ms: float = 300.0) -> SyncCalibration:
    """Synchro à conversion de cadence PAL → cinéma, méthode « auto » non tranchée."""
    from core.workflows.cadence import detect_cadence_from_metadata

    return SyncCalibration((SyncSegment(0, offset_ms),),
                           cadence_mismatch=detect_cadence_from_metadata("24000/1001", "25/1"),
                           cadence_audio_method="auto")


def _capture_pitch_analyses(panel, monkeypatch):
    """Analyses de hauteur mémorisées (lancées par le test) ; méthode retenue : asetrate.
    Retourne (tâches, pistes analysées)."""
    from dataclasses import replace

    from core.workflows.audio_sync_scan import AudioSyncScanner

    submitted: list = []
    targets: list = []
    monkeypatch.setattr(panel._executor, "submit", lambda fn, *args: submitted.append(fn))

    def resolve(_self, _reference, target, calibration, log=None):
        targets.append(target.stream_index)
        return replace(calibration, cadence_audio_method="asetrate", cadence_method_auto=True)

    monkeypatch.setattr(AudioSyncScanner, "resolve_cadence_method", resolve)
    return submitted, targets


def test_auto_cadence_of_a_realignment_in_its_file_is_analysed(films, monkeypatch):
    """Recalage dans le fichier avec conversion de cadence « auto » non tranchée : analyse de hauteur
    planifiée sur la piste, résultat porté par son lien ; reprise de session : analyse relancée."""
    from core.workflows.cadence import detect_cadence_from_metadata

    panel, _master, vf, vo, _subtitle, _logs = films
    pal = detect_cadence_from_metadata("24000/1001", "25/1")

    class _CadenceStudio(_Studio):
        def result_calibration(self):
            calibration = SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal,
                                          cadence_audio_method="auto")
            return calibration, 300

    submitted: list = []
    monkeypatch.setattr(panel._executor, "submit", lambda fn, *args: submitted.append(fn))
    from core.workflows.audio_sync_scan import AudioSyncScanner
    targets: list = []

    def resolve(_self, _reference, target, calibration, log=None):
        from dataclasses import replace
        targets.append(target.stream_index)
        return replace(calibration, cadence_audio_method="asetrate", cadence_method_auto=True)

    monkeypatch.setattr(AudioSyncScanner, "resolve_cadence_method", resolve)
    monkeypatch.setattr(studio_module, "SyncStudioDialog", _CadenceStudio)
    monkeypatch.setattr("ui.panels.remux_panel.panel._AudioSyncReferenceDialog",
                        lambda choices, **_k: SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted,
                                                              selected_entry=lambda: vf))
    panel._on_sync_studio_requested(vo)
    assert panel.cadence_resolution_pending() and len(submitted) == 1
    from PySide6.QtCore import QCoreApplication
    submitted.pop()()
    QCoreApplication.processEvents()
    assert targets == [vo.mkv_tid]
    assert panel._track_sync_links[vo.entry_id].relative.cadence_audio_method == "asetrate"
    assert SyncCalibration.from_dict(vo.sync_calibration).cadence_audio_method == "asetrate"
    assert not panel.cadence_resolution_pending()
    # Lien restauré encore « auto » : reprise de l'analyse.
    from dataclasses import replace
    link = panel._track_sync_links[vo.entry_id]
    panel._track_sync_links[vo.entry_id] = replace(link, relative=replace(link.relative, cadence_audio_method="auto"))
    panel._resume_cadence_analyses()
    assert len(submitted) == 1


def test_physical_variant_of_an_atmos_track_is_not_refused_for_its_title(films, monkeypatch, tmp_path):
    """Variante AAC de « VO Dolby Atmos » recalée avec une coupure, original décoché : conversion
    demandée, titre hérité sans effet (préparation et validation)."""
    from core.workflows.remux import RemuxWorkflow

    panel, _master, vf, _vo, _subtitle, _logs = films
    panel._workflow_options["sync_mode"] = "physical"
    atmos = TrackEntry(5, "audio", "E-AC-3", "7.1  768 kbps", "eng", "VO Dolby Atmos", file_id="B")
    panel._track_table.append_tracks(_COLOR_A, [atmos])
    panel._find_source("B").tracks.append(atmos)
    panel.add_audio_track_variant(atmos, entry_id="variant", codec="aac", bitrate_kbps=384)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    assert "Atmos" in variant.title
    _cut_realign(panel, monkeypatch, variant, vf)
    _uncheck(panel, atmos)
    assert _prepared_shifts(panel, tmp_path)[variant.entry_id] == 200
    errors = RemuxWorkflow().validate(panel.collect_config())
    assert not any("incompatible" in error for error in errors), errors


# --- Dixième relecture Codex (commit 8d7c5dd). ---


def test_profile_keeps_the_constant_own_sync_of_an_independent_variant(qt_app, monkeypatch, tmp_path):
    """Variante figée à +120 (constante) ; B reçoit ensuite des coupures ; profil rejoué sur la
    variante existante : elle reste à +120 constants, sans les coupures de B."""
    from core.profiles.decision import remux_config_to_decision_profile
    from core.workflows.remux_mapping import track_sync_payload

    panel, master, vf, vo, _sub = _distinct_panel(tmp_path)
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    _independent(panel, monkeypatch, variant, vf, 120)
    profile = remux_config_to_decision_profile(panel.collect_config())
    cuts = SyncCalibration((SyncSegment(0, 0), SyncSegment(60000, 200)))
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 0, .95, cuts)
    panel._apply_decision_profile(profile)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    payload = track_sync_payload(panel.collect_config(), variant, 1)
    assert SyncCalibration.from_dict(payload).segments == SyncCalibration.linear(120).segments
    panel.close()


def test_resumed_source_cadence_is_analysed_on_a_track_following_the_source(films, monkeypatch):
    """VF recalée seule sur VO ; B synchronisée face à A avec cadence « auto » ; reprise : analyse
    sur VO (qui suit B), résultat porté par le lien de B."""
    panel, master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vf, vo, 200)
    submitted, targets = _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done(vo.entry_id, master.entry_id, 300, .95, _pal_auto())
    submitted.clear()
    targets.clear()
    panel._cadence_pending.clear()
    panel._resume_cadence_analyses()
    assert len(submitted) == 1
    from PySide6.QtCore import QCoreApplication
    submitted.pop()()
    QCoreApplication.processEvents()
    assert targets == [vo.mkv_tid]
    assert panel._sync_links["B"].relative.cadence_audio_method == "asetrate"
    assert not panel.cadence_resolution_pending()


def test_physical_eac3_reencode_of_an_atmos_track(films, monkeypatch, tmp_path):
    """Variante E-AC-3 384 kb/s d'une E-AC-3 Atmos 768 kb/s, recalée avec une coupure, original
    décoché : réencodage demandé, accepté (préparation et validation)."""
    from core.workflows.remux import RemuxWorkflow

    panel, _master, vf, _vo, _subtitle, _logs = films
    panel._workflow_options["sync_mode"] = "physical"
    atmos = TrackEntry(5, "audio", "E-AC-3", "7.1  768 kbps", "eng", "VO Dolby Atmos", file_id="B")
    panel._track_table.append_tracks(_COLOR_A, [atmos])
    panel._find_source("B").tracks.append(atmos)
    panel.add_audio_track_variant(atmos, entry_id="variant", codec="eac3", bitrate_kbps=384)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    _cut_realign(panel, monkeypatch, variant, vf)
    _uncheck(panel, atmos)
    assert _prepared_shifts(panel, tmp_path)[variant.entry_id] == 200
    errors = RemuxWorkflow().validate(panel.collect_config())
    assert not any("incompatible" in error for error in errors), errors


def test_cancelled_realignment_no_longer_holds_the_media_operation(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films

    class _CadenceStudio(_Studio):
        def result_calibration(self):
            return _pal_auto(), 300

    _capture_pitch_analyses(panel, monkeypatch)
    ready: list = []
    panel.ready_changed.connect(ready.append)
    monkeypatch.setattr(studio_module, "SyncStudioDialog", _CadenceStudio)
    monkeypatch.setattr("ui.panels.remux_panel.panel._AudioSyncReferenceDialog",
                        lambda choices, **_k: SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted,
                                                              selected_entry=lambda: vf))
    panel._on_sync_studio_requested(vo)
    assert panel.cadence_resolution_pending()
    ready.clear()
    panel._on_auto_sync_cancel_requested(vo)
    assert not panel.cadence_resolution_pending() and ready


# --- Choix manuel de la conversion de cadence (référence du même fichier). ---


def test_studio_offers_manual_cadence_only_for_a_reference_of_the_same_file(films, monkeypatch):
    from core.workflows.cadence import manual_cadence_choices

    panel, master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 0)
    choices = _Studio.opened[0]["cadence_choices"]
    assert choices == manual_cadence_choices() and choices[0].description.startswith("PAL 25 -> 23.976")
    _studio(panel, monkeypatch, vo, master, 0)
    assert _Studio.opened[0]["cadence_choices"] == ()


def test_studio_manual_cadence_choice_drives_the_calibration(qt_app, tmp_path, monkeypatch):
    from core.workflows.cadence import manual_cadence_choices

    monkeypatch.setattr(studio_module.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_resolve_cadence_method", lambda *_a, **_k: None)
    vf = TrackEntry(1, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="B")
    vo = TrackEntry(2, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="B")
    choices = manual_cadence_choices()
    dialog = studio_module.SyncStudioDialog(
        target_entry=vf, target_source_path=tmp_path / "b.mkv", target_stream_index=1,
        reference_entry=vo, reference_source_path=tmp_path / "b.mkv", reference_stream_index=2,
        cadence_choices=choices,
    )
    assert not dialog.cadence_check.isChecked() and dialog.current_calibration.cadence_mismatch is None
    dialog.cadence_check.setChecked(True)
    assert dialog.current_calibration.cadence_mismatch == choices[0]
    dialog.cadence_combo.setCurrentIndex(2)
    assert dialog.current_calibration.cadence_mismatch == choices[2]
    calibration, _offset = dialog.result_calibration()
    dialog.close()
    # Réouverture avec cette conversion : liste conservée, positionnée sur le choix.
    reopened = studio_module.SyncStudioDialog(
        target_entry=vf, target_source_path=tmp_path / "b.mkv", target_stream_index=1,
        reference_entry=vo, reference_source_path=tmp_path / "b.mkv", reference_stream_index=2,
        calibration=calibration, cadence_choices=choices,
    )
    assert reopened.cadence_check.isChecked() and reopened.cadence_combo.currentIndex() == 2
    reopened.close()


def test_manual_cadence_is_applied_to_the_realigned_track_alone(films, monkeypatch, tmp_path):
    """VF issue d'un master PAL, recalée sur VO avec la conversion PAL → 23,976 : piste seule convertie."""
    from core.workflows.cadence import manual_cadence_choices

    panel, _master, vf, vo, subtitle, _logs = films
    panel._workflow_options["sync_mode"] = "physical"
    pal = manual_cadence_choices()[0]

    class _PalStudio(_Studio):
        def result_calibration(self):
            calibration = SyncCalibration((SyncSegment(0, 150.0),), cadence_mismatch=pal,
                                          cadence_audio_method="asetrate")
            return calibration, 150

    monkeypatch.setattr(studio_module, "SyncStudioDialog", _PalStudio)
    monkeypatch.setattr("ui.panels.remux_panel.panel._AudioSyncReferenceDialog",
                        lambda choices, **_k: SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted,
                                                              selected_entry=lambda: vo))
    panel._on_sync_studio_requested(vf)
    assert panel._track_calibration(vf).active_cadence == pal
    assert panel._track_calibration(vo).active_cadence is None
    assert _shifts(vo, subtitle) == [0, 0]
    shifts = _prepared_shifts(panel, tmp_path)
    assert shifts[vf.entry_id] == 150 and vo.entry_id not in shifts


# --- Relecture Gemini Neon (série 904ffe4..e07857a). ---


def _run_pending(submitted) -> None:
    """Analyses de hauteur mémorisées exécutées, signaux traités."""
    from PySide6.QtCore import QCoreApplication

    while submitted:
        submitted.pop(0)()
        QCoreApplication.processEvents()


def _studio_with(panel, monkeypatch, calibration, reference, **attributes):
    """Studio validé avec `calibration` face à `reference` ; attributs du dialogue en plus."""
    class _Fixed(_Studio):
        def result_calibration(self):
            return calibration, round(calibration.segments[0].shift_ms)

    for name, value in attributes.items():
        setattr(_Fixed, name, value)
    monkeypatch.setattr(studio_module, "SyncStudioDialog", _Fixed)
    monkeypatch.setattr("ui.panels.remux_panel.panel._AudioSyncReferenceDialog",
                        lambda choices, **_k: SimpleNamespace(exec=lambda: QDialog.DialogCode.Accepted,
                                                              selected_entry=lambda: reference))


def test_studio_changing_the_manual_ratio_relaunches_the_pitch_analysis(qt_app, tmp_path, monkeypatch):
    """Rapport changé après une analyse en « Auto » : méthode remise à « auto », analyse relancée ;
    liste réalignée à la réinitialisation."""
    from dataclasses import replace

    from core.workflows.cadence import manual_cadence_choices

    resolutions: list = []
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_resolve_cadence_method",
                        lambda self: resolutions.append(self.current_calibration))
    vf = TrackEntry(1, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="B")
    vo = TrackEntry(2, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="B")
    choices = manual_cadence_choices()
    initial = SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=choices[0], cadence_audio_method="asetrate")
    dialog = studio_module.SyncStudioDialog(
        target_entry=vf, target_source_path=tmp_path / "b.mkv", target_stream_index=1,
        reference_entry=vo, reference_source_path=tmp_path / "b.mkv", reference_stream_index=2,
        calibration=initial, cadence_choices=choices, cadence_audio_method="auto",
    )
    dialog.rb_auto.setChecked(True)
    dialog._on_cadence_method_changed()
    dialog.current_calibration = replace(dialog.current_calibration, cadence_audio_method="asetrate",
                                         cadence_method_auto=True)
    resolutions.clear()
    dialog.cadence_combo.setCurrentIndex(2)
    assert dialog.current_calibration.cadence_mismatch == choices[2]
    assert dialog.current_calibration.cadence_method_undecided and resolutions
    dialog._reset_shifts()
    assert dialog.current_calibration.cadence_mismatch == choices[0] and dialog.cadence_combo.currentIndex() == 0
    dialog.close()


def test_declining_a_manual_cadence_in_its_file_is_not_a_source_refusal(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    _studio_with(panel, monkeypatch, SyncCalibration.linear(200), vf, cadence_declined=True)
    panel._on_sync_studio_requested(vo)
    assert vo.time_shift_ms == 200 and panel._cadence_declined_source_ids == set()


def test_source_analysis_survives_a_realignment_of_its_track(films, monkeypatch):
    """Analyse de la synchro de B lancée sur VF, puis VF recalée sur VO avant le résultat :
    la méthode est portée par le lien de B."""
    panel, master, vf, vo, _subtitle, _logs = films
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 300, .95, _pal_auto())
    assert len(submitted) == 1
    _studio(panel, monkeypatch, vf, vo, 50)
    _run_pending(submitted)
    assert panel._sync_links["B"].relative.cadence_audio_method == "asetrate"
    assert not panel.cadence_resolution_pending()


def test_new_realignment_drops_the_former_pending_analysis(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, _pal_auto(), vf)
    panel._on_sync_studio_requested(vo)
    assert panel.cadence_resolution_pending()
    from dataclasses import replace
    _studio_with(panel, monkeypatch, replace(_pal_auto(), cadence_audio_method="atempo"), vf)
    panel._on_sync_studio_requested(vo)
    assert not panel.cadence_resolution_pending()


def test_own_track_against_another_file_gets_its_pitch_analysis(films, monkeypatch):
    """VO indépendante calée face au master avec cadence « auto » : analyse planifiée, résultat posé
    sur sa synchro propre ; reprise : analyse relancée face à sa dernière référence."""
    from dataclasses import replace

    panel, master, vf, vo, _subtitle, _logs = films
    _independent(panel, monkeypatch, vo, vf, 700)
    submitted, targets = _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, _pal_auto(), master)
    panel._on_sync_studio_requested(vo)
    assert len(submitted) == 1
    _run_pending(submitted)
    assert targets == [vo.mkv_tid]
    assert panel._track_calibration(vo).cadence_audio_method == "asetrate"
    vo.sync_calibration = replace(panel._track_calibration(vo), cadence_audio_method="auto").to_dict()
    panel._resume_cadence_analyses()
    assert len(submitted) == 1


def test_variant_created_during_the_analysis_gets_the_resolved_method(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, _pal_auto(), vf)
    panel._on_sync_studio_requested(vo)
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    assert "variant" in panel._track_sync_links
    _run_pending(submitted)
    assert panel._track_sync_links["variant"].relative.cadence_audio_method == "asetrate"
    assert panel._track_sync_links[vo.entry_id].relative.cadence_audio_method == "asetrate"


def test_profile_variant_without_own_offset_keeps_following_its_source(films, monkeypatch):
    """Profil : VF à +500 et sa variante au même décalage ; B à coupures (-1000) : la variante suit B
    (elle n'est pas détachée), y compris quand la synchro de B est annulée ensuite."""
    import copy

    from core.workflows.remux_models import clone_track_entry

    panel, master, vf, _vo, _subtitle, _logs = films
    cuts = SyncCalibration((SyncSegment(0, -1000), SyncSegment(60000, -800)))
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, -1000, .95, cuts)
    tracks = [copy.deepcopy(track) for track in panel._track_table.current_tracks()]
    new_vf = next(track for track in tracks if track.entry_id == vf.entry_id)
    new_vf.time_shift_ms = 500
    variant = clone_track_entry(new_vf, entry_id="variant")
    tracks.append(variant)
    panel._commit_decision_profile_result(tracks, {"written_fields": {vf.entry_id: ("time_shift_ms",)}})
    assert "variant" not in panel._own_sync_entry_ids
    assert panel._track_calibration(variant).segments == panel._track_calibration(new_vf).segments
    panel._on_auto_sync_cancel_requested(new_vf)
    assert panel._track_calibration(variant).segments == panel._track_calibration(new_vf).segments


def test_entered_zero_on_a_realigned_track_refreshes_the_cancel_buttons(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    _studio(panel, monkeypatch, vo, vf, 200)
    assert vo.entry_id in panel._track_table._linked_sync_entry_ids
    vo.time_shift_ms = 0
    panel._track_table.time_shift_edited.emit(vo)
    assert vo.entry_id not in panel._track_table._linked_sync_entry_ids
    assert panel._global_sync_cancel_btn.isHidden()


# --- Seconde relecture Gemini Neon (commit 7a0f063). ---


def test_studio_ratio_change_outside_auto_drops_the_former_pitch_analysis(qt_app, tmp_path, monkeypatch):
    from dataclasses import replace

    from core.workflows.cadence import manual_cadence_choices
    from core.workflows.cadence_pitch import CadencePitchAnalysis

    resolutions: list = []
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_resolve_cadence_method",
                        lambda self: resolutions.append(self.current_calibration))
    vf = TrackEntry(1, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="B")
    vo = TrackEntry(2, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="B")
    choices = manual_cadence_choices()
    dialog = studio_module.SyncStudioDialog(
        target_entry=vf, target_source_path=tmp_path / "b.mkv", target_stream_index=1,
        reference_entry=vo, reference_source_path=tmp_path / "b.mkv", reference_stream_index=2,
        cadence_choices=choices,
    )
    dialog.cadence_check.setChecked(True)
    analysis = CadencePitchAnalysis("asetrate", 0.7, 0.9, 0.5, 220.0, 230.0)
    dialog.current_calibration = replace(dialog.current_calibration, cadence_audio_method="asetrate",
                                         cadence_method_auto=True, cadence_pitch_analysis=analysis)
    dialog.rb_atempo.setChecked(True)
    dialog._on_cadence_method_changed()
    dialog.cadence_combo.setCurrentIndex(2)
    assert dialog.current_calibration.cadence_pitch_analysis is None
    assert dialog.current_calibration.cadence_audio_method == "atempo"
    resolutions.clear()
    dialog.rb_auto.setChecked(True)
    dialog._on_cadence_method_changed()
    assert dialog.current_calibration.cadence_method_undecided and resolutions
    dialog.close()


def test_declining_on_an_own_track_against_another_file_is_not_a_source_refusal(films, monkeypatch):
    panel, master, vf, vo, _subtitle, _logs = films
    _independent(panel, monkeypatch, vo, vf, 700)
    _studio_with(panel, monkeypatch, SyncCalibration.linear(150), master, cadence_declined=True)
    panel._on_sync_studio_requested(vo)
    assert panel._cadence_declined_source_ids == set()


def test_resolved_method_does_not_reach_another_track_of_the_file(films, monkeypatch):
    """VO et VD recalées sur VF avec la même conversion « auto » : le résultat de VO ne vaut pas pour VD."""
    from PySide6.QtCore import QCoreApplication

    panel, _master, vf, vo, _subtitle, _logs = films
    vd = _track(4, "audio", file_id="B", language="ger", title="VD")
    panel._track_table.append_tracks(_COLOR_A, [vd])
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, _pal_auto(0.0), vf)
    panel._on_sync_studio_requested(vo)
    panel._on_sync_studio_requested(vd)
    assert len(submitted) == 2
    submitted.pop(0)()
    QCoreApplication.processEvents()
    assert panel._track_sync_links[vo.entry_id].relative.cadence_audio_method == "asetrate"
    assert panel._track_sync_links[vd.entry_id].relative.cadence_audio_method == "auto"


def test_variant_of_an_own_track_gets_the_resolved_method(films, monkeypatch):
    """VO indépendante calée sur le master en « auto », variante créée pendant l'analyse : même résultat."""
    panel, master, vf, vo, _subtitle, _logs = films
    _independent(panel, monkeypatch, vo, vf, 700)
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, _pal_auto(), master)
    panel._on_sync_studio_requested(vo)
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    assert panel._last_reference_ids["variant"] == master.entry_id
    _run_pending(submitted)
    assert panel._track_calibration(vo).cadence_audio_method == "asetrate"
    assert panel._track_calibration(variant).cadence_audio_method == "asetrate"


def test_profile_variant_with_its_own_offset_is_cancellable(qt_app, monkeypatch, tmp_path):
    from core.profiles.decision import remux_config_to_decision_profile

    panel, master, vf, vo, _sub = _distinct_panel(tmp_path)
    cuts = SyncCalibration((SyncSegment(0, 300), SyncSegment(60000, 500)))
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 300, .95, cuts)
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    _studio(panel, monkeypatch, variant, vf, 120)
    profile = remux_config_to_decision_profile(panel.collect_config())
    panel.remove_audio_track_variant("variant")
    panel._apply_decision_profile(profile)
    recreated = next(track for track in panel._track_table.current_tracks() if track.is_new)
    assert panel._track_table._has_cancelable_auto_sync(recreated)
    panel.close()


def test_redone_source_sync_no_longer_waits_for_the_former_analysis(films, monkeypatch):
    from dataclasses import replace

    panel, master, vf, _vo, _subtitle, _logs = films
    _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 300, .95, _pal_auto())
    assert panel.cadence_resolution_pending()
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 300, .95, replace(_pal_auto(), cadence_audio_method="atempo"))
    assert not panel.cadence_resolution_pending()


def test_entered_offset_on_a_realignment_keeps_its_pitch_analysis(films, monkeypatch):
    """Recalage « auto » dans le fichier, décalage saisi avant le résultat (lien retiré) : analyse
    en cours gardée, résultat posé sur la synchro propre au décalage saisi."""
    panel, _master, vf, vo, _subtitle, _logs = films
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, _pal_auto(), vf)
    panel._on_sync_studio_requested(vo)
    vo.time_shift_ms = 500
    panel._track_table.time_shift_edited.emit(vo)
    assert vo.entry_id not in panel._track_sync_links and panel.cadence_resolution_pending()
    _run_pending(submitted)
    assert panel._track_calibration(vo).cadence_audio_method == "asetrate"
    assert vo.time_shift_ms == 500 and not panel.cadence_resolution_pending()


# --- Troisième relecture Gemini Neon (commit b09d020). ---


def test_entered_offset_on_an_own_track_keeps_its_analysis(films, monkeypatch):
    """VO indépendante calée sur le master en « auto », décalage saisi avant le résultat : analyse
    gardée, résultat posé au décalage saisi."""
    panel, master, vf, vo, _subtitle, _logs = films
    _independent(panel, monkeypatch, vo, vf, 700)
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, _pal_auto(), master)
    panel._on_sync_studio_requested(vo)
    vo.time_shift_ms = 500
    panel._track_table.time_shift_edited.emit(vo)
    assert panel.cadence_resolution_pending() and len(submitted) == 1
    _run_pending(submitted)
    assert panel._track_calibration(vo).cadence_audio_method == "asetrate" and vo.time_shift_ms == 500


def test_entered_offset_on_a_source_keeps_its_analysis(films, monkeypatch):
    """B calée sur le master en « auto », décalage saisi sur VF avant le résultat (lien retiré) :
    analyse gardée, résultat posé au décalage saisi ; reprise de session sans lien : dernière référence."""
    from dataclasses import replace

    panel, master, vf, _vo, _subtitle, _logs = films
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 300, .95, _pal_auto())
    vf.time_shift_ms = 500
    panel._track_table.time_shift_edited.emit(vf)
    assert "B" not in panel._sync_links and panel.cadence_resolution_pending() and len(submitted) == 1
    _run_pending(submitted)
    assert panel._source_calibration("B").cadence_audio_method == "asetrate"
    assert round(panel._source_calibration("B").segments[0].shift_ms) == 500
    assert not panel.cadence_resolution_pending()
    panel._set_source_calibration("B", replace(panel._source_calibration("B"), cadence_audio_method="auto"))
    panel._last_reference_ids[vf.entry_id] = master.entry_id
    panel._resume_cadence_analyses()
    assert len(submitted) == 1


def test_variant_keeps_its_own_analysis_when_the_model_is_cancelled(films, monkeypatch):
    panel, _master, vf, vo, _subtitle, _logs = films
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, _pal_auto(), vf)
    panel._on_sync_studio_requested(vo)
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    assert len(submitted) == 2
    panel._on_auto_sync_cancel_requested(vo)
    _run_pending(submitted)
    assert panel._track_sync_links["variant"].relative.cadence_audio_method == "asetrate"
    assert not panel.cadence_resolution_pending()


def test_loop_release_drops_the_analysis_of_the_released_source(films, monkeypatch):
    panel, master, vf, _vo, _subtitle, _logs = films
    _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 300, .95, _pal_auto())
    assert panel.cadence_resolution_pending()
    panel._on_audio_sync_done(master.entry_id, vf.entry_id, 50, .95, None)
    assert "B" not in panel._sync_links and not panel.cadence_resolution_pending()


def test_removed_variant_drops_its_own_analysis(films, monkeypatch):
    panel, master, vf, vo, _subtitle, _logs = films
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    _independent(panel, monkeypatch, variant, vf, 300)
    _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, _pal_auto(), master)
    panel._on_sync_studio_requested(variant)
    assert panel.cadence_resolution_pending()
    panel.remove_audio_track_variant("variant")
    assert not panel.cadence_resolution_pending()


# --- Quatrième relecture Gemini Neon (commit 789d3b0). ---


def test_removing_the_variant_that_launched_a_source_analysis_keeps_it(films, monkeypatch):
    panel, master, vf, _vo, _subtitle, _logs = films
    panel.add_audio_track_variant(vf, entry_id="variant", codec="aac", bitrate_kbps=192)
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done("variant", master.entry_id, 300, .95, _pal_auto())
    assert panel.cadence_resolution_pending()
    panel.remove_audio_track_variant("variant")
    assert panel.cadence_resolution_pending()
    _run_pending(submitted)
    assert panel._sync_links["B"].relative.cadence_audio_method == "asetrate"


def _three_sources(qt_app, tmp_path, monkeypatch, *, entered: bool, b_method: str = "asetrate",
                   c_cadence=("25/1", "24/1")):
    """A ; B calée sur A (PAL 25 → 23,976, `b_method`) ; C calée sur VF de B (`c_cadence`, « auto »),
    décalage saisi sur C avant le résultat si `entered`. Retourne la synchro appliquée à C."""
    from core.workflows.cadence import detect_cadence_from_metadata

    tmp_path.mkdir(parents=True, exist_ok=True)
    panel = RemuxPanel(AppConfig())
    master, vf, c1 = _track(1, "audio", file_id="A"), _track(1, "audio", file_id="B"), _track(1, "audio", file_id="C")
    _add_source(panel, tmp_path, "A", [master])
    _add_source(panel, tmp_path, "B", [vf])
    _add_source(panel, tmp_path, "C", [c1])
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    film = detect_cadence_from_metadata(*c_cadence)
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 0, .95,
                              SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=pal, cadence_audio_method=b_method))
    panel._on_audio_sync_done(c1.entry_id, vf.entry_id, 0, .95,
                              SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=film, cadence_audio_method="auto"))
    if entered:
        c1.time_shift_ms = 500
        panel._track_table.time_shift_edited.emit(c1)
    _run_pending(submitted)
    applied = panel._source_calibration("C")
    panel.close()
    return applied


@pytest.mark.parametrize("b_method,c_cadence", [
    ("asetrate", ("25/1", "24/1")),          # référence déjà convertie
    ("auto", ("25/1", "24/1")),              # référence tranchée pendant l'analyse de C
    ("asetrate", ("25/1", "24000/1001")),    # cadences réciproques : vitesses annulées
])
def test_source_analysis_without_link_is_composed_with_its_reference(qt_app, tmp_path, monkeypatch, b_method, c_cadence):
    """Méthode tranchée après un décalage saisi (lien retiré) : même hauteur qu'avec le lien."""
    linked = _three_sources(qt_app, tmp_path / "linked", monkeypatch, entered=False, b_method=b_method, c_cadence=c_cadence)
    entered = _three_sources(qt_app, tmp_path / "entered", monkeypatch, entered=True, b_method=b_method, c_cadence=c_cadence)
    assert entered.cadence_audio_method == linked.cadence_audio_method
    assert entered.audio_pitch_factor == pytest.approx(linked.audio_pitch_factor)
    assert round(entered.segments[0].shift_ms) == 500


def test_source_last_reference_is_an_audio_track(films, monkeypatch):
    panel, master, vf, _vo, subtitle, _logs = films
    sub_a = _track(2, "subtitle", codec="SRT", file_id="A")
    panel._track_table.append_tracks(_COLOR_A, [sub_a])
    panel._last_reference_ids[subtitle.entry_id] = sub_a.entry_id
    panel._last_reference_ids[vf.entry_id] = master.entry_id
    tracks = panel._track_table.current_tracks()
    panel._replace_track_table_tracks([subtitle] + [track for track in tracks if track is not subtitle])
    assert panel._source_last_reference("B") is master


def test_track_detached_during_its_source_analysis_is_analysed_too(films, monkeypatch):
    """B en cours d'analyse « auto » ; VO recalée sur VF puis décalage saisi (détachée, conversion
    de B gardée) : VO analysée face à la référence de B, méthode tranchée pour les deux."""
    panel, master, vf, vo, _subtitle, _logs = films
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 300, .95, _pal_auto())
    _studio(panel, monkeypatch, vo, vf, 100)
    vo.time_shift_ms = 420
    panel._track_table.time_shift_edited.emit(vo)
    assert vo.entry_id not in panel._track_sync_links
    _run_pending(submitted)
    assert panel._sync_links["B"].relative.cadence_audio_method == "asetrate"
    assert panel._track_calibration(vo).cadence_audio_method == "asetrate"
    assert not panel.cadence_resolution_pending()


# --- Cinquième relecture Gemini Neon (commit e2b5db3). ---


def test_removed_source_drops_an_analysis_launched_from_a_removed_variant(films, monkeypatch):
    from ui.panels.remux_panel.functions import inspection as inspection_functions

    panel, master, vf, _vo, _subtitle, _logs = films
    panel.add_audio_track_variant(vf, entry_id="variant", codec="aac", bitrate_kbps=192)
    _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done("variant", master.entry_id, 300, .95, _pal_auto())
    panel.remove_audio_track_variant("variant")
    assert panel.cadence_resolution_pending()
    inspection_functions.on_remove_file(panel, "B")
    assert not panel.cadence_resolution_pending()


def test_variant_of_a_detached_track_is_analysed_against_its_source_reference(films, monkeypatch):
    """B en cours d'analyse « auto » ; VO détachée (recalée sur VF puis saisie) ; variante de VO :
    analysée face à la référence de B (face à VF, les conversions s'annulent)."""
    panel, master, vf, vo, _subtitle, _logs = films
    submitted, targets = _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 300, .95, _pal_auto())
    _studio(panel, monkeypatch, vo, vf, 100)
    vo.time_shift_ms = 420
    panel._track_table.time_shift_edited.emit(vo)
    count = len(submitted)
    panel.add_audio_track_variant(vo, entry_id="variant", codec="aac", bitrate_kbps=192)
    assert len(submitted) == count + 1
    _run_pending(submitted)
    variant = next(track for track in panel._track_table.current_tracks() if track.entry_id == "variant")
    assert panel._track_calibration(variant).cadence_audio_method == "asetrate"


# --- Relecture Codex (commits 5e8f732, e07857a). ---


def test_ffmpeg_command_reencodes_a_same_codec_variant_at_its_bitrate(films):
    """Variante E-AC-3 384 kb/s d'une E-AC-3 768 kb/s : réencodée au débit demandé, jamais copiée ;
    une variante copie et une piste déjà réécrite par la synchro physique restent copiées."""
    from dataclasses import replace

    from core.workflows.remux_command import _audio_variant_codec_args

    panel, _master, _vf, vo, _subtitle, _logs = films
    vo.display_info = vo.orig_display_info = "5.1  768 kbps"
    panel.add_audio_track_variant(vo, entry_id="variant", codec="eac3", bitrate_kbps=384)
    panel.add_audio_track_variant(vo, entry_id="copy", codec="copy")
    mapped = {item.track.entry_id: item for item in resolve_mapped_tracks(panel.collect_config())}
    args = _audio_variant_codec_args(mapped["variant"])
    assert any(arg.startswith("-c:a:") for arg in args) and "384k" in args
    assert _audio_variant_codec_args(mapped["copy"]) == []
    rewritten = replace(mapped["variant"], track=replace(mapped["variant"].track, is_new=False,
                                                          orig_codec=mapped["variant"].track.codec,
                                                          orig_display_info=mapped["variant"].track.display_info))
    assert _audio_variant_codec_args(rewritten) == []


def test_studio_matches_a_reciprocal_ratio_written_differently(qt_app, tmp_path, monkeypatch):
    """Conversion 23,976 → 25 portée par la calibration sous la forme « 1001/960 » : la liste la
    reconnaît (25025/24000)."""
    from core.workflows.cadence import manual_cadence_choices

    monkeypatch.setattr(studio_module.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_resolve_cadence_method", lambda *_a, **_k: None)
    vo = TrackEntry(2, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="B")
    vf = TrackEntry(1, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="B")
    choices = manual_cadence_choices()
    reciprocal = choices[0].inverse()
    assert reciprocal.speed_ratio != choices[2].speed_ratio
    calibration = SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=reciprocal, cadence_audio_method="asetrate")
    dialog = studio_module.SyncStudioDialog(
        target_entry=vo, target_source_path=tmp_path / "b.mkv", target_stream_index=2,
        reference_entry=vf, reference_source_path=tmp_path / "b.mkv", reference_stream_index=1,
        calibration=calibration, cadence_choices=choices,
    )
    assert dialog.cadence_combo.currentIndex() == 2
    dialog.close()


def test_subtitle_scan_compares_converted_times():
    """Cible à 25 i/s non convertie : comparée sur les temps convertis, le décalage est retrouvé et
    les coupures rendues dans le temps de la cible."""
    from core.workflows.cadence import manual_cadence_choices
    from core.workflows.subtitle_sync_scan import SubtitleCue, SubtitleSyncScanner

    import random

    scale = manual_cadence_choices()[0].time_stretch_ratio
    rng = random.Random(7)
    reference, start = [], 10_000.0
    for i in range(400):
        duration = rng.uniform(900.0, 3_500.0)
        reference.append(SubtitleCue(start, start + duration, f"r{i}"))
        start += duration + rng.uniform(300.0, 9_000.0)
    target = [SubtitleCue((cue.start_ms - 800.0) / scale, (cue.end_ms - 800.0) / scale, cue.text) for cue in reference]
    scanner = SubtitleSyncScanner()
    scanner.extract_cues = lambda path, index=None: reference if "ref" in str(path) else target
    calibration = scanner.scan(Path("ref.srt"), 0, Path("tgt.srt"), 0, target_time_scale=scale)
    assert abs(calibration.segments[0].shift_ms - 800.0) <= 40.0


def test_studio_subtitle_alignment_keeps_the_chosen_cadence(qt_app, tmp_path, monkeypatch):
    from typing import Any, cast

    from core.workflows.cadence import manual_cadence_choices
    from core.workflows.subtitle_sync_scan import SubtitleSyncScanner

    monkeypatch.setattr(studio_module.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_resolve_cadence_method", lambda *_a, **_k: None)
    scales: list = []

    def scan(_self, *_a, target_time_scale=1.0, **_k):
        scales.append(target_time_scale)
        return SyncCalibration.linear(150)

    monkeypatch.setattr(SubtitleSyncScanner, "scan", scan)
    sub = TrackEntry(3, "subtitle", "SubRip", "", "fre", "VF", file_id="B")
    ref = TrackEntry(4, "subtitle", "SubRip", "", "eng", "VO", file_id="B")
    choices = manual_cadence_choices()
    dialog = studio_module.SyncStudioDialog(
        target_entry=sub, target_source_path=tmp_path / "b.mkv", target_stream_index=3,
        reference_entry=ref, reference_source_path=tmp_path / "b.mkv", reference_stream_index=4,
        cadence_choices=choices,
    )
    dialog.cadence_combo.setCurrentIndex(1)
    dialog.cadence_check.setChecked(True)
    dialog._executor = cast(Any, SimpleNamespace(submit=lambda callback: callback(), shutdown=lambda **_k: None))
    dialog._run_auto_subtitle_sync()
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.processEvents()
    assert scales == [pytest.approx(choices[1].time_stretch_ratio)]
    assert dialog.current_calibration.cadence_mismatch == choices[1]
    assert dialog.current_calibration.segments[0].shift_ms == 150
    dialog.close()


# --- Relecture Codex (e07857a..7011e19). ---


def test_detached_track_realigned_on_a_converted_source_gets_its_analysis(qt_app, tmp_path, monkeypatch):
    """C/c1 détachée, calée dans le Studio sur B (PAL, convertie en asetrate vers A) avec 23,976 → 25 en
    « auto » : vitesses annulées dans sa synchro, analyse planifiée sur la synchro relative validée."""
    from core.workflows.cadence import detect_cadence_from_metadata

    panel = RemuxPanel(AppConfig())
    master, b1 = _track(1, "audio", file_id="A"), _track(1, "audio", file_id="B")
    c1, c2 = _track(1, "audio", file_id="C"), _track(2, "audio", file_id="C", language="eng")
    _add_source(panel, tmp_path, "A", [master])
    _add_source(panel, tmp_path, "B", [b1])
    _add_source(panel, tmp_path, "C", [c1, c2])
    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    panel._on_audio_sync_done(b1.entry_id, master.entry_id, 0, .95,
                              SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=pal, cadence_audio_method="asetrate"))
    _independent(panel, monkeypatch, c1, c2, 300)
    submitted, targets = _capture_pitch_analyses(panel, monkeypatch)
    reverse = detect_cadence_from_metadata("25/1", "24000/1001")
    _studio_with(panel, monkeypatch, SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=reverse,
                                                     cadence_audio_method="auto"), b1)
    panel._on_sync_studio_requested(c1)
    assert len(submitted) == 1
    _run_pending(submitted)
    assert targets == [c1.mkv_tid]
    assert not panel.cadence_resolution_pending()
    panel.close()


def test_loop_broken_by_a_rejoining_track_drops_the_released_analysis(films, monkeypatch):
    panel, master, vf, vo, _subtitle, _logs = films
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 100, .95, None)
    _independent(panel, monkeypatch, vo, vf, 700)
    _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done(master.entry_id, vo.entry_id, 50, .95, _pal_auto(50.0))
    assert panel.cadence_resolution_pending()
    panel._on_auto_sync_cancel_requested(vo)
    assert "A" not in panel._sync_links and not panel.cadence_resolution_pending()


def test_late_result_keeps_the_junction_frozen_at_the_entry(films, monkeypatch):
    """VF recalée en « auto » sur VO (corrigée à une jonction), décalage saisi sur VF, correction de VO
    annulée avant le résultat : VF garde la jonction figée à la saisie."""
    panel, _master, vf, vo, _subtitle, _logs = films
    vd = _track(4, "audio", file_id="B", language="ger", title="VD")
    panel._track_table.append_tracks(_COLOR_A, [vd])
    _cut_realign(panel, monkeypatch, vo, vd)
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, _pal_auto(0.0), vo)
    panel._on_sync_studio_requested(vf)
    assert len(panel._track_calibration(vf).segments) == 2
    vf.time_shift_ms = 500
    panel._track_table.time_shift_edited.emit(vf)
    panel._on_auto_sync_cancel_requested(vo)
    _run_pending(submitted)
    calibration = panel._track_calibration(vf)
    assert calibration.cadence_audio_method == "asetrate"
    assert len(calibration.segments) == 2 and round(calibration.segments[0].shift_ms) == 500


def test_resumed_source_analysis_finds_the_source_reference(films, monkeypatch):
    """Synchro de B lancée depuis une variante retirée ensuite, décalage saisi, session rouverte : la
    référence de la source est retrouvée, l'analyse reprise."""
    panel, master, vf, _vo, _subtitle, _logs = films
    panel.add_audio_track_variant(vf, entry_id="variant", codec="aac", bitrate_kbps=192)
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done("variant", master.entry_id, 300, .95, _pal_auto())
    panel.remove_audio_track_variant("variant")
    vf.time_shift_ms = 500
    panel._track_table.time_shift_edited.emit(vf)
    assert panel._source_reference_ids["B"] == master.entry_id
    submitted.clear()
    _reloaded(panel, monkeypatch)
    assert len(submitted) == 1


def test_studio_discards_a_subtitle_alignment_made_with_another_ratio(qt_app, tmp_path, monkeypatch):
    from core.workflows.cadence import manual_cadence_choices

    monkeypatch.setattr(studio_module.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_resolve_cadence_method", lambda *_a, **_k: None)
    sub = TrackEntry(3, "subtitle", "SubRip", "", "fre", "VF", file_id="B")
    ref = TrackEntry(4, "subtitle", "SubRip", "", "eng", "VO", file_id="B")
    choices = manual_cadence_choices()
    dialog = studio_module.SyncStudioDialog(
        target_entry=sub, target_source_path=tmp_path / "b.mkv", target_stream_index=3,
        reference_entry=ref, reference_source_path=tmp_path / "b.mkv", reference_stream_index=4,
        cadence_choices=choices,
    )
    dialog.cadence_combo.setCurrentIndex(1)
    dialog.cadence_check.setChecked(True)
    submitted: list = []
    dialog._executor = SimpleNamespace(submit=submitted.append, shutdown=lambda **_k: None)
    dialog._run_auto_subtitle_sync()
    dialog.cadence_combo.setCurrentIndex(0)
    dialog._on_auto_sub_sync_ready(SyncCalibration.linear(150))
    assert dialog.current_calibration.cadence_mismatch == choices[0]
    assert dialog.current_calibration.segments[0].shift_ms == 0
    dialog.close()


def test_native_plan_copies_a_copy_variant(films):
    from core.workflows.remux_plan import build_audio_variant_command, plan_native_audio_variants

    panel, _master, _vf, vo, _subtitle, _logs = films
    vo.display_info = vo.orig_display_info = "5.1  768 kbps"
    panel.add_audio_track_variant(vo, entry_id="copy", codec="copy")
    panel.add_audio_track_variant(vo, entry_id="lower", codec="eac3", bitrate_kbps=384)
    variants = {variant.entry_id: variant for variant in plan_native_audio_variants(panel.collect_config())}
    assert variants["copy"].encoder == "copy" and variants["copy"].bitrate_kbps is None
    command = build_audio_variant_command(variants["copy"], Path("b.mkv"), 2, Path("out.mkv"), "ffmpeg")
    assert "-b:a" not in command and command[command.index("-c:a") + 1] == "copy"
    assert variants["lower"].encoder != "copy" and variants["lower"].bitrate_kbps == 384


# --- Relecture Codex (998c22e). ---


def test_studio_audio_sync_result_is_kept_with_an_active_cadence(qt_app, tmp_path, monkeypatch):
    """« Synchro rapide / approfondie » avec une conversion cochée : résultat appliqué (le contrôle du
    rapport ne vaut que pour « Synchro sous-titres »)."""
    from core.workflows.cadence import manual_cadence_choices

    monkeypatch.setattr(studio_module.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_resolve_cadence_method", lambda *_a, **_k: None)
    vf = TrackEntry(1, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="B")
    vo = TrackEntry(2, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="B")
    choices = manual_cadence_choices()
    dialog = studio_module.SyncStudioDialog(
        target_entry=vf, target_source_path=tmp_path / "b.mkv", target_stream_index=1,
        reference_entry=vo, reference_source_path=tmp_path / "b.mkv", reference_stream_index=2,
        cadence_choices=choices,
    )
    dialog.cadence_check.setChecked(True)
    measured = SyncCalibration((SyncSegment(0, 150.0),), cadence_mismatch=choices[0], cadence_audio_method="asetrate")
    dialog._on_auto_sub_sync_ready(measured)
    assert dialog.current_calibration.segments[0].shift_ms == 150
    dialog.close()


def test_late_result_never_changes_the_frozen_speed(films, monkeypatch):
    """VO ralentie (25 → 23,976) sur VD ; VF calée sur VO au rapport inverse en « auto » (vitesse nulle),
    décalage saisi sur VF, recalage de VO annulé avant le résultat : VF garde sa vitesse."""
    from core.workflows.cadence import detect_cadence_from_metadata

    panel, _master, vf, vo, _subtitle, _logs = films
    vd = _track(4, "audio", file_id="B", language="ger", title="VD")
    panel._track_table.append_tracks(_COLOR_A, [vd])
    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    reverse = detect_cadence_from_metadata("25/1", "24000/1001")
    _studio_with(panel, monkeypatch, SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=pal,
                                                     cadence_audio_method="asetrate"), vd)
    panel._on_sync_studio_requested(vo)
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=reverse,
                                                     cadence_audio_method="auto"), vo)
    panel._on_sync_studio_requested(vf)
    frozen_ratio = panel._track_calibration(vf)._stretch_ratio()
    vf.time_shift_ms = 500
    panel._track_table.time_shift_edited.emit(vf)
    panel._on_auto_sync_cancel_requested(vo)
    _run_pending(submitted)
    calibration = panel._track_calibration(vf)
    assert calibration._stretch_ratio() == pytest.approx(frozen_ratio)
    assert calibration.cadence_audio_method != "auto" and round(calibration.segments[0].shift_ms) == 500


def test_resumed_session_relaunches_an_analysis_whose_ratios_cancel(qt_app, tmp_path, monkeypatch):
    """B convertie (asetrate) sur A ; C calée sur B au rapport inverse en « auto » (vitesses annulées),
    décalage saisi sur C ; session rouverte avant le résultat : l'analyse de C reprend."""
    from cli.logging import Logger
    from core.jobs.options import CommonOptions
    from core.jobs.remux_config import build_remux_config

    panel = RemuxPanel(AppConfig())
    panel._output_edit.setText(str(tmp_path / "output.mkv"))
    master, b1, c1 = _track(1, "audio", file_id="A"), _track(1, "audio", file_id="B"), _track(1, "audio", file_id="C")
    _add_source(panel, tmp_path, "A", [master])
    _add_source(panel, tmp_path, "B", [b1])
    _add_source(panel, tmp_path, "C", [c1])
    from core.workflows.cadence import detect_cadence_from_metadata

    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    reverse = detect_cadence_from_metadata("25/1", "24000/1001")
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done(b1.entry_id, master.entry_id, 0, .95,
                              SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=pal, cadence_audio_method="asetrate"))
    panel._on_audio_sync_done(c1.entry_id, b1.entry_id, 0, .95,
                              SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=reverse, cadence_audio_method="auto"))
    c1.time_shift_ms = 500
    panel._track_table.time_shift_edited.emit(c1)
    assert panel.cadence_resolution_pending()
    infos = {source.path: source.info for source in panel._source_files}
    monkeypatch.setattr("core.inspector.FileInspector.inspect", lambda _self, path, *_a, **_k: infos[Path(path)])
    job = workflow_functions._session_job(panel)
    config = build_remux_config(job, panel._config, CommonOptions(), Logger())
    submitted.clear()
    workflow_functions.restore(panel, config, [source.info for source in panel._source_files], job["_muxiveo_session"])
    assert panel.cadence_resolution_pending() and len(submitted) == 1
    _run_pending(submitted)
    resolved = panel._source_calibration(panel._source_files[2].id)
    # asetrate tranché : les deux conversions s'annulent, vitesse et hauteur comprises.
    assert resolved.cadence_audio_method != "auto" and resolved.audio_pitch_factor == pytest.approx(1.0)
    assert not panel.cadence_resolution_pending()
    panel.close()


# --- Relecture Codex (3514c3f). ---


def test_pending_source_analysis_saved_after_its_variant_is_removed(films, monkeypatch):
    """Synchro de B lancée depuis une variante, décalage saisi, variante retirée, session sauvegardée :
    l'analyse en cours est sauvegardée sur une piste qui suit encore B."""
    panel, master, vf, _vo, _subtitle, _logs = films
    panel.add_audio_track_variant(vf, entry_id="variant", codec="aac", bitrate_kbps=192)
    _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done("variant", master.entry_id, 300, .95, _pal_auto())
    vf.time_shift_ms = 500
    panel._track_table.time_shift_edited.emit(vf)
    panel.remove_audio_track_variant("variant")
    assert panel.cadence_resolution_pending()
    pending = workflow_functions._session_job(panel)["_muxiveo_session"]["pending_cadence_analyses"]
    assert len(pending) == 1 and pending[0]["track"]["id"] == vf.mkv_tid


def test_studio_discards_an_audio_result_after_the_conversion_was_unchecked(qt_app, tmp_path, monkeypatch):
    from core.workflows.cadence import manual_cadence_choices

    monkeypatch.setattr(studio_module.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_resolve_cadence_method", lambda *_a, **_k: None)
    vf = TrackEntry(1, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="B")
    vo = TrackEntry(2, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="B")
    choices = manual_cadence_choices()
    dialog = studio_module.SyncStudioDialog(
        target_entry=vf, target_source_path=tmp_path / "b.mkv", target_stream_index=1,
        reference_entry=vo, reference_source_path=tmp_path / "b.mkv", reference_stream_index=2,
        cadence_choices=choices,
    )
    dialog.cadence_check.setChecked(True)
    dialog._executor = SimpleNamespace(submit=lambda *_a, **_k: None, shutdown=lambda **_k: None)
    dialog._run_auto_audio_sync(deep=True)
    dialog.cadence_check.setChecked(False)
    measured = SyncCalibration((SyncSegment(0, 150.0),), cadence_mismatch=choices[0], cadence_audio_method="asetrate")
    dialog._on_auto_sub_sync_ready(measured)
    assert dialog.current_calibration.cadence_mismatch is None
    assert dialog.current_calibration.segments[0].shift_ms == 0
    dialog.close()


# --- Relecture Codex (5ed1d5f). ---


def test_studio_shows_review_ranges_when_the_analysis_finds_a_cadence(qt_app, tmp_path, monkeypatch):
    from core.workflows.cadence import manual_cadence_choices

    monkeypatch.setattr(studio_module.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_resolve_cadence_method", lambda *_a, **_k: None)
    vf = TrackEntry(1, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="B")
    master = TrackEntry(1, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="A")
    dialog = studio_module.SyncStudioDialog(
        target_entry=vf, target_source_path=tmp_path / "b.mkv", target_stream_index=1,
        reference_entry=master, reference_source_path=tmp_path / "a.mkv", reference_stream_index=1,
    )
    dialog._executor = SimpleNamespace(submit=lambda *_a, **_k: None, shutdown=lambda **_k: None)
    dialog._run_auto_audio_sync(deep=True)
    proposal = SyncCalibration((SyncSegment(0, 150.0),), cadence_mismatch=manual_cadence_choices()[0],
                               cadence_audio_method="asetrate")
    dialog._on_auto_sync_review(proposal, ((10_000.0, 40_000.0),))
    assert dialog.current_calibration.cadence_mismatch is not None
    assert dialog.review_ranges_ms == ((10_000.0, 40_000.0),)
    dialog.close()


def test_saved_source_analysis_keeps_its_scope_when_its_track_is_realigned(films, monkeypatch):
    """Analyse de B lancée sur VF, VF recalée seule ensuite : sauvegardée et reprise comme l'analyse
    de la synchro de B (sur une piste qui la suit), jamais comme celle de VF seule."""
    panel, master, vf, vo, _subtitle, _logs = films
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    panel._on_audio_sync_done(vf.entry_id, master.entry_id, 300, .95, _pal_auto())
    _studio(panel, monkeypatch, vf, vo, 50)
    pending = workflow_functions._session_job(panel)["_muxiveo_session"]["pending_cadence_analyses"]
    source_pending = [item for item in pending if item["scope"] == "source"]
    assert len(source_pending) == 1 and source_pending[0]["track"]["id"] == vo.mkv_tid
    panel._cadence_pending.clear()
    submitted.clear()
    panel._restored_cadence_analyses = [(vf.entry_id, master.entry_id, panel._sync_links["B"].relative, "source")]
    panel._resume_cadence_analyses()
    assert any(scope == "B" for _e, _r, scope in panel._cadence_pending.values())


# --- Relecture Codex (4c1c0bc). ---


def test_resume_infers_the_scope_of_an_analysis_saved_without_it(films, monkeypatch):
    """Analyse sauvegardée sans portée (format antérieur) d'une VO recalée dans son fichier : reprise
    comme analyse de la piste seule, une seule fois."""
    panel, _master, vf, vo, _subtitle, _logs = films
    submitted, _targets = _capture_pitch_analyses(panel, monkeypatch)
    _studio_with(panel, monkeypatch, _pal_auto(), vf)
    panel._on_sync_studio_requested(vo)
    relative = panel._track_sync_links[vo.entry_id].relative
    panel._cadence_pending.clear()
    submitted.clear()
    panel._restored_cadence_analyses = [(vo.entry_id, vf.entry_id, relative, "")]
    panel._resume_cadence_analyses()
    scopes = [scope for _e, _r, scope in panel._cadence_pending.values()]
    assert scopes == [f"track:{vo.entry_id}"] and len(submitted) == 1


def test_studio_reveals_the_cadence_card_when_an_analysis_finds_a_conversion(qt_app, tmp_path, monkeypatch):
    from core.workflows.cadence import manual_cadence_choices

    monkeypatch.setattr(studio_module.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    monkeypatch.setattr(studio_module.SyncStudioDialog, "_resolve_cadence_method", lambda *_a, **_k: None)
    vf = TrackEntry(1, "audio", "E-AC-3", "5.1", "fre", "VF", file_id="B")
    master = TrackEntry(1, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="A")
    dialog = studio_module.SyncStudioDialog(
        target_entry=vf, target_source_path=tmp_path / "b.mka", target_stream_index=1,
        reference_entry=master, reference_source_path=tmp_path / "a.mkv", reference_stream_index=1,
    )
    assert not hasattr(dialog, "cadence_check")
    found = SyncCalibration((SyncSegment(0, 150.0),), cadence_mismatch=manual_cadence_choices()[0],
                            cadence_audio_method="asetrate")
    dialog._on_auto_sub_sync_ready(found)
    assert dialog.cadence_check.isChecked()
    dialog.cadence_check.setChecked(False)
    assert dialog.current_calibration.cadence_mismatch is None
    dialog.close()
