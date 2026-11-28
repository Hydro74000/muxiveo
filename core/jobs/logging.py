"""Journal des jobs : interface commune au ``Logger`` du CLI et aux événements de service."""

from __future__ import annotations

from typing import Any, Protocol

from core.service.events import Emitter


class JobLogger(Protocol):
    """Destination des messages d'un job (``Logger`` du CLI, :class:`EmitterLogger`…)."""

    def emit(self, level: str, message: str, **fields: Any) -> None: ...


class EmitterLogger:
    """Adapte un émetteur d'événements de service à l'interface :class:`JobLogger`."""

    def __init__(self, events: Emitter, *, origin: str | None = None) -> None:
        self.events = events
        self.origin = origin

    def emit(self, level: str, message: str, **fields: Any) -> None:
        self.events.log(level, message, origin=self.origin, **fields)

    def workflow_log(self, level: str, message: str) -> None:
        self.emit(level, message)
