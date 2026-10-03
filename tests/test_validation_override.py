"""Décision après rejet : atomicité, thread Qt, annulation et modes non interactifs."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox, QWidget

from core.matroska.contract import MatroskaOutputContract
from core.runner import TaskCancelledError, TaskSignals
from core.subprocess_utils import run_cancellable_capture, subprocess_text_kwargs
from core.workflows.common.matroska_finalize import MatroskaOutputTransaction
from core.workflows.common.validation_override import ValidationOverrideRequest, validate_final_output
from core.workflows.encode.models import EncodeConfig, VideoEncodeSettings
from core.workflows.encode.planning.plan_models import EncodePlan
from core.workflows.encode.runtime.direct_output import DirectOutputRunner, DirectOutputRunnerCallbacks
from core.workflows.encode.workflow import EncodeWorkflow
from core.workflows.encode.runtime.frame_count_guard import FrameCountGuard, MetadataAdjustment
from ui.confirm import ValidationOverridePrompt


@pytest.mark.parametrize("answer", [True, False, None, "cancel"])
def test_final_rejection_commits_only_after_explicit_acceptance(qt_app, tmp_path, monkeypatch, answer):
    output = tmp_path / "film.mkv"
    output.write_bytes(b"old result")
    signals = TaskSignals()
    requests = []
    warnings = []

    def run(cmd, _cwd, label, _progress, _signals):
        if label == "encode":
            Path(cmd[-1]).write_bytes(b"candidate")
        else:
            raise RuntimeError("ffprobe rejection")
        return "encoded"

    def decide(path, message, cancelled):
        assert output.read_bytes() == b"old result"
        assert path.read_bytes() == b"candidate"
        assert "semantic rejection" in message and "ffprobe rejection" in message
        requests.append(message)
        if answer == "cancel":
            signals._cancel_event.set()
            return True
        return answer

    monkeypatch.setattr("core.workflows.common.matroska_finalize.validate_matroska_output", lambda *_a, **_k: ["semantic rejection"])
    transaction = MatroskaOutputTransaction(
        output, MatroskaOutputContract(track_types=("video",)), "ffprobe", run,
        validation_override=decide if answer is not None else None,
        warn=warnings.append,
    )
    if answer is True:
        transaction.execute(["ffmpeg", str(output)], cwd=None, label="encode", signals=signals)
        assert output.read_bytes() == b"candidate"
        assert warnings
    else:
        error = TaskCancelledError if answer == "cancel" else RuntimeError
        with pytest.raises(error):
            transaction.execute(["ffmpeg", str(output)], cwd=None, label="encode", signals=signals)
        assert output.read_bytes() == b"old result"
    assert len(requests) == (0 if answer is None else 1)
    assert not transaction.candidate.exists()


def test_valid_output_does_not_ask_for_override(tmp_path):
    decide = MagicMock()
    validate_final_output(tmp_path / "candidate.mkv", [], lambda: None, message_prefix="validation: ", override=decide)
    decide.assert_not_called()


@pytest.mark.parametrize("accepted", [True, False])
def test_popup_runs_on_gui_thread_and_defaults_to_no(qt_app, tmp_path, monkeypatch, accepted):
    parent = QWidget()
    prompt = ValidationOverridePrompt(parent)
    received = []
    result = []

    def exec_dialog(dialog):
        assert threading.current_thread() is threading.main_thread()
        assert dialog.defaultButton() == dialog.button(QMessageBox.StandardButton.No)
        received.append(dialog.text())
        return QMessageBox.StandardButton.Yes if accepted else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "exec", exec_dialog)
    worker = threading.Thread(target=lambda: result.append(prompt.request(tmp_path / "candidate.mkv", "rejected counts", lambda: False)))
    worker.start()
    deadline = time.monotonic() + 3
    while worker.is_alive() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    worker.join(timeout=0.1)
    assert result == [accepted]
    assert "rejected counts" in received[0]
    parent.deleteLater()


def test_open_popup_closes_when_operation_is_cancelled(qt_app, tmp_path):
    parent = QWidget()
    prompt = ValidationOverridePrompt(parent)
    request = ValidationOverrideRequest(tmp_path / "candidate.mkv", "rejected", threading.Event().is_set)
    cancel = threading.Event()
    request.cancelled = cancel.is_set
    QTimer.singleShot(80, cancel.set)
    prompt._show(request)
    assert request.resolved and not request.wait()
    parent.deleteLater()


def test_pending_decision_does_not_block_shutdown(tmp_path):
    cancel = threading.Event()
    request = ValidationOverrideRequest(tmp_path / "candidate.mkv", "rejected", cancel.is_set)
    result = []
    worker = threading.Thread(target=lambda: result.append(request.wait()))
    worker.start()
    cancel.set()
    worker.join(timeout=1)
    assert not worker.is_alive() and result == [False]


def test_frame_scan_is_registered_and_cancelled_even_without_output():
    signals = TaskSignals()
    errors = []
    started = threading.Event()
    processes = []

    def check():
        if signals._cancel_event.is_set():
            raise TaskCancelledError()

    def register(proc):
        signals._register_proc(proc)
        processes.append(proc)
        started.set()

    def worker_task():
        try:
            run_cancellable_capture(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cancel_cb=signals._cancel_event.is_set, check_cancelled=check,
                on_start=register, on_end=signals._unregister_proc,
                **subprocess_text_kwargs(),
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=worker_task)
    worker.start()
    assert started.wait(3)
    signals.cancel()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], TaskCancelledError)
    assert processes[0].poll() is not None


@pytest.mark.parametrize("error", [OSError("attachment failure"), TaskCancelledError()])
def test_encode_preparation_failure_removes_owned_workspace(qt_app, tmp_path, monkeypatch, error):
    source = tmp_path / "source.mkv"
    source.touch()
    work = tmp_path / "work"
    work.mkdir()
    sentinel = work / "personal.txt"
    sentinel.write_text("keep")
    wf = EncodeWorkflow(generate_nfo=False)

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(wf, "_prepare_attachment_config", fail)
    cfg = EncodeConfig(source=source, output=tmp_path / "out.mkv", work_dir=work, video=VideoEncodeSettings(codec="libx265"))
    with pytest.raises(type(error)):
        wf._run_with_preparation(cfg, validate=False)
    assert list(work.iterdir()) == [sentinel]


@pytest.mark.parametrize("two_pass", [False, True])
def test_direct_encode_cancel_closes_live_sync_session(qt_app, tmp_path, two_pass):
    session = MagicMock()
    signals = TaskSignals()

    def cancelled(*_args, **_kwargs):
        raise TaskCancelledError()

    cb = SimpleNamespace(
        check_cancelled=lambda _s: None, log_step=MagicMock(), log_info=MagicMock(),
        uses_two_pass=lambda _c: two_pass,
        build_runtime_two_pass_with_sync=lambda *_a, **_k: ([["ffmpeg"], ["ffmpeg"]], session, []),
        build_runtime_single_pass_with_sync=lambda *_a, **_k: (["ffmpeg"], session, []),
        run_cmd=cancelled, finalize_ffmpeg=cancelled,
        bind_live_sync_cleanup=MagicMock(),
    )
    cfg = EncodeConfig(source=tmp_path / "s.mkv", output=tmp_path / "o.mkv", video=VideoEncodeSettings())
    with pytest.raises(TaskCancelledError):
        DirectOutputRunner(cast(DirectOutputRunnerCallbacks, cb)).run(
            config=cfg, cleanup_paths=[], cwd=tmp_path, prep_signals=signals,
            plan=cast(EncodePlan, SimpleNamespace(all_sources=[cfg.source])),
        )
    session.close.assert_called_once()


def test_secondary_video_audit_counts_selected_stream(tmp_path):
    import subprocess

    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        assert command[command.index("-select_streams") + 1] == "2"
        return subprocess.CompletedProcess(command, 0, stdout="48\n", stderr="")

    guard = FrameCountGuard(run_command=probe)
    audit = guard.audit(source=tmp_path / "multi.mkv", source_stream_index=2,
                        encoded=tmp_path / "encoded.hevc", known_encoded_frames=48)
    guard.enforce(audit, adjustment=MetadataAdjustment.EXACT)
    assert audit.source == 48 and len(calls) == 1


@pytest.mark.parametrize("kind", ["video", "audio", "subtitle", "attachment", "tags", "extra_attachment"])
def test_encode_rejects_output_matching_any_input_source(qt_app, tmp_path, kind):
    from core.workflows.encode.models import AudioTrackSettings

    source = tmp_path / "primary.mkv"
    secondary = tmp_path / "secondary.mkv"
    source.touch()
    secondary.write_bytes(b"source to preserve")
    cfg = EncodeConfig(source=source, output=secondary, video=VideoEncodeSettings(codec="libx265"))
    if kind == "video":
        assert cfg.video is not None
        cfg.video.source_path = secondary
    elif kind == "audio":
        cfg.audio_tracks = [AudioTrackSettings(stream_index=1, source_path=secondary)]
    elif kind == "subtitle":
        cfg.subtitle_tracks = [(secondary, 2)]
    elif kind == "attachment":
        cfg.attachment_streams = [(secondary, 3)]
    elif kind == "tags":
        cfg.tag_sources = [secondary]
    else:
        cfg.extra_attachments = [secondary]
    errors = EncodeWorkflow(generate_nfo=False).validate(cfg)
    assert any("différent du fichier source" in e for e in errors)
    assert secondary.read_bytes() == b"source to preserve"


@pytest.mark.parametrize("link_type", ["symlink", "hardlink"])
def test_encode_rejects_output_aliasing_source(qt_app, tmp_path, link_type):
    source = tmp_path / "source.mkv"
    source.touch()
    alias = tmp_path / "output.mkv"
    try:
        if link_type == "symlink":
            alias.symlink_to(source)
        else:
            alias.hardlink_to(source)
    except OSError:
        pytest.skip("liens non disponibles sur ce système")
    cfg = EncodeConfig(source=source, output=alias, video=VideoEncodeSettings(codec="libx265"))
    assert any("différent du fichier source" in e for e in EncodeWorkflow(generate_nfo=False).validate(cfg))
