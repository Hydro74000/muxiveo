"""Vérificateur structurel minimal des schémas publiés (sans dépendance ``jsonschema``).

Mots-clés pris en charge : ``$ref`` (``#/$defs/…``), ``type``, ``const``, ``enum``, ``oneOf`` (exactement une
variante), ``anyOf``, ``properties``, ``required``, ``additionalProperties`` (booléen ou schéma),
``patternProperties``, ``items``, ``minItems`` / ``maxItems``, ``minimum`` / ``maximum``, ``pattern``.
"""

from __future__ import annotations

import re
from typing import Any

_TYPES: dict[str, Any] = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list,
                          "object": dict, "null": type(None)}


def _is(value: Any, kind: str) -> bool:
    if kind in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[kind])


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


def errors(value: Any, schema: dict[str, Any], root: dict[str, Any], where: str = "$") -> list[str]:  # noqa: C901
    """Écarts de ``value`` à ``schema`` (références résolues dans ``root``) ; vide : conforme."""
    if "$ref" in schema:
        found = errors(value, resolve(schema["$ref"], root), root, where)
        rest = {key: item for key, item in schema.items() if key not in ("$ref", "description", "default")}
        return found + (errors(value, rest, root, where) if rest else [])
    found: list[str] = []
    kinds = schema.get("type")
    if kinds is not None and not any(_is(value, kind) for kind in ([kinds] if isinstance(kinds, str) else kinds)):
        return [f"{where} : type {kinds} attendu"]
    if "const" in schema and value != schema["const"]:
        found.append(f"{where} : {schema['const']!r} attendu")
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{where} : valeur hors énumération")
    if "oneOf" in schema:
        matching = sum(not errors(value, variant, root, where) for variant in schema["oneOf"])
        if matching != 1:
            found.append(f"{where} : {matching} variante(s) oneOf satisfaite(s)")
    if "anyOf" in schema and all(errors(value, variant, root, where) for variant in schema["anyOf"]):
        found.append(f"{where} : aucune variante anyOf")
    if isinstance(value, str) and "pattern" in schema and re.search(schema["pattern"], value) is None:
        found.append(f"{where} : motif")
    if _is(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            found.append(f"{where} : sous le minimum")
        if "maximum" in schema and value > schema["maximum"]:
            found.append(f"{where} : au-dessus du maximum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            found.append(f"{where} : nombre d'éléments")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                found += errors(item, schema["items"], root, f"{where}[{index}]")
    if isinstance(value, dict):
        found += [f"{where}.{key} : requis" for key in schema.get("required", ()) if key not in value]
        properties = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                found += errors(item, properties[key], root, f"{where}.{key}")
                continue
            matched = [rule for pattern, rule in patterns.items() if re.search(pattern, key)]
            for rule in matched:
                found += errors(item, rule, root, f"{where}.{key}")
            if matched:
                continue
            if extra is False:
                found.append(f"{where}.{key} : clé inconnue")
            elif isinstance(extra, dict):
                found += errors(item, extra, root, f"{where}.{key}")
    return found
