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


@pytest.mark.parametrize("pitch", [1.0, 0.96])
def test_imported_physical_offset_is_preserved_for_studio_and_chapters(chain, pitch):
    panel, tracks, _logs = chain
    calibration = SyncCalibration((SyncSegment(0, 2000),), cadence_pitch_factor=pitch)
    panel._workflow_options["sync_calibrations"] = {"1": calibration.to_dict()}
    panel._sync_entry_calibrations()
    audio, subtitle = tracks["B"]
    assert audio.time_shift_ms == subtitle.time_shift_ms == 0
    assert panel._track_calibration(audio) == calibration
    assert panel._track_calibration(subtitle) == SyncCalibration.linear(2000)
    assert panel._source_calibration("B") == calibration
    assert panel._chapter_sync_calibration_for_index(1) == SyncCalibration.linear(2000)


def test_pitch_only_does_not_create_a_chapter_transformation(chain):
    panel, _tracks, _logs = chain
    panel._workflow_options["sync_calibrations"] = {
        "1": SyncCalibration((SyncSegment(0, 0),), cadence_pitch_factor=0.96).to_dict(),
    }
    assert panel._chapter_sync_calibration_for_index(1) is None


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


def test_declined_cadence_is_offered_unchecked_at_reopening(chain, monkeypatch):
    """Conversion décochée dans le studio : toujours proposée, plus recochée d'office (session comprise)."""
    from tests.test_remux import _video
    from ui.panels.remux_panel.widgets import sync_studio_dialog as mod

    panel, tracks, logs = chain
    panel._source_files[0].info.video_tracks = [_video(0, frame_rate="24000/1001")]
    panel._source_files[2].info.video_tracks = [_video(0, frame_rate="25/1")]
    opened: list[dict] = []

    class _Declining(_Studio):
        cadence_declined = True

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            opened.append(kwargs)

    monkeypatch.setattr(mod, "SyncStudioDialog", _Declining)
    monkeypatch.setattr(panel, "_audio_sync_reference_choices", lambda _entry: [("A", tracks["A"][0])])
    panel._on_sync_studio_requested(tracks["C"][0])
    assert opened[0]["cadence_hint"].speed_ratio == "24000/25025" and opened[0]["apply_cadence_hint"]
    assert panel._cadence_declined_source_ids == {"C"} and _shifts(tracks)["C"] == [0, 0]
    assert ("INFO", translate_text("Synchro Studio : aucune modification, synchro inchangée.")) in logs
    panel._on_sync_studio_requested(tracks["C"][0])
    assert opened[1]["cadence_hint"] is not None and not opened[1]["apply_cadence_hint"]

    session = workflow_functions._session_job(panel)["_muxiveo_session"]
    assert session["cadence_declined_source_indices"] == [2]
    # Analyse automatique avec conversion : le refus antérieur ne vaut plus.
    pal = opened[0]["cadence_hint"]
    _sync(panel, tracks, "C", "A", 300, SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal))
    assert panel._cadence_declined_source_ids == set()


def _capture_cadence_analyses(panel, monkeypatch):
    """Analyses de hauteur du panneau : tâches mémorisées (lancées par le test) ; méthode retenue
    asetrate face à la source A, atempo face aux autres."""
    from dataclasses import replace
    from core.workflows.audio_sync_scan import AudioSyncScanner

    submitted: list = []
    monkeypatch.setattr(panel._executor, "submit", lambda fn, *args: submitted.append(fn))

    def resolve(_self, reference, _target, calibration, log=None):
        method = "asetrate" if reference.source_path.name == "A.mkv" else "atempo"
        return replace(calibration, cadence_audio_method=method, cadence_method_auto=True)

    monkeypatch.setattr(AudioSyncScanner, "resolve_cadence_method", resolve)
    return submitted


def _run_analysis(task) -> None:
    from PySide6.QtCore import QCoreApplication
    task()
    QCoreApplication.processEvents()


