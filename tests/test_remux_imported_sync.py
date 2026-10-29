"""Synchronisations importées : retouches individuelles et héritage des sous-titres."""
from __future__ import annotations

import copy

import pytest

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
