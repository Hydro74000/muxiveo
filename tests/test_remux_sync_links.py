"""Panneau Conteneur : chaînes de références de synchro (une source suit la source sur laquelle elle a été mesurée)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QDialog

from core.config import AppConfig
from core.i18n import translate_text
from core.workflows.sync_calibration import SyncCalibration, SyncSegment
from tests.test_remux import _COLOR_A, _audio, _file_info, _subtitle, _track
from ui.panels.remux_panel import RemuxPanel, SourceFile
from ui.panels.remux_panel.functions import inspection as inspection_functions
from ui.panels.remux_panel.functions import workflow as workflow_functions


@pytest.fixture
def chain(qt_app, tmp_path):
    """Trois sources A (maître), B, C : une piste audio et une piste de sous-titres chacune."""
    panel = RemuxPanel(AppConfig())
    tracks: dict[str, list] = {}
    for name in ("A", "B", "C"):
        path = tmp_path / f"{name}.mkv"
        path.touch()
        audio = _track(1, "audio", file_id=name)
        subtitle = _track(2, "subtitle", codec="SRT", file_id=name)
        info = _file_info(path=path, audios=[_audio(index=1)], subs=[_subtitle(index=2)])
        panel._source_files.append(SourceFile(id=name, path=path, color=_COLOR_A, info=info, tracks=[audio, subtitle]))
        panel._source_names[name] = f"{name}.mkv"
        panel._source_colors[name] = _COLOR_A
        panel._track_table.append_tracks(_COLOR_A, [audio, subtitle])
        tracks[name] = [audio, subtitle]
    logs: list[tuple[str, str]] = []
    panel.log_message.connect(lambda level, text: logs.append((level, text)))
    yield panel, tracks, logs
    panel.close()


def _sync(panel, tracks, target, reference, offset_ms, calibration=None):
    panel._on_audio_sync_done(tracks[target][0].entry_id, tracks[reference][0].entry_id, offset_ms, .95, calibration)


def _shifts(tracks):
    return {name: [track.time_shift_ms for track in entries] for name, entries in tracks.items()}


def test_target_follows_a_synced_reference(chain):
    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", -5466)
    _sync(panel, tracks, "C", "B", -100)
    assert _shifts(tracks) == {"A": [0, 0], "B": [-5466, -5466], "C": [-5566, -5566]}
    assert panel._sync_links["C"].reference_file_id == "B"


def test_reference_change_moves_its_dependents(chain):
    panel, tracks, logs = chain
    _sync(panel, tracks, "B", "A", -5466)
    _sync(panel, tracks, "C", "B", -100)
    _sync(panel, tracks, "B", "A", -5000)
    assert _shifts(tracks)["C"] == [-5100, -5100]
    assert any("C.mkv" in text and "B.mkv" in text for level, text in logs if level == "INFO")


def test_reverse_sync_releases_the_reference(chain):
    """Synchro dans le mauvais sens (A sur B) puis corrigée (B sur A) : A revient à son audio d'origine."""
    panel, tracks, _logs = chain
    _sync(panel, tracks, "A", "B", 200)
    _sync(panel, tracks, "B", "A", -200)
    assert _shifts(tracks)["A"] == [0, 0] and _shifts(tracks)["B"] == [-200, -200]
    assert "A" not in panel._sync_links


def test_indirect_loop_releases_the_reference_and_moves_the_rest(chain):
    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", 100)   # B suit A
    _sync(panel, tracks, "C", "B", 50)    # C suit B : 150
    _sync(panel, tracks, "A", "C", -150)  # A sur C : boucle A → C → B → A
    shifts = _shifts(tracks)
    # C libérée (audio d'origine), A placée face à C, B suit A (100 − 150).
    assert (shifts["C"][0], shifts["A"][0], shifts["B"][0]) == (0, -150, -50)
    assert set(panel._sync_links) == {"A", "B"}


