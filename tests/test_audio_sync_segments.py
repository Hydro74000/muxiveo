"""Mesures de contenu : les jonctions détectées doivent correspondre aux edits réels."""
from __future__ import annotations

import numpy as np
import pytest

from core.workflows.audio_sync import AudioSyncError
from core.workflows.audio_sync_segments import Anchor, locate_transition, match_window, scan_envelopes


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


def test_long_unverified_interval_is_rejected():
    ref, donor = _edited_pair([], [200])
    donor[60000:120000] = 0
    with pytest.raises(AudioSyncError, match="non vérifié"):
        scan_envelopes(ref, donor)


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

    def fail(*_a, **_k):
        raise AudioSyncError("Jonction non vérifiée")

    def forbidden(*_a, **_k):
        pytest.fail("Une analyse rejetée ne doit pas devenir un offset constant")

    monkeypatch.setattr(AudioSyncScanner, "scan", fail)
    monkeypatch.setattr(AudioSyncWorkflow, "detect_offset", forbidden)
    errors = []
    fake = SimpleNamespace(
        _audio_sync_family=lambda _entry: "surround",
        _audio_sync_reference_choices=lambda _entry: [reference],
        _audio_sync_track=lambda entry: entry,
        _config=SimpleNamespace(tool_ffmpeg="ffmpeg", tool_ffprobe="ffprobe"),
        log_message=SimpleNamespace(emit=lambda *_a: None),
        audio_sync_started=SimpleNamespace(emit=lambda *_a: None),
        _audio_sync_error=SimpleNamespace(emit=lambda *args: errors.append(args)),
        _audio_sync_done=SimpleNamespace(emit=forbidden),
        _executor=SimpleNamespace(submit=lambda callback: callback()),
    )
    panel.RemuxPanel._on_audio_sync_requested(fake, target)
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
