from __future__ import annotations

from pathlib import Path

import pytest

from core.workflows.audio_sync import (
    AudioProbe,
    AudioSyncError,
    AudioSyncTrack,
    AudioSyncWorkflow,
    _MarkerSignature,
)


def test_best_lag_returns_negative_offset_when_target_is_late():
    workflow = AudioSyncWorkflow()
    reference = [0.0] * 80
    target = [0.0] * 80
    for idx, value in enumerate([0.0, 3.0, 8.0, 2.0, 0.0]):
        reference[30 + idx] = value
        target[34 + idx] = value

    lag, confidence = workflow._best_lag(
        workflow._normalize(reference),
        workflow._normalize(target),
    )

    assert lag == 4
    assert confidence > 0.9
    assert -lag * 100 == -400


def test_analysis_mode_accepts_stereo_when_both_tracks_are_stereo():
    assert AudioSyncWorkflow._analysis_mode(
        AudioProbe(channels=2, channel_layout="stereo"),
        AudioProbe(channels=2, channel_layout="stereo"),
    ) == "stereo"


def test_analysis_mode_rejects_mixed_stereo_and_surround():
    with pytest.raises(AudioSyncError):
        AudioSyncWorkflow._analysis_mode(
            AudioProbe(channels=6, channel_layout="5.1(side)"),
            AudioProbe(channels=2, channel_layout="stereo"),
        )


def test_ensure_compatible_rejects_mono():
    with pytest.raises(AudioSyncError):
        AudioSyncWorkflow._ensure_compatible(
            AudioProbe(channels=1, channel_layout="mono"),
            "target",
        )


def test_stereo_marker_signature_keeps_one_sided_impacts():
    workflow = AudioSyncWorkflow()
    values = [1.0] * 140
    companion = [1.0] * 140
    for idx in (24, 68, 112):
        values[idx] = 120.0
        companion[idx] = 20.0

    signature = workflow._marker_signature("left", values, companion=companion)

    assert signature.marker_count == 3
    assert all(signature.values[idx] > 0.0 for idx in (24, 68, 112))


def test_stereo_lag_uses_same_side_markers(monkeypatch):
    workflow = AudioSyncWorkflow()

    def markers(indices: tuple[int, ...]) -> list[float]:
        values = [0.0] * 120
        for idx in indices:
            values[idx] = 1.0
            values[idx - 1] = 0.45
            values[idx + 1] = 0.45
        return values

    reference = AudioSyncTrack(Path("reference.mkv"), 1)
    target = AudioSyncTrack(Path("target.mkv"), 2)

    def fake_signatures(track: AudioSyncTrack) -> list[_MarkerSignature]:
        if track is reference:
            return [_MarkerSignature("left", markers((20, 52, 88)), 3, 4.0, (20, 52, 88))]
        return [_MarkerSignature("left", markers((24, 56, 92)), 3, 4.0, (24, 56, 92))]

    monkeypatch.setattr(workflow, "_stereo_signatures", fake_signatures)

    lag, confidence = workflow._detect_stereo_lag(reference, target)

    assert lag == 4
    assert confidence > 0.9


def test_stereo_lag_rejects_markers_on_different_sides(monkeypatch):
    workflow = AudioSyncWorkflow()

    def markers(indices: tuple[int, ...]) -> list[float]:
        values = [0.0] * 120
        for idx in indices:
            values[idx] = 1.0
        return values

    reference = AudioSyncTrack(Path("reference.mkv"), 1)
    target = AudioSyncTrack(Path("target.mkv"), 2)

    def fake_signatures(track: AudioSyncTrack) -> list[_MarkerSignature]:
        if track is reference:
            return [_MarkerSignature("left", markers((20, 52, 88)), 3, 4.0, (20, 52, 88))]
        return [_MarkerSignature("right", markers((24, 56, 92)), 3, 4.0, (24, 56, 92))]

    monkeypatch.setattr(workflow, "_stereo_signatures", fake_signatures)

    with pytest.raises(AudioSyncError):
        workflow._detect_stereo_lag(reference, target)
