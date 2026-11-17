"""Modèles publics de la couche de service : requêtes et résultats JSON typés.

Une sous-classe de :class:`Model` est une ``@dataclass(frozen=True)`` dont les annotations
font foi : ``from_dict`` vérifie les types, refuse les clés inconnues (mode strict, requêtes
API) ou les signale (mode tolérant, documents historiques du CLI), distingue un champ absent
(valeur par défaut) d'un ``null`` explicite (accepté seulement si le type l'autorise), et
résout les chemins (``Path``). ``json_schema`` décrit le même contrat pour les clients.

Types pris en charge : ``str``, ``int``, ``float``, ``bool``, ``Path``, ``Any``,
``Literal[...]``, ``Enum``, ``X | None``, ``tuple[X, ...]``, ``dict[str, X]`` et les
sous-classes de :class:`Model` (imbriquées).
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import math
import re
import sys
import types
import typing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeVar, Union

from core.service.errors import ApiError, ErrorCode, ErrorDetail

M = TypeVar("M", bound="Model")

_NONE_TYPE = type(None)


def field(
    *,
    default: Any = dataclasses.MISSING,
    default_factory: Any = dataclasses.MISSING,
    description: str = "",
    minimum: float | None = None,
    maximum: float | None = None,
    min_items: int | None = None,
    pattern: str | None = None,
    schema: dict[str, Any] | None = None,
) -> Any:
    """Champ documenté et contraint (bornes numériques, nombre minimal d'éléments, motif d'un texte).

    ``pattern`` : expression complète (ancres comprises), vérifiée par ``re.fullmatch`` sur le texte tel quel
    et publiée avec une fin de texte stricte en ECMA-262 comme en Python (``portable_pattern``). ``schema`` : schéma JSON publié du champ (document décrit
    ailleurs, ``$ref`` vers une définition partagée), prioritaire sur celui déduit du type ; ``null`` reste
    annoncé si le type l'admet. Publication seule : la lecture suit le type.
    """
    metadata = {
        key: value
        for key, value in (("description", description), ("minimum", minimum),
                           ("maximum", maximum), ("min_items", min_items), ("pattern", pattern),
                           ("schema", schema))
        if value not in (None, "")
    }
    if default_factory is not dataclasses.MISSING:
        return dataclasses.field(default_factory=default_factory, metadata=metadata)
    return dataclasses.field(default=default, metadata=metadata)


@dataclass(frozen=True)
class _Parse:
    strict: bool
    base_dir: Path | None
    allow_relative: bool
    errors: list[ErrorDetail]
    warnings: list[ErrorDetail]
    #: Documents portés par la requête (job, template…) : refus (requête API) ou avertissement (CLI).
    strict_documents: bool = True
    #: Volume des chemins relatifs résolus contre ``base_dir`` (borné, voir ``ResolutionBudget``).
    resolution: Any = None

    def fail(self, path: str, message: str) -> None:
        self.errors.append(ErrorDetail(path, message))


class Model:
    """Base des requêtes et résultats publics (sous-classes ``@dataclass(frozen=True)``)."""

    @classmethod
    def parse(
        cls: type[M],
        data: Any,
        *,
        strict: bool = True,
        base_dir: Path | None = None,
        allow_relative: bool = False,
        strict_documents: bool | None = None,
    ) -> tuple[M, tuple[ErrorDetail, ...]]:
        """Construit le modèle depuis un objet JSON ; renvoie aussi les avertissements (mode tolérant).

        Chemins relatifs : résolus depuis ``base_dir`` ; conservés tels quels avec
        ``allow_relative`` (arguments historiques du CLI) ; refusés sinon.
        ``strict_documents`` (défaut : ``strict``) : écarts des documents portés (``document_issues``)
        refusés ou rendus en avertissements (documents historiques du CLI, toujours tolérants).
        Toute erreur lève :class:`ApiError` ``invalid_request`` avec un détail par champ.
        """
        from core.json_documents import resolution_budget

        with resolution_budget() as budget:  # partagé par les champs chemins et les hooks des documents
            state = _Parse(strict, base_dir, allow_relative, [], [],
                           strict if strict_documents is None else strict_documents, budget)
            value = _parse_model(cls, data, "", state)
        if state.errors or value is None:
            # Requête de l'API (chemins relatifs jamais admis tels quels) : écarts détaillés bornés ; documents et
            # commandes historiques : tous les écarts, comme avant.
            details = state.errors if allow_relative or not strict else _bounded(state.errors)
            raise ApiError(ErrorCode.INVALID_REQUEST, "Requête invalide.", details=details)
        return typing.cast(M, value), tuple(state.warnings)

    @classmethod
    def from_dict(cls: type[M], data: Any, **options: Any) -> M:
        return cls.parse(data, **options)[0]

    def to_dict(self, *, omit_none: bool = False) -> dict[str, Any]:
        """Objet JSON : chemins en texte, énumérations par valeur, tuples en listes.

        Le contenu des champs ``dict``/``Any`` est transmis tel quel (sérialisation finale
        par ``core.jobs.json_io.json_default``).
        """
        payload = {}
        for item in dataclasses.fields(typing.cast(Any, self)):
            value = getattr(self, item.name)
            if omit_none and value is None:
                continue
            payload[item.name] = _dump(value, omit_none)
        return payload

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return _model_schema(cls)


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _join(path: str, name: str | int) -> str:
    if isinstance(name, int):
        return f"{path}[{name}]"
    return f"{path}.{name}" if path else name


def _is_union(tp: Any) -> bool:
    return typing.get_origin(tp) in (Union, types.UnionType)


def _type_label(tp: Any) -> str:
    if _is_union(tp):
        return " | ".join(_type_label(arg) for arg in typing.get_args(tp))
    return {bool: "booléen", int: "entier", float: "nombre", str: "texte", Path: "chemin",
            _NONE_TYPE: "null"}.get(tp, getattr(tp, "__name__", str(tp)))


def _parse_model(cls: type, data: Any, path: str, state: _Parse) -> Any:
    if not isinstance(data, dict):
        state.fail(path, "objet JSON attendu")
        return None
    hints = _hints(cls)
    fields = {item.name: item for item in dataclasses.fields(cls)}
    for key in data:
        if key not in fields:
            detail = ErrorDetail(_join(path, str(key)), "clé inconnue")
            (state.errors if state.strict else state.warnings).append(detail)
    errors_before = len(state.errors)
    values: dict[str, Any] = {}
    for name, item in fields.items():
        if not item.init:
            continue
        child = _join(path, name)
        if name not in data:
            if item.default is dataclasses.MISSING and item.default_factory is dataclasses.MISSING:
                state.fail(child, "champ requis")
            continue
        value = _parse_value(hints[name], data[name], child, state)
        _check_constraints(item, value, child, state)
        values[name] = value
    if len(state.errors) > errors_before:
        return None
    issues = getattr(cls, "document_issues", None)
    if issues is not None:
        for child, message in issues(values):
            (state.errors if state.strict_documents else state.warnings).append(
                ErrorDetail(_join(path, child), message))
    contract = getattr(cls, "document_errors", None)
    if contract is not None and state.strict_documents:
        # Requête API : valeurs des documents refusées dès la lecture (documents CLI : refus du contrat, inchangé).
        for child, message in contract(values):
            state.fail(_join(path, child), message)
    if len(state.errors) > errors_before:
        return None
    if not state.allow_relative:
        # Requête de l'API : chemins des documents utilisables ici, relatifs seulement avec un dossier de référence.
        path_errors = getattr(cls, "document_path_errors", None)
        if path_errors is not None:
            for child, message in path_errors(values, state.base_dir):
                state.fail(_join(path, child), message)
        if len(state.errors) > errors_before:
            return None
        internal = getattr(cls, "without_internal_keys", None)
        if internal is not None:
            values = internal(values)
    resolve = getattr(cls, "resolve_relative_paths", None)
    if resolve is not None and state.base_dir is not None and not state.allow_relative:
        values = resolve(values, state.base_dir)
    try:
        return cls(**values)
    except (TypeError, ValueError) as exc:
        state.fail(path, str(exc))
        return None


def _check_constraints(item: dataclasses.Field, value: Any, path: str, state: _Parse) -> None:
    meta = item.metadata
    if value is None:
        return
    if "min_items" in meta and isinstance(value, tuple) and len(value) < meta["min_items"]:
        state.fail(path, f"au moins {meta['min_items']} élément(s) attendu(s)")
    if "pattern" in meta and isinstance(value, str) and re.fullmatch(meta["pattern"], value) is None:
        state.fail(path, f"format attendu : {meta.get('description') or meta['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in meta and value < meta["minimum"]:
            state.fail(path, f"valeur minimale : {meta['minimum']}")
        if "maximum" in meta and value > meta["maximum"]:
            state.fail(path, f"valeur maximale : {meta['maximum']}")


def _parse_value(tp: Any, value: Any, path: str, state: _Parse) -> Any:  # noqa: C901 - table de types
    if tp is Any:
        return value
    if _is_union(tp):
        options = [arg for arg in typing.get_args(tp) if arg is not _NONE_TYPE]
        if value is None:
            if len(options) < len(typing.get_args(tp)):
                return None
            state.fail(path, f"null refusé ({_type_label(tp)} attendu)")
            return None
        if len(options) != 1:
            raise TypeError(f"Union non prise en charge : {tp!r}")
        return _parse_value(options[0], value, path, state)
    if value is None:
        state.fail(path, f"null refusé ({_type_label(tp)} attendu)")
        return None
    origin = typing.get_origin(tp)
    if origin is Literal:
        allowed = typing.get_args(tp)
        if isinstance(value, bool) or value not in allowed:
            state.fail(path, "valeur attendue parmi : " + ", ".join(map(str, allowed)))
        return value
    if tp is bool:
        if not isinstance(value, bool):
            state.fail(path, "booléen attendu")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            state.fail(path, "entier attendu")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            state.fail(path, "nombre attendu")
            return value
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            state.fail(path, "texte attendu")
        return value
    if tp is Path:
        return _parse_path(value, path, state)
    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(value)
        except ValueError:
            state.fail(path, "valeur attendue parmi : " + ", ".join(str(m.value) for m in tp))
            return value
    if origin in (tuple, list):
        args = typing.get_args(tp)
        if not isinstance(value, list):
            state.fail(path, "liste attendue")
            return ()
        item_type = args[0] if args else Any
        return tuple(_parse_value(item_type, item, _join(path, index), state) for index, item in enumerate(value))
    if origin is dict or tp is dict:
        args = typing.get_args(tp)
        if not isinstance(value, dict):
            state.fail(path, "objet JSON attendu")
            return {}
        value_type = args[1] if len(args) == 2 else Any
        return {key: _parse_value(value_type, item, _join(path, str(key)), state) for key, item in value.items()}
    if isinstance(tp, type) and issubclass(tp, Model):
        return _parse_model(tp, value, path, state)
    raise TypeError(f"Type non pris en charge par les modèles de service : {tp!r}")


def _parse_path(value: Any, path: str, state: _Parse) -> Any:
    if not isinstance(value, str) or not value.strip():
        state.fail(path, "chemin attendu")
        return value
    try:
        candidate = Path(value).expanduser()
    except RuntimeError:  # « ~inconnu/… » : dossier personnel introuvable
        state.fail(path, "dossier personnel inconnu (~utilisateur)")
        return value
    if candidate.is_absolute() or state.allow_relative:
        return candidate
    from core.json_documents import path_kind

    if path_kind(value) == "unusable":  # « C:film.mkv », chemin Windows sous POSIX : jamais rattaché à un dossier
        state.fail(path, "chemin inutilisable sur ce système (absolu pour un autre OS ou ambigu)")
        return candidate
    if state.base_dir is not None:
        if not state.resolution.spend(state.base_dir, value):  # budget épuisé : plus aucune jointure
            from core.json_documents import ResolutionBudget

            state.fail(path, ResolutionBudget.MESSAGE)
            return candidate
        return state.base_dir / candidate
    state.fail(path, "chemin absolu attendu")
    return candidate


# ---------------------------------------------------------------------------
# Écriture
# ---------------------------------------------------------------------------

def _dump(value: Any, omit_none: bool) -> Any:
    if isinstance(value, Model):
        return value.to_dict(omit_none=omit_none)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_dump(item, omit_none) for item in value]
    return value


# ---------------------------------------------------------------------------
# Schéma JSON
# ---------------------------------------------------------------------------

def _model_schema(cls: type) -> dict[str, Any]:
    hints = _hints(cls)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for item in dataclasses.fields(cls):
        if not item.init:
            continue
        meta = item.metadata
        schema = _declared_schema(meta["schema"], hints[item.name]) if "schema" in meta \
            else _type_schema(hints[item.name])
        if meta.get("description"):
            schema["description"] = meta["description"]
        if "pattern" in meta:
            from core.service.schema_check import portable_pattern

            schema["pattern"] = portable_pattern(meta["pattern"])
        if "minimum" in meta:
            schema["minimum"] = meta["minimum"]
        if "maximum" in meta:
            schema["maximum"] = meta["maximum"]
        if "min_items" in meta:
            schema["minItems"] = meta["min_items"]
        if item.default is not dataclasses.MISSING:
            default = _dump(item.default, False)
            if default is None or isinstance(default, (bool, int, float, str)):
                schema["default"] = default
        elif item.default_factory is dataclasses.MISSING:
            required.append(item.name)
        properties[item.name] = schema
    schema = {"type": "object", "title": cls.__name__, "properties": properties, "additionalProperties": False}
    doc = (cls.__doc__ or "").strip().splitlines()
    if doc and not doc[0].startswith(f"{cls.__name__}("):  # signature générée par ``dataclass`` : pas une description
        schema["description"] = doc[0]
    if required:
        schema["required"] = required
    return schema


#: Imbrication maximale d'une requête de l'API (un job en compte une dizaine).
MAX_API_DEPTH = 64
#: Longueur maximale d'une clé d'objet d'une requête de l'API (champs, noms de variables, index…).
MAX_API_KEY_LENGTH = 256
#: Écarts détaillés au plus par document refusé (au-delà : un dernier écart en donne le nombre).
MAX_API_DIAGNOSTICS = 100


def api_payload(value: Any) -> Any:
    """Requête de l'API normalisée selon la sémantique JSON du schéma publié.

    Entiers écrits en flottants (``1.0``) rendus entiers ; refus (``invalid_request``) d'une imbrication de plus de
    :data:`MAX_API_DEPTH` niveaux et des nombres non représentables en double précision (RFC 8259 § 6), que les
    traitements ne pourraient pas convertir, et des clés de plus de :data:`MAX_API_KEY_LENGTH` caractères (chemins
    de diagnostic bornés). Parcours linéaire : chemin construit seulement pour un refus.
    """
    trail: list[str | int] = []

    def refuse(reason: str) -> ApiError:
        path = "".join(f"[{step}]" if isinstance(step, int) else f".{step}" for step in trail).lstrip(".")
        return ApiError(ErrorCode.INVALID_REQUEST, f"{path or 'requête'} : {reason}")

    def walk(item: Any) -> Any:
        if len(trail) > MAX_API_DEPTH:
            raise refuse(f"imbrication de plus de {MAX_API_DEPTH} niveaux.")
        if isinstance(item, bool):
            return item
        if (isinstance(item, int) and abs(item) > sys.float_info.max) or (
                isinstance(item, float) and not math.isfinite(item)):
            raise refuse("nombre hors de la plage des flottants double précision.")
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, list):
            items: list[Any] = []
            for index, child in enumerate(item):
                trail.append(index)
                items.append(walk(child))
                trail.pop()
            return items
        if isinstance(item, dict):
            fields: dict[str, Any] = {}
            for key, child in item.items():
                if len(key) > MAX_API_KEY_LENGTH:
                    raise refuse(f"clé de plus de {MAX_API_KEY_LENGTH} caractères ({key[:32]}…).")
                trail.append(key)
                fields[key] = walk(child)
                trail.pop()
            return fields
        return item

    return walk(value)


#: Marque du message d'un écart qui en résume d'autres (borne d'un document ou de la requête).
UNDETAILED = "écart(s) non détaillé(s)"


def _bounded(errors: list[ErrorDetail]) -> list[ErrorDetail]:
    """Au plus :data:`MAX_API_DIAGNOSTICS` écarts détaillés, puis un dernier donnant le nombre des autres (borne
    inférieure si un écart masqué en résumait déjà d'autres)."""
    if len(errors) <= MAX_API_DIAGNOSTICS:
        return errors
    hidden = errors[MAX_API_DIAGNOSTICS:]
    bound = "au moins " if any(UNDETAILED in detail.message for detail in hidden) else ""
    return [*errors[:MAX_API_DIAGNOSTICS], ErrorDetail("", f"{bound}{len(hidden)} autre(s) {UNDETAILED}")]


def dataclass_schema(cls: type, *, exclude: tuple[str, ...] = (),
                     overrides: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Schéma JSON d'une dataclass existante publiée par ``dataclasses.asdict`` (toutes ses clés présentes).

    ``exclude`` : champs retirés de la représentation publique ; ``overrides`` : schéma d'un champ publié
    autrement que son type (énumération rendue par son libellé…).
    """
    hints = typing.get_type_hints(cls)
    overrides = overrides or {}
    properties = {item.name: copy.deepcopy(overrides[item.name]) if item.name in overrides
                  else _type_schema(hints[item.name])
                  for item in dataclasses.fields(cls) if item.name not in exclude}
    schema = {"type": "object", "title": cls.__name__, "properties": properties, "additionalProperties": False,
              "required": list(properties)}
    doc = (cls.__doc__ or "").strip().splitlines()
    if doc and not doc[0].startswith(f"{cls.__name__}("):
        schema["description"] = doc[0]
    return schema


def list_of(item: type[Model] | dict[str, Any]) -> dict[str, Any]:
    """Schéma d'une liste d'objets décrits (``Model`` de description ou schéma)."""
    return {"type": "array", "items": item if isinstance(item, dict) else item.json_schema()}


def _declared_schema(declared: dict[str, Any], tp: Any) -> dict[str, Any]:
    """Schéma explicite d'un champ (copie) ; ``null`` ajouté si le type l'admet."""
    schema = copy.deepcopy(declared)
    if _is_union(tp) and _NONE_TYPE in typing.get_args(tp):
        return {"anyOf": [schema, {"type": "null"}]}
    return schema


def _type_schema(tp: Any) -> dict[str, Any]:  # noqa: C901 - table de types
    if tp is Any:
        return {}
    if _is_union(tp):
        options = [arg for arg in typing.get_args(tp) if arg is not _NONE_TYPE]
        inner = _type_schema(options[0])
        return {"anyOf": [inner, {"type": "null"}]}
    origin = typing.get_origin(tp)
    if origin is Literal:
        return {"enum": list(typing.get_args(tp))}
    simple = {bool: "boolean", int: "integer", float: "number", str: "string"}
    if tp in simple:
        return {"type": simple[tp]}
    if tp is Path:
        return {"type": "string", "format": "path"}
    if isinstance(tp, type) and issubclass(tp, Enum):
        return {"enum": [member.value for member in tp]}
    if origin in (tuple, list, frozenset, set):
        args = typing.get_args(tp)
        return {"type": "array", "items": _type_schema(args[0]) if args else {}}
    if origin is dict or tp is dict:
        args = typing.get_args(tp)
        extra = _type_schema(args[1]) if len(args) == 2 else {}
        return {"type": "object", "additionalProperties": extra or True}
    if isinstance(tp, type) and issubclass(tp, Model):
        return _model_schema(tp)
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return dataclass_schema(tp)
    raise TypeError(f"Type non pris en charge par les modèles de service : {tp!r}")
