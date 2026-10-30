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
    # Coupe dès le début : la sortie démarre sur l'audio cible situé à 500 ms.
    assert _shifts(SyncCalibration.linear(0).with_edit(0, 500, silence=False)) == [(0, -500)]
    # Le silence de tête (40 ms) est retiré avant l'audio.
    assert _shifts(SyncCalibration.linear(40).with_edit(0, 500, silence=False)) == [(0, -460)]


def test_cut_starting_inside_silence_keeps_remaining_silence():
    """Audit : silence 5–6 s, coupe 5,5–6,5 s → 500 ms de silence gardés, 500 ms d'audio retirés."""
    calibration = SyncCalibration.linear(0).with_edit(5000, 6000, silence=True).with_edit(5500, 6500, silence=False)
    assert _shifts(calibration) == [(0, 0), (5000, -500), (5500, 0)]
    assert list(calibration.intervals(0, 10_000)) == [(0, 5000), (5500, 10_000)]
    graph = audio_filter(calibration, crossfade_ms=0)
    assert "[seg1]" not in graph and "atrim=start=5.500000" in graph and "adelay=500.000000" in graph


def test_cut_inside_silence_only_shortens_it():
    calibration = SyncCalibration.linear(0).with_edit(5000, 6000, silence=True).with_edit(5200, 5700, silence=False)
    assert _shifts(calibration) == [(0, 0), (5000, 500)]


def test_cut_at_segment_start_after_silence_keeps_the_silence():
    calibration = SyncCalibration.linear(0).with_edit(5000, 6000, silence=True).with_edit(6000, 6500, silence=False)
    # Cible 5,5–10 s rejouée à partir de 6 s : seconde de silence intacte, 500 ms d'audio retirées.
    assert list(calibration.intervals(0, 10_000)) == [(0, 5000), (6000, 10_500)]
    assert calibration.donor_time_ms(6000) == 5500


def test_cut_up_to_next_junction():
    base = SyncCalibration((SyncSegment(0, 0), SyncSegment(10_000, 500)))
    calibration = base.with_edit(8000, 10_000, silence=False)
    # Cible 8–10 s retirée ; la suite (10 s) rejouée à 8,5 s, le saut de +500 ms conservé.
    assert list(calibration.intervals(0, 12_000)) == [(0, 8000), (8500, 10_500)]


def test_cut_from_silence_beyond_next_segment_is_refused():
    base = SyncCalibration((SyncSegment(0, 0), SyncSegment(5000, 1000), SyncSegment(8000, 1000 - 200)))
    with pytest.raises(ValueError, match="jonction"):
        base.with_edit(5500, 9500, silence=False)


def test_donor_time_follows_cuts_gaps_and_cadence():
    calibration = SyncCalibration((SyncSegment(0, 100), SyncSegment(5000, -900)))
    # Avant la jonction, le segment 1 est conservé ; ensuite le segment 2.
    assert calibration.donor_time_ms(3000) == 2900
    assert calibration.donor_time_ms(6000) == 6900
    gap = SyncCalibration((SyncSegment(0, 0), SyncSegment(5000, 1000)))
    assert gap.donor_time_ms(5500) == 5000
    assert (gap.segment_index_at(5500), gap.output_start_ms(1)) == (1, 6000)
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


def test_studio_invalid_field_blocks_previous_section(qt_app, tmp_path, monkeypatch):
    _mod, dialog = _studio(tmp_path, monkeypatch)
    dialog._set_section(1022, 2022)
    assert dialog.btn_apply_section.isEnabled()
    dialog.section_end_edit.setText("00:00:xx")
    dialog._on_section_fields_edited()
    assert dialog._section is None and dialog.waveform.section is None
    assert not dialog.btn_apply_section.isEnabled()
    # Champ modifié sans validation (pas d'editingFinished) : rien n'est appliqué sur l'ancienne plage.
    dialog._set_section(1022, 2022)
    dialog.section_start_edit.setText("abc")
    dialog._apply_section_edit()
    assert _shifts(dialog.current_calibration) == [(0, 22)]
    dialog.close()


