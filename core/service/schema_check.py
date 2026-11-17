"""Vérification structurelle d'une valeur JSON face à un schéma publié (sous-ensemble de JSON Schema 2020-12).

Sert l'admission des documents d'une requête de l'API (le job est vérifié contre le schéma que publie le
catalogue : même verdict pour le client et pour le service) et les tests de contrat. Mots-clés pris en
charge : ``$ref`` (``#/$defs/…``), ``type``, ``const``, ``enum``, ``oneOf`` (exactement une variante), ``anyOf``,
``not``, ``properties``, ``required``, ``additionalProperties`` (booléen ou schéma), ``patternProperties``,
``propertyNames``, ``items``, ``prefixItems``, ``minItems`` / ``maxItems``, ``minimum`` / ``maximum``,
``exclusiveMinimum``, ``uniqueItems``, ``pattern`` ; sous-schémas booléens (``true`` / ``false``). Les autres
(``description``, ``default``, ``format``…) n'ont pas d'effet. Sémantique JSON : un entier peut s'écrire ``1.0``, un booléen
n'est jamais un nombre, ``$`` d'un motif marque la fin du texte (ECMA-262, pas « avant un saut de ligne final »).
"""

from __future__ import annotations

import functools
import json
import re
import sys
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


def _canonical(value: Any) -> str:
    """Forme comparable selon l'égalité JSON (``1`` = ``1.0``, ``true`` ≠ ``1``, ordre des clés indifférent) :
    doublons détectés en temps linéaire."""
    def plain(item: Any) -> Any:
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, list):
            return [plain(child) for child in item]
        if isinstance(item, dict):
            return {key: plain(child) for key, child in item.items()}
        return item

    return json.dumps(plain(value), sort_keys=True, ensure_ascii=False)


@functools.lru_cache(maxsize=256)
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


#: Chemin construit à la demande : ``(parent, clé)`` chaînés depuis le chemin de départ (texte).
_At = tuple[Any, Any]

#: Mots-clés qui contraignent une valeur ; un schéma sans aucun d'eux accepte tout (aucun parcours).
_CONSTRAINTS = frozenset({"$ref", "type", "const", "enum", "oneOf", "anyOf", "not", "pattern", "minimum",
                          "maximum", "exclusiveMinimum", "minItems", "maxItems", "uniqueItems", "prefixItems", "items",
                          "properties", "required", "additionalProperties", "patternProperties", "propertyNames"})


def _text(at: _At | str) -> str:
    keys: list[Any] = []
    while not isinstance(at, str):
        at, key = at
        keys.append(key)
    for key in reversed(keys):
        at = _join(at, key)
    return at


class _Full(Exception):
    """Budget d'écarts épuisé : le parcours s'arrête (verdict déjà acquis)."""


class _Out:
    """Écarts collectés, au plus ``budget`` (au-delà : :class:`_Full`)."""

    __slots__ = ("found", "budget")

    def __init__(self, budget: int) -> None:
        self.found: list[tuple[_At | str, str]] = []
        self.budget = budget

    def add(self, at: _At | str, message: str) -> None:
        if len(self.found) >= self.budget:
            raise _Full
        self.found.append((at, message))


def schema_errors(value: Any, schema: dict[str, Any] | bool, root: dict[str, Any],
                  path: str = "", *, distinct: bool = False, limit: int | None = None) -> list[tuple[str, str]]:
    """Écarts de ``value`` à ``schema`` : liste de ``(chemin, message)`` ; références résolues dans ``root``.

    Parcours linéaire : chemins construits seulement pour un écart retenu, sous-schémas sans contrainte non
    parcourus, variantes d'un combinateur évaluées jusqu'à leur premier écart. ``limit`` : au plus ``limit``
    écarts rendus (parcours arrêté après dix fois plus d'écarts collectés). ``distinct`` : un écart par cause —
    les plus profonds d'abord, un écart au même endroit qu'un écart retenu ou au-dessus de lui (forme non
    reconnue, champ requis alternatif) est omis.
    """
    out = _Out(sys.maxsize if limit is None else 10 * limit)
    try:
        _collect(value, schema, root, path, out)
    except _Full:
        pass
    found = _distinct(out.found) if distinct else out.found
    return [(_text(at), message) for at, message in found[:limit]]


def _fails(value: Any, schema: dict[str, Any] | bool, root: dict[str, Any], at: _At | str) -> bool:
    """Vrai au premier écart (variante d'un combinateur : le détail n'est jamais rendu)."""
    out = _Out(0)
    try:
        _collect(value, schema, root, at, out)
    except _Full:
        return True
    return False


def _depth(at: _At | str) -> int:
    depth = 0
    while not isinstance(at, str):
        at = at[0]
        depth += 1
    return depth


