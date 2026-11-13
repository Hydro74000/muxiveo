"""Exécution d'un encodage (core.jobs.encode_runtime), routage des lots, capacités ; workflows simulés."""

from __future__ import annotations

import dataclasses
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QObject, Signal

from core.jobs import encode_runtime
from core.jobs.batch import BatchSteps, PlannedBatchJob, execute_job
from core.jobs.errors import JobError
from core.jobs.options import CommonOptions
from core.runner import TaskSignals
from core.service import ApiError, CancelToken, ErrorCode, ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.workflows.encode.models import EncodeConfig, VideoCropSettings, VideoEncodeSettings


class _Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def emit(self, level: str, message: str, **fields: Any) -> None:
        self.records.append((level, message, fields))


def _config(tmp_path: Path, **video: Any) -> EncodeConfig:
    settings = VideoEncodeSettings(stream_index=0, source_path=tmp_path / "src.mkv", codec="libx265", **video)
    return EncodeConfig(source=tmp_path / "src.mkv", output=tmp_path / "out.mkv", video=settings)


class _FakeWorkflow(QObject):
    log_message = Signal(str, str)
    failed_work_dir_retained = Signal(object)

    def __init__(self, work=None, errors=()) -> None:
        super().__init__()
        self._work = work or (lambda s: s.finished.emit("ffmpeg …\nencoded 25 frames"))
        self.errors = list(errors)
        self.ran: list[EncodeConfig] = []

    def validate(self, _config):
        return self.errors

    def parse_progress(self, _config, line):
        return SimpleNamespace(percent=50.0, stage_label=None, fps=24.0, frame=12, eta_seconds=1.0) \
            if line.startswith("frame=") else None

    def run(self, config, validate=True, retain_failed_work_dir=False):
        self.ran.append(config)
        self.retain = retain_failed_work_dir
        signals = TaskSignals()
        executor = ThreadPoolExecutor(max_workers=1)

        def go():
            time.sleep(0.05)
            signals.progress.emit("frame=12")
            self._work(signals)

        signals.watch_future(executor.submit(go))
        executor.shutdown(wait=False)
        return signals


def _run(tmp_path, wf, **kwargs):
    logger = _Logger()
    events: list[Any] = []
    config = SimpleNamespace(tool_ffmpeg="ffmpeg")
    try:
        result = encode_runtime.run_encode(_config(tmp_path), config=config, options=CommonOptions(**kwargs.pop(
            "options", {})), logger=logger, cancel=kwargs.pop("cancel", CancelToken()), workflow_factory=lambda: wf,
            on_progress=events.append, **kwargs)
    except (ApiError, JobError) as error:
        result = error
    return result, logger, events


def test_run_encode_returns_summary_and_forwards_progress(qapp, tmp_path):
    result, logger, events = _run(tmp_path, _FakeWorkflow())
    assert result == "encoded 25 frames" and events and events[0].percent == 50.0
    assert not any("ffmpeg …" in message for _l, message, _f in logger.records), "sortie brute hors verbose"
    _result, logger, _events = _run(tmp_path, _FakeWorkflow(), options={"verbose": True})
    assert any(message.startswith("ffmpeg …") for _l, message, _f in logger.records)


def test_run_encode_refusals(qapp, tmp_path):
    (tmp_path / "out.mkv").write_bytes(b"x")
    result, _l, _e = _run(tmp_path, _FakeWorkflow())
    assert isinstance(result, JobError) and result.exit_code == 5
    (tmp_path / "out.mkv").unlink()
    result, _l, _e = _run(tmp_path, _FakeWorkflow(errors=["preset inconnu"]))
    assert isinstance(result, JobError) and result.exit_code == 3 and "preset inconnu" in result.message


def test_run_encode_failure_and_cancel_are_logged_once(qapp, tmp_path):
    result, logger, _e = _run(tmp_path, _FakeWorkflow(lambda s: s.failed.emit("x265 KO", RuntimeError("x"))))
    assert isinstance(result, ApiError) and result.logged and result.code is ErrorCode.WORKFLOW_FAILED
    assert logger.records[-1][1] == "x265 KO"

    def wait_cancel(signals):
        while not signals._cancel_event.is_set():
            time.sleep(0.01)
        signals.cancelled.emit()

    token = CancelToken()
    token.cancel()
    result, _logger, _e = _run(tmp_path, _FakeWorkflow(wait_cancel), cancel=token)
    assert isinstance(result, ApiError) and result.code is ErrorCode.CANCELLED


