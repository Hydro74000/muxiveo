"""Synchronisations importées : retouches individuelles et héritage des sous-titres."""
from __future__ import annotations

import copy

import pytest

from core.i18n import translate_text
from core.workflows.cadence import detect_cadence_from_metadata
from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings
from core.workflows.encode.remux_bridge import merge_remux_into_encode_config
from core.workflows.physical_sync import preparation_commands
from core.workflows.remux_mapping import resolve_mapped_tracks
from core.workflows.sync_calibration import SyncCalibration, SyncSegment
from tests.test_remux_sync_links import _sync, chain as chain
from ui.panels.remux_panel.functions import workflow


def _restore(chain, tmp_path, calibration, *, mode="physical", mirror="mirror", subtitle_offset=0):
    panel, _tracks, _logs = chain
    panel._output_edit.setText(str(tmp_path / "output.mkv"))
    config = panel.collect_config()
    config.sync_mode = mode
    config.sync_subtitles = mirror
    config.sync_calibrations = {"1": calibration.to_dict()}
    config.sources[1].tracks[1].time_shift_ms = subtitle_offset
    workflow.restore(panel, config, [source.info for source in panel._source_files])
    displayed = panel._track_table.current_tracks()
    assert len(displayed) == len({track.entry_id for track in displayed}) == 6
    tracks = panel.collect_config().sources[1].tracks
    assert len(tracks) == 2
    return panel, tracks


@pytest.mark.parametrize("mode", ["container", "physical"])
@pytest.mark.parametrize("track_type,offset", [("audio", 3000), ("audio", 0), ("subtitle", -3000)])
def test_entered_offset_overrides_imported_linear_calibration(chain, tmp_path, mode, track_type, offset):
    panel, tracks = _restore(chain, tmp_path, SyncCalibration.linear(2000), mode=mode)
    edited = next(track for track in tracks if track.track_type == track_type)
    edited.time_shift_ms = offset
    panel._track_table.time_shift_edited.emit(edited)
    config = panel.collect_config()
    assert panel._track_calibration(edited) == SyncCalibration.linear(offset)
    mapped = resolve_mapped_tracks(config)
    if mode == "physical":
        transformed = {item.track.entry_id: calibration.segments[0].shift_ms
                       for item, _path, _command, calibration in preparation_commands(config, tmp_path, "ffmpeg")}
        actual = {item.track.entry_id: transformed.get(item.track.entry_id, item.track.time_shift_ms)
                  for item in mapped}
    else:
        actual = {item.track.entry_id: item.track.time_shift_ms for item in mapped}
    assert actual[edited.entry_id] == offset
    other = next(track for track in tracks if track is not edited)
    assert actual[other.entry_id] == 2000
    assert panel._chapter_sync_offset_ms_for_index(1) == 2000
    # Les lectures et rafraîchissements suivants ne doivent pas rétablir l'ancien payload.
    panel._sync_entry_calibrations()
    assert panel._track_calibration(edited) == SyncCalibration.linear(offset)
    exported = workflow._session_job(panel)
    assert not exported.get("sync_calibrations")
    workflow.restore(panel, panel.collect_config(), [source.info for source in panel._source_files],
                     exported["_muxiveo_session"])
    restored = next(track for track in panel._track_table.current_tracks() if track.entry_id == edited.entry_id)
    assert panel._track_calibration(restored) == SyncCalibration.linear(offset)
    assert panel._chapter_sync_offset_ms_for_index(1) == 2000


def test_profile_keeps_distinct_offsets_after_imported_linear_calibration(chain, tmp_path):
    panel, tracks = _restore(chain, tmp_path, SyncCalibration.linear(2000))
    changed = copy.deepcopy(panel._track_table.current_tracks())
    expected = {tracks[0].entry_id: 3000, tracks[1].entry_id: -500}
    for track in changed:
        if track.entry_id in expected:
            track.time_shift_ms = expected[track.entry_id]
    panel._commit_decision_profile_result(changed)
    assert {track.entry_id: panel._track_calibration(track).segments[0].shift_ms
            for track in panel._track_table.current_tracks() if track.entry_id in expected} == expected


