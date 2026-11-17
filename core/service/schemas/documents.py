"""Documents décrits une fois dans le catalogue de l'API (``$defs``) et reliés par ``$ref``.

``job`` : copie du contrat ``cli-job-v1`` pour les requêtes, fermée comme l'admission
(``core.jobs.schema.unknown_job_keys``) ; ``encode_block`` : bloc ``encode`` ; ``decision_profile`` :
profil décisionnel v1. Chaque document publié (catalogue, description d'une opération) joint les
définitions qu'il utilise (:func:`with_definitions`) et reste ainsi autonome.
"""

from __future__ import annotations

import copy
import functools
import re
from typing import Any

_PREFIX = "#/$defs/"

JOB_REF: dict[str, Any] = {"$ref": f"{_PREFIX}job"}
ENCODE_BLOCK_REF: dict[str, Any] = {"$ref": f"{_PREFIX}encode_block"}
DECISION_PROFILE_REF: dict[str, Any] = {"$ref": f"{_PREFIX}decision_profile"}

#: Clés ignorées par l'admission (internes ``_…``, ``$…``) : admises par les objets fermés.
_IGNORED_KEYS = "^[_$]"
#: Objets fermés par leur propre validateur (``validate_sync_spec``, ``_validate_flags``) : clés internes refusées.
_STRICT_OBJECTS = frozenset({"sync", "flags"})


def _rewrite_refs(node: Any, name: str, internal: set[str]) -> Any:
    """``#/$defs/x`` interne au document → ``#/$defs/<name>.x`` ; références partagées inchangées."""
    if isinstance(node, list):
        return [_rewrite_refs(item, name, internal) for item in node]
    if not isinstance(node, dict):
        return node
    return {key: (f"{_PREFIX}{name}.{value.removeprefix(_PREFIX)}"
                  if key == "$ref" and isinstance(value, str) and value.removeprefix(_PREFIX) in internal
                  else _rewrite_refs(value, name, internal))
            for key, value in node.items()}


def _hoisted(schema: dict[str, Any], name: str) -> dict[str, Any]:
    """Document autonome → définition ``name`` et ses ``$defs`` internes sous ``name.<définition>``."""
    internal = set(schema.get("$defs", {}))
    body = {key: value for key, value in schema.items() if key not in ("$schema", "$id", "$defs")}
    definitions = {name: _rewrite_refs(body, name, internal)}
    definitions.update({f"{name}.{key}": _rewrite_refs(value, name, internal)
                        for key, value in schema.get("$defs", {}).items()})
    return definitions


def _closed(node: Any, *, strict: bool = False) -> Any:
    """Objets à propriétés déclarées fermés comme l'admission (clés ``_…`` / ``$…`` ignorées, sauf dans
    ``sync`` et ``flags`` qui les refusent) ; tables (``additionalProperties`` = schéma de valeur) inchangées."""
    if isinstance(node, list):
        return [_closed(item) for item in node]
    if not isinstance(node, dict):
        return node
    closed = {key: ({name: _closed(sub, strict=name in _STRICT_OBJECTS) for name, sub in value.items()}
                    if key == "properties" and isinstance(value, dict) else _closed(value))
              for key, value in node.items()}
    if "properties" in closed and not isinstance(closed.get("additionalProperties", True), dict):
        closed["additionalProperties"] = False
        if not strict:
            closed["patternProperties"] = {_IGNORED_KEYS: {}}
    return closed


def _job_definitions() -> dict[str, Any]:
    from core.jobs.schema import build_cli_json_schema

    base = build_cli_json_schema()
    properties = dict(base["properties"])
    variables = properties.pop("variables")  # noms choisis par l'utilisateur : libres, comme à l'admission
    properties.pop("encode")  # bloc strict décrit à part
    job = _closed({**base, "properties": properties})
    job["title"] = "job"
    job["description"] = ("Document de job (contrat v1 ; `kind: \"exact-job\"` : contrat exact-job). "
                          "`version` facultatif en requête.")
    job.pop("required", None)
    job["properties"].update({
        "kind": {"const": "exact-job", "description": "Exact-job : sélecteurs stricts."},
        "variables": variables,
        "encode": ENCODE_BLOCK_REF,
    })
    return _hoisted(job, "job")


@functools.lru_cache(maxsize=1)
def _definitions() -> dict[str, Any]:
    from core.jobs.schema import build_decision_profile_schema_v1
    from core.service.schemas.encode import EncodeBlock

    return {**_job_definitions(), "encode_block": EncodeBlock.json_schema(),
            **_hoisted(build_decision_profile_schema_v1(), "decision_profile")}


@functools.lru_cache(maxsize=1)
def _root() -> dict[str, Any]:
    return {"$defs": _definitions()}


def job_schema_errors(job: dict[str, Any], prefix: str = "job") -> list[tuple[str, str]]:
    """Écarts d'un job au schéma publié ``job`` (admission d'une requête de l'API : même verdict que le client).

    Bloc ``encode`` objet exclu (lecture stricte propre) ; clés déjà signalées par ``unknown_job_keys`` exclues.
    """
    from core.jobs.schema import unknown_job_keys
    from core.service.schema_check import schema_errors

    document = copy.deepcopy({key: value for key, value in job.items()
                              if not (key == "encode" and isinstance(value, dict))})
    for path in unknown_job_keys(job, path=""):  # signalées à part : ni en double, ni masquant leur objet parent
        _drop(document, path)
    root = _root()
    return [(f"{prefix}{'' if not path or path.startswith('[') else '.'}{path}", message)
            for path, message in schema_errors(document, root["$defs"]["job"], root)]


def _drop(document: Any, path: str) -> None:
    """Retire la clé désignée par ``path`` (``.clé`` / ``[rang]`` successifs) si elle existe."""
    steps = [int(index) if index else key for key, index in re.findall(r"\.([^.\[]+)|\[(\d+)\]", path)]
    node = document
    for step in steps[:-1]:
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError):
            return
    if steps and isinstance(node, dict):
        node.pop(steps[-1], None)


def _refs(node: Any) -> set[str]:
    if isinstance(node, list):
        return set().union(*(_refs(item) for item in node))
    if not isinstance(node, dict):
        return set()
    found = {value.removeprefix(_PREFIX) for key, value in node.items()
             if key == "$ref" and isinstance(value, str) and value.startswith(_PREFIX)}
    return found.union(*(_refs(value) for value in node.values()))


def used_definitions(*schemas: Any) -> dict[str, Any]:
    """Définitions partagées utilisées (transitivement) par ces schémas, triées par nom."""
    available = _definitions()
    needed: dict[str, Any] = {}
    pending = sorted(_refs(list(schemas)))
    while pending:
        name = pending.pop()
        if name in needed or name not in available:
            continue
        needed[name] = available[name]
        pending.extend(sorted(_refs(available[name])))
    return copy.deepcopy(dict(sorted(needed.items())))


__all__ = ["DECISION_PROFILE_REF", "ENCODE_BLOCK_REF", "JOB_REF", "job_schema_errors", "used_definitions"]
