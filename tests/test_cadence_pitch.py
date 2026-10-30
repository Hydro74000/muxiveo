"""Tests pour l'analyse acoustique de hauteur tonale (Pitch / Hz) et sélection automatique de la cadence."""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner
from core.workflows.cadence import CadenceMismatch, CadenceType
from core.workflows.cadence_pitch import (
    CadencePitchAnalysis,
    analyze_cadence_pitch,
    compute_welch_psd,
    extract_f0_candidates,
)
from core.workflows.sync_calibration import SyncCalibration, SyncSegment


def test_cadence_pitch_analysis_dataclass_and_serialization():
    analysis = CadencePitchAnalysis(
        selected_method="asetrate",
        pitch_shift_semitones=0.71,
        spectral_corr_asetrate=0.998,
        spectral_corr_atempo=0.755,
        f0_ref_hz=132.2,
        f0_donor_hz=139.9,
        confidence=0.95,
        details="PAL Speedup non compensé",
    )
    data = analysis.to_dict()
    assert data["selected_method"] == "asetrate"
    assert data["pitch_shift_semitones"] == 0.71
    assert data["spectral_corr_asetrate"] == 0.998
    assert data["spectral_corr_atempo"] == 0.755
    assert data["f0_ref_hz"] == 132.2
    assert data["f0_donor_hz"] == 139.9
    assert data["confidence"] == 0.95
    assert data["details"] == "PAL Speedup non compensé"

    restored = CadencePitchAnalysis.from_dict(data)
    assert restored == analysis

    assert CadencePitchAnalysis.from_dict(None) is None
    assert CadencePitchAnalysis.from_dict({}) is None
    assert CadencePitchAnalysis.from_dict(cast(Any, "not a dict")) is None


def test_compute_welch_psd():
    sr = 16000
    # Signal trop court (< 256)
    short = np.zeros(100)
    assert len(compute_welch_psd(short, sr)) == 0

    # Signal 1 seconde avec un ton pur à 400 Hz
    t = np.linspace(0, 1.0, sr, endpoint=False)
    sig = np.sin(2 * np.pi * 400.0 * t)
    psd = compute_welch_psd(sig, sr, nperseg=1024)
    assert len(psd) == 513  # rfft of 1024 points is 513
    freqs = np.fft.rfftfreq(1024, 1.0 / sr)
    peak_freq = freqs[np.argmax(psd)]
    assert abs(peak_freq - 400.0) < 20.0


def test_extract_f0_candidates():
    sr = 16000
    t = np.linspace(0, 2.0, sr * 2, endpoint=False)

    # Signal pur à 150 Hz
    tone_150 = np.sin(2 * np.pi * 150.0 * t)
    f0_list = extract_f0_candidates(tone_150, sr, min_f0=75.0, max_f0=400.0)
    assert len(f0_list) > 0
    med_f0 = float(np.median(f0_list))
    assert abs(med_f0 - 150.0) < 3.0

    # Signal pur à 220 Hz
    tone_220 = np.sin(2 * np.pi * 220.0 * t)
    f0_list_220 = extract_f0_candidates(tone_220, sr)
    assert len(f0_list_220) > 0
    assert abs(float(np.median(f0_list_220)) - 220.0) < 3.0

    # Silence
    silence = np.zeros(sr)
    assert extract_f0_candidates(silence, sr) == []


def test_analyze_cadence_pitch_short_sample():
    sr = 16000
    # Moins de 1s
    res = analyze_cadence_pitch(np.zeros(500), np.zeros(500), np.zeros(500), sr=sr)
    assert res.selected_method == "atempo"
    assert res.confidence == 0.5