def test_studio_relaunch_keeps_cadence_and_method(qt_app, tmp_path, monkeypatch):
    from core.workflows.audio_sync_scan import AudioSyncScanner
    from types import SimpleNamespace
    mismatch = CadenceMismatch(CadenceType.PAL_TO_FILM_23976, 25.0, 23.976, 24000.0 / 25025.0)
    calibration = SyncCalibration((SyncSegment(0, 0),), cadence_mismatch=mismatch, cadence_audio_method="asetrate")
    _mod, dialog = _studio(tmp_path, monkeypatch, calibration)
    dialog._executor = SimpleNamespace(submit=lambda callback: callback(), shutdown=lambda **_k: None)
    seen = []

    def scan(_scanner, *_a, **kwargs):
        seen.append((kwargs.get("cadence_mismatch"), kwargs.get("cadence_audio_method")))
        return calibration

    monkeypatch.setattr(AudioSyncScanner, "scan", scan)
    dialog.btn_deep_audio_sync.click()
    qt_app.processEvents()
    assert seen == [(mismatch, "asetrate")]
    dialog.close()


def test_studio_target_ranges_follow_cadence_and_previous_cuts(qt_app, tmp_path, monkeypatch):
    mismatch = CadenceMismatch(CadenceType.PAL_TO_FILM_23976, 25.0, 23.976, 24000.0 / 25025.0)
    ratio = mismatch.time_stretch_ratio
    pal = SyncCalibration((SyncSegment(0, 0),), cadence_mismatch=mismatch)
    _mod, dialog = _studio(tmp_path, monkeypatch, pal)
    (lo, hi), = dialog._target_ranges(3600.0)
    # À une heure, la cible jouée est lue ~144 s plus tôt (conversion 25 → 23,976).
    assert lo == pytest.approx(3_600_000 / ratio - 2000, abs=1)
    assert hi == pytest.approx(3_620_000 / ratio + 2000, abs=1)
    dialog.close()

    # Coupe de 30 s à 10 min : plus loin, la cible garde ce décalage cumulé.
    cut = SyncCalibration.linear(0).with_edit(600_000, 630_000, silence=False)
    _mod, dialog = _studio(tmp_path, monkeypatch, cut)
    assert dialog._target_ranges(3600.0) == ((3_628_000, 3_652_000),)
    dialog._current_start_s = 3600.0
    dialog._loaded_target_ranges = dialog._target_ranges(3600.0)
    dialog._activate_segment(1)
    dialog.spin_shift.setValue(dialog.spin_shift.value() - 5000)
    assert dialog._reload_timer.isActive()
    dialog.close()


def test_waveform_reads_target_chunk_at_its_own_origin(qt_app):
    import numpy as np
    from ui.widgets.waveform_view import WaveformView

    view = WaveformView()
    view.resize(1000, 200)
    # Fenêtre de sortie à 1 h ; extrait cible lu 144 s plus tôt.
    view.set_audio_data(
        np.ones(16000 * 20, dtype=np.float32), None, start_time_ms=3_600_000,
        target_chunks=[(3_456_000.0, np.ones(16000 * 21, dtype=np.float32))], window_duration_ms=20_000,
    )
    view.set_calibration_view([(0.0, float("inf"), 144_000.0)])
    peaks = view._piecewise_target_peaks(3_600_000.0, 3_620_000.0, 1000)
    assert peaks.min() == 1
    view.close()


def test_output_time_follows_cuts_silences_and_cadence():
    calibration = SyncCalibration((SyncSegment(0, 100), SyncSegment(5000, -900)))
    assert calibration.output_time_ms(1000) == 1100
    # Cible 5–6 s coupée : un chapitre à 5,5 s tombe sur la jonction (5,1 s de sortie).
    assert calibration.output_time_ms(5500) == 5100
    assert calibration.output_time_ms(8000) == 7100
    silence = SyncCalibration.linear(0).with_edit(5000, 6000, silence=True)
    assert silence.output_time_ms(7000) == 8000
    mismatch = CadenceMismatch(CadenceType.PAL_TO_FILM_23976, 25.0, 23.976, 24000.0 / 25025.0)
    pal = SyncCalibration((SyncSegment(0, 0),), cadence_mismatch=mismatch)
    assert pal.output_time_ms(3_600_000) == pytest.approx(3_600_000 * mismatch.time_stretch_ratio)


