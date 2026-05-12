"""JSON and small data-shaping helpers for the CLI."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

from cli.constants import EXIT_ARGS
from cli.errors import CliError


def json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.name
    if is_dataclass(value):
        return asdict(cast(Any, value))
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CliError(f"JSON introuvable : {path}", EXIT_ARGS) from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"JSON invalide {path}:{exc.lineno}:{exc.colno} : {exc.msg}", EXIT_ARGS) from exc
    if not isinstance(data, dict):
        raise CliError("Le fichier JSON racine doit être un objet.", EXIT_ARGS)
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
