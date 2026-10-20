"""Coupes et silences ajoutés à la main dans le Synchro Studio."""
from __future__ import annotations

import pytest

from core.workflows.cadence import CadenceMismatch, CadenceType
from core.workflows.physical_sync import audio_filter
from core.workflows.sync_calibration import SyncCalibration, SyncSegment


def _shifts(calibration):
    return [(segment.start_ms, segment.shift_ms) for segment in calibration.segments]


def test_cut_removes_target_content_under_the_section():
    calibration = SyncCalibration.linear(22).with_edit(1022, 2022, silence=False)
    assert _shifts(calibration) == [(0, 22), (1000, -978)]
    graph = audio_filter(calibration, crossfade_ms=0)
    # Segment 2 : contenu cible repris à 2 s, placé juste après la fin du segment 1.
    assert "atrim=start=2.000000" in graph
    assert list(calibration.intervals(0, 3000)) == [(22, 1022), (1022, 2022)]


def test_silence_inserts_the_section_length():
    calibration = SyncCalibration.linear(22).with_edit(1022, 2022, silence=True)
    assert _shifts(calibration) == [(0, 22), (1000, 1022)]
    assert "adelay=1000.000000" in audio_filter(calibration, crossfade_ms=0)


def test_edit_shifts_following_segments():
    base = SyncCalibration((SyncSegment(0, 0), SyncSegment(10_000, 500)))
    calibration = base.with_edit(4000, 4250, silence=True)
    assert _shifts(calibration) == [(0, 0), (4000, 250), (10_000, 750)]
    assert _shifts(calibration.without_junction(1)) == _shifts(base)


def test_cut_over_existing_junction_is_refused():
    base = SyncCalibration((SyncSegment(0, 0), SyncSegment(10_000, 500)))
    with pytest.raises(ValueError, match="jonction"):
        base.with_edit(9000, 11_000, silence=False)
    with pytest.raises(ValueError, match="fin"):
        base.with_edit(2000, 2000, silence=True)


def test_edit_at_start():
    assert _shifts(SyncCalibration.linear(40).with_edit(0, 500, silence=True)) == [(0, 540)]
    # Coupe dès le début : jonction à 1 ms, le segment 1 doit commencer à 0 ms.
    assert _shifts(SyncCalibration.linear(0).with_edit(0, 500, silence=False)) == [(0, 0), (1.0, -500)]


def test_donor_time_follows_cuts_gaps_and_cadence():
    calibration = SyncCalibration((SyncSegment(0, 100), SyncSegment(5000, -900)))
    # Avant la jonction, le segment 1 est conservé ; ensuite le segment 2.
    assert calibration.donor_time_ms(3000) == 2900
    assert calibration.donor_time_ms(6000) == 6900
    gap = SyncCalibration((SyncSegment(0, 0), SyncSegment(5000, 1000)))
    assert gap.donor_time_ms(5500) == 5000
    pal = SyncCalibration(
        (SyncSegment(0, 0),),
        cadence_mismatch=CadenceMismatch(CadenceType.PAL_TO_FILM_23976, 25.0, 23.976, 24000.0 / 25025.0),
    )
    assert pal.donor_time_ms(1000) == pytest.approx(1000 / pal.cadence_mismatch.time_stretch_ratio)


def test_edits_reset_quick_check():
    calibration = SyncCalibration((SyncSegment(0, 0),), quick_check=True)
    assert calibration.with_edit(100, 200, silence=True).quick_check is False


