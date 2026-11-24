"""Suivi des processus et de l'annulation, partagé par les modules d'alignement."""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass


def _never() -> bool:
    return False


@dataclass(frozen=True)
class ProcessHooks:
    """Annulation et suivi des processus lancés (fournis par le workflow).

    ``cancelled`` est interrogé entre deux lectures ; ``on_start`` / ``on_end`` enregistrent chaque
    ``Popen`` pour qu'une annulation le tue et que le nettoyage attende sa fin.
    """

    cancelled: Callable[[], bool] = _never
    on_start: Callable[[subprocess.Popen], None] | None = None
    on_end: Callable[[subprocess.Popen], None] | None = None

    def check(self) -> None:
        """``InterruptedError`` si l'annulation est demandée."""
        if self.cancelled():
            raise InterruptedError("Alignement annulé.")
