"""Tests pour l'analyse acoustique de hauteur tonale (Pitch / Hz) et sélection automatique de la cadence."""
from __future__ import annotations

import math
from unittest.mock import MagicMock

import numpy as np
import pytest

from core.workflows.audio_sync import AudioSyncTrack
from core.workflows.audio_sync_scan import AudioSyncScanner
from core.workflows.cadence import CadenceAudioMethod, CadenceMismatch, CadenceType, build_cadence_audio_filter
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
    assert CadencePitchAnalysis.from_dict("not a dict") is None


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
        AudioSyncTrack("ref.mkv", 0),
        AudioSyncTrack("donor.mkv", 0),
        cadence_mismatch=mismatch,
        cadence_audio_method="auto",
        log=lambda msg: logs.append(msg),
    )

    assert calib.cadence_audio_method == "asetrate"
    assert calib.cadence_pitch_analysis is not None
    assert calib.cadence_pitch_analysis.selected_method == "asetrate"
    assert any("Analyse acoustique de cadence" in log for log in logs)
