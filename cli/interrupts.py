"""Ctrl+C pendant une opération : annulation propre (processus arrêtés, dossier de travail nettoyé)."""

from __future__ import annotations

import os
import signal
import sys
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from core.service.context import CancelToken


class _Interrupts:
    """Gestionnaire SIGINT unique du processus et son fil de veille, partagés par les gardes imbriquées.

    Le gestionnaire ne prend aucun verrou et n'écrit au plus qu'un octet en attente (demandes regroupées) : il
    interrompt le fil principal n'importe où — y compris pendant ``CancelToken.on_cancel`` ou pendant une
    garde qui s'ouvre ou se ferme — sans jamais pouvoir se bloquer. La veille annule, dans l'ordre, chaque jeton
    gardé au moment du signal (job puis lot) ; une garde ne se referme qu'une fois toute demande reçue traitée,
    pour elle comme pour les gardes englobantes.
    """

    def __init__(self) -> None:
        self.tokens: list[CancelToken] = []  # gardes ouvertes (fil principal seulement)
        self.previous: Any = None
        self.targets: tuple[CancelToken, ...] = ()
        self.requested = 0  # écrit par le gestionnaire seul
        self.handled = 0  # écrit par la veille seule
        self.signalled = False  # un octet attend la veille
        self.done = threading.Condition()
        self.read_fd, self.write_fd = os.pipe()  # gardés jusqu'à la fin du processus (aucune fuite par garde)
        threading.Thread(target=self._watch, name="interrupt-guard", daemon=True).start()

    def handler(self, signum: int, frame: Any) -> None:
        self.targets = tuple(self.tokens)
        self.requested += 1
        if not self.signalled:  # au plus un octet en attente : l'écriture ne bloque jamais
            self.signalled = True
            os.write(self.write_fd, b"c")
        previous = self.previous
        if callable(previous) and previous is not signal.default_int_handler:
            previous(signum, frame)

    def _watch(self) -> None:
        while os.read(self.read_fd, 1):
            self.signalled = False
            requested, targets = self.requested, self.targets
            for token in reversed(targets):  # le plus intérieur d'abord : job, puis lot
                try:
                    token.cancel()
                except Exception:  # noqa: BLE001 - rappel d'annulation en échec : signalé, les autres continuent
                    traceback.print_exc(file=sys.stderr)
            with self.done:
                self.handled = max(self.handled, requested)
                self.done.notify_all()

    def settle(self) -> None:
        """Attend que chaque demande déjà reçue soit appliquée (jetons de toutes les gardes concernées)."""
        with self.done:
            self.done.wait_for(lambda: self.handled >= self.requested)


_STATE: _Interrupts | None = None


@contextmanager
def interrupt_guard(cancel: CancelToken) -> Iterator[CancelToken]:
    """Pendant le bloc, SIGINT (Ctrl+C) annule ``cancel`` au lieu d'interrompre l'interpréteur.

    L'opération s'arrête alors comme une annulation demandée depuis l'interface : processus
    tués, dossier de travail supprimé, état terminal ``cancelled``. Les gardes s'imbriquent :
    un Ctrl+C pendant un job d'un lot annule le job puis le lot, avant la sortie de la garde du job. Hors du
    thread principal (seul autorisé à poser un gestionnaire), le bloc s'exécute sans garde.
    """
    global _STATE
    if threading.current_thread() is not threading.main_thread():
        yield cancel
        return
    if _STATE is None:
        _STATE = _Interrupts()
    state = _STATE
    outermost = not state.tokens
    state.tokens.append(cancel)
    try:
        if outermost:
            state.previous = signal.signal(signal.SIGINT, state.handler)
        yield cancel
    finally:
        try:
            # Restauré seulement s'il a été posé (un Ctrl+C avant la pose interrompt avec le gestionnaire
            # d'origine) ; une fois posé, plus aucune interruption ne peut survenir dans cette garde.
            if outermost and signal.getsignal(signal.SIGINT) == state.handler:
                signal.signal(signal.SIGINT, state.previous)
        finally:
            state.tokens.remove(cancel)
            state.settle()