def test_edit_dialog_can_clear_imported_linear_offset(chain, tmp_path, monkeypatch):
    from ui.panels.track_edit_dialog import TrackEditDialog

    panel, (audio, subtitle) = _restore(chain, tmp_path, SyncCalibration.linear(2000))
    audio.language = "fr-FR"
    row = panel._track_table._find_row_for_entry(audio)
    panel._track_table.item(row, panel._track_table.COL_LANG).setText(audio.language)

    def edit(dialog):
        assert dialog._offset_edit.text() == "2000"
        dialog._offset_edit.setText("0")
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(TrackEditDialog, "exec", edit)
    panel._track_table._open_edit_dialog(audio)
    assert panel._track_calibration(audio) == SyncCalibration.linear(0)
    assert panel._track_calibration(subtitle) == SyncCalibration.linear(2000)


def test_reference_edit_propagates_after_imported_linear_calibration(chain):
    panel, tracks, _logs = chain
    panel._workflow_options["sync_calibrations"] = {"1": SyncCalibration.linear(2000).to_dict()}
    panel._sync_entry_calibrations()
    _sync(panel, tracks, "C", "B", 100)
    assert tracks["C"][0].time_shift_ms == 2100
    tracks["B"][0].time_shift_ms = 3000
    panel._track_table.time_shift_edited.emit(tracks["B"][0])
    assert tracks["C"][0].time_shift_ms == 3100
    assert tracks["B"][1].time_shift_ms == 2000


@pytest.mark.parametrize("calibration", [
    SyncCalibration.linear(2000),
    SyncCalibration((SyncSegment(0, 2000),), cadence_pitch_factor=0.96),
    SyncCalibration((SyncSegment(0, 2000), SyncSegment(10000, 2400))),
    SyncCalibration((SyncSegment(0, 2000),),
                    cadence_mismatch=detect_cadence_from_metadata("24000/1001", "25/1")),
])
@pytest.mark.parametrize("subtitle_offset", [0, 500])
def test_imported_source_sync_does_not_reach_subtitles_without_mirror(chain, tmp_path, calibration, subtitle_offset):
    panel, tracks = _restore(chain, tmp_path, calibration, mirror="none", subtitle_offset=subtitle_offset)
    config = panel.collect_config()
    audio, subtitle = tracks
    assert panel._track_calibration(audio) == calibration
    assert panel._track_calibration(subtitle) == SyncCalibration.linear(subtitle_offset)
    prepared = preparation_commands(config, tmp_path, "ffmpeg")
    assert any(item.track.is_audio for item, *_rest in prepared)
    sub_prepared = [applied for item, _path, _command, applied in prepared if item.track.is_subtitle]
    assert sub_prepared == ([SyncCalibration.linear(subtitle_offset)] if subtitle_offset else [])
    encode = merge_remux_into_encode_config(
        EncodeConfig(config.sources[0].path, config.output, video=VideoEncodeSettings()), config)
    offsets = [offset for offset in encode.track_time_offsets if offset.track_type == "subtitle"]
    assert [(offset.offset_ms, offset.calibration) for offset in offsets] == (
        [(subtitle_offset, None)] if subtitle_offset else [])
    # Déplacer la synchro audio dans le panneau respecte également l'option importée.
    panel._set_source_calibration(audio.file_id, calibration.with_start_offset(3000))
    assert panel._track_calibration(subtitle) == SyncCalibration.linear(subtitle_offset)
    panel._on_auto_sync_cancel_requested(audio)
    assert panel._track_calibration(subtitle) == SyncCalibration.linear(subtitle_offset)


