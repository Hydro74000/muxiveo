"""Small stdout/stderr logger used by the headless CLI."""

from __future__ import annotations

import json
import sys
from typing import Any

from cli.json_io import json_default


class Logger:
    def __init__(self, *, fmt: str = "text", stream=None) -> None:
        self.fmt = fmt
        self.stream = stream or sys.stderr

    def emit(self, level: str, message: str, **fields: Any) -> None:
        if self.fmt == "jsonl":
            payload = {"level": level.lower(), "message": message, **fields}
            print(json.dumps(payload, ensure_ascii=False, default=json_default), file=self.stream)
            return
        print(f"[{level.upper()}] {message}", file=self.stream)

    def workflow_log(self, level: str, message: str) -> None:
        self.emit(level, message)