def _distinct(found: list[tuple[_At | str, str]]) -> list[tuple[_At | str, str]]:
    """Écarts retenus, les plus profonds d'abord ; nœuds identifiés par identité (chemins partagés du parcours) :
    chaque nœud n'est marqué qu'une fois, coût linéaire."""
    covered: set[int] = set()
    kept: list[tuple[_At | str, str]] = []
    for at, message in sorted(found, key=lambda error: -_depth(error[0])):
        if id(at) in covered:
            continue
        kept.append((at, message))
        node: _At | str = at
        while id(node) not in covered:
            covered.add(id(node))
            if isinstance(node, str):
                break
            node = node[0]
    return kept


def _collect(value: Any, schema: dict[str, Any] | bool, root: dict[str, Any],  # noqa: C901 - mots-clés
             at: _At | str, out: _Out) -> None:
    if schema is True or (isinstance(schema, dict) and _CONSTRAINTS.isdisjoint(schema)):
        return
    if schema is False:
        out.add(at, "valeur refusée")
        return
    ref = schema.get("$ref")
    if isinstance(ref, str):
        _collect(value, root["$defs"][ref.removeprefix(_REF_PREFIX)], root, at, out)
    kinds = schema.get("type")
    if kinds is not None:
        expected = [kinds] if isinstance(kinds, str) else list(kinds)
        if not any(_is(value, kind) for kind in expected):
            out.add(at, f"{' ou '.join(expected)} attendu")
            return
    if "const" in schema and not _same(value, schema["const"]):
        out.add(at, f"{schema['const']!r} attendu")
    if "enum" in schema and not any(_same(value, choice) for choice in schema["enum"]):
        out.add(at, f"valeur parmi {schema['enum']} attendue")
    if "oneOf" in schema:
        matching = sum(not _fails(value, variant, root, at) for variant in schema["oneOf"])
        if matching != 1:
            out.add(at, "aucune forme admise ne correspond" if not matching else "forme ambiguë")
    if "anyOf" in schema and all(_fails(value, variant, root, at) for variant in schema["anyOf"]):
        out.add(at, _any_of_message(schema["anyOf"]))
    if "not" in schema and not _fails(value, schema["not"], root, at):
        out.add(at, _not_message(schema["not"]))
    if isinstance(value, str) and "pattern" in schema and re.search(portable_pattern(schema["pattern"]),
                                                                    value) is None:
        out.add(at, f"format attendu : {schema['pattern']}")
    if _is(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            out.add(at, f"au moins {schema['minimum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            out.add(at, f"plus de {schema['exclusiveMinimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.add(at, f"au plus {schema['maximum']}")
    if isinstance(value, list):
        _array(value, schema, root, at, out)
    if isinstance(value, dict):
        _object(value, schema, root, at, out)


def _array(value: list[Any], schema: dict[str, Any], root: dict[str, Any], at: _At | str, out: _Out) -> None:
    if len(value) < schema.get("minItems", 0):
        out.add(at, f"au moins {schema['minItems']} élément(s)")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        out.add(at, f"au plus {schema['maxItems']} élément(s)")
    if schema.get("uniqueItems") and len({_canonical(item) for item in value}) < len(value):
        out.add(at, "éléments en double")
    prefix = schema.get("prefixItems", [])
    for index, item in enumerate(value):
        rule = prefix[index] if index < len(prefix) else schema.get("items", True)
        _collect(item, rule, root, (at, index), out)


def _object(value: dict[str, Any], schema: dict[str, Any], root: dict[str, Any], at: _At | str, out: _Out) -> None:
    for key in schema.get("required", ()):
        if key not in value:
            out.add((at, key), "champ requis")
    properties = schema.get("properties", {})
    patterns = schema.get("patternProperties", {})
    extra = schema.get("additionalProperties", True)
    names = schema.get("propertyNames", True)
    for key, item in value.items():
        child = (at, key)
        if _fails(key, names, root, child):
            out.add(child, "nom de clé non admis")
            continue
        matched = [rule for pattern, rule in patterns.items() if re.search(portable_pattern(pattern), key)]
        if key in properties:
            matched.insert(0, properties[key])
        for rule in matched:  # propriété déclarée et motifs : tous s'appliquent
            _collect(item, rule, root, child, out)
        if matched:
            continue
        if extra is False:
            out.add(child, "clé inconnue")
        else:
            _collect(item, extra, root, child, out)


def _any_of_message(variants: list[dict[str, Any] | bool]) -> str:
    required = [variant["required"] for variant in variants if isinstance(variant, dict) and set(variant) == {"required"}]
    if variants and len(required) == len(variants):
        return "l'un de ces champs est requis : " + ", ".join(" + ".join(keys) for keys in required)
    return "aucune forme admise ne correspond"


def _not_message(rule: dict[str, Any] | bool) -> str:
    if isinstance(rule, dict) and set(rule) == {"required"}:
        return "champs exclusifs : " + ", ".join(rule["required"])
    return "forme refusée"


__all__ = ["portable_pattern", "schema_errors"]