def test_analyze_cadence_pitch_pal_speedup_favors_asetrate():
    """Simule un PAL speedup non compensé historique : asetrate restaure le pitch original."""
    sr = 16000
    t = np.linspace(0, 4.0, int(sr * 4.0), endpoint=False)

    f_orig = 160.0
    f_pal = f_orig * (25025.0 / 24000.0)  # ~166.83 Hz

    # Référence cinéma à 160 Hz
    ref = np.sin(2 * np.pi * f_orig * t) + 0.5 * np.sin(2 * np.pi * 2 * f_orig * t)
    # asetrate a ralenti et descendu le pitch à 160 Hz
    donor_ase = np.sin(2 * np.pi * f_orig * t) + 0.5 * np.sin(2 * np.pi * 2 * f_orig * t)
    # atempo a ralenti mais conservé le pitch accéléré à 166.83 Hz
    donor_ate = np.sin(2 * np.pi * f_pal * t) + 0.5 * np.sin(2 * np.pi * 2 * f_pal * t)

    mismatch = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_23976,
        source_fps=25.0,
        target_fps=23.976,
        speed_factor=24000.0 / 25025.0,
    )

    res = analyze_cadence_pitch(ref, donor_ase, donor_ate, sr=sr, mismatch=mismatch)
    assert res.selected_method == "asetrate"
    assert res.confidence >= 0.7
    assert res.spectral_corr_asetrate > res.spectral_corr_atempo
    assert "asetrate" in res.details


def test_analyze_cadence_pitch_compensated_favors_atempo():
    """Simule une diffusion broadcast compensée : la hauteur originale est déjà préservée sur le donneur."""
    sr = 16000
    t = np.linspace(0, 4.0, int(sr * 4.0), endpoint=False)

    f_orig = 160.0
    f_down = f_orig * (24000.0 / 25025.0)  # ~153.44 Hz

    # Référence cinéma à 160 Hz
    ref = np.sin(2 * np.pi * f_orig * t) + 0.5 * np.sin(2 * np.pi * 2 * f_orig * t)
    # atempo a conservé le pitch 160 Hz
    donor_ate = np.sin(2 * np.pi * f_orig * t) + 0.5 * np.sin(2 * np.pi * 2 * f_orig * t)
    # asetrate a descendu le pitch à 153.4 Hz
    donor_ase = np.sin(2 * np.pi * f_down * t) + 0.5 * np.sin(2 * np.pi * 2 * f_down * t)

    mismatch = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_23976,
        source_fps=25.0,
        target_fps=23.976,
        speed_factor=24000.0 / 25025.0,
    )

    res = analyze_cadence_pitch(ref, donor_ase, donor_ate, sr=sr, mismatch=mismatch)
    assert res.selected_method == "atempo"
    assert res.confidence >= 0.65
    assert res.spectral_corr_atempo > res.spectral_corr_asetrate
    assert "atempo" in res.details


def test_sync_calibration_with_pitch_analysis():
    analysis = CadencePitchAnalysis(
        selected_method="asetrate",
        pitch_shift_semitones=0.707,
        spectral_corr_asetrate=0.99,
        spectral_corr_atempo=0.74,
        f0_ref_hz=130.0,
        f0_donor_hz=138.0,
        confidence=0.95,
        details="PAL Speedup non compensé détecté",
    )
    mismatch = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_23976,
        source_fps=25.0,
        target_fps=23.976,
        speed_factor=24000.0 / 25025.0,
    )

    calib = SyncCalibration(
        (SyncSegment(0, 100.0),),
        confidence=0.9,
        cadence_mismatch=mismatch,
        cadence_audio_method="asetrate",
        cadence_pitch_analysis=analysis,
    )

    data = calib.to_dict()
    assert data["cadence_audio_method"] == "asetrate"
    assert data["cadence_pitch_analysis"]["selected_method"] == "asetrate"

    restored = SyncCalibration.from_dict(data)
    assert restored.cadence_audio_method == "asetrate"
    assert isinstance(restored.cadence_pitch_analysis, CadencePitchAnalysis)
    assert restored.cadence_pitch_analysis.selected_method == "asetrate"

    summary = calib.summary_lines()
    assert any("23.976 FPS" in line for line in summary)
    assert any("asetrate" in line for line in summary)
    assert any("PAL Speedup non compensé détecté" in line for line in summary)


