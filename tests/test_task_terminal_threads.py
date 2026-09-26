"""Livraison des fins de tâche depuis/vers des threads Python sans boucle Qt."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from core import runner as runner_module
from core.runner import TaskSignals
from core.workflows.encode.runtime.bindings import SignalBindingService, SignalBindingServiceCallbacks
from core.workflows.encode.runtime.preparation import EncodePreparationRunner

_ARGS = {"finished": ("ok",), "failed": ("boom", RuntimeError("boom")), "cancelled": ()}


def _emit_from_worker(signals: TaskSignals, kind: str) -> None:
    worker = threading.Thread(target=lambda: getattr(signals, kind).emit(*_ARGS[kind]))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()


def _wait(qt_app, predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.mark.parametrize("subscribe_after_end", [False, True])
@pytest.mark.parametrize("direct", [False, True])
def test_loopless_thread_subscription(qt_app, subscribe_after_end, direct):
    seen: list[tuple] = []

    def owner_thread() -> None:
        # Objet créé dans un thread sans boucle : la livraison différée serait perdue.
        signals = TaskSignals()
        if subscribe_after_end:
            _emit_from_worker(signals, "finished")
        signals.connect_terminal(
            finished=lambda result: seen.append((result, threading.get_ident())), direct=direct,
        )
        if not subscribe_after_end:
            _emit_from_worker(signals, "finished")
        _emit_from_worker(signals, "finished")  # Seconde fin ignorée.

    owner = threading.Thread(target=owner_thread, name="owner")
    owner.start()
    owner.join(timeout=2)
    assert not owner.is_alive()
    if not direct:
        assert seen == []
        assert _wait(qt_app, lambda: bool(seen))
    assert len(seen) == 1 and seen[0][0] == "ok"
    if not direct:
        assert seen[0][1] == threading.get_ident()
    elif subscribe_after_end:
        assert seen[0][1] == owner.ident
    else:
        assert seen[0][1] != threading.get_ident()


def test_direct_terminal_runs_after_existing_hooks_and_once(qt_app):
    signals = TaskSignals()
    order: list[str] = []
    signals.connect_terminal(finished=lambda _r: order.append("hook"), direct=True)
    signals.connect_terminal(
        finished=lambda _r: order.append("finished"),
        failed=lambda *_a: order.append("failed"),
        direct=True,
    )
    _emit_from_worker(signals, "finished")
    _emit_from_worker(signals, "failed")
    assert order == ["hook", "finished"]


@pytest.mark.parametrize("kind", ["finished", "failed", "cancelled"])
@pytest.mark.parametrize("timing", ["before_relay", "while_executor_alive"])
def test_preparation_relay_survives_fast_inner_task(qt_app, kind, timing):
    """L'encodage interne peut finir avant ou pendant le branchement du relais."""

    def run_with_preparation(_config, *, validate, prep_signals):
        assert validate is False and prep_signals is not None
        # Comme en production : inner naît dans le thread d'exécution (sans boucle Qt).
        inner = TaskSignals()
        if timing == "before_relay":
            _emit_from_worker(inner, kind)
        else:
            original = inner.connect_terminal

            def connect_then_finish(**kwargs):
                original(**kwargs)
                _emit_from_worker(inner, kind)  # Le thread d'exécution vit encore.

            inner.connect_terminal = connect_then_finish  # type: ignore[method-assign]
        return inner

    runner = EncodePreparationRunner(SimpleNamespace(run_with_preparation=run_with_preparation))  # type: ignore[arg-type]
    outer = runner.run_async_preparation(SimpleNamespace())  # type: ignore[arg-type]
    seen: list[str] = []
    outer.connect_terminal(
        finished=lambda _r: seen.append("finished"),
        failed=lambda *_a: seen.append("failed"),
        cancelled=lambda: seen.append("cancelled"),
    )
    assert _wait(qt_app, lambda: bool(seen)), "fin interne perdue : l'encodage resterait « en cours »"
    qt_app.processEvents()
    assert seen == [kind]


