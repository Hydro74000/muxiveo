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
    interrompt le fil principal n'importe où — y compris pendant ``CancelToken.on_cancel`` — sans jamais pouvoir
    se bloquer. Chaque signal ajoute les jetons gardés à ce moment (job puis lot) à une file que la veille annule
    dans l'ordre, le plus intérieur d'abord ; aucune demande n'en écrase une autre. Une garde ne se referme
    qu'une fois toute demande reçue traitée, pour elle comme pour les gardes englobantes.
    """

    def __init__(self) -> None:
        self.tokens: list[CancelToken] = []  # gardes ouvertes (fil principal seulement)
        self.previous: Any = None
        self.queue: list[CancelToken] = []  # ajouts du gestionnaire seul (atomiques), lus par la veille
        self.requested = 0  # signaux reçus (gestionnaire seul)
        self.handled = 0  # signaux traités (veille seule)
        self.signalled = False  # un octet attend la veille
        self.done = threading.Condition()
        self.read_fd, self.write_fd = os.pipe()  # pour toute la vie du processus (aucune ressource par garde)
        self.thread = threading.Thread(target=self._watch, name="interrupt-guard", daemon=True)
        self.thread.start()

    def handler(self, signum: int, frame: Any) -> None:
        self.queue.extend(reversed(self.tokens))  # le plus intérieur d'abord
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
            requested = self.requested  # lu avant la file : les jetons de ces signaux y sont déjà
            count = len(self.queue)  # ajouts ultérieurs laissés pour le tour suivant
            batch = self.queue[:count]
            del self.queue[:count]  # opérations atomiques : un ajout concurrent reste en fin de file
            seen: set[int] = set()
            for token in batch:
                if id(token) in seen:
                    continue
                seen.add(id(token))
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


#: Créé à l'import (fil principal) : aucune initialisation qu'un Ctrl+C pourrait interrompre à moitié.
_STATE = _Interrupts()


@contextmanager
def interrupt_guard(cancel: CancelToken) -> Iterator[CancelToken]:
    """Pendant le bloc, SIGINT (Ctrl+C) annule ``cancel`` au lieu d'interrompre l'interpréteur.

    L'opération s'arrête alors comme une annulation demandée depuis l'interface : processus
    tués, dossier de travail supprimé, état terminal ``cancelled``. Les gardes s'imbriquent :
    un Ctrl+C pendant un job d'un lot annule le job puis le lot, avant la sortie de la garde du job. Hors du
    thread principal (seul autorisé à poser un gestionnaire), le bloc s'exécute sans garde.
    """
    if threading.current_thread() is not threading.main_thread():
        yield cancel
        return
    state = _STATE
    outermost = not state.tokens
    try:
        state.tokens.append(cancel)
        if outermost:
            state.previous = signal.signal(signal.SIGINT, state.handler)
        yield cancel
    finally:
        # Jeton retiré puis demandes reçues traitées, gestionnaire encore en place (un second Ctrl+C reste noté) ;
        # gestionnaire d'origine rétabli en dernier, seulement s'il a été remplacé.
        if cancel in state.tokens:
            state.tokens.remove(cancel)
        state.settle()
        if outermost and signal.getsignal(signal.SIGINT) == state.handler:
            signal.signal(signal.SIGINT, state.previous)