def test_audio_sync_scanner_auto_pitch_detection_mocked(monkeypatch):
    """Vérifie que AudioSyncScanner.scan déclenche l'analyse de pitch lorsque cadence_audio_method='auto'."""
    scanner = AudioSyncScanner("ffmpeg", "ffprobe")

    mismatch = CadenceMismatch(
        cadence_type=CadenceType.PAL_TO_FILM_23976,
        source_fps=25.0,
        target_fps=23.976,
        speed_factor=24000.0 / 25025.0,
    )

    sr = 16000
    t = np.linspace(0, 4.0, int(sr * 4.0), endpoint=False)
    ref_audio = np.sin(2 * np.pi * 150.0 * t)
    ase_audio = np.sin(2 * np.pi * 150.0 * t)
    ate_audio = np.sin(2 * np.pi * 156.4 * t)

    def mock_pitch_samples(track, start, duration, cadence_filter=None):
        if cadence_filter and "asetrate" in cadence_filter:
            return ase_audio
        elif cadence_filter and "atempo" in cadence_filter:
            return ate_audio
        return ref_audio

    monkeypatch.setattr(scanner, "pitch_samples", mock_pitch_samples)
    monkeypatch.setattr(scanner, "duration", lambda track: 100.0)
    monkeypatch.setattr(scanner, "measure", lambda r, d, p, w, cadence_filter=None, speed_factor=1.0: (120.0, 0.95))

    logs = []
    calib = scanner.scan(
        AudioSyncTrack(Path("ref.mkv"), 0),
        AudioSyncTrack(Path("donor.mkv"), 0),
        cadence_mismatch=mismatch,
        cadence_audio_method="auto",
        log=lambda msg: logs.append(msg),
    )

    assert calib.cadence_audio_method == "asetrate"
    assert calib.cadence_pitch_analysis is not None
    assert calib.cadence_pitch_analysis.selected_method == "asetrate"
    assert any("Analyse acoustique de cadence" in log for log in logs)


_PAL = CadenceMismatch(
    cadence_type=CadenceType.PAL_TO_FILM_23976,
    source_fps=25.0,
    target_fps=23.976,
    speed_factor=24000.0 / 25025.0,
    speed_ratio="24000/25025",
)


def _harmonics(freq: float, sr: int = 16000, seconds: float = 4.0):
    t = np.arange(int(sr * seconds)) / sr
    return sum(np.sin(2 * np.pi * freq * k * t) / k for k in range(1, 12))


def test_spectral_pitch_shift_measures_cents_between_two_mixes():
    from core.workflows.cadence_pitch import spectral_pitch_shift_cents

    reference = _harmonics(180.0)
    cents, score = spectral_pitch_shift_cents(reference, _harmonics(180.0 * 2 ** (72 / 1200)), 16000)
    assert cents == pytest.approx(72, abs=2) and score > 0.95
    cents, score = spectral_pitch_shift_cents(reference, _harmonics(180.0 * 2 ** (-40 / 1200)), 16000)
    assert cents == pytest.approx(-40, abs=2) and score > 0.95
    assert spectral_pitch_shift_cents(reference[:100], reference, 16000) == (0.0, 0.0)


@pytest.mark.parametrize("donor_freq,method,semitones", [
    (180.0 * 25025 / 24000, "asetrate", 0.72),  # hauteur montée par la diffusion PAL
    (180.0, "atempo", 0.0),                     # hauteur déjà compensée
])
def test_analyze_cadence_pitch_decides_from_the_spectral_shift(donor_freq, method, semitones):
    reference = _harmonics(180.0)
    donor_ate = _harmonics(donor_freq)
    donor_ase = _harmonics(donor_freq * 24000 / 25025)
    result = analyze_cadence_pitch(reference, donor_ase, donor_ate, sr=16000, mismatch=_PAL)
    assert result.selected_method == method
    assert result.pitch_shift_semitones == pytest.approx(semitones, abs=0.03)
    assert "spectre" in result.details