def _auto_cadence(offset_ms: float) -> SyncCalibration:
    from core.workflows.cadence import detect_cadence_from_metadata
    return SyncCalibration((SyncSegment(0, offset_ms),), cadence_mismatch=detect_cadence_from_metadata("24000/1001", "25/1"),
                           cadence_audio_method="auto")


def test_stale_cadence_method_analysis_is_relaunched(chain, monkeypatch):
    """Synchro modifiée pendant l'analyse de hauteur : résultat écarté, analyse relancée sur la synchro actuelle."""
    from dataclasses import replace

    panel, tracks, _logs = chain
    submitted = _capture_cadence_analyses(panel, monkeypatch)
    _sync(panel, tracks, "C", "A", 300, _auto_cadence(300.0))
    assert len(submitted) == 1
    link = panel._sync_links["C"]
    panel._sync_links["C"] = replace(link, relative=link.relative.with_start_offset(450))
    _run_analysis(submitted.pop())
    # Analyse faite sur l'ancien décalage : rien appliqué, analyse relancée.
    assert panel._source_calibration("C").cadence_audio_method == "auto" and len(submitted) == 1
    _run_analysis(submitted.pop())
    assert panel._sync_links["C"].relative.cadence_audio_method == "asetrate"
    assert panel._source_calibration("C").cadence_audio_method == "asetrate"
    assert not panel.cadence_resolution_pending()


def test_late_analysis_against_a_former_reference_is_rejected(chain, monkeypatch):
    """C analysée face à A, synchros annulées, C resynchronisée face à B (même décalage, même
    cadence) : le résultat tardif de A est écarté, celui de B s'applique."""
    panel, tracks, _logs = chain
    submitted = _capture_cadence_analyses(panel, monkeypatch)
    _sync(panel, tracks, "C", "A", 300, _auto_cadence(300.0))
    panel._on_global_sync_cancel_requested()
    _sync(panel, tracks, "C", "B", 300, _auto_cadence(300.0))
    against_a, against_b = submitted
    _run_analysis(against_a)
    assert panel._source_calibration("C").cadence_audio_method == "auto" and panel.cadence_resolution_pending()
    _run_analysis(against_b)
    assert panel._source_calibration("C").cadence_audio_method == "atempo"
    assert not panel.cadence_resolution_pending()


def test_new_analysis_of_a_source_supersedes_the_previous_one(chain, monkeypatch):
    """Resynchronisation face à une autre référence sans annulation : l'analyse précédente est remplacée."""
    panel, tracks, _logs = chain
    submitted = _capture_cadence_analyses(panel, monkeypatch)
    _sync(panel, tracks, "C", "A", 300, _auto_cadence(300.0))
    _sync(panel, tracks, "C", "B", 300, _auto_cadence(300.0))
    against_a, against_b = submitted
    _run_analysis(against_b)
    _run_analysis(against_a)
    assert panel._source_calibration("C").cadence_audio_method == "atempo"
    assert not panel.cadence_resolution_pending()


def test_pending_cadence_analysis_holds_the_media_operation(chain, monkeypatch):
    """Méthode « auto » en cours d'analyse : le traitement attend ; un échec retient atempo, signalé."""
    from PySide6.QtCore import QCoreApplication
    from core.workflows.audio_sync_scan import AudioSyncScanner
    from core.workflows.cadence import detect_cadence_from_metadata

    panel, tracks, logs = chain
    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    submitted: list = []

    def fail(*_args, **_kwargs):
        raise RuntimeError("ffprobe absent")

    monkeypatch.setattr(panel._executor, "submit", lambda fn, *args: submitted.append(fn))
    monkeypatch.setattr(AudioSyncScanner, "resolve_cadence_method", fail)
    ready: list[bool] = []
    panel.ready_changed.connect(ready.append)
    # Synchro appliquée avec une méthode « auto » non tranchée : analyse planifiée d'office.
    _sync(panel, tracks, "C", "A", 300, SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal,
                                                       cadence_audio_method="auto"))
    assert panel.cadence_resolution_pending() and ready and len(submitted) == 1
    submitted.pop()()
    QCoreApplication.processEvents()
    assert not panel.cadence_resolution_pending()
    applied = panel._source_calibration("C")
    assert applied.cadence_audio_method == "atempo" and applied.cadence_method_requested == "auto"
    assert any(level == "WARN" and "ffprobe absent" in text for level, text in logs)


