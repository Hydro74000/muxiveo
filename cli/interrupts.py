"""Ctrl+C pendant une opération : annulation propre (processus arrêtés, dossier de travail nettoyé)."""

from __future__ import annotations

import os
import signal
import sys
import threading
import traceback
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

    Le gestionnaire ne fait qu'écrire un octet dans un tube (aucun verrou : il interrompt le fil principal
    n'importe où, y compris pendant ``cancel.on_cancel``) ; un fil de veille applique l'annulation, et toute
    demande reçue est traitée avant la sortie du bloc.
    """
    if threading.current_thread() is not threading.main_thread():
        yield cancel
        return
    previous = signal.getsignal(signal.SIGINT)
    read_fd, write_fd = os.pipe()

    def watch() -> None:
        while os.read(read_fd, 1) == b"c":
            try:
                cancel.cancel()
            except Exception:  # noqa: BLE001 - rappel d'annulation en échec : signalé, la veille continue
                traceback.print_exc(file=sys.stderr)

    def handler(signum, frame) -> None:
        os.write(write_fd, b"c")
        # Gardes imbriquées (job dans un lot) : la garde englobante est aussi prévenue.
        if callable(previous) and previous is not signal.default_int_handler:
            previous(signum, frame)

    watcher = threading.Thread(target=watch, name="interrupt-guard", daemon=True)
    watcher.start()
    signal.signal(signal.SIGINT, handler)
    try:
        yield cancel
    finally:
        signal.signal(signal.SIGINT, previous)
        os.write(write_fd, b"q")  # après toute demande déjà écrite : traitée avant la sortie
        watcher.join()
        os.close(read_fd)
        os.close(write_fd)