def test_studio_keeps_cadence_when_auto_offset_is_negligible(qt_app, tmp_path, monkeypatch):
    mismatch = CadenceMismatch(CadenceType.PAL_TO_FILM_23976, 25.0, 23.976, 24000.0 / 25025.0)
    _mod, dialog = _studio(tmp_path, monkeypatch)
    dialog._on_auto_sub_sync_ready(SyncCalibration((SyncSegment(0, -3.0),), 0.95, cadence_mismatch=mismatch))
    calibration, offset = dialog.result_calibration()
    # Conversion de cadence conservée avec son départ mesuré : rien n'est « non appliqué ».
    assert (offset, calibration.cadence_mismatch) == (-3, mismatch)
    assert "(≤ 5 ms)" not in dialog.listen_status.text()
    dialog.close()


def test_chapter_in_fully_cut_segment_lands_on_the_resume_after_kept_silence():
    """Audit Codex C1 : coupe débutant dans un silence de 55 s, chapitre dans l'audio retiré."""
    base = SyncCalibration((SyncSegment(0, 0), SyncSegment(5000, 60000)))
    cut = base.with_edit(60000, 66000, silence=False)
    assert [(a, b) for a, b, _i in cut.output_ranges()] == [(0, 5000), (60000, float("inf"))]
    assert cut.output_time_ms(5500) == 60000
    assert cut.output_time_ms(6500) == 60500


def test_waveform_target_cues_follow_cuts_silences_and_cadence(qt_app):
    from ui.widgets.waveform_view import WaveformView

    view = WaveformView()
    # Cible 5–6 s coupée : sortie 0–5 s (décalage 0) puis reprise à 5 s (décalage −1 s).
    view.set_calibration_view([(0.0, 5000.0, 0.0), (5000.0, float("inf"), -1000.0)])
    assert view._target_cue_intervals(5200, 5800) == []
    assert view._target_cue_intervals(4500, 6500) == [(4500.0, 5000.0), (5000.0, 5500.0)]
    view.set_calibration_view([(0.0, float("inf"), 40.0)], stretch_ratio=25.0 / 24.0)
    assert view._target_cue_intervals(24_000, 48_000) == [(25_040.0, 50_040.0)]
    view.set_calibration_view([])
    view.set_shift(300)
    assert view._target_cue_intervals(1000, 2000) == [(1300.0, 2300.0)]
    view.close()


def test_waveform_draws_target_cue_text(qt_app, monkeypatch):
    import numpy as np
    from PySide6.QtGui import QPainter
    import ui.widgets.waveform_view as waveform

    drawn: list[str] = []

    class _Painter(QPainter):
        def drawText(self, *args):  # noqa: N802
            if isinstance(args[-1], str):
                drawn.append(args[-1])
            return super().drawText(*args)

    monkeypatch.setattr(waveform, "QPainter", _Painter)
    view = waveform.WaveformView()
    view.resize(1000, 200)
    view.set_audio_data(np.ones(16000 * 10, dtype=np.float32), np.ones(16000 * 10, dtype=np.float32))
    view.set_subtitle_cues([], [(1000, 6000, "REPLIQUE CIBLE")])
    view.grab()
    assert any(text.startswith("REPL") for text in drawn)
    view.close()


