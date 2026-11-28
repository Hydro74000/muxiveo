"""Aides des tests de contrat : références d'un schéma publié, écarts d'une valeur (vérificateur du cœur)."""

from __future__ import annotations

from typing import Any

from core.service.schema_check import schema_errors


def refs(node: Any) -> set[str]:
    """Toutes les ``$ref`` d'un schéma."""
    if isinstance(node, list):
        return set().union(*(refs(item) for item in node)) if node else set()
    if not isinstance(node, dict):
        return set()
    found = {node["$ref"]} if isinstance(node.get("$ref"), str) else set()
    return found.union(*(refs(value) for value in node.values()))


def resolve(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    assert ref.startswith("#/$defs/"), ref
    return root["$defs"][ref.removeprefix("#/$defs/")]


def errors(value: Any, schema: dict[str, Any], root: dict[str, Any], where: str = "$") -> list[str]:
    """Écarts de ``value`` à ``schema`` (références résolues dans ``root``) ; vide : conforme."""
    return [f"{where}{'.' if path and not path.startswith('[') else ''}{path} : {message}"
            for path, message in schema_errors(value, schema, root)]


def result_errors(operation: str, result: Any) -> list[str]:
    """Écarts du résultat réel d'une opération au schéma publié par sa description (autonome)."""
    import json

    from core.jobs.json_io import json_default
    from core.service.catalog import CATALOG

    described = CATALOG.get(operation).describe()
    payload = json.loads(json.dumps(result.to_dict(), default=json_default))
    return errors(payload, described["result"], described)
