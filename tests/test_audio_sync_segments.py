"""Mesures de contenu : les jonctions détectées doivent correspondre aux edits réels."""
from __future__ import annotations

import threading
from typing import Any, cast

import numpy as np
import pytest

from core.workflows.audio_sync import AudioSyncError
from core.workflows.audio_sync_segments import (
    Anchor, _returning_excursion, locate_transition, match_window, scan_envelopes,
)


def _edited_pair(cuts, shifts, duration=180_000):
    rng = np.random.default_rng(728)
    donor = np.repeat(rng.uniform(0.01, 1, duration // 10), 10)
    reference = np.zeros(duration + max(shifts) + 1000)
    previous_end = 0
    for start, stop, shift in zip([0, *cuts], [*cuts, duration], shifts):
        start = max(start, -shift, previous_end - shift)
        if stop > start:
            reference[start + shift:stop + shift] = donor[start:stop]
            previous_end = stop + shift
    return reference, donor


def _fake_sync_panel(**overrides):
    """État minimal lu par `RemuxPanel._on_audio_sync_requested` ; analyse exécutée sur place."""
    from types import SimpleNamespace

    state = dict(
        _scan_cancel=threading.Event(),
        _can_start_auxiliary_operation=lambda: True,
        _audio_sync_family=lambda _entry: "surround",
        _audio_sync_measure_entry=lambda target, _reference: target,
        _audio_sync_cadence=lambda *_a: None,
        _audio_sync_track=lambda entry: entry,
        _config=SimpleNamespace(tool_ffmpeg="ffmpeg", tool_ffprobe="ffprobe"),
        log_message=SimpleNamespace(emit=lambda *_a: None),
        audio_sync_started=SimpleNamespace(emit=lambda *_a: None),
        _audio_sync_error=SimpleNamespace(emit=lambda *_a: None),
        _executor=SimpleNamespace(submit=lambda callback: callback()),
    )
    state.update(overrides)
    return SimpleNamespace(**state)


def test_short_window_can_find_offset_larger_than_window():
    ref, donor = _edited_pair([], [-18000])
    anchor = match_window(ref, donor, 10000, 6000, 30000)
    assert anchor.shift_ms == -18000
    assert anchor.confidence > 0.99


def test_all_plateaus_including_intro_are_detected():
    cuts = [25000, 60000, 90000, 120000, 150000]
    shifts = [-5417, -3415, -1413, 589, 2591, 4593]
    ref, donor = _edited_pair(cuts, shifts)
    segments, confidence, _ = scan_envelopes(ref, donor)
    assert [s.shift_ms for s in segments] == shifts
    assert [s.start_ms for s in segments[1:]] == pytest.approx(cuts, abs=50)
    assert confidence > 0.5


def test_short_intermediate_plateau_with_equal_outer_offsets():
    ref, donor = _edited_pair([60000, 76000], [200, 900, 200])
    segments, _, _ = scan_envelopes(ref, donor)
    assert [s.shift_ms for s in segments] == [200, 900, 200]
    assert segments[1].start_ms == pytest.approx(60000, abs=50)
    # Le saut négatif écrase le début du dernier segment : la jonction est
    # indiscernable dans cette zone, mais ne doit pas être placée ailleurs.
    assert 75950 <= segments[2].start_ms <= 76750


def test_unverified_transition_raises_instead_of_inventing_midpoint():
    rng = np.random.default_rng(82)
    ref, donor = rng.random(30000), rng.random(30000)
    with pytest.raises(AudioSyncError):
        locate_transition(ref, donor, Anchor(4000, 200, .99), Anchor(20000, 4000, .99))


def test_long_unverified_interval_with_shift_change_is_rejected():
    ref, donor = _edited_pair([90000], [200, 900])
    donor[60000:120000] = 0
    with pytest.raises(AudioSyncError, match="non vérifié"):
        scan_envelopes(ref, donor)


def test_long_unmatched_interval_with_stable_shift_is_rejected():
    ref, donor = _edited_pair([], [200])
    # Les deux pistes restent sonores, mais le doublage de ce passage ne
    # fournit aucune correspondance acoustique exploitable.
    donor[60000:120000] = np.random.default_rng(419).uniform(0.01, 1, 60000)
    with pytest.raises(AudioSyncError, match="non vérifié"):
        scan_envelopes(ref, donor)


def test_short_unmatched_interval_with_stable_shift_is_accepted():
    ref, donor = _edited_pair([], [200])
    donor[60000:72000] = np.random.default_rng(419).uniform(0.01, 1, 12000)
    segments, _, _ = scan_envelopes(ref, donor)
    assert [s.shift_ms for s in segments] == [200]


def test_cancelled_dense_analysis_stops():
    def cancel():
        raise AudioSyncError("Analyse annulée.")
    ref, donor = _edited_pair([], [200])
    with pytest.raises(AudioSyncError, match="annulée"):
        scan_envelopes(ref, donor, check_cancelled=cancel)


def test_cut_coordinates_are_converted_back_to_original_donor():
    ref, donor = _edited_pair([60000], [200, 900])
    segments, _, _ = scan_envelopes(ref, donor, speed_factor=.96)
    assert segments[1].start_ms == pytest.approx(57600, abs=50)


def test_silent_search_candidates_cannot_produce_false_perfect_match():
    rng = np.random.default_rng(902)
    audio = rng.uniform(.01, .1, 80000)
    reference = np.r_[np.zeros(4593), audio]
    donor = np.r_[audio, np.zeros(40000)]
    anchor = match_window(reference, donor, 76000, 8000, 30000)
    assert anchor.shift_ms == 4593
    assert anchor.confidence == pytest.approx(1)


def test_unverified_intro_is_rejected():
    ref, donor = _edited_pair([], [200])
    ref[:50000] = 0
    with pytest.raises(AudioSyncError, match="Début de piste"):
        scan_envelopes(ref, donor)


def test_silent_intro_on_both_sides_is_accepted():
    ref, donor = _edited_pair([], [1006])
    ref[:31000] = 1e-6
    donor[:30000] = 1e-6
    messages = []
    segments, _, _ = scan_envelopes(ref, donor, log=messages.append)
    assert [s.shift_ms for s in segments] == [1006]
    assert any("Début silencieux" in message for message in messages)


def _distinct_intros(reference_film_start):
    """Logos propres à chaque piste ; film au même instant décalé de +3212 ms (cas réel By Any Means)."""
    ref, donor = _edited_pair([], [3212])
    rng = np.random.default_rng(31)
    donor[1000:11600] = np.repeat(rng.uniform(0.01, 1, 1060), 10)
    donor[:1000] = donor[11600:18500] = 0
    ref[1000:19700] = np.repeat(rng.uniform(0.01, 1, 1870), 10)
    ref[:1000] = ref[19700:reference_film_start] = 0
    # Son actif propre à la référence entre son silence et le début du film.
    ref[reference_film_start:21712] = rng.uniform(0.01, 1, 21712 - reference_film_start)
    return ref, donor


def test_distinct_intros_closed_by_aligned_silences_are_accepted():
    ref, donor = _distinct_intros(21712)
    messages: list[str] = []
    segments, _, _ = scan_envelopes(ref, donor, log=messages.append)
    assert [s.shift_ms for s in segments] == [3212]
    assert any("Introduction propre" in message for message in messages)


def test_distinct_intro_whose_silence_misses_the_plateau_is_rejected():
    # Silence de la référence fini 1,5 s trop tôt : son actif non apparié avant le film.
    ref, donor = _distinct_intros(20212)
    with pytest.raises(AudioSyncError, match="Début de piste"):
        scan_envelopes(ref, donor)


def test_short_small_unverified_excursion_returning_to_plateau_is_ignored():
    anchors = [Anchor(t, 1000, .8) for t in (32000, 36000)] + [Anchor(t, 1031, .75) for t in (44000, 48000, 52000)]
    anchors += [Anchor(t, 1001, .8) for t in (56000, 60000)]
    assert _returning_excursion(anchors, 2, 1000, 25) == 5
    assert _returning_excursion(anchors[:5], 2, 1000, 25) is None


def test_large_or_long_excursion_is_not_ignored():
    large = [Anchor(32000, 1000, .8), Anchor(36000, 1100, .8), Anchor(40000, 1000, .8)]
    assert _returning_excursion(large, 1, 1000, 25) is None
    long = [Anchor(32000, 1000, .8), *[Anchor(t, 1031, .8) for t in range(36000, 70000, 4000)], Anchor(70000, 1000, .8)]
    assert _returning_excursion(long, 1, 1000, 25) is None


def test_scanner_retries_without_center_channel(monkeypatch):
    from pathlib import Path
    from core.workflows.audio_sync import AudioSyncTrack
    from core.workflows.audio_sync_scan import AudioSyncScanner

    scanner = AudioSyncScanner()
    monkeypatch.setattr(scanner, "measure", lambda *args, **kwargs: (1006.0, .9))
    monkeypatch.setattr(scanner, "duration", lambda track: 7600.0)
    monkeypatch.setattr(scanner, "channel_layout", lambda track: (6, "5.1(side)"))
    ref, donor = _edited_pair([], [1006])
    calls = []

    def envelope_mixes(track, cadence_filter, downmixes, abort=None):
        calls.append(downmixes)
        mono = np.zeros(len(ref)) if track.stream_index == 1 else donor
        return [mono if downmix is None else ref if track.stream_index == 1 else donor for downmix in downmixes]

    monkeypatch.setattr(scanner, "envelope_mixes", envelope_mixes)
    messages = []
    calibration = scanner.scan(AudioSyncTrack(Path("ref.mkv"), 1), AudioSyncTrack(Path("don.mkv"), 2),
                               detect_cuts=True, log=messages.append)
    assert [s.shift_ms for s in calibration.segments] == [1006]
    # Une seule lecture de chaque fichier pour les deux mixages.
    assert calls == [(None, "pan=mono|c0=SL+SR")] * 2
    assert any("hors dialogues" in message for message in messages)


def test_scanner_retries_when_initial_mono_windows_fail(monkeypatch):
    from pathlib import Path
    from core.workflows.audio_sync import AudioSyncTrack
    from core.workflows.audio_sync_scan import AudioSyncScanner

    scanner = AudioSyncScanner()
    monkeypatch.setattr(scanner, "duration", lambda track: 180.0)
    monkeypatch.setattr(scanner, "channel_layout", lambda track: (6, "5.1(side)"))
    ref, donor = _edited_pair([], [1006])
    measured = []

    def measure(*args, downmixes=None, **kwargs):
        measured.append(downmixes)
        if downmixes is None:
            raise AudioSyncError("Corrélation mono insuffisante")
        return 1006.0, .9

    def envelope(track, cadence_filter=None, downmix=None, abort=None):
        assert downmix == "pan=mono|c0=SL+SR"
        return ref if track.stream_index == 1 else donor

    monkeypatch.setattr(scanner, "measure", measure)
    monkeypatch.setattr(scanner, "envelope", envelope)
    calibration = scanner.scan(AudioSyncTrack(Path("ref.mkv"), 1), AudioSyncTrack(Path("don.mkv"), 2),
                               detect_cuts=True)
    assert [s.shift_ms for s in calibration.segments] == [1006]
    assert None in measured
    assert ("pan=mono|c0=SL+SR",) * 2 in measured


def test_dialogue_free_downmixes_follow_layouts():
    from core.workflows.audio_sync_scan import AudioSyncScanner

    assert AudioSyncScanner.dialogue_free_downmixes((8, "7.1"), (6, "5.1(side)")) == (
        "pan=mono|c0=BL+BR+SL+SR", "pan=mono|c0=SL+SR")
    assert AudioSyncScanner.dialogue_free_downmixes((8, "7.1(wide)"), (8, "7.1(wide-side)")) == (
        "pan=mono|c0=BL+BR", "pan=mono|c0=SL+SR")
    assert AudioSyncScanner.dialogue_free_downmixes((6, "5.1"), (2, "stereo")) == ("pan=mono|c0=c0-c1",) * 2
    assert AudioSyncScanner.dialogue_free_downmixes((1, "mono"), (6, "5.1")) is None
    with pytest.raises(AudioSyncError, match="Disposition multicanal non reconnue"):
        AudioSyncScanner.dialogue_free_downmixes((8, "7.1(inconnu)"), (6, "5.1(side)"))


def test_ui_reports_failed_analysis_without_constant_offset_fallback(monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QDialog
    from ui.panels.remux_panel import panel
    from core.workflows.audio_sync import AudioSyncWorkflow
    from core.workflows.audio_sync_scan import AudioSyncScanner

    target = SimpleNamespace(track_type="audio", mkv_tid=2, entry_id="target")
    reference = SimpleNamespace(mkv_tid=1, entry_id="reference")
    monkeypatch.setattr(panel, "_AudioSyncReferenceDialog", lambda *_a, **_k: SimpleNamespace(
        exec=lambda: QDialog.DialogCode.Accepted, selected_entry=lambda: reference,
    ))

    def fail(scanner, *_a, **_k):
        assert scanner.cancel_event is fake._scan_cancel
        raise AudioSyncError("Jonction non vérifiée")

    def forbidden(*_a, **_k):
        pytest.fail("Une analyse rejetée ne doit pas devenir un offset constant")

    monkeypatch.setattr(AudioSyncScanner, "scan", fail)
    monkeypatch.setattr(AudioSyncWorkflow, "detect_offset", forbidden)
    errors = []
    fake = _fake_sync_panel(
        _audio_sync_reference_choices=lambda _entry: [reference],
        _audio_sync_error=SimpleNamespace(emit=lambda *args: errors.append(args)),
        _audio_sync_done=SimpleNamespace(emit=forbidden),
    )
    panel.RemuxPanel._on_audio_sync_requested(cast(Any, fake), cast(Any, target))
    assert errors == [("target", "Jonction non vérifiée")]


def test_audio_filter_uses_source_cut_time_after_cadence_conversion(tmp_path):
    import shutil
    import subprocess
    import wave
    from core.workflows.cadence import CadenceMismatch, CadenceType
    from core.workflows.physical_sync import audio_filter
    from core.workflows.sync_calibration import SyncCalibration, SyncSegment

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg requis")
    rate = 48000
    t = np.arange(rate * 4) / rate
    pcm = (10000 * np.sin(2 * np.pi * 700 * t)).astype('<i2')
    source = tmp_path / 'donor.wav'
    with wave.open(str(source), 'wb') as out:
        out.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        out.writeframes(pcm.tobytes())
    calibration = SyncCalibration(
        (SyncSegment(0, 0), SyncSegment(2000, 500)),
        cadence_mismatch=CadenceMismatch(CadenceType.CUSTOM, 24, 12, .5),
        cadence_audio_method='asetrate',
    )
    result = subprocess.run([
        ffmpeg, '-v', 'error', '-i', str(source), '-filter_complex', audio_filter(calibration, 0),
        '-map', '[out]', '-ar', str(rate), '-f', 'f32le', '-',
    ], capture_output=True, check=True)
    values = np.frombuffer(result.stdout, dtype='<f4')
    # La coupe donneur à 2 s est à 4 s après ralentissement ; le silence est
    # inséré là, et non à 2 s dans la piste déjà ralentie.
    assert np.max(np.abs(values[int(4.05 * rate):int(4.45 * rate)])) < 1e-5
    assert np.std(values[int(2.05 * rate):int(2.45 * rate)]) > .1
    assert np.std(values[int(4.55 * rate):int(4.95 * rate)]) > .1


def test_positive_delay_longer_than_probe_preserves_beginning_and_tail():
    ref, donor = _edited_pair([], [18000])
    segments, _, samples = scan_envelopes(ref, donor)
    assert len(segments) == 1 and segments[0].shift_ms == 18000
    assert samples[-1]['start_ms'] > len(donor) - 8000


def _choice_track(entry_id, file_id, tid, info="5.1", language="", commentary=False):
    from types import SimpleNamespace
    return SimpleNamespace(
        entry_id=entry_id, file_id=file_id, mkv_tid=tid, track_type="audio",
        codec="ac3", display_info=info, language=language, title="", flag_commentary=commentary,
    )


def _choice_panel(tracks):
    from pathlib import Path
    from types import SimpleNamespace
    from ui.panels.remux_panel import panel
    return SimpleNamespace(
        _source_files=[SimpleNamespace(id="A", path=Path("a.mkv")), SimpleNamespace(id="B", path=Path("b.mkv"))],
        _track_table=SimpleNamespace(current_tracks=lambda: tracks),
        _audio_sync_family=panel.RemuxPanel._audio_sync_family,
    )


def test_audio_reference_choices_list_every_track_of_other_sources_same_layout_first():
    from ui.panels.remux_panel import panel

    target = _choice_track("t", "A", 1)
    tracks = [target, _choice_track("b3", "B", 3, "stereo"), _choice_track("b2", "B", 2), _choice_track("b1", "B", 1)]
    choices = panel.RemuxPanel._audio_sync_reference_choices(cast(Any, _choice_panel(tracks)), cast(Any, target))
    # Ordre du tableau conservé (pistes réordonnées), même disposition d'abord.
    assert [entry.entry_id for _label, entry in choices] == ["b2", "b1", "b3"]
    # Cible stéréo, référence 5.1 seule (WEB 25 i/s face à un WEB-DL 23,976 i/s) : proposée.
    stereo = _choice_track("s", "A", 2, "2.0")
    choices = panel.RemuxPanel._audio_sync_reference_choices(
        cast(Any, _choice_panel([stereo, _choice_track("b1", "B", 1)])), cast(Any, stereo))
    assert [entry.entry_id for _label, entry in choices] == ["b1"]


def test_audio_sync_analyses_the_target_source_track_in_the_reference_language():
    from ui.panels.remux_panel import panel

    target = _choice_track("fr", "A", 1, "2.0", "fre")
    commentary = _choice_track("com", "A", 3, "2.0", "eng", commentary=True)
    english = _choice_track("en", "A", 2, "2.0", "eng")
    reference = _choice_track("ref", "B", 1, "5.1", "en-US")
    fake = _choice_panel([target, commentary, english, reference])
    measure = panel.RemuxPanel._audio_sync_measure_entry
    assert measure(cast(Any, fake), cast(Any, target), cast(Any, reference)) is english
    assert measure(cast(Any, fake), cast(Any, english), cast(Any, reference)) is english
    assert measure(cast(Any, fake), cast(Any, target), cast(Any, _choice_track("x", "B", 2, "5.1", ""))) is target
    assert measure(cast(Any, fake), cast(Any, target), None) is target


def _scan_with_samples(monkeypatch, *, quick_check, shift=1006.0, confidence=.95):
    """Scan avec mesures ponctuelles constantes ; retourne (calibration, appels d'enveloppe, journal)."""
    from pathlib import Path
    from core.workflows.audio_sync import AudioSyncTrack
    from core.workflows.audio_sync_scan import AudioSyncScanner

    scanner = AudioSyncScanner(quick_check=quick_check)
    monkeypatch.setattr(scanner, "measure", lambda *args, **kwargs: (shift, confidence))
    monkeypatch.setattr(scanner, "duration", lambda track: 180.0)
    monkeypatch.setattr(scanner, "_dialogue_free_downmixes", lambda *_a: None)
    ref, donor = _edited_pair([], [1006])
    calls = []

    def envelope(track, cadence_filter=None, downmix=None, abort=None):
        calls.append(track.stream_index)
        return ref if track.stream_index == 1 else donor

    monkeypatch.setattr(scanner, "envelope", envelope)
    messages: list[str] = []
    calibration = scanner.scan(AudioSyncTrack(Path("ref.mkv"), 1), AudioSyncTrack(Path("don.mkv"), 2),
                               detect_cuts=True, log=messages.append)
    return calibration, calls, messages


def test_quick_check_skips_continuous_scan_when_samples_agree(monkeypatch):
    calibration, calls, messages = _scan_with_samples(monkeypatch, quick_check=True)
    assert [s.shift_ms for s in calibration.segments] == [1006]
    assert calibration.confidence == pytest.approx(.95)
    assert calls == []
    assert any("Vérification rapide :" in message for message in messages)


def test_quick_check_requires_confident_samples(monkeypatch):
    calibration, calls, messages = _scan_with_samples(monkeypatch, quick_check=True, confidence=.85)
    assert [s.shift_ms for s in calibration.segments] == [1006]
    assert sorted(calls) == [1, 2]
    assert any("Vérification rapide écartée" in message for message in messages)


def test_continuous_scan_kept_without_quick_check(monkeypatch):
    _calibration, calls, messages = _scan_with_samples(monkeypatch, quick_check=False)
    assert sorted(calls) == [1, 2]
    assert not any("Vérification rapide" in message for message in messages)


def test_quick_check_option_overrides_config():
    from types import SimpleNamespace
    from core.workflows.audio_sync_scan import quick_check_enabled

    config = SimpleNamespace(sync_quick_audio_check=True)
    assert quick_check_enabled(SimpleNamespace(), config)
    assert quick_check_enabled(SimpleNamespace(quick_sync_check=None), config)
    assert not quick_check_enabled(SimpleNamespace(quick_sync_check=False), config)
    assert quick_check_enabled(SimpleNamespace(quick_sync_check=True), SimpleNamespace())


def test_envelope_failure_interrupts_other_decode(monkeypatch):
    from pathlib import Path
    from core.workflows.audio_sync import AudioSyncTrack
    from core.workflows.audio_sync_scan import AudioSyncScanner

    scanner = AudioSyncScanner()
    donor_started = threading.Event()
    donor_aborted: list[bool] = []

    def envelope(track, cadence_filter=None, downmix=None, abort=None):
        assert abort is not None
        if track.stream_index == 1:
            assert donor_started.wait(5)
            raise AudioSyncError("échec référence")
        donor_started.set()
        donor_aborted.append(abort.wait(5))
        raise AudioSyncError("Analyse interrompue.")

    monkeypatch.setattr(scanner, "envelope", envelope)
    with pytest.raises(AudioSyncError, match="échec référence"):
        scanner._envelopes(AudioSyncTrack(Path("ref.mkv"), 1), AudioSyncTrack(Path("don.mkv"), 2))
    assert donor_aborted == [True]


def test_run_stops_process_when_aborted():
    import sys
    import time
    from core.workflows.audio_sync_scan import AudioSyncScanner

    abort = threading.Event()
    timer = threading.Timer(0.2, abort.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(AudioSyncError, match="interrompue"):
            AudioSyncScanner()._run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=30, abort=abort)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 10


def test_run_timeout_raises_sync_error():
    import sys
    from core.workflows.audio_sync_scan import AudioSyncScanner

    with pytest.raises(AudioSyncError, match="Délai"):
        AudioSyncScanner()._run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)


@pytest.mark.parametrize("deep, expected", [(False, True), (True, False)])
def test_remux_panel_quick_check_unless_deep(monkeypatch, deep, expected):
    from types import SimpleNamespace
    from ui.panels.remux_panel import panel
    from core.workflows.audio_sync_scan import AudioSyncScanner

    target = SimpleNamespace(track_type="audio", mkv_tid=2, entry_id="target")
    reference = SimpleNamespace(mkv_tid=1, entry_id="reference")
    dialogs = []
    monkeypatch.setattr(panel, "_AudioSyncReferenceDialog", lambda *_a, **_k: dialogs.append(1))
    seen = []

    def scan(scanner, *_a, **_k):
        seen.append(scanner.quick_check)
        raise AudioSyncError("arrêt")

    monkeypatch.setattr(AudioSyncScanner, "scan", scan)
    fake = _fake_sync_panel()
    panel.RemuxPanel._on_audio_sync_requested(cast(Any, fake), cast(Any, target), deep=deep,
                                              reference_entry=cast(Any, reference))
    assert seen == [expected]
    assert dialogs == []


def test_remux_panel_passes_detected_cadence_to_scan(monkeypatch):
    from types import SimpleNamespace
    from ui.panels.remux_panel import panel
    from core.workflows.audio_sync_scan import AudioSyncScanner
    from core.workflows.cadence import CadenceMismatch, CadenceType

    mismatch = CadenceMismatch(CadenceType.PAL_TO_FILM_23976, 25.0, 23.976, 24000.0 / 25025.0)
    target = SimpleNamespace(track_type="audio", mkv_tid=2, entry_id="target")
    reference = SimpleNamespace(mkv_tid=1, entry_id="reference")
    seen = []

    def scan(_scanner, *_a, **kwargs):
        seen.append((kwargs.get("cadence_mismatch"), kwargs.get("cadence_audio_method")))
        raise AudioSyncError("arrêt")

    monkeypatch.setattr(AudioSyncScanner, "scan", scan)
    fake = _fake_sync_panel(
        _audio_sync_cadence=lambda *_a: mismatch,
        _config=SimpleNamespace(tool_ffmpeg="ffmpeg", tool_ffprobe="ffprobe", sync_cadence_audio_method="asetrate"),
    )
    panel.RemuxPanel._on_audio_sync_requested(cast(Any, fake), cast(Any, target), reference_entry=cast(Any, reference))
    assert seen == [(mismatch, "asetrate")]


def test_imperceptible_constant_respects_lead_and_lag_thresholds():
    from core.workflows.audio_sync_segments import imperceptible_constant

    def anchors(shifts):
        return [Anchor(index * 4000, shift, .9) for index, shift in enumerate(shifts)]

    assert imperceptible_constant(anchors([200] * 10)) == 200
    # Son en avance de 45 ms au plus (C ≥ max − 45), en retard de 125 ms au plus (C ≤ min + 125).
    assert imperceptible_constant(anchors([0] * 99 + [-150])) == -25
    assert imperceptible_constant(anchors([0] * 99 + [171])) is None


def test_verified_small_junctions_follow_drift_threshold():
    # Jonctions vérifiées : le seuil de coupure (--drift-threshold-ms) reste seul juge.
    ref, donor = _edited_pair([60000, 120000], [200, 240, 200])
    segments, _, _ = scan_envelopes(ref, donor)
    assert [s.shift_ms for s in segments] == [200, 240, 200]


def test_unverifiable_junction_falls_back_to_imperceptible_constant(monkeypatch):
    from core.workflows import audio_sync_segments

    def unverified(*_args):
        raise AudioSyncError("Jonction vers 60.000 s non vérifiée des deux côtés (0.50, 0.40) ; calibration manuelle requise.")

    monkeypatch.setattr(audio_sync_segments, "locate_transition", unverified)
    ref, donor = _edited_pair([90000], [200, 300])
    messages: list[str] = []
    segments, _, _ = scan_envelopes(ref, donor, log=messages.append)
    assert len(segments) == 1 and 255 <= segments[0].shift_ms <= 325
    assert any("décalage constant" in message for message in messages)
    # Écart au-delà des seuils : refus inchangé.
    ref, donor = _edited_pair([90000], [200, 900])
    with pytest.raises(AudioSyncError, match="non vérifiée"):
        scan_envelopes(ref, donor)


def test_dts_is_decoded_core_only(monkeypatch):
    import json
    import subprocess
    from pathlib import Path
    from core.workflows.audio_sync import AudioSyncTrack
    from core.workflows.audio_sync_scan import AudioSyncScanner

    scanner = AudioSyncScanner()
    streams = {1: ("dts", 6, 48000), 2: ("truehd", 8, 48000), 3: ("dts", 8, 48000), 4: ("dts", 6, 96000),
               5: ("dts", 6, 48000)}
    probes = []

    def run(command, **_kwargs):
        probes.append(command)
        index = int(command[command.index("-select_streams") + 1])
        codec, channels, rate = streams[index]
        stream = {"codec_name": codec, "channels": channels, "sample_rate": str(rate),
                  "profile": "DTS Express" if index == 5 else "DTS-HD MA"}
        return subprocess.CompletedProcess(command, 0, json.dumps({"streams": [stream]}), "")

    monkeypatch.setattr(scanner, "_run", run)
    dts = AudioSyncTrack(Path("a.mkv"), 1)
    assert scanner._decoder_args(dts) == ["-core_only", "1"]
    assert scanner._decoder_args(AudioSyncTrack(Path("a.mkv"), 2)) == []
    # Cœur 5.1 à 48 kHz d'un DTS-HD MA 7.1 ou 96 kHz : disposition ou fréquence différente.
    assert scanner._decoder_args(AudioSyncTrack(Path("a.mkv"), 3)) == []
    assert scanner._decoder_args(AudioSyncTrack(Path("a.mkv"), 4)) == []
    assert scanner._decoder_args(dts) == ["-core_only", "1"] and len(probes) == 4
    # DTS Express (LBR seul) : aucun cœur à décoder.
    assert scanner._decoder_args(AudioSyncTrack(Path("a.mkv"), 5)) == []


@pytest.mark.parametrize("stream_index, spec", [(2, "0:2"), ("0:a:1", "0:a:1")])
def test_envelopes_select_the_requested_stream(monkeypatch, stream_index, spec):
    """Index absolu (GUI) ou spécificateur (`sync-scan --stream-ref 0:a:1`) : même flux partout."""
    import subprocess
    from pathlib import Path
    from core.workflows.audio_sync import AudioSyncTrack
    from core.workflows.audio_sync_scan import AudioSyncScanner

    scanner = AudioSyncScanner()
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, b"\x00" * 16, b"")

    monkeypatch.setattr(scanner, "_run", run)
    monkeypatch.setattr(scanner, "_decoder_args", lambda _track: [])
    monkeypatch.setattr(scanner, "duration", lambda _track: 10.0)
    track = AudioSyncTrack(Path("a.mkv"), stream_index)
    scanner.envelope(track)
    scanner.envelope_mixes(track, None, (None, "pan=mono|c0=c0"))
    assert commands[0][commands[0].index("-map") + 1] == spec
    assert commands[1][commands[1].index("-filter_complex") + 1].startswith(f"[{spec}]asplit=2")