@pytest.mark.parametrize("calibration", [
    SyncCalibration.linear(2000),
    SyncCalibration((SyncSegment(0, 2000),), cadence_pitch_factor=0.96),
    SyncCalibration((SyncSegment(0, 2000), SyncSegment(10000, 2400))),
])
def test_cancelling_independent_subtitle_offset_keeps_audio_calibration(chain, tmp_path, calibration):
    panel, (audio, subtitle) = _restore(chain, tmp_path, calibration, mirror="none", subtitle_offset=500)
    panel._on_auto_sync_cancel_requested(subtitle)
    assert panel._track_calibration(subtitle) == SyncCalibration.linear(0)
    assert panel._track_calibration(audio) == calibration
    assert panel._source_calibration(audio.file_id) == calibration


def test_cancelling_imported_linear_audio_offset_preserves_other_tracks(chain, tmp_path):
    panel, (audio, subtitle) = _restore(chain, tmp_path, SyncCalibration.linear(2000))
    panel._on_auto_sync_cancel_requested(audio)
    assert panel._track_calibration(audio) == SyncCalibration.linear(0)
    assert panel._track_calibration(subtitle) == SyncCalibration.linear(2000)


_OWN_CUTS = SyncCalibration((SyncSegment(0, 0.0), SyncSegment(60_000, -1000.0)))


def _restore_own(chain, tmp_path, track_type, own, *, mirror="mirror", source_calibration=None, mode="physical"):
    """Restaure un exact-job dont une piste de B porte une calibration propre."""
    panel, _tracks, _logs = chain
    panel._output_edit.setText(str(tmp_path / "output.mkv"))
    config = panel.collect_config()
    config.sync_mode = mode
    config.sync_subtitles = mirror
    config.sync_calibrations = {"1": source_calibration.to_dict()} if source_calibration else {}
    next(track for track in config.sources[1].tracks if track.track_type == track_type).sync_calibration = own.to_dict()
    workflow.restore(panel, config, [source.info for source in panel._source_files])
    tracks = panel.collect_config().sources[1].tracks
    edited = next(track for track in tracks if track.track_type == track_type)
    return panel, edited, next(track for track in tracks if track is not edited)


@pytest.mark.parametrize("source_calibration", [
    None, SyncCalibration.linear(2000), SyncCalibration((SyncSegment(0, 2000), SyncSegment(10000, 2400))),
])
@pytest.mark.parametrize("track_type", ["audio", "subtitle"])
def test_imported_own_track_calibration_survives_restore(chain, tmp_path, track_type, source_calibration):
    """Calibration propre d'une piste qui suit normalement sa source : conservée, exportée, appliquée."""
    panel, edited, other = _restore_own(chain, tmp_path, track_type, _OWN_CUTS, source_calibration=source_calibration)
    assert panel._track_calibration(edited) == _OWN_CUTS
    assert panel._track_calibration(other) == (source_calibration or SyncCalibration.linear(0))
    panel._on_table_changed()
    panel._sync_entry_calibrations()
    assert panel._track_calibration(edited) == _OWN_CUTS
    payloads = {item["selector"]["id"]: item for item in workflow._session_job(panel)["tracks"]
                if item["selector"]["source"] == 1}
    assert payloads[edited.mkv_tid]["sync_calibration"] == _OWN_CUTS.to_dict()
    # La synchro de la source change : ses autres pistes suivent, la synchro propre reste.
    panel._set_source_calibration(edited.file_id, SyncCalibration.linear(500))
    assert panel._track_calibration(edited) == _OWN_CUTS
    assert panel._track_calibration(other) == SyncCalibration.linear(500)
    prepared = {item.track.entry_id: applied
                for item, _path, _command, applied in preparation_commands(panel.collect_config(), tmp_path, "ffmpeg")}
    assert prepared[edited.entry_id].segments == _OWN_CUTS.segments
    # Annulation de la piste : elle suit de nouveau sa source.
    panel._on_auto_sync_cancel_requested(edited)
    assert panel._track_calibration(edited) == SyncCalibration.linear(500)
    panel._set_source_calibration(edited.file_id, SyncCalibration.linear(700))
    assert panel._track_calibration(edited) == SyncCalibration.linear(700)


