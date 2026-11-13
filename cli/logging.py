"""Small stdout/stderr logger used by the headless CLI."""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

from core.jobs.json_io import json_default
from core.service.events import Emitter, Event


class EventStreamSink:
    """Écrit chaque événement de service en JSON Lines (``--log-format events``)."""

    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()

    def emit(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, default=json_default)
        with self._lock:
            print(line, file=self.stream, flush=True)


class Logger:
    def __init__(self, *, fmt: str = "text", stream=None) -> None:
        self.fmt = fmt
        self.stream = stream or sys.stderr
        # Format « events » : journal de la session CLI publié comme événements versionnés.
        self.event_sink = EventStreamSink(self.stream) if fmt == "events" else None
        self.events = Emitter(self.event_sink) if self.event_sink is not None else None

    def emit(self, level: str, message: str, **fields: Any) -> None:
        if self.events is not None:
            self.events.log(level, message, **fields)
            return
        if self.fmt == "jsonl":
            payload = {"level": level.lower(), "message": message, **fields}
            print(json.dumps(payload, ensure_ascii=False, default=json_default), file=self.stream)
            return
        print(f"[{level.upper()}] {message}", file=self.stream)

    def workflow_log(self, level: str, message: str) -> None:
        self.emit(level, message)