@pytest.mark.parametrize("measured", [True, False])
def test_resolve_cadence_method_analyses_the_longest_played_range(monkeypatch, measured):
    """Milieu du plus long morceau joué ; décalage mesuré à cet endroit (réglage manuel approximatif),
    sinon celui de la calibration."""
    from core.workflows.audio_sync import AudioSyncError

    scanner = AudioSyncScanner("ffmpeg", "ffprobe")
    monkeypatch.setattr(scanner, "duration", lambda _track: 1000.0)

    def measure(_reference, _donor, start, window, cadence_filter=None, speed_factor=1.0, **_kwargs):
        assert "asetrate" in cadence_filter and window == 60
        if not measured:
            raise AudioSyncError("corrélation insuffisante")
        return 7420.0, 0.95

    monkeypatch.setattr(scanner, "measure", measure)
    analysis = CadencePitchAnalysis("asetrate", 0.72, 0.9, 0.8, 0.0, 0.0, details="ok")
    calls = []

    def analyse(_reference, _donor, p_ref, offset_ms, mismatch, duration=4.0):
        calls.append((p_ref, offset_ms, mismatch))
        return analysis

    monkeypatch.setattr(scanner, "_analyze_cadence_pitch", analyse)
    calibration = SyncCalibration((SyncSegment(0, 5000.0), SyncSegment(100_000, 7000.0)),
                                  cadence_mismatch=_PAL, cadence_audio_method="auto")
    resolved = scanner.resolve_cadence_method(
        AudioSyncTrack(Path("ref.mkv"), 1), AudioSyncTrack(Path("donor.mkv"), 2), calibration,
    )
    assert resolved.cadence_audio_method == "asetrate" and resolved.cadence_pitch_analysis is analysis
    assert resolved.segments == calibration.segments
    # Second morceau, le plus long : de 100 s × 25025/24000 + 7 s jusqu'à la fin de la référence.
    start_ms = 100_000 * 25025 / 24000 + 7000
    assert calls == [(pytest.approx((start_ms + 1_000_000) / 2000 - 2), 7420.0 if measured else 7000.0, _PAL)]


def test_resolve_cadence_method_keeps_explicit_methods_and_falls_back_to_atempo(monkeypatch):
    scanner = AudioSyncScanner("ffmpeg", "ffprobe")
    monkeypatch.setattr(scanner, "duration", lambda _track: 1000.0)
    monkeypatch.setattr(scanner, "measure", lambda *_args, **_kwargs: (0.0, 0.95))
    monkeypatch.setattr(scanner, "_analyze_cadence_pitch", lambda *_args, **_kwargs: None)
    tracks = (AudioSyncTrack(Path("ref.mkv"), 1), AudioSyncTrack(Path("donor.mkv"), 2))
    explicit = SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=_PAL, cadence_audio_method="asetrate")
    without_cadence = SyncCalibration.linear(250)
    assert scanner.resolve_cadence_method(*tracks, explicit) is explicit
    assert scanner.resolve_cadence_method(*tracks, without_cadence) is without_cadence
    logs: list[str] = []
    unresolved = scanner.resolve_cadence_method(
        *tracks, SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=_PAL, cadence_audio_method="auto"),
        log=logs.append,
    )
    assert unresolved.cadence_audio_method == "atempo" and logs