def test_only_the_latest_analysis_of_a_source_is_awaited(chain, monkeypatch):
    panel, tracks, _logs = chain
    submitted = _capture_cadence_analyses(panel, monkeypatch)
    _sync(panel, tracks, "C", "A", 300, _auto_cadence(300.0))
    panel._schedule_cadence_method(tracks["C"][0], tracks["A"][0])
    first, latest = submitted
    _run_analysis(first)
    # Remplacée : son résultat ne lève pas l'attente de la plus récente, ni ne s'applique.
    assert panel.cadence_resolution_pending()
    assert panel._source_calibration("C").cadence_audio_method == "auto"
    _run_analysis(latest)
    assert not panel.cadence_resolution_pending()
    assert panel._source_calibration("C").cadence_audio_method == "asetrate"


def test_applied_sync_comparison_includes_the_requested_mode():
    from core.workflows.cadence import detect_cadence_from_metadata
    from ui.panels.remux_panel.panel import _same_applied_sync

    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    explicit = SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal, cadence_audio_method="asetrate")
    auto = SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal, cadence_audio_method="asetrate",
                           cadence_method_auto=True)
    assert not _same_applied_sync(explicit, auto)
    cuts = (SyncSegment(0, 0.0), SyncSegment(1000, 500.0))
    # Sans conversion, le mode n'a pas d'effet.
    assert _same_applied_sync(SyncCalibration(cuts), SyncCalibration(cuts, cadence_method_auto=True))


@pytest.mark.parametrize("removed", ["C", "A"])
def test_removing_a_source_drops_its_cadence_analyses(chain, monkeypatch, removed):
    """Source de la piste ou de sa référence retirée : son analyse ne bloque plus le traitement média."""
    from core.workflows.cadence import detect_cadence_from_metadata

    panel, tracks, _logs = chain
    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    monkeypatch.setattr(panel._executor, "submit", lambda fn, *args: None)
    _sync(panel, tracks, "C", "A", 300, SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal,
                                                       cadence_audio_method="auto"))
    assert panel.cadence_resolution_pending()
    inspection_functions.on_remove_file(panel, removed)
    assert not panel.cadence_resolution_pending()


def test_global_cancel_drops_cadence_analyses(chain, monkeypatch):
    from core.workflows.cadence import detect_cadence_from_metadata

    panel, tracks, _logs = chain
    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    monkeypatch.setattr(panel._executor, "submit", lambda fn, *args: None)
    _sync(panel, tracks, "C", "A", 300, SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal,
                                                       cadence_audio_method="auto"))
    assert panel.cadence_resolution_pending()
    panel._on_global_sync_cancel_requested()
    assert not panel.cadence_resolution_pending()


def test_restored_session_resumes_an_unfinished_cadence_analysis(chain, monkeypatch, tmp_path):
    """Sauvegarde pendant l'analyse d'une méthode « auto » : la restauration la relance et le
    traitement l'attend (sinon atempo implicite, hauteur fausse conservée)."""
    from ui.panels.remux_panel.functions import config_builder

    panel, tracks, _logs = chain
    submitted = _capture_cadence_analyses(panel, monkeypatch)
    _sync(panel, tracks, "C", "A", 300, _auto_cadence(300.0))
    assert panel.cadence_resolution_pending()
    config = config_builder.current_config(panel, output_fallback=str(tmp_path / "out.mkv"))
    session = workflow_functions._session_job(panel)["_muxiveo_session"]
    infos = [source.info for source in panel._source_files]

    resumed = RemuxPanel(AppConfig())
    try:
        resumed_submitted = _capture_cadence_analyses(resumed, monkeypatch)
        workflow_functions.restore(resumed, config, infos, session)
        target = resumed._source_files[2].id
        assert resumed._sync_links[target].relative.cadence_audio_method == "auto"
        assert resumed.cadence_resolution_pending() and len(resumed_submitted) == 1
        _run_analysis(resumed_submitted.pop())
        assert resumed._source_calibration(target).cadence_audio_method == "asetrate"
        assert not resumed.cadence_resolution_pending()
    finally:
        resumed.close()
    assert len(submitted) == 1


