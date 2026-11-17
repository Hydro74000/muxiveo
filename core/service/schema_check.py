"""Vérification structurelle d'une valeur JSON face à un schéma publié (sous-ensemble de JSON Schema 2020-12).

Sert l'admission des documents d'une requête de l'API (le job est vérifié contre le schéma que publie le
catalogue : même verdict pour le client et pour le service) et les tests de contrat. Mots-clés pris en
charge : ``$ref`` (``#/$defs/…``), ``type``, ``const``, ``enum``, ``oneOf`` (exactement une variante), ``anyOf``,
``not``, ``properties``, ``required``, ``additionalProperties`` (booléen ou schéma), ``patternProperties``,
``propertyNames``, ``items``, ``prefixItems``, ``minItems`` / ``maxItems``, ``minimum`` / ``maximum``,
``exclusiveMinimum``, ``pattern`` ; sous-schémas booléens (``true`` / ``false``). Les autres (``description``,
``default``, ``format``…) n'ont pas d'effet. Sémantique JSON : un entier peut s'écrire ``1.0``, un booléen
n'est jamais un nombre, ``$`` d'un motif marque la fin du texte (ECMA-262, pas « avant un saut de ligne final »).
"""

from __future__ import annotations

import re
from typing import Any

_TYPES: dict[str, Any] = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list,
                          "object": dict, "null": type(None)}
_REF_PREFIX = "#/$defs/"


def _is(value: Any, kind: str) -> bool:
    if kind in ("number", "integer") and isinstance(value, bool):
        return False
    if kind == "integer" and isinstance(value, float):
        return value.is_integer()
    return isinstance(value, _TYPES[kind])


def _same(value: Any, expected: Any) -> bool:
    """Égalité JSON, récursive : un booléen n'est jamais égal à un nombre (``0 == False`` en Python)."""
    if isinstance(value, bool) or isinstance(expected, bool):
        return isinstance(value, bool) and isinstance(expected, bool) and value == expected
    if isinstance(value, list) and isinstance(expected, list):
        return len(value) == len(expected) and all(_same(a, b) for a, b in zip(value, expected))
    if isinstance(value, dict) and isinstance(expected, dict):
        return value.keys() == expected.keys() and all(_same(value[key], expected[key]) for key in value)
    if isinstance(value, (list, dict)) or isinstance(expected, (list, dict)):
        return False
    return bool(value == expected)


def portable_pattern(pattern: str) -> str:
    """Motif dont ``$`` (hors classe de caractères) marque la fin du texte en ECMA-262 comme en Python :
    remplacé par ``(?![\\s\\S])`` (aucun caractère après), valable dans les deux moteurs."""
    out: list[str] = []
    in_class = escaped = False
    for char in pattern:
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\":
            out.append(char)
            escaped = True
        elif in_class:
            out.append(char)
            in_class = char != "]"
        elif char == "[":
            out.append(char)
            in_class = True
        elif char == "$":
            out.append(r"(?![\s\S])")
        else:
            out.append(char)
    return "".join(out)


def _join(path: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def schema_errors(value: Any, schema: dict[str, Any] | bool, root: dict[str, Any],  # noqa: C901 - mots-clés
                  path: str = "") -> list[tuple[str, str]]:
    """Écarts de ``value`` à ``schema`` : liste de ``(chemin, message)`` ; références résolues dans ``root``."""
    if schema is True:
        return []
    if schema is False:
        return [(path, "valeur refusée")]
    found: list[tuple[str, str]] = []
    ref = schema.get("$ref")
    if isinstance(ref, str):
        found += schema_errors(value, root["$defs"][ref.removeprefix(_REF_PREFIX)], root, path)
    kinds = schema.get("type")
    if kinds is not None:
        expected = [kinds] if isinstance(kinds, str) else list(kinds)
        if not any(_is(value, kind) for kind in expected):
            return found + [(path, f"{' ou '.join(expected)} attendu")]
    if "const" in schema and not _same(value, schema["const"]):
        found.append((path, f"{schema['const']!r} attendu"))
    if "enum" in schema and not any(_same(value, choice) for choice in schema["enum"]):
        found.append((path, f"valeur parmi {schema['enum']} attendue"))
    if "oneOf" in schema:
        matching = sum(not schema_errors(value, variant, root, path) for variant in schema["oneOf"])
        if matching != 1:
            found.append((path, "aucune forme admise ne correspond" if not matching else "forme ambiguë"))
    if "anyOf" in schema and all(schema_errors(value, variant, root, path) for variant in schema["anyOf"]):
        found.append((path, _any_of_message(schema["anyOf"])))
    if "not" in schema and not schema_errors(value, schema["not"], root, path):
        found.append((path, _not_message(schema["not"])))
    if isinstance(value, str) and "pattern" in schema and re.search(portable_pattern(schema["pattern"]),
                                                                    value) is None:
        found.append((path, f"format attendu : {schema['pattern']}"))
    if _is(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            found.append((path, f"au moins {schema['minimum']}"))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            found.append((path, f"plus de {schema['exclusiveMinimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            found.append((path, f"au plus {schema['maximum']}"))
    if isinstance(value, list):
        found += _array_errors(value, schema, root, path)
    if isinstance(value, dict):
        found += _object_errors(value, schema, root, path)
    return found


def _array_errors(value: list[Any], schema: dict[str, Any], root: dict[str, Any], path: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if len(value) < schema.get("minItems", 0):
        found.append((path, f"au moins {schema['minItems']} élément(s)"))
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        found.append((path, f"au plus {schema['maxItems']} élément(s)"))
    prefix = schema.get("prefixItems", [])
    for index, item in enumerate(value):
        rule = prefix[index] if index < len(prefix) else schema.get("items", True)
        found += schema_errors(item, rule, root, _join(path, index))
    return found


def _object_errors(value: dict[str, Any], schema: dict[str, Any], root: dict[str, Any],
                   path: str) -> list[tuple[str, str]]:
    found = [(_join(path, key), "champ requis") for key in schema.get("required", ()) if key not in value]
    properties = schema.get("properties", {})
    patterns = schema.get("patternProperties", {})
    extra = schema.get("additionalProperties", True)
    names = schema.get("propertyNames", True)
    for key, item in value.items():
        child = _join(path, key)
        if schema_errors(key, names, root, child):
            found.append((child, "nom de clé non admis"))
            continue
        matched = [rule for pattern, rule in patterns.items() if re.search(portable_pattern(pattern), key)]
        if key in properties:
            matched.insert(0, properties[key])
        for rule in matched:  # propriété déclarée et motifs : tous s'appliquent
            found += schema_errors(item, rule, root, child)
        if matched:
            continue
        if extra is False:
            found.append((child, "clé inconnue"))
        else:
            found += schema_errors(item, extra, root, child)
    return found


def _any_of_message(variants: list[dict[str, Any]]) -> str:
    required = [variant["required"] for variant in variants if set(variant) == {"required"}]
    if len(required) == len(variants):
        return "l'un de ces champs est requis : " + ", ".join(" + ".join(keys) for keys in required)
    return "aucune forme admise ne correspond"


def _not_message(rule: dict[str, Any]) -> str:
    if set(rule) == {"required"}:
        return "champs exclusifs : " + ", ".join(rule["required"])
    return "forme refusée"


__all__ = ["portable_pattern", "schema_errors"]
