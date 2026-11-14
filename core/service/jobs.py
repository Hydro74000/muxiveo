"""Attente d'une tâche Qt (``TaskSignals``) par une opération : fin, nettoyage, annulation.

Sur le thread de l'application Qt, une boucle locale (``QEventLoop``) livre les événements
jusqu'au **nettoyage complet** : l'attente bloquante des workers (``wait_for_workers``)
tourne dans un fil auxiliaire qui signale la fin, si bien que les slots postés pendant le
nettoyage (dossier de travail…) sont traités avant le retour. Hors du thread Qt (fils d'un
futur daemon), l'attente passe par un ``threading.Event``.

Un minuteur réveille l'interpréteur pendant la boucle : un gestionnaire de signal Python
(Ctrl+C → annulation) s'exécute alors sans attendre la fin de la tâche.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, Qt, QThread, QTimer, Signal

from core.service.context import CancelToken

#: Période du réveil de l'interpréteur pendant l'attente (gestionnaires de signaux Python).
_WAKE_MS = 100


@dataclass(frozen=True)
class TaskOutcome:
    status: Literal["finished", "failed", "cancelled"]
    result: str = ""
    message: str = ""
    exception: Any = None


class _Cleaned(QObject):
    done = Signal()


def await_task(
    signals: Any,
    cancel: CancelToken,
    *,
    on_progress: Callable[[str], None] | None = None,
    on_progress_pct: Callable[[int], None] | None = None,
) -> TaskOutcome:
    """Attend la fin d'une tâche et de ses workers ; ``cancel`` annule la tâche.

    ``signals`` : ``core.runner.TaskSignals`` (ou équivalent : ``connect_terminal``,
    ``wait_for_workers``, ``cancel``, signaux ``progress`` / ``progress_pct``).
    """
    cancel.on_cancel(signals.cancel)
    app = QCoreApplication.instance()
    on_qt_thread = app is not None and QThread.currentThread() is app.thread()
    connection = Qt.ConnectionType.AutoConnection if on_qt_thread else Qt.ConnectionType.DirectConnection
    if on_progress is not None:
        signals.progress.connect(on_progress, connection)
    if on_progress_pct is not None and hasattr(signals, "progress_pct"):
        signals.progress_pct.connect(on_progress_pct, connection)

    outcome: dict[str, TaskOutcome] = {}
    if not on_qt_thread:
        done = threading.Event()

        def record(value: TaskOutcome) -> None:
            outcome["value"] = value
            done.set()

        _connect(signals, record, direct=True)
        done.wait()
        signals.wait_for_workers()
        return outcome["value"]

    loop = QEventLoop()
    cleaned = _Cleaned()
    cleaned.done.connect(loop.quit, Qt.ConnectionType.QueuedConnection)
    waker = QTimer()
    waker.setInterval(_WAKE_MS)
    waker.timeout.connect(lambda: None)

    cleanup: list[threading.Thread] = []

    def finish(value: TaskOutcome) -> None:
        outcome["value"] = value

        def wait_workers() -> None:
            try:
                signals.wait_for_workers()
            finally:
                cleaned.done.emit()

        thread = threading.Thread(target=wait_workers, name="muxiveo-task-cleanup", daemon=True)
        cleanup.append(thread)
        thread.start()

    _connect(signals, finish, direct=False)
    waker.start()
    try:
        loop.exec()
    finally:
        waker.stop()
        # Émission de `done` terminée avant la destruction de `cleaned` (sinon accès concurrent à un
        # QObject détruit : plantage à la sortie de l'attente).
        for thread in cleanup:
            thread.join()
        # Slots postés après la fin (nettoyage du dossier de travail…) : traités avant le retour.
        QCoreApplication.sendPostedEvents()
        QCoreApplication.processEvents()
    return outcome["value"]


def _connect(signals: Any, record: Callable[[TaskOutcome], None], *, direct: bool) -> None:
    signals.connect_terminal(
        finished=lambda result="": record(TaskOutcome("finished", result=str(result or ""))),
        failed=lambda message, exc=None: record(TaskOutcome("failed", message=str(message), exception=exc)),
        cancelled=lambda: record(TaskOutcome("cancelled")),
        direct=direct,
    )


def raise_for_outcome(outcome: TaskOutcome, cancel: CancelToken, *, cancelled: str,
                      failed: Callable[[str], BaseException]) -> None:
    """Issue d'une tâche attendue par :func:`await_task` : rien si elle a réussi, sinon l'erreur publique.

    Annulation : issue ``cancelled``, ou issue ``failed`` causée par une annulation (sonde, inspection…, causes
    enveloppées comprises) ou survenue après la levée du jeton. Une réussite reste une réussite, même si le
    jeton a été levé entre-temps (annulation trop tardive).
    """
    from core.service.errors import ApiError, ErrorCode, error_code

    if outcome.status == "finished":
        return
    if outcome.status == "cancelled" or cancel.cancelled or error_code(outcome.exception) is ErrorCode.CANCELLED:
        raise ApiError(ErrorCode.CANCELLED, cancelled)
    raise failed(outcome.message)