def test_resolved_method_recomposes_the_pitch_of_a_chained_reference(chain, monkeypatch):
    """A suit B en asetrate (23,976 → 25), C mesurée face à A en « auto » : avant l'analyse, la
    hauteur de A reste appliquée à C ; tranchée en asetrate, les deux hauteurs s'annulent."""
    panel, tracks, _logs = chain
    submitted = _capture_cadence_analyses(panel, monkeypatch)
    pal = _auto_cadence(0.0).cadence_mismatch
    _sync(panel, tracks, "A", "B", 0, SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=pal.inverse(),
                                                      cadence_audio_method="asetrate"))
    _sync(panel, tracks, "C", "A", 0, _auto_cadence(0.0))
    pending = panel._source_calibration("C")
    assert pending.cadence_mismatch is None
    assert pending.audio_pitch_factor == pytest.approx(pal.inverse().speed_factor)
    _run_analysis(submitted.pop())
    assert panel._sync_links["C"].relative.cadence_audio_method == "asetrate"
    assert not panel._source_calibration("C").converts_audio


def test_cancelled_source_sync_no_longer_awaits_its_cadence_analysis(chain, monkeypatch):
    """Synchro à conversion « auto » annulée : le traitement n'attend plus l'analyse de sa hauteur."""
    panel, tracks, _logs = chain
    submitted = _capture_cadence_analyses(panel, monkeypatch)
    _sync(panel, tracks, "C", "A", 300, _auto_cadence(300.0))
    assert panel.cadence_resolution_pending() and len(submitted) == 1
    panel._cancel_source_calibration(tracks["C"][0], panel._source_index_for_file_id("C"))
    assert not panel.cadence_resolution_pending()
    _run_analysis(submitted.pop())
    assert panel._source_calibration("C").cadence_mismatch is None


def test_reference_cadence_does_not_clear_a_declined_conversion(chain):
    """Conversion refusée pour C, C remesurée sans conversion face à B (elle-même convertie) :
    la conversion composée de B ne lève pas le refus de C."""
    panel, tracks, _logs = chain
    pal = _auto_cadence(0.0).cadence_mismatch
    _sync(panel, tracks, "B", "A", 0, SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=pal,
                                                      cadence_audio_method="atempo"))
    panel._cadence_declined_source_ids.add("C")
    _sync(panel, tracks, "C", "B", -120)
    assert panel._source_calibration("C").cadence_mismatch is not None
    assert "C" in panel._cadence_declined_source_ids
    _sync(panel, tracks, "C", "A", 0, SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=pal,
                                                      cadence_audio_method="atempo"))
    assert "C" not in panel._cadence_declined_source_ids


def test_conflicting_profile_offsets_on_a_calibrated_source_keep_the_audio_one(chain):
    """Source C calibrée (cadence) ; le profil fixe le sous-titre à +500 puis l'audio à +200 :
    la calibration commune suit l'audio, la valeur ignorée est signalée."""
    import copy

    panel, tracks, logs = chain
    _sync(panel, tracks, "C", "A", 0, SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=_auto_cadence(0.0)
                                                      .cadence_mismatch, cadence_audio_method="atempo"))
    edited = copy.deepcopy(panel._track_table.current_tracks())
    by_id = {track.entry_id: track for track in edited}
    by_id[tracks["C"][1].entry_id].time_shift_ms = 500
    by_id[tracks["C"][0].entry_id].time_shift_ms = 200
    panel._commit_decision_profile_result([by_id[tracks["C"][1].entry_id], *[t for t in edited
                                           if t.entry_id != tracks["C"][1].entry_id]])
    assert round(panel._source_calibration("C").segments[0].shift_ms) == 200
    assert any(level == "WARN" and "+500" in text for level, text in logs)