def test_binding_hooks_run_when_task_ends_while_binding_thread_alive(qt_app, tmp_path):
    removed: list[Path] = []
    nfo: list[Path] = []
    service = SignalBindingService(SignalBindingServiceCallbacks(write_nfo=nfo.append, remove_path=removed.append))
    output, temp = tmp_path / "out.mkv", tmp_path / "tmp"

    def binding_thread() -> None:
        signals = TaskSignals()
        service.bind_temp_cleanup(signals, [temp])
        service.bind_nfo_write(signals, output)
        _emit_from_worker(signals, "finished")  # Fin pendant que ce thread vit.

    thread = threading.Thread(target=binding_thread)
    thread.start()
    thread.join(timeout=2)
    assert removed == [temp]
    assert nfo == [output]


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("subscribe_during_hook", [False, True])
def test_terminal_waits_for_running_nfo_hook(qt_app, tmp_path, direct, subscribe_during_hook):
    signals = TaskSignals()
    entered, release = threading.Event(), threading.Event()
    order = []

    def write_nfo(_output):
        entered.set()
        assert release.wait(2)
        order.append("nfo")

    service = SignalBindingService(SignalBindingServiceCallbacks(write_nfo=write_nfo))
    service.bind_nfo_write(signals, tmp_path / "out.mkv")

    def subscribe():
        signals.connect_terminal(finished=lambda _r: order.append("finished"), direct=direct)

    if not subscribe_during_hook:
        subscribe()
    worker = threading.Thread(target=lambda: signals.finished.emit("ok"))
    worker.start()
    try:
        assert entered.wait(2)
        if subscribe_during_hook:
            subscribe()
        qt_app.processEvents()
        assert order == [], "fin annoncée pendant l'écriture NFO"
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert _wait(qt_app, lambda: len(order) == 2)
    assert order == ["nfo", "finished"]


@pytest.mark.parametrize("kind", ["finished", "failed", "cancelled"])
def test_binding_hooks_replay_completed_task_once(qt_app, tmp_path, kind):
    signals = TaskSignals()
    removed, nfo = [], []
    service = SignalBindingService(SignalBindingServiceCallbacks(write_nfo=nfo.append, remove_path=removed.append))
    output, temp = tmp_path / "out.mkv", tmp_path / "tmp"
    getattr(signals, kind).emit(*_ARGS[kind])
    service.bind_output_hooks(signals, output=output, cleanup_paths=[temp])
    assert removed == [temp]
    assert nfo == ([output] if kind == "finished" else [])
    getattr(signals, kind).emit(*_ARGS[kind])
    assert removed == [temp]
    assert nfo == ([output] if kind == "finished" else [])


@pytest.mark.parametrize("direct", [False, True])
def test_callback_exception_does_not_drop_other_subscribers(qt_app, monkeypatch, direct):
    signals = TaskSignals()
    seen, errors = [], []
    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args[1]))

    def broken(_result):
        raise RuntimeError("broken hook")

    signals.connect_terminal(finished=broken, direct=direct)
    signals.connect_terminal(finished=lambda result: seen.append(result), direct=direct)
    _emit_from_worker(signals, "finished")
    assert _wait(qt_app, lambda: bool(seen))
    assert seen == ["ok"]
    assert len(errors) == 1 and str(errors[0]) == "broken hook"
    signals.connect_terminal(finished=lambda result: seen.append(result), direct=direct)
    assert _wait(qt_app, lambda: len(seen) == 2)


@pytest.mark.parametrize("kind", ["finished", "failed", "cancelled"])
def test_no_application_uses_direct_replay(monkeypatch, kind):
    monkeypatch.setattr(runner_module, "QCoreApplication", SimpleNamespace(instance=lambda: None))
    signals = TaskSignals()
    seen = []
    signals.connect_terminal(**{kind: lambda *args: seen.append(args)})
    _emit_from_worker(signals, kind)
    assert seen == [_ARGS[kind]]
    signals.connect_terminal(**{kind: lambda *args: seen.append(args)})
    assert seen == [_ARGS[kind], _ARGS[kind]]


def test_default_subscription_after_owner_thread_exits_uses_application_thread(qt_app):
    created, seen = [], []
    owner = threading.Thread(target=lambda: created.append(TaskSignals()))
    owner.start()
    owner.join(timeout=2)
    assert not owner.is_alive()
    signals = created[0]
    signals.connect_terminal(finished=lambda _r: seen.append(threading.get_ident()))
    _emit_from_worker(signals, "finished")
    assert seen == []
    assert _wait(qt_app, lambda: bool(seen))
    assert seen == [threading.get_ident()]


def test_competing_terminal_during_hook_keeps_first_result(qt_app):
    signals = TaskSignals()
    entered, release = threading.Event(), threading.Event()
    seen = []

    def slow_hook(_result):
        entered.set()
        assert release.wait(2)

    signals.connect_terminal(finished=slow_hook, direct=True)
    worker = threading.Thread(target=lambda: signals.finished.emit("ok"))
    worker.start()
    try:
        assert entered.wait(2)
        signals.connect_terminal(
            finished=lambda _r: seen.append("finished"),
            failed=lambda *_a: seen.append("failed"), direct=True,
        )
        _emit_from_worker(signals, "failed")
        assert seen == []
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert seen == ["finished"]