def _cadence_studio(tmp_path, monkeypatch, calibration, *, record_resolution=True, **kwargs):
    from core.workflows.remux_models import TrackEntry
    from ui.panels.remux_panel.widgets import sync_studio_dialog as mod
    monkeypatch.setattr(mod.SyncStudioDialog, "_load_segment_audio_async", lambda *_a, **_k: None)
    resolutions: list = []
    if record_resolution:
        monkeypatch.setattr(mod.SyncStudioDialog, "_resolve_cadence_method",
                            lambda self: resolutions.append(self.current_calibration))
    target = TrackEntry(1, "audio", "AAC", "2.0", "fre", "VFF", file_id="src1")
    reference = TrackEntry(1, "audio", "E-AC-3", "5.1", "eng", "VO", file_id="src0")
    dialog = mod.SyncStudioDialog(
        target_entry=target, target_source_path=tmp_path / "t.mkv", target_stream_index=1,
        reference_entry=reference, reference_source_path=tmp_path / "r.mkv", reference_stream_index=1,
        calibration=calibration, **kwargs,
    )
    return dialog, resolutions


def test_studio_offers_the_video_cadence_and_follows_the_detected_method(qt_app, tmp_path, monkeypatch):
    from dataclasses import replace
    from core.workflows.cadence import detect_cadence_from_metadata
    from core.workflows.cadence_pitch import CadencePitchAnalysis

    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    dialog, resolutions = _cadence_studio(tmp_path, monkeypatch, SyncCalibration.linear(0),
                                          cadence_hint=pal, analysis_stream_index=2)
    # Synchro sans cadence : conversion d'après les vidéos, cochée ; hauteur analysée dès l'ouverture.
    assert dialog.current_calibration.cadence_mismatch == pal
    assert dialog.cadence_check.isChecked() and dialog.rb_auto.isChecked()
    assert dialog.analysis_stream_index == 2 and resolutions

    analysis = CadencePitchAnalysis("asetrate", 0.72, 0.9, 0.8, 0.0, 0.0, details="spectre")
    changes: list = []
    dialog.calibration_changed.connect(changes.append)
    dialog._on_cadence_resolved(dialog.current_calibration, replace(
        dialog.current_calibration, cadence_audio_method="asetrate", cadence_pitch_analysis=analysis,
    ))
    assert dialog.current_calibration.cadence_audio_method == "asetrate"
    assert "asetrate" in dialog.rb_auto.text() and changes

    # « Auto » : la méthode détectée, jamais le repli atempo d'une méthode non tranchée.
    dialog.rb_atempo.setChecked(True)
    dialog._on_cadence_method_changed()
    assert dialog.current_calibration.cadence_audio_method == "atempo"
    dialog.rb_auto.setChecked(True)
    dialog._on_cadence_method_changed()
    assert dialog.current_calibration.cadence_audio_method == "asetrate"

    # Case décochée : plus de conversion ; recochée : la même revient.
    dialog.cadence_check.setChecked(False)
    assert dialog.current_calibration.cadence_mismatch is None and not dialog.rb_auto.isEnabled()
    dialog.cadence_check.setChecked(True)
    assert dialog.current_calibration.cadence_mismatch == pal and dialog.rb_auto.isEnabled()
    calibration, offset = dialog.result_calibration()
    assert offset == 0 and calibration.cadence_mismatch == pal


def test_studio_keeps_an_existing_cadence_and_shows_none_without_hint(qt_app, tmp_path, monkeypatch):
    from core.workflows.cadence import detect_cadence_from_metadata

    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    measured = SyncCalibration((SyncSegment(0, 5150.0),), cadence_mismatch=pal.inverse(),
                               cadence_audio_method="atempo")
    dialog, _resolutions = _cadence_studio(tmp_path, monkeypatch, measured, cadence_hint=pal)
    assert dialog.current_calibration is measured and dialog.rb_atempo.isChecked()
    dialog, _resolutions = _cadence_studio(tmp_path, monkeypatch, SyncCalibration.linear(0))
    assert not hasattr(dialog, "cadence_check") and dialog.current_calibration.cadence_mismatch is None