def test_dovi_nvenc_crop_requires_acceptance(tmp_path, monkeypatch):
    required = VideoCropSettings(enabled=True, unit="px", top=8, bottom=8)
    monkeypatch.setattr(encode_runtime, "_dimensions", lambda wf, video, config: (3840, 2160))
    monkeypatch.setattr("core.workflows.encode.runtime.dovi_geometry.align_nvenc_dovi_geometry",
                        lambda video, dims, l5_offsets=None: SimpleNamespace(
                            video=dataclasses.replace(video, crop=required), full_frame_crop=True,
                            crop_offsets=(0, 8, 0, 8)))
    wf = SimpleNamespace(_probe_dovi_l5_offsets=lambda *a, **k: None)
    config = _config(tmp_path, copy_dv=True)
    config = dataclasses.replace(config, video_tracks=[dataclasses.replace(config.video, codec="hevc_nvenc")])
    with pytest.raises(JobError, match="accept_dovi_nvenc_crop") as caught:
        encode_runtime.align_dovi_nvenc(config, wf, accept=False)
    assert caught.value.exit_code == 3 and "libx265" in caught.value.message
    aligned = encode_runtime.align_dovi_nvenc(config, wf, accept=True)
    assert aligned.video_tracks[0].crop == required and aligned.video.crop == required


def test_batch_routes_encoded_jobs():
    calls: list[str] = []
    steps = BatchSteps(
        build=lambda *a: None,
        preview=lambda *a: calls.append("preview") or 0,
        run=lambda *a, **k: calls.append("run") or 0,
        encode=lambda *a: "cfg",
        preview_encode=lambda *a: calls.append("preview_encode") or 0,
        run_encode=lambda *a, force, accept_dovi_nvenc_crop: calls.append(f"run_encode:{accept_dovi_nvenc_crop}") or 0,
    )
    remux_job = PlannedBatchJob(0, "a", "a.mkv", remux_config="remux")
    encode_job = PlannedBatchJob(1, "b", "b.mkv", remux_config="remux", encode_config="cfg", accept_dovi_nvenc_crop=True)
    for entry in (remux_job, encode_job):
        execute_job(steps, entry, None, None, None, dry_run=False, force=False)  # type: ignore[arg-type]
        execute_job(steps, entry, None, None, None, dry_run=True, force=False)  # type: ignore[arg-type]
    assert calls == ["run", "preview", "run_encode:True", "preview_encode"]


def test_capabilities_operation_reports_requested_sections(monkeypatch):
    from core.workflows.encode import hardware, vulkan

    monkeypatch.setattr(hardware.HardwareEncoderDetector, "detect_software", lambda self, ffmpeg: {"libx265"})
    monkeypatch.setattr(hardware.HardwareEncoderDetector, "detect",
                        lambda self, ffmpeg, nvencc_bin=None: ({"hevc_nvenc"}, ffmpeg))
    monkeypatch.setattr(vulkan, "detect_vulkan", lambda ffmpeg: vulkan.VulkanCapability(reason="test"))
    ctx = ExecutionContext.create(SimpleNamespace(tool_ffmpeg="ffmpeg"), ListSink())
    result = call(CATALOG, "system.capabilities", {"sections": ["encoders", "vulkan"]}, ctx)
    assert result.encoders == {"software": ["libx265"], "hardware": ["hevc_nvenc"]}
    assert result.vulkan["reason"] == "test" and result.presets is None and result.rife is None


def test_keep_failed_work_dir_keeps_the_retained_folder(qapp, tmp_path):
    kept: list[str] = []
    retained = SimpleNamespace(path=tmp_path / "work" / "job", keep=lambda: kept.append("keep"))
    wf = _FakeWorkflow()

    def fail(signals):
        wf.failed_work_dir_retained.emit(retained)
        signals.failed.emit("x265 KO", RuntimeError("x"))

    wf._work = fail
    result, logger, _e = _run(tmp_path, wf, options={"keep_failed_work_dir": True})
    assert isinstance(result, ApiError) and wf.retain is True and kept == ["keep"]
    assert any("Fichiers temporaires conservés" in message for _l, message, _f in logger.records)
    wf2 = _FakeWorkflow(lambda s: s.failed.emit("KO", None))
    _run(tmp_path, wf2)
    assert wf2.retain is False
