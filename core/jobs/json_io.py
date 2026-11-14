"""Lecture / écriture JSON des jobs et sérialisation partagée (CLI, service)."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

from core.json_documents import JsonDocumentError, read_json_document, resolve_document_paths

from core.jobs.constants import EXIT_ARGS
from core.jobs.errors import JobError


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


def load_json(path: Path) -> dict[str, Any]:
    """Document JSON utilisateur : UTF-8 avec ou sans BOM, sans NaN/Infinity."""
    try:
        data = read_json_document(path)
    except FileNotFoundError as exc:
        raise JobError(f"JSON introuvable : {path}", EXIT_ARGS) from exc
    except json.JSONDecodeError as exc:
        raise JobError(f"JSON invalide {path}:{exc.lineno}:{exc.colno} : {exc.msg}", EXIT_ARGS) from exc
    except (JsonDocumentError, UnicodeDecodeError) as exc:
        raise JobError(f"JSON invalide {path} : {exc}", EXIT_ARGS) from exc
    if not isinstance(data, dict):
        raise JobError("Le fichier JSON racine doit être un objet.", EXIT_ARGS)
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


#: Choix exclusifs d'un objet : une surcharge qui fournit une clé d'un groupe retire de la base celles de l'autre.
#: ``static_hdr`` : analyse HDR10 P5→P8.1 (``estimate``) ou valeurs / mode explicites (la dernière couche gagne).
EXCLUSIVE_KEYS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "static_hdr": (frozenset({"estimate"}), frozenset({"mode", "master_display", "max_cll"})),
}


def without_excluded(key: str, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """``base`` (objet de clé ``key``) sans les choix que ``override`` rend caducs (:data:`EXCLUSIVE_KEYS`).

    Seules les clés **non nulles** de ``override`` comptent : ``null`` veut dire « non fourni » (couches, ``to_dict``).
    """
    groups = EXCLUSIVE_KEYS.get(key)
    if groups is None:
        return base
    provided = {name for name, value in override.items() if value is not None}
    dropped: set[str] = set()
    for group, other in (groups, groups[::-1]):
        if group & provided:
            dropped |= other
    return {name: value for name, value in base.items() if name not in dropped}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = deep_merge(without_excluded(key, merged[key], value), value)
        else:
            merged[key] = value
    return merged
