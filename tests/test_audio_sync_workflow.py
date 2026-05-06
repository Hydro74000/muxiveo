from __future__ import annotations

import pytest

from core.workflows.audio_sync import AudioProbe, AudioSyncError, AudioSyncWorkflow


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


def test_ensure_compatible_rejects_stereo():
    with pytest.raises(AudioSyncError):
        AudioSyncWorkflow._ensure_compatible(
            AudioProbe(channels=2, channel_layout="stereo"),
            "target",
        )