@pytest.mark.parametrize("track_type,mirror", [("subtitle", "none"), ("audio", "mirror")])
def test_entered_offset_replaces_imported_linear_own_calibration(chain, tmp_path, track_type, mirror):
    """Calibration propre linéaire importée : la valeur saisie est celle appliquée (Remux et Encode)."""
    panel, edited, _other = _restore_own(chain, tmp_path, track_type, SyncCalibration.linear(1200),
                                         mirror=mirror, mode="container")
    assert edited.time_shift_ms == 1200
    for offset in (600, 0):
        edited.time_shift_ms = offset
        panel._track_table.time_shift_edited.emit(edited)
        config = panel.collect_config()
        mapped = {item.track.entry_id: item.track.time_shift_ms for item in resolve_mapped_tracks(config)}
        assert mapped[edited.entry_id] == offset
        if track_type != "subtitle":
            continue
        # Pont Encode (sous-titres copiés ; l'audio n'y passe qu'avec ses réglages d'encodage).
        encode = merge_remux_into_encode_config(
            EncodeConfig(config.sources[0].path, config.output, video=VideoEncodeSettings()), config)
        offsets = [item.offset_ms for item in encode.track_time_offsets
                   if item.track_type == "subtitle" and item.source_path == config.sources[1].path]
        assert offsets == ([offset] if offset else [])


def test_zero_start_own_calibration_stays_cancelable_after_table_edit(chain):
    """Coupures sans décalage de départ : l'annulation individuelle survit aux éditions et au réordonnancement."""
    panel, tracks, _logs = chain
    panel._workflow_options["sync_subtitles"] = "none"
    subtitle = tracks["B"][1]
    panel._on_subtitle_sync_done(subtitle.entry_id, tracks["A"][1].entry_id, 0, .95, _OWN_CUTS)
    assert subtitle.time_shift_ms == 0 and subtitle.sync_calibration is not None
    assert panel._track_table._has_cancelable_auto_sync(subtitle)
    panel._on_table_changed()
    assert panel._track_table._has_cancelable_auto_sync(subtitle)
    panel._on_track_order_changed()
    assert panel._track_table._has_cancelable_auto_sync(subtitle)


def test_save_workflow_preserves_sync_links(chain, tmp_path, monkeypatch):
    """« Sauvegarder le workflow… » : les références en chaîne survivent au rechargement."""
    from pathlib import Path

    from cli.inspection import FileInspector
    from cli.logging import Logger
    from cli.options import CommonOptions
    from cli.remux_config import build_remux_config
    from core.workflows.workflow_store import load_workflow, save_workflow

    panel, tracks, _logs = chain
    for _audio_track, subtitle in tracks.values():
        subtitle.codec = subtitle.orig_codec = "SUBRIP"  # Codec de l'inspection simulée : sélecteurs résolus.
    _sync(panel, tracks, "B", "A", 1000)
    _sync(panel, tracks, "C", "B", 100)
    panel._output_edit.setText(str(tmp_path / "output.mkv"))
    path = tmp_path / "saved.exact-job.json"
    save_workflow(path, panel._current_exact_job())
    job = load_workflow(path)
    # Sélecteurs descriptifs : le document reste réutilisable comme template CLI.
    assert all(set(item["selector"]) != {"source", "type", "id"} for item in job["tracks"])
    infos = {source.path.resolve(): source.info for source in panel._source_files}
    monkeypatch.setattr(FileInspector, "inspect", lambda _self, source, *_args, **_kwargs: infos[Path(source).resolve()])
    config = build_remux_config(job, panel._config, CommonOptions(), Logger())
    workflow.restore(panel, config, [infos[source.path.resolve()] for source in config.sources],
                     job["_muxiveo_session"])
    b_id, c_id = (source.id for source in panel._source_files[1:])
    restored = panel._track_table.current_tracks()
    b_audio = next(track for track in restored if track.file_id == b_id and track.is_audio)
    c_audio = next(track for track in restored if track.file_id == c_id and track.is_audio)
    assert c_audio.time_shift_ms == 1100
    b_audio.time_shift_ms = 2000
    panel._track_table.time_shift_edited.emit(b_audio)
    assert panel._track_calibration(c_audio) == SyncCalibration.linear(2100)


