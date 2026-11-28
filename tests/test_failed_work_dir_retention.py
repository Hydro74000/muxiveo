"""Encodage GUI en échec : dossier de travail gardé jusqu'au choix de l'utilisateur (popup)."""

from __future__ import annotations

import threading
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast

import pytest

from core.runner import TaskCancelledError, TaskSignals
from core.workdir import (
    PROCESS_DIR_MARKER,
    FailedWorkDirRetention,
    RetainedProcessWorkDir,
    cleanable_work_dir_entries,
    create_process_work_dir,
    is_active_process_dir,
    process_dir_payload_bytes,
)
from core.workflows.encode import EncodeError, EncodeWorkflow
from core.workflows.encode.runtime.bindings import SignalBindingService, SignalBindingServiceCallbacks
from core.workflows.encode.runtime.preparation import EncodePreparationRunner
from ui.main_window import MainWindow


def _emit_from_worker(signals: TaskSignals, kind: str, *args: object) -> None:
    worker = threading.Thread(target=lambda: getattr(signals, kind).emit(*args))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()


def _workflow() -> tuple[EncodeWorkflow, list[RetainedProcessWorkDir]]:
    from PySide6.QtCore import Qt

    wf = EncodeWorkflow(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool", hdr10plus_bin="hdr10plus_tool")
    retained: list[RetainedProcessWorkDir] = []
    wf.failed_work_dir_retained.connect(retained.append, Qt.ConnectionType.DirectConnection)
    return wf, retained


# --- core/workdir -------------------------------------------------------------


def test_payload_bytes_skip_markers_and_excluded_entries(tmp_path):
    job = create_process_work_dir(tmp_path)
    (job.path / "attachments").mkdir()
    (job.path / "attachments" / "cover.jpg").write_bytes(b"x" * 100)
    assert process_dir_payload_bytes(job.path, excluded=lambda entry: entry.name == "attachments") == 0

    (job.path / "video.hevc").write_bytes(b"x" * 7)
    (job.path / "pass").mkdir()
    (job.path / "pass" / "x265.log").write_bytes(b"x" * 3)
    assert process_dir_payload_bytes(job.path, excluded=lambda entry: entry.name == "attachments") == 10
    job.remove()


def test_kept_dir_is_released_and_offered_at_next_cleanup(tmp_path):
    job = create_process_work_dir(tmp_path)
    (job.path / "video.hevc").write_bytes(b"x")
    assert is_active_process_dir(job.path)
    assert job.path not in cleanable_work_dir_entries(tmp_path)

    RetainedProcessWorkDir(job, 1).keep()

    assert (job.path / "video.hevc").exists() and (job.path / PROCESS_DIR_MARKER).exists()
    assert not is_active_process_dir(job.path)
    assert job.path in cleanable_work_dir_entries(tmp_path)


def test_retention_is_claimed_once_and_only_when_requested(tmp_path):
    retention = FailedWorkDirRetention()
    requested, other = create_process_work_dir(tmp_path), create_process_work_dir(tmp_path)
    retention.request(requested)

    assert retention.claim(other.path) is None
    assert retention.claim(requested.path) == requested
    assert retention.claim(requested.path) is None  # second crochet de fin : déjà réclamé
    requested.remove()
    other.remove()


# --- crochet de fin -------------------------------------------------------------


@pytest.mark.parametrize(("kind", "retain", "removed_expected"), [
    ("failed", True, False),
    ("failed", False, True),
    ("finished", True, True),  # seule la fin en échec consulte la conservation
    ("cancelled", True, True),
])
def test_temp_cleanup_hook_defers_to_retention_on_failure(qt_app, tmp_path, kind, retain, removed_expected):
    removed: list[Path] = []
    asked: list[list[Path]] = []

    def retain_failed(paths: list[Path]) -> bool:
        asked.append(paths)
        return retain

    service = SignalBindingService(SignalBindingServiceCallbacks(
        write_nfo=lambda _output: None, remove_path=removed.append, retain_failed=retain_failed,
    ))
    signals = TaskSignals()
    paths = [tmp_path / "work"]
    service.bind_temp_cleanup(signals, paths)
    args = {"failed": ("boom", None), "finished": ("ok",), "cancelled": ()}[kind]
    _emit_from_worker(signals, kind, *args)

    assert (removed == paths) is removed_expected
    assert bool(asked) is (kind == "failed")


# --- EncodeWorkflow -------------------------------------------------------------


def test_workflow_keeps_requested_dir_with_produced_files(qt_app, tmp_path):
    wf, retained = _workflow()
    job = create_process_work_dir(tmp_path / "work")
    wf._failed_work_dir_retention.request(job)
    (job.path / "enc_attachments_ab12").mkdir()
    (job.path / "enc_attachments_ab12" / "font.ttf").write_bytes(b"x" * 50)
    (job.path / "track_0.hevc").write_bytes(b"x" * 20)
    outside = tmp_path / "outside.tmp"
    outside.write_bytes(b"x")

    assert wf._retain_failed_work_dir([job.path / "enc_attachments_ab12", outside, job.path]) is True

    assert [(item.path, item.payload_bytes) for item in retained] == [(job.path, 20)]  # pièces jointes exclues
    assert (job.path / "track_0.hevc").exists() and (job.path / "enc_attachments_ab12").exists()
    assert not outside.exists()
    retained[0].discard()
    assert not job.path.exists()


@pytest.mark.parametrize("requested", [True, False])
def test_workflow_cleans_normally_without_material_or_request(qt_app, tmp_path, requested):
    wf, retained = _workflow()
    job = create_process_work_dir(tmp_path / "work")
    (job.path / "attachments").mkdir()
    (job.path / "attachments" / "cover.jpg").write_bytes(b"x")
    if requested:
        wf._failed_work_dir_retention.request(job)  # seulement des pièces jointes
    else:
        (job.path / "track_0.hevc").write_bytes(b"x")

    assert wf._retain_failed_work_dir([job.path]) is False
    assert retained == []
    job.remove()


# --- échec pendant la préparation ---------------------------------------------------


def _preparation_runner(wf: EncodeWorkflow, root: Path) -> tuple[EncodePreparationRunner, list[Path]]:
    created: list[Path] = []

    def prepare_process_work_dir(work_root: Path, **_kwargs: object) -> Path:
        created.append(create_process_work_dir(work_root).path)
        return created[-1]

    callbacks = SimpleNamespace(
        check_cancelled=lambda _signals: None,
        resolve_dovi_sources=None,
        resolve_source_color_transfer=None,
        validate_config=lambda _config: [],
        log_workflow_type=lambda _kind: None,
        log_step=lambda _index, _label: None,
        log=lambda _level, _message: None,
        config_warnings=None,
        prepare_process_work_dir=prepare_process_work_dir,
        request_failed_work_dir_retention=wf._failed_work_dir_retention.request,
        retain_failed_work_dir=wf._retain_failed_work_dir,
    )
    return EncodePreparationRunner(cast(Any, callbacks)), created


@pytest.mark.parametrize(("error", "retain_flag", "kept"), [
    (EncodeError("extraction RPU impossible"), True, True),
    (EncodeError("extraction RPU impossible"), False, False),  # aperçu, CLI : nettoyage habituel
    (TaskCancelledError(), True, False),  # annulation : jamais de popup
])
def test_preparation_failure_keeps_dir_only_for_gui_run(qt_app, tmp_path, error, retain_flag, kept):
    wf, retained = _workflow()
    runner, created = _preparation_runner(wf, tmp_path)

    def failing_run_prepared(_config, _root, process_dir, _signals):
        (process_dir / "p5_rpu_0.bin").write_bytes(b"x" * 4)
        raise error

    runner._run_prepared = failing_run_prepared  # type: ignore[method-assign]
    config = SimpleNamespace(work_dir=tmp_path / "work", output=tmp_path / "out.mkv")
    with pytest.raises(type(error)):
        runner.run_with_preparation(cast(Any, config), validate=False, retain_failed_work_dir=retain_flag)

    assert created[0].exists() is kept
    assert [item.path for item in retained] == ([created[0]] if kept else [])
    for item in retained:
        item.discard()


# --- MainWindow : popup -----------------------------------------------------------


def _window(*, running: bool, keep: bool) -> tuple[Any, list[tuple[str, str]], list[Any], list[str]]:
    logs: list[tuple[str, str]] = []
    discarded: list[Any] = []
    reasons: list[str] = []

    def ask(_self, _retained, reason: str) -> bool:
        reasons.append(reason)
        return keep

    window = SimpleNamespace(
        _running=running,
        _failed_work_dir=None,
        log_requested=SimpleNamespace(emit=lambda level, message: logs.append((level, message))),
        _ask_keep_failed_work_dir=None,
        _discard_failed_work_dir=discarded.append,
    )
    window._ask_keep_failed_work_dir = MethodType(ask, window)
    window._prompt_failed_work_dir = MethodType(MainWindow._prompt_failed_work_dir, window)
    window._on_failed_work_dir_retained = MethodType(MainWindow._on_failed_work_dir_retained, window)
    return window, logs, discarded, reasons


def test_popup_waits_for_failure_then_keeps_on_request(tmp_path):
    job = create_process_work_dir(tmp_path)
    window, logs, discarded, reasons = _window(running=True, keep=True)

    window._on_failed_work_dir_retained(RetainedProcessWorkDir(job, 10))
    assert reasons == []  # encodage pas encore terminé côté interface
    window._running = False
    window._prompt_failed_work_dir("ffmpeg a échoué (code 1)")

    assert reasons == ["ffmpeg a échoué (code 1)"]
    assert discarded == [] and job.path.exists() and not is_active_process_dir(job.path)
    assert logs and str(job.path) in logs[0][1]


def test_popup_discards_by_default_even_when_notice_arrives_late(tmp_path):
    job = create_process_work_dir(tmp_path)
    window, logs, discarded, reasons = _window(running=False, keep=False)
    retained = RetainedProcessWorkDir(job, 10)

    window._on_failed_work_dir_retained(retained)  # échec déjà affiché

    assert reasons == [""]
    assert discarded == [retained] and logs == []
    window._prompt_failed_work_dir("plus rien")  # une seule popup
    assert reasons == [""]
    job.remove()