def test_studio_offers_a_declined_cadence_unchecked(qt_app, tmp_path, monkeypatch):
    from core.workflows.cadence import detect_cadence_from_metadata

    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    dialog, _resolutions = _cadence_studio(tmp_path, monkeypatch, SyncCalibration.linear(120),
                                           cadence_hint=pal, apply_cadence_hint=False)
    # Refus antérieur : le panneau le garde (validation sans conversion), le studio n'en crée pas d'autre.
    assert dialog.current_calibration.cadence_mismatch is None and not dialog.cadence_declined
    assert not dialog.cadence_check.isChecked() and not dialog.rb_auto.isEnabled()
    dialog.cadence_check.setChecked(True)
    # Recochée : méthode des réglages (pas l'atempo par défaut d'une calibration sans cadence).
    assert dialog.current_calibration.cadence_mismatch == pal and not dialog.cadence_declined
    assert dialog.current_calibration.cadence_audio_method == "auto" and dialog.rb_auto.isChecked()
    dialog.rb_asetrate.setChecked(True)
    dialog._on_cadence_method_changed()
    dialog.cadence_check.setChecked(False)
    dialog.cadence_check.setChecked(True)
    assert dialog.current_calibration.cadence_audio_method == "asetrate"


def _recording_executor(submitted: list):
    """Exécuteur du studio : tâches mémorisées, lancées à la main par le test."""
    class _Executor:
        def __init__(self, *_args, **_kwargs):
            pass

        def submit(self, fn, *args):
            submitted.append(fn)

        def shutdown(self, *_args, **_kwargs):
            pass

    return _Executor


def test_studio_runs_one_pitch_analysis_and_relaunches_a_stale_one(qt_app, tmp_path, monkeypatch):
    from dataclasses import replace
    from core.workflows.cadence import detect_cadence_from_metadata
    from ui.panels.remux_panel.widgets import sync_studio_dialog as mod

    submitted: list = []
    analysed: list[float] = []

    def resolve(_self, _reference, _target, calibration, log=None):
        analysed.append(calibration.segments[0].shift_ms)
        return replace(calibration, cadence_audio_method="asetrate")

    monkeypatch.setattr(mod, "ThreadPoolExecutor", _recording_executor(submitted))
    monkeypatch.setattr(mod.AudioSyncScanner, "resolve_cadence_method", resolve)
    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    dialog, _resolutions = _cadence_studio(tmp_path, monkeypatch, SyncCalibration.linear(0),
                                           record_resolution=False, cadence_hint=pal)
    assert len(submitted) == 1
    dialog._resolve_cadence_method()
    assert len(submitted) == 1  # une analyse à la fois
    dialog.spin_shift.setValue(400)  # décalage modifié pendant l'analyse
    submitted.pop(0)()
    # Résultat calculé sur l'ancien décalage : écarté, analyse relancée.
    assert analysed == [0.0] and dialog.current_calibration.cadence_audio_method == "auto"
    assert len(submitted) == 1
    submitted.pop(0)()
    assert analysed == [0.0, 400.0] and dialog.current_calibration.cadence_audio_method == "asetrate"
    assert submitted == []


