"""Rejet injecté au contrôle final d'un vrai Remux/Encode FFmpeg ou natif."""

import shutil

import pytest

from core.workflows.encode import EncodeConfig, EncodeWorkflow, VideoEncodeSettings
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from tests.integration._synth import ffprobe_json, make_av_container, wait_task


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg et ffprobe requis",
)


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    path = tmp_path_factory.mktemp("override-source") / "source.mkv"
    make_av_container(path, duration=0.4)
    return path


@pytest.mark.parametrize("operation", ["remux", "encode"])
@pytest.mark.parametrize("backend", ["ffmpeg", "native"])
@pytest.mark.parametrize("answer", [True, False, "cancel"])
def test_final_override_preserves_output_until_decision(qt_app, source, tmp_path, monkeypatch, operation, backend, answer):
    output = tmp_path / "output.mkv"
    output.write_bytes(b"old output")
    work = tmp_path / "work"
    requests = []
    warnings = []
    state = {}

    def decide(candidate, message, cancelled):
        assert output.read_bytes() == b"old output"
        assert candidate.is_file()
        assert "rejet simulé du contrôle final" in message
        requests.append(candidate)
        if answer == "cancel":
            state["signals"]._cancel_event.set()
            return True
        return answer

    for module in ("core.workflows.remux_runtime", "core.workflows.remux_backend",
                   "core.workflows.common.matroska_finalize", "core.workflows.encode.runtime.native_mux"):
        monkeypatch.setattr(f"{module}.validate_matroska_output", lambda *_a, **_k: ["rejet simulé du contrôle final"])
    if operation == "remux":
        tracks = [TrackEntry(0, "video", "H264", "", "und", "")]
        config = RemuxConfig(
            sources=[SourceInput(source, 0, tracks)], track_order=[(0, 0)],
            output=output, work_dir=work, mux_backend=backend, keep_chapters=False,
        )
        workflow = RemuxWorkflow(generate_nfo=False, ffmpeg_threads=1)
        workflow.log_message.connect(lambda level, message: warnings.append(message) if level == "WARN" else None)
        workflow.set_validation_override(decide)
        signals = workflow.run(config)
    else:
        config = EncodeConfig(
            source=source, output=output, work_dir=work, mux_backend=backend,
            video=VideoEncodeSettings(codec="libx264", preset="ultrafast", crf=35),
            copy_subtitles=False, keep_chapters=False, duration_s=0.4,
        )
        workflow = EncodeWorkflow(generate_nfo=False, ffmpeg_threads=1)
        workflow.log_message.connect(lambda level, message: warnings.append(message) if level == "WARN" else None)
        workflow.set_validation_override(decide)
        signals = workflow.run(config)
    state["signals"] = signals
    result = wait_task(signals, timeout=30)
    signals.wait_for_workers()
    assert len(requests) == 1, result
    assert not output.with_suffix(".mkv.partial").exists()
    assert not any(p.is_dir() for p in work.iterdir())
    if answer is True:
        assert result["finished"] is not None and result["failed"] is None, result
        assert ffprobe_json(output)["streams"][0]["codec_type"] == "video"
        assert any("décision explicite" in message for message in warnings)
    else:
        assert output.read_bytes() == b"old output"
        assert result["cancelled"] if answer == "cancel" else result["failed"] is not None