def test_cancelled_reference_track_moves_its_dependents(chain):
    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", -5466)
    _sync(panel, tracks, "C", "B", -100)
    panel._on_auto_sync_cancel_requested(tracks["B"][0])
    # La piste de référence de C revient à son audio d'origine ; C la suit.
    assert _shifts(tracks)["B"] == [0, -5466] and _shifts(tracks)["C"] == [-100, -100]
    panel._on_auto_sync_cancel_requested(tracks["B"][1])
    assert "B" not in panel._sync_links and _shifts(tracks)["C"] == [-100, -100]


def test_reference_cuts_are_carried_to_the_dependent(chain):
    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", 0, SyncCalibration((SyncSegment(0, 0), SyncSegment(60000, -1000))))
    _sync(panel, tracks, "C", "B", 100)
    stored = SyncCalibration.from_dict(panel._workflow_options["sync_calibrations"]["2"])
    assert [(s.start_ms, s.shift_ms) for s in stored.segments] == [(0, 100), (59900, -900)]
    panel._cancel_source_calibration(tracks["B"][0], 1)
    # Coupures de B retirées : C suit l'audio d'origine de B.
    assert "2" not in panel._workflow_options.get("sync_calibrations", {})
    assert _shifts(tracks)["C"] == [100, 100]


class _Studio:
    """Synchro Studio validé sans modification ; mémorise la calibration affichée."""

    shown: list[SyncCalibration] = []

    def __init__(self, **kwargs):
        self.calibration = kwargs["calibration"]
        _Studio.shown.append(self.calibration)

    def exec(self):
        return QDialog.DialogCode.Accepted

    def result_calibration(self):
        return self.calibration, round(self.calibration.segments[0].shift_ms)


@pytest.mark.parametrize("linked", [False, True])
def test_studio_validated_unchanged_keeps_the_sync(chain, monkeypatch, linked):
    """Le studio affiche la synchro face à l'audio d'origine de la référence ; validée telle quelle, rien ne bouge."""
    from ui.panels.remux_panel.widgets import sync_studio_dialog as mod

    panel, tracks, _logs = chain
    monkeypatch.setattr(mod, "SyncStudioDialog", _Studio)
    monkeypatch.setattr(panel, "_audio_sync_reference_choices", lambda _entry: [("B", tracks["B"][0])])
    _sync(panel, tracks, "B", "A", -5466)
    if linked:
        _sync(panel, tracks, "C", "B", -100)
    before = _shifts(tracks)
    _Studio.shown.clear()
    panel._on_sync_studio_requested(tracks["C"][0])
    assert [s.shift_ms for s in _Studio.shown[0].segments] == ([-100] if linked else [5466])
    assert _shifts(tracks) == before


def test_studio_unchanged_against_a_reference_with_added_silence_changes_nothing(chain, monkeypatch):
    """Audit P1 : référence avec 1 s de silence ajoutée à 60 s, cible indépendante, studio validé tel quel."""
    from ui.panels.remux_panel.widgets import sync_studio_dialog as mod

    panel, tracks, logs = chain
    monkeypatch.setattr(mod, "SyncStudioDialog", _Studio)
    monkeypatch.setattr(panel, "_audio_sync_reference_choices", lambda _entry: [("B", tracks["B"][0])])
    _sync(panel, tracks, "B", "A", 0, SyncCalibration((SyncSegment(0, 0), SyncSegment(60000, 1000))))
    panel._on_sync_studio_requested(tracks["C"][0])
    assert _shifts(tracks)["C"] == [0, 0] and "C" not in panel._sync_links
    assert "2" not in panel._workflow_options.get("sync_calibrations", {})
    assert ("INFO", translate_text("Synchro Studio : aucune modification, synchro inchangée.")) in logs