def _studio(tmp_path, monkeypatch, calibration=None):
    from core.workflows.remux_models import TrackEntry
    from ui.panels.remux_panel.widgets import sync_studio_dialog as mod
    monkeypatch.setattr(mod.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    target = TrackEntry(1, "audio", "E-AC-3", "5.1", "fre", "VFF", time_shift_ms=22, file_id="src1")
    reference = TrackEntry(1, "audio", "DTS-HD MA", "5.1", "eng", "VO", file_id="src0")
    return mod, mod.SyncStudioDialog(
        target_entry=target, target_source_path=tmp_path / "t.mkv", target_stream_index=1,
        reference_entry=reference, reference_source_path=tmp_path / "r.mkv", reference_stream_index=1,
        calibration=calibration,
    )


def test_studio_manual_fields_add_cut_and_silence(qt_app, tmp_path, monkeypatch):
    _mod, dialog = _studio(tmp_path, monkeypatch)
    assert not dialog.btn_apply_section.isEnabled()
    dialog.section_start_edit.setText("00:00:01.022")
    dialog.section_end_edit.setText("00:00:02.022")
    dialog._on_section_fields_edited()
    assert dialog.waveform.section == (1022.0, 2022.0)
    dialog._apply_section_edit()
    assert _shifts(dialog.current_calibration) == [(0, 22), (1000, -978)]
    assert dialog.waveform.section is None and dialog._cuts_card.isVisibleTo(dialog)
    assert dialog.cuts_table.item(1, 3).text().startswith("-1000.0 ms")

    dialog._set_section(5000, 5500)
    dialog.rb_section_silence.setChecked(True)
    dialog._apply_section_edit()
    assert dialog.current_calibration.segments[-1].shift_ms == pytest.approx(-478)
    calibration, offset = dialog.result_calibration()
    assert (offset, len(calibration.segments)) == (22, 3)

    dialog._remove_junction(2)
    assert _shifts(dialog.current_calibration) == [(0, 22), (1000, -978)]
    dialog.close()


def test_studio_graph_context_menu_builds_and_applies_section(qt_app, tmp_path, monkeypatch):
    mod, dialog = _studio(tmp_path, monkeypatch)
    choices = iter(["Début de section ici", "Fin de section ici", "Allonger la section (ajout de silence)"])

    def exec_menu(menu, _pos):
        wanted = mod.translate_text(next(choices))
        return next(action for action in menu.actions() if action.text() == wanted)

    monkeypatch.setattr(mod.SyncStudioDialog, "_exec_menu", staticmethod(exec_menu))
    dialog._on_waveform_context(3000.0, None)
    assert dialog._section == (3000.0, 4000.0)
    dialog._on_waveform_context(3250.0, None)
    assert dialog._section == (3000.0, 3250.0)
    assert dialog.section_end_edit.text() == "00:00:03.250"
    dialog._on_waveform_context(3100.0, None)
    assert _shifts(dialog.current_calibration) == [(0, 22), (2978, 272)]
    assert dialog.waveform.section is None
    dialog.close()


def test_studio_refuses_cut_over_junction(qt_app, tmp_path, monkeypatch):
    base = SyncCalibration((SyncSegment(0, 0), SyncSegment(10_000, 500)))
    mod, dialog = _studio(tmp_path, monkeypatch, base)
    dialog._set_section(9000, 11_000)
    dialog._apply_section_edit()
    assert dialog.current_calibration == base
    assert dialog.listen_status.text() == mod.translate_text("La coupe chevauche une jonction existante.")
    dialog.close()


def test_waveform_draws_target_piecewise_and_drags_section_edge(qt_app):
    import numpy as np
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from ui.widgets.waveform_view import WaveformView

    view = WaveformView()
    view.resize(1000, 200)
    view.set_audio_data(np.ones(16000 * 20, dtype=np.float32), np.ones(16000 * 20, dtype=np.float32))
    # Silence de 2 s inséré à 5 s : la cible est absente de 5 s à 7 s.
    calibration = SyncCalibration.linear(0).with_edit(5000, 7000, silence=True)
    ranges = calibration.output_ranges()
    view.set_calibration_view(
        [(a, b, calibration.segments[i].shift_ms) for a, b, i in ranges], [(a, i) for a, _b, i in ranges if i],
    )
    peaks = view._piecewise_target_peaks(0.0, 20_000.0, 1000)
    assert peaks[100] == 1 and peaks[300] == 0 and peaks[400] == 1
    assert view.junction_near(view.x_at_time(7000.0)) == 1

    view.set_section(5000, 6000)
    edge_x = int(view.x_at_time(6000.0))
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(edge_x, 50))
    QTest.mouseMove(view, QPoint(400, 50))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(400, 50))
    assert view.section == (5000.0, pytest.approx(8000.0))
    view.close()
