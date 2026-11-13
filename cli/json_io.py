"""JSON and small data-shaping helpers for the CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.jobs.json_io import json_default
from core.json_documents import JsonDocumentError, read_json_document, resolve_document_paths

from cli.constants import EXIT_ARGS
from cli.errors import CliError


def load_json(path: Path) -> dict[str, Any]:
    """Document JSON utilisateur : UTF-8 avec ou sans BOM, sans NaN/Infinity."""
    try:
        data = read_json_document(path)
    except FileNotFoundError as exc:
        raise CliError(f"JSON introuvable : {path}", EXIT_ARGS) from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"JSON invalide {path}:{exc.lineno}:{exc.colno} : {exc.msg}", EXIT_ARGS) from exc
    except (JsonDocumentError, UnicodeDecodeError) as exc:
        raise CliError(f"JSON invalide {path} : {exc}", EXIT_ARGS) from exc
    if not isinstance(data, dict):
        raise CliError("Le fichier JSON racine doit être un objet.", EXIT_ARGS)
    return data


def load_job_document(path: Path) -> dict[str, Any]:
    """Job/template JSON dont les chemins relatifs sont résolus depuis son dossier."""
    path = Path(path).expanduser()
    return resolve_document_paths(load_json(path), path.absolute().parent)


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
