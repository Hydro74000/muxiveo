"""Origine démuxée des pistes réécrites, y compris le pré-roll AC3."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.workflows.encode.models import EncodeError
from core.workflows.encode.runtime.native_mux import _rewritten_audio_time_shift


@pytest.mark.parametrize("start_time, expected", [("-0.005000", 5), ("0.005000", -5), ("0.000000", 0)])
def test_rewritten_audio_uses_demuxed_origin(monkeypatch, start_time, expected):
    calls = []

    def probe(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps({"streams": [{"start_time": start_time}]}))

    monkeypatch.setattr(subprocess, "run", probe)
    assert _rewritten_audio_time_shift(Path("rewritten.mka"), "/custom/ffprobe") == expected
    args, kwargs = calls[0]
    assert args[0] == "/custom/ffprobe"
    assert args[-1] == "rewritten.mka"
    assert "stream=start_time" in args
    assert kwargs["timeout"] == 20


@pytest.mark.parametrize("stdout", [
    '{"streams": []}', '{"streams": [{}]}',
    '{"streams": [{"start_time": "N/A"}]}', "invalid",
])
def test_unreadable_origin_fails_instead_of_shifting_silently(monkeypatch, stdout):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=stdout))
    with pytest.raises(EncodeError, match="Origine audio"):
        _rewritten_audio_time_shift(Path("rewritten.mka"), "ffprobe")
