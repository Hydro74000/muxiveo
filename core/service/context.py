"""Contexte d'exécution d'une opération : configuration figée, événements, annulation.

Aucun état global : chaque appel reçoit son contexte. La configuration est un instantané
(copie superficielle, conteneurs copiés, sans objet QSettings) : une modification des
réglages pendant l'exécution n'atteint pas l'opération, et l'opération ne peut pas les
enregistrer. Les « ports » remplacent un service externe (réseau…) sans globale.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.service.errors import ApiError, ErrorCode
from core.service.events import Emitter, EventSink


class CancelToken:
    """Demande d'annulation partagée entre une opération et ses sous-tâches."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks = list(self._callbacks)
        for callback in callbacks:
            callback()

    def on_cancel(self, callback: Callable[[], None]) -> None:
        """Appelle ``callback`` à l'annulation (immédiatement si elle a déjà eu lieu)."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
        callback()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ApiError(ErrorCode.CANCELLED, "Opération annulée.")

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


@dataclass(frozen=True)
class Overrides:
    """Surcharges propres à l'appel (outils, threads, dossier de travail).

    Portées par l'adaptateur (options du CLI), jamais écrites dans la configuration ;
    un daemon pourra les refuser (exécution d'un binaire arbitraire).
    """

    ffmpeg: str | None = None
    ffprobe: str | None = None
    mediainfo: str | None = None
    threads: int | None = None
    work_dir: Path | None = None


def snapshot_config(config: Any) -> Any:
    """Instantané de lecture de la configuration (sans objet QSettings)."""
    snapshot = copy.copy(config)
    state = getattr(snapshot, "__dict__", None)
    if isinstance(state, dict):
        for key, value in list(state.items()):
            if isinstance(value, (dict, list, set)):
                state[key] = copy.copy(value)
        if "_settings" in state:
            state["_settings"] = None
    return snapshot


@dataclass(frozen=True)
class ExecutionContext:
    config: Any
    events: Emitter
    cancel: CancelToken = field(default_factory=CancelToken)
    overrides: Overrides = field(default_factory=Overrides)
    ports: Mapping[str, Callable[..., Any]] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        config: Any,
        sink: EventSink,
        *,
        job: str | None = None,
        parent: str | None = None,
        cancel: CancelToken | None = None,
        overrides: Overrides | None = None,
        ports: Mapping[str, Callable[..., Any]] | None = None,
    ) -> ExecutionContext:
        return cls(
            config=snapshot_config(config),
            events=Emitter(sink, job, parent),
            cancel=cancel or CancelToken(),
            overrides=overrides or Overrides(),
            ports=dict(ports or {}),
        )

    def tool(self, name: str) -> str:
        """Commande de l'outil ``name`` : surcharge de l'appel, sinon configuration."""
        override = getattr(self.overrides, name, None)
        return str(override or getattr(self.config, f"tool_{name}", None) or name)

    def port(self, name: str, default: Callable[..., Any]) -> Callable[..., Any]:
        return self.ports.get(name, default)