def test_studio_change_is_applied_against_the_reference_position(chain, monkeypatch):
    from ui.panels.remux_panel.widgets import sync_studio_dialog as mod

    class _Shifted(_Studio):
        def result_calibration(self):
            changed = SyncCalibration.linear(self.calibration.segments[0].shift_ms + 40)
            return changed, round(changed.segments[0].shift_ms)

    panel, tracks, _logs = chain
    monkeypatch.setattr(mod, "SyncStudioDialog", _Shifted)
    monkeypatch.setattr(panel, "_audio_sync_reference_choices", lambda _entry: [("B", tracks["B"][0])])
    _sync(panel, tracks, "B", "A", -5466)
    _sync(panel, tracks, "C", "B", -100)
    panel._on_sync_studio_requested(tracks["C"][0])
    assert _shifts(tracks)["C"] == [-5526, -5526]
    assert panel._sync_links["C"].relative.segments[0].shift_ms == -60


def test_profile_offsets_are_all_kept(chain):
    """Audit P2 : B suit A (+50) ; le profil fixe A à +100 et B à +200 : B garde +200."""
    import copy

    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", 50)
    edited = copy.deepcopy(panel._track_table.current_tracks())
    by_id = {track.entry_id: track for track in edited}
    by_id[tracks["A"][0].entry_id].time_shift_ms = 100
    by_id[tracks["B"][0].entry_id].time_shift_ms = 200
    panel._commit_decision_profile_result(edited)
    current = {track.entry_id: track.time_shift_ms for track in panel._track_table.current_tracks()}
    assert (current[tracks["A"][0].entry_id], current[tracks["B"][0].entry_id]) == (100, 200)
    assert "B" not in panel._sync_links


def test_profile_write_with_an_unchanged_value_is_kept(chain):
    """Audit : B suit A (+50) ; le profil fixe A à +100 et B à +50 (même valeur) : B reste à +50."""
    import copy

    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", 50)
    edited = copy.deepcopy(panel._track_table.current_tracks())
    next(track for track in edited if track.entry_id == tracks["A"][0].entry_id).time_shift_ms = 100
    written = {tracks["A"][0].entry_id: ["time_shift_ms"], tracks["B"][0].entry_id: ["time_shift_ms"]}
    panel._commit_decision_profile_result(edited, {"written_fields": written})
    current = {track.entry_id: track.time_shift_ms for track in panel._track_table.current_tracks()}
    assert (current[tracks["A"][0].entry_id], current[tracks["B"][0].entry_id]) == (100, 50)
    assert "B" not in panel._sync_links


def test_entered_offset_moves_a_calibration_with_cuts(chain):
    """Audit : saisie du départ d'une source à coupures : la calibration effective suit, ses dépendantes aussi."""
    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", 0, SyncCalibration((SyncSegment(0, -5000), SyncSegment(60000, -6000))))
    _sync(panel, tracks, "C", "B", 100)
    tracks["B"][0].time_shift_ms = -4000
    panel._track_table.time_shift_edited.emit(tracks["B"][0])
    stored = SyncCalibration.from_dict(panel._workflow_options["sync_calibrations"]["1"])
    assert [(s.start_ms, s.shift_ms) for s in stored.segments] == [(0, -4000), (60000, -5000)]
    assert panel._track_calibration(tracks["B"][0]).segments[0].shift_ms == -4000
    assert _shifts(tracks)["B"] == [-4000, -4000]
    dependent = SyncCalibration.from_dict(panel._workflow_options["sync_calibrations"]["2"])
    assert [(s.start_ms, s.shift_ms) for s in dependent.segments] == [(0, -3900), (59900, -4900)]


def test_cadence_method_change_reaches_dependents(chain):
    """Audit P3 : la référence passe de atempo à asetrate ; ses dépendantes suivent."""
    from core.workflows.cadence import CadenceMismatch, CadenceType

    pal = CadenceMismatch(CadenceType.PAL_TO_FILM_23976, 25.0, 23.976, 24000.0 / 25025.0, speed_ratio="24000/25025")
    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", 0, SyncCalibration((SyncSegment(0, 0),), cadence_mismatch=pal,
                                                      cadence_audio_method="atempo"))
    _sync(panel, tracks, "C", "B", 100)
    assert panel._workflow_options["sync_calibrations"]["2"]["cadence_audio_method"] == "atempo"
    _sync(panel, tracks, "B", "A", 0, SyncCalibration((SyncSegment(0, 0),), cadence_mismatch=pal,
                                                      cadence_audio_method="asetrate"))
    assert panel._workflow_options["sync_calibrations"]["2"]["cadence_audio_method"] == "asetrate"


