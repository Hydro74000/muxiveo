"""Ctrl+C pendant une opération : annulation propre (processus arrêtés, dossier de travail nettoyé)."""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from core.service.context import CancelToken


@contextmanager
def interrupt_guard(cancel: CancelToken) -> Iterator[CancelToken]:
    """Pendant le bloc, SIGINT (Ctrl+C) annule ``cancel`` au lieu d'interrompre l'interpréteur.

    L'opération s'arrête alors comme une annulation demandée depuis l'interface : processus
    tués, dossier de travail supprimé, état terminal ``cancelled``. Les gardes s'imbriquent :
    un Ctrl+C pendant un job d'un lot annule le job puis arrête le lot. Hors du thread principal
    (seul autorisé à poser un gestionnaire), le bloc s'exécute sans garde.
    """
    if threading.current_thread() is not threading.main_thread():
        yield cancel
        return
    previous = signal.getsignal(signal.SIGINT)

    def handler(signum, frame) -> None:
        cancel.cancel()
        # Gardes imbriquées (job dans un lot) : la garde englobante est aussi prévenue.
        if callable(previous) and previous is not signal.default_int_handler:
            previous(signum, frame)

    signal.signal(signal.SIGINT, handler)
    try:
        yield cancel
    finally:
        signal.signal(signal.SIGINT, previous)
