"""Sérialisation JSON partagée par le CLI et la couche de service."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast


def json_default(value: Any) -> Any:
    """``default`` de ``json.dumps`` : chemins, énumérations (par nom), dataclasses, octets."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.name
    if is_dataclass(value):
        return asdict(cast(Any, value))
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