def test_acoustic_cadence_pitch_reads_the_target_where_it_plays(monkeypatch):
    """Cadence détectée par la dérive (sans métadonnées) : décalage mesuré sans conversion, la cible
    est lue à l'instant de la référence moins ce décalage, sans étirement."""
    scanner = AudioSyncScanner("ffmpeg", "ffprobe")
    monkeypatch.setattr(scanner, "duration", lambda _track: 3000.0)
    # Dérive PAL : 5 s au départ, +41,7 ms par seconde de référence.
    monkeypatch.setattr(scanner, "measure", lambda _r, _d, p, _w, cadence_filter=None, speed_factor=1.0, **_k:
                        (5000.0 + 41.7 * p, 0.95))
    calls = []

    def analyse(_reference, _donor, p_ref, offset_ms, _mismatch, duration=4.0, donor_start=None):
        calls.append((p_ref, offset_ms, donor_start))
        return None

    monkeypatch.setattr(scanner, "_analyze_cadence_pitch", analyse)
    calibration = scanner.scan(AudioSyncTrack(Path("ref.mkv"), 1), AudioSyncTrack(Path("donor.mkv"), 2),
                               cadence_audio_method="auto")
    assert calibration.cadence_mismatch is not None
    p_ref, offset_ms, donor_start = calls[0]
    assert (p_ref, offset_ms) == (28.0, 5000.0) and donor_start == pytest.approx(23.0)


@pytest.mark.parametrize("atempo_measurable", [True, False])
def test_atempo_output_is_measured_exactly_then_compensated(monkeypatch, atempo_measurable):
    """Sortie atempo : mesures en asetrate (exactes), avance propre à atempo mesurée à part et
    ajoutée ; non mesurable : synchro de la mesure exacte conservée."""
    from core.workflows.audio_sync import AudioSyncError

    scanner = AudioSyncScanner("ffmpeg", "ffprobe", quick_check=True)
    monkeypatch.setattr(scanner, "duration", lambda _track: 3000.0)
    filters: list[str] = []

    def measure(_r, _d, _p, _w, cadence_filter=None, speed_factor=1.0, **_k):
        filters.append(cadence_filter or "")
        if "atempo" in (cadence_filter or ""):
            if not atempo_measurable:
                raise AudioSyncError("corrélation insuffisante")
            return 311.0, 0.8
        return 300.0, 0.95

    monkeypatch.setattr(scanner, "measure", measure)
    logs: list[str] = []
    calibration = scanner.scan(AudioSyncTrack(Path("ref.mkv"), 1), AudioSyncTrack(Path("donor.mkv"), 2),
                               detect_cuts=True, cadence_mismatch=_PAL, cadence_audio_method="atempo", log=logs.append)
    assert calibration.cadence_audio_method == "atempo" and calibration.cadence_method_requested == "atempo"
    assert all("asetrate" in item for item in filters[:6])
    assert calibration.segments[0].shift_ms == pytest.approx(311.0 if atempo_measurable else 300.0)
    assert any("atempo" in line and ("compensé" if atempo_measurable else "non mesurable") in line for line in logs)


def test_cancelled_cadence_method_resolution_is_never_decided_as_atempo(monkeypatch):
    """Annulation pendant l'analyse de hauteur : erreur remontée, jamais une méthode atempo décidée."""
    import threading
    from core.workflows.audio_sync import AudioSyncError

    cancel = threading.Event()
    scanner = AudioSyncScanner("ffmpeg", "ffprobe", cancel_event=cancel)
    monkeypatch.setattr(scanner, "duration", lambda _track: 1000.0)

    def cancelled(*_args, **_kwargs):
        cancel.set()
        raise AudioSyncError("Analyse annulée.")

    monkeypatch.setattr(scanner, "measure", cancelled)
    monkeypatch.setattr(scanner, "pitch_samples", cancelled)
    tracks = (AudioSyncTrack(Path("ref.mkv"), 1), AudioSyncTrack(Path("donor.mkv"), 2))
    auto = SyncCalibration((SyncSegment(0, 0.0),), cadence_mismatch=_PAL, cadence_audio_method="auto")
    with pytest.raises(AudioSyncError, match="annulée"):
        scanner.resolve_cadence_method(*tracks, auto)