def test_manual_edit_of_a_reference_moves_dependents_and_unlinks_it(chain):
    panel, tracks, logs = chain
    _sync(panel, tracks, "B", "A", -5466)
    _sync(panel, tracks, "C", "B", -100)
    tracks["B"][0].time_shift_ms = -5000
    panel._track_table.time_shift_edited.emit(tracks["B"][0])
    assert "B" not in panel._sync_links
    assert _shifts(tracks)["C"] == [-5100, -5100]
    assert any("#1" in text and "B.mkv" in text for level, text in logs if level == "INFO")
    # Édition manuelle de la cible : elle ne suit plus sa référence.
    tracks["C"][0].time_shift_ms = 0
    panel._track_table.time_shift_edited.emit(tracks["C"][0])
    _sync(panel, tracks, "B", "A", -4000)
    assert "C" not in panel._sync_links and tracks["C"][0].time_shift_ms == 0


def test_video_offset_edit_keeps_the_source_link(chain):
    panel, tracks, _logs = chain
    _sync(panel, tracks, "C", "B", -100)
    video = _track(0, "video", file_id="C")
    video.time_shift_ms = 40
    panel._track_table.time_shift_edited.emit(video)
    assert "C" in panel._sync_links


def test_profile_offset_rule_acts_like_an_entered_offset(chain):
    import copy

    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", -5466)
    _sync(panel, tracks, "C", "B", -100)
    edited = copy.deepcopy(panel._track_table.current_tracks())
    next(track for track in edited if track.entry_id == tracks["B"][0].entry_id).time_shift_ms = -5000
    panel._commit_decision_profile_result(edited)
    current = {track.entry_id: track for track in panel._track_table.current_tracks()}
    assert "B" not in panel._sync_links
    assert [current[track.entry_id].time_shift_ms for track in tracks["C"]] == [-5100, -5100]


def test_removed_reference_leaves_dependents_unchanged(chain):
    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", -5466)
    _sync(panel, tracks, "C", "B", -100)
    inspection_functions.on_remove_file(panel, "B")
    assert panel._sync_links == {} and _shifts(tracks)["C"] == [-5566, -5566]


def test_proposal_shows_the_offset_that_will_be_applied(chain):
    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", -5466)
    shown = panel._proposed_sync(tracks["C"][0], tracks["B"][0], SyncCalibration.linear(-100))
    assert [s.shift_ms for s in shown.segments] == [-5566]
    # Boucle : la référence sera libérée, la mesure s'applique telle quelle.
    looped = panel._proposed_sync(tracks["A"][0], tracks["B"][0], SyncCalibration.linear(5466))
    assert [s.shift_ms for s in looped.segments] == [5466]


def test_global_cancel_clears_links(chain):
    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", -5466)
    _sync(panel, tracks, "C", "B", -100)
    panel._on_global_sync_cancel_requested()
    assert panel._sync_links == {}


def test_session_round_trips_links(chain):
    panel, tracks, _logs = chain
    _sync(panel, tracks, "B", "A", -5466)
    _sync(panel, tracks, "C", "B", -100)
    session = workflow_functions._session_job(panel)["_muxiveo_session"]
    assert set(session["sync_links"]) == {"1", "2"}
    config = SimpleNamespace(sources=[SimpleNamespace(tracks=entries) for entries in tracks.values()])
    restored = workflow_functions._restored_sync_links(panel, config, session["sync_links"])
    assert restored == panel._sync_links
    # Sessions antérieures : pas de lien, synchros conservées telles quelles.
    assert workflow_functions._restored_sync_links(panel, config, None) == {}