def _own_reference(chain, tmp_path):
    """Audio de B à synchro propre importée (+1 200 ms) ; C synchronisée face à elle à +100 ms."""
    panel, own, _other = _restore_own(chain, tmp_path, "audio", SyncCalibration.linear(1200), mode="container")
    tracks = panel._track_table.current_tracks()
    a_audio, c_audio = (next(track for track in tracks if track.file_id == source.id and track.is_audio)
                        for source in (panel._source_files[0], panel._source_files[2]))
    panel._on_audio_sync_done(c_audio.entry_id, own.entry_id, 100, .95, None)
    assert panel._track_calibration(c_audio) == SyncCalibration.linear(1300)
    return panel, own, a_audio, c_audio


def test_own_reference_offset_edit_updates_dependent(chain, tmp_path):
    panel, own, _a_audio, c_audio = _own_reference(chain, tmp_path)
    own.time_shift_ms = 2000
    panel._track_table.time_shift_edited.emit(own)
    assert panel._track_calibration(own) == SyncCalibration.linear(2000)
    assert panel._track_calibration(c_audio) == SyncCalibration.linear(2100)


def test_own_reference_cancel_updates_dependent(chain, tmp_path):
    panel, own, _a_audio, c_audio = _own_reference(chain, tmp_path)
    panel._on_auto_sync_cancel_requested(own)
    assert panel._track_calibration(own) == SyncCalibration.linear(0)
    assert panel._track_calibration(c_audio) == SyncCalibration.linear(100)


def test_own_reference_studio_updates_dependent_and_breaks_loops(chain, tmp_path):
    """Synchro Studio sur une piste à synchro propre : dépendantes recalculées ; référence qui en
    découle (boucle) détachée, la nouvelle mesure prime."""
    panel, own, a_audio, c_audio = _own_reference(chain, tmp_path)
    panel._apply_track_only_sync(own, a_audio, SyncCalibration.linear(800), "Synchro Studio")
    assert panel._track_calibration(own) == SyncCalibration.linear(800)
    assert panel._track_calibration(c_audio) == SyncCalibration.linear(900)
    panel._apply_track_only_sync(own, c_audio, SyncCalibration.linear(50), "Synchro Studio")
    assert c_audio.file_id not in panel._sync_links
    assert panel._track_calibration(c_audio) == SyncCalibration.linear(0)
    assert panel._track_calibration(own) == SyncCalibration.linear(50)


def test_audio_resync_replaces_target_own_calibration(chain, tmp_path):
    """Nouvelle synchro audio d'une piste à synchro propre importée : elle rejoint sa source, mesure appliquée."""
    panel, own, other = _restore_own(chain, tmp_path, "audio", SyncCalibration.linear(1200), mode="container")
    _panel, _tracks, logs = chain
    a_audio = next(track for track in panel._track_table.current_tracks()
                   if track.file_id == panel._source_files[0].id and track.is_audio)
    panel._on_audio_sync_done(own.entry_id, a_audio.entry_id, 2500, .95, None)
    assert own.entry_id not in panel._own_sync_entry_ids
    assert panel._track_calibration(own) == SyncCalibration.linear(2500)
    assert panel._track_calibration(other) == SyncCalibration.linear(2500)
    mapped = {item.track.entry_id: item.track.time_shift_ms for item in resolve_mapped_tracks(panel.collect_config())}
    assert mapped[own.entry_id] == 2500
    assert ("INFO", translate_text(
        "Nouvelle synchro audio de la piste #{idx} : sa synchro propre importée est remplacée, "
        "elle suit de nouveau sa source.", idx=own.mkv_tid)) in logs
