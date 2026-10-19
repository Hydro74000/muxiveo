"""Un benchmark FEL ne doit pas poursuivre après une annulation non confirmée."""
from unittest.mock import Mock

import pytest

from scripts import benchmark_fel_workflow as benchmark
from scripts.benchmark_fel_matrix import benchmark_preset, matrix_summary
from core.workflows.encode.domain.codecs import preset_problem
from core.workflows.encode.models import VideoEncodeSettings


@pytest.mark.parametrize("terminal", [{"cancelled": True}, {"finished": "ok"}, {"failed": "erreur"}])
def test_benchmark_waits_for_cancellation(monkeypatch, terminal):
    state = {"finished": None, "failed": None, "cancelled": False, **terminal}
    wait = Mock(return_value=state)
    monkeypatch.setattr(benchmark, "wait_task", wait)
    signals = Mock()
    benchmark.cancel_and_wait(signals)
    signals.cancel.assert_called_once()
    wait.assert_called_once_with(signals, timeout=15)


def test_benchmark_stops_matrix_when_cancellation_is_unconfirmed(monkeypatch):
    monkeypatch.setattr(benchmark, "wait_task", Mock(return_value={"finished": None, "failed": None, "cancelled": False}))
    with pytest.raises(benchmark.BenchmarkCleanupError):
        benchmark.cancel_and_wait(Mock())
    assert not issubclass(benchmark.BenchmarkCleanupError, Exception)


def test_matrix_without_baseline_does_not_invent_an_overhead():
    case = {"codec": "libx265", "rife_gpu_choice": "off", "fel_choice": "cpu", "seconds": 10,
            "frames": 24, "width": 3840, "height": 2160}
    assert matrix_summary([case])[0]["added_percent_same_rife"] is None


@pytest.mark.parametrize("codec", ["libx265", "nvencc_hevc", "hevc_nvenc"])
def test_benchmark_codec_defaults_are_accepted(codec):
    video = VideoEncodeSettings(codec=codec, preset=benchmark_preset(codec))
    assert preset_problem(video) == ("", "")
    if codec == "libx265":
        assert video.preset == "medium"
