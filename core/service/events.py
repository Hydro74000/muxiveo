"""Événements versionnés émis par les opérations (journal, progression, artefacts, état).

Forme commune : ``{"v", "job", "parent", "seq", "ts", "type", …}``. ``seq`` croît de 1 en 1
par job ; ``parent`` relie un job enfant (épisode, élément de lot) à son parent. Le CLI les
écrit en JSON Lines (``--log-format events``) ; un futur daemon les diffusera tels quels.
"""

from __future__ import annotations

import itertools
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

EVENT_VERSION = 1


class EventType(str, Enum):
    STATE = "state"
    LOG = "log"
    PROGRESS = "progress"
    WARNING = "warning"
    ARTIFACT = "artifact"
    RESULT = "result"
    ERROR = "error"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


@dataclass(frozen=True)
class Event:
    type: EventType
    job: str
    seq: int
    ts: str
    data: Mapping[str, Any] = field(default_factory=dict)
    parent: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"v": EVENT_VERSION, "job": self.job, "parent": self.parent, "seq": self.seq,
                "ts": self.ts, "type": self.type.value, **self.data}


class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class NullSink:
    """Destination qui ignore tout (opération appelée pour son seul résultat)."""

    def emit(self, event: Event) -> None:
        return None


class ListSink:
    """Destination en mémoire (tests, agrégation)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[Event] = []

    def emit(self, event: Event) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)


class CallbackSink:
    def __init__(self, callback: Callable[[Event], None]) -> None:
        self._callback = callback

    def emit(self, event: Event) -> None:
        self._callback(event)


def new_job_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Emitter:
    """Émetteur lié à un job : numérote les événements et les transmet à la destination."""

    def __init__(self, sink: EventSink, job: str | None = None, parent: str | None = None) -> None:
        self.sink = sink
        self.job = job or new_job_id()
        self.parent = parent
        self._seq = itertools.count(1)
        # Numérotation et livraison sous le même verrou : un job publié par plusieurs fils livre ses
        # événements dans l'ordre de ``seq``. Réentrant : une destination peut republier sur ce job.
        self._lock = threading.RLock()

    def emit(self, event_type: EventType, /, **data: Any) -> None:
        with self._lock:
            self.sink.emit(Event(event_type, self.job, next(self._seq), _now(), data, self.parent))

    def child(self, job: str | None = None) -> Emitter:
        """Émetteur d'un job enfant (même destination, ``parent`` = ce job)."""
        return Emitter(self.sink, job, parent=self.job)

    def state(self, state: JobState, **data: Any) -> None:
        self.emit(EventType.STATE, state=state.value, **data)

    def log(self, level: str, message: str, *, origin: str | None = None, **fields: Any) -> None:
        payload: dict[str, Any] = {"level": str(level).lower(), "message": message}
        if origin:
            payload["origin"] = origin
        self.emit(EventType.LOG, **payload, **fields)

    def warning(self, message: str, *, code: str | None = None, path: str | None = None) -> None:
        payload: dict[str, Any] = {"message": message}
        if code:
            payload["code"] = code
        if path is not None:
            payload["path"] = path
        self.emit(EventType.WARNING, **payload)

    def progress(self, *, pct: float | None = None, stage: str | None = None, **fields: Any) -> None:
        payload = {key: value for key, value in (("pct", pct), ("stage", stage)) if value is not None}
        self.emit(EventType.PROGRESS, **payload, **fields)

    def artifact(self, kind: str, path: str, **fields: Any) -> None:
        self.emit(EventType.ARTIFACT, kind=kind, path=path, **fields)

    def result(self, result: Mapping[str, Any]) -> None:
        self.emit(EventType.RESULT, result=dict(result))

    def error(self, error: Mapping[str, Any]) -> None:
        self.emit(EventType.ERROR, error=dict(error))


#: Schéma JSON d'un événement (catalogue ``schema --version api``).
EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Event",
    "required": ["v", "job", "parent", "seq", "ts", "type"],
    "properties": {
        "v": {"const": EVENT_VERSION},
        "job": {"type": "string"},
        "parent": {"type": ["string", "null"]},
        "seq": {"type": "integer", "minimum": 1},
        "ts": {"type": "string", "format": "date-time"},
        "type": {"enum": [item.value for item in EventType]},
        "state": {"enum": [item.value for item in JobState]},
        "level": {"type": "string"},
        "message": {"type": "string"},
        "origin": {"type": "string"},
        "code": {"type": "string"},
        "pct": {"type": "number"},
        "stage": {"type": "string"},
        "kind": {"type": "string"},
        "path": {"type": "string"},
        "result": {"type": "object"},
        "error": {"type": "object"},
    },
}