def test_studio_auto_mode_keeps_the_fallback_once_the_method_is_decided(qt_app, tmp_path, monkeypatch):
    """« Auto » coché : méthode tranchée par la hauteur (atempo), les synchros du studio demandent
    toujours « auto » (repli asetrate si atempo ne corrèle pas) ; méthode explicite : transmise."""
    from dataclasses import replace
    from core.workflows.cadence import detect_cadence_from_metadata
    from core.workflows.cadence_pitch import CadencePitchAnalysis
    from ui.panels.remux_panel.widgets import sync_studio_dialog as mod

    submitted: list = []
    requested: list[str] = []
    pal = detect_cadence_from_metadata("24000/1001", "25/1")

    def scan(_self, _reference, _target, *, cadence_audio_method, **_kwargs):
        requested.append(cadence_audio_method)
        # Repli du scanner : seule asetrate corrèle ; mode demandé conservé, comme le vrai scanner.
        return SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal, cadence_audio_method="asetrate",
                               cadence_method_auto=cadence_audio_method == "auto")

    monkeypatch.setattr(mod, "ThreadPoolExecutor", _recording_executor(submitted))
    monkeypatch.setattr(mod.AudioSyncScanner, "scan", scan)
    dialog, _resolutions = _cadence_studio(tmp_path, monkeypatch, SyncCalibration.linear(0), cadence_hint=pal)
    analysis = CadencePitchAnalysis("atempo", 0.0, 0.5, 0.6, 0.0, 0.0, details="recommandation atempo")
    dialog._on_cadence_resolved(dialog.current_calibration, replace(
        dialog.current_calibration, cadence_audio_method="atempo", cadence_pitch_analysis=analysis,
    ))
    assert dialog.current_calibration.cadence_audio_method == "atempo" and dialog.rb_auto.isChecked()

    for _ in range(2):
        dialog._run_auto_audio_sync(deep=False)
        submitted.pop()()
        assert dialog.current_calibration.cadence_audio_method == "asetrate" and dialog.rb_auto.isChecked()
    assert requested == ["auto", "auto"]

    dialog.rb_atempo.setChecked(True)
    dialog._on_cadence_method_changed()
    dialog._run_auto_audio_sync(deep=False)
    submitted.pop()()
    assert requested[-1] == "atempo" and dialog.rb_asetrate.isChecked()


def test_studio_reopens_the_requested_mode_not_the_recommendation(qt_app, tmp_path, monkeypatch):
    """Mode conservé dans la calibration : asetrate choisie explicitement (égale à la recommandation)
    reste explicite ; Auto ayant retenu un repli différent de la recommandation reste Auto."""
    from core.workflows.cadence import detect_cadence_from_metadata
    from core.workflows.cadence_pitch import CadencePitchAnalysis

    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    recommends_asetrate = CadencePitchAnalysis("asetrate", 0.72, 0.9, 0.8, 0.0, 0.0, details="spectre")
    explicit = SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal, cadence_audio_method="asetrate",
                               cadence_pitch_analysis=recommends_asetrate)
    dialog, _resolutions = _cadence_studio(tmp_path, monkeypatch, SyncCalibration.from_dict(explicit.to_dict()))
    assert dialog.rb_asetrate.isChecked() and dialog._requested_cadence_method() == "asetrate"

    fallback = SyncCalibration((SyncSegment(0, 300.0),), cadence_mismatch=pal, cadence_audio_method="atempo",
                               cadence_pitch_analysis=recommends_asetrate, cadence_method_auto=True)
    dialog, _resolutions = _cadence_studio(tmp_path, monkeypatch, SyncCalibration.from_dict(fallback.to_dict()))
    assert dialog.rb_auto.isChecked() and dialog._requested_cadence_method() == "auto"
    assert dialog.current_calibration.cadence_audio_method == "atempo"

    # Choix dans le studio : enregistré dans la calibration rendue.
    dialog.rb_asetrate.setChecked(True)
    dialog._on_cadence_method_changed()
    assert dialog.result_calibration()[0].cadence_method_requested == "asetrate"
    dialog.rb_auto.setChecked(True)
    dialog._on_cadence_method_changed()
    returned = dialog.result_calibration()[0]
    assert returned.cadence_method_auto and returned.cadence_audio_method == "asetrate"


def test_studio_records_a_refusal_only_when_the_user_unchecks(qt_app, tmp_path, monkeypatch):
    """Résultat d'analyse sans conversion (synchro de sous-titres) : pas un refus ; case décochée : refus."""
    from core.workflows.cadence import detect_cadence_from_metadata

    pal = detect_cadence_from_metadata("24000/1001", "25/1")
    dialog, _resolutions = _cadence_studio(tmp_path, monkeypatch, SyncCalibration.linear(0), cadence_hint=pal)
    dialog._on_auto_sub_sync_ready(SyncCalibration.linear(400))
    assert dialog.current_calibration.cadence_mismatch is None and not dialog.cadence_declined
    dialog.cadence_check.setChecked(True)
    dialog.cadence_check.setChecked(False)
    assert dialog.cadence_declined
    dialog._reset_shifts()
    assert not dialog.cadence_declined
