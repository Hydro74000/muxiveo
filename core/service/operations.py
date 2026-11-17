"""Registre des opérations et exécution synchrone.

Une opération = nom stable (``famille.action``), modèle de requête, modèle de résultat,
gestionnaire ``handler(requête, contexte) -> résultat`` et métadonnées (durée, mutation,
GPU) qui serviront à l'autorisation et à l'ordonnancement d'un futur daemon. Le catalogue
est construit explicitement (``core/service/catalog.py``) : aucun enregistrement par effet
de bord d'import.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.service.context import ExecutionContext
from core.service.errors import ApiError, ErrorCode
from core.service.events import EVENT_SCHEMA, EVENT_VERSION, JobState
from core.service.models import Model
from core.service.schemas.documents import used_definitions

#: Version du catalogue (ajout d'opération ou de champ facultatif : inchangée ;
#: retrait ou changement incompatible : incrémentée).
API_VERSION = 1

Handler = Callable[[Any, ExecutionContext], Model]


@dataclass(frozen=True)
class OperationSpec:
    name: str
    summary: str
    request: type[Model]
    result: type[Model]
    handler: Handler
    long_running: bool = False
    mutating: bool = False
    uses_gpu: bool = False
    errors: tuple[ErrorCode, ...] = ()
    #: Publie des événements ``progress`` (pourcentage, vitesse…) : annoncé par ``describe()``.
    progress: bool = False

    def describe(self, *, definitions: bool = True) -> dict[str, Any]:
        """Description publiée ; ``definitions`` : document autonome (``$defs`` partagées qu'il utilise jointes),
        faux quand le catalogue les joint une fois pour toutes."""
        described = {
            "summary": self.summary,
            "long_running": self.long_running,
            "mutating": self.mutating,
            "uses_gpu": self.uses_gpu,
            "errors": sorted({ErrorCode.INVALID_REQUEST.value, *(code.value for code in self.errors)}),
            # Événements possibles (progression : opérations qui la publient ; artefacts : opérations qui écrivent).
            "events": ["state", "log", "warning", *(["progress"] if self.progress else []),
                       *(["artifact"] if self.mutating else []), "result", "error"],
            "request": self.request.json_schema(),
            "result": self.result.json_schema(),
        }
        used = used_definitions(described["request"], described["result"]) if definitions else {}
        return {**described, "$defs": used} if used else described


def operation(
    name: str,
    *,
    request: type[Model],
    result: type[Model],
    summary: str,
    long_running: bool = False,
    mutating: bool = False,
    uses_gpu: bool = False,
    errors: Iterable[ErrorCode] = (),
    progress: bool = False,
) -> Callable[[Handler], OperationSpec]:
    """Décorateur : transforme un gestionnaire en :class:`OperationSpec`."""

    def wrap(handler: Handler) -> OperationSpec:
        return OperationSpec(name, summary, request, result, handler, long_running, mutating, uses_gpu,
                             tuple(errors), progress)

    return wrap


class Catalog:
    """Ensemble immuable d'opérations, indexé par nom."""

    def __init__(self, specs: Iterable[OperationSpec]) -> None:
        index: dict[str, OperationSpec] = {}
        for spec in specs:
            if spec.name in index:
                raise ValueError(f"Opération en double : {spec.name}")
            index[spec.name] = spec
        self._index: Mapping[str, OperationSpec] = dict(sorted(index.items()))

    def __iter__(self) -> Iterator[OperationSpec]:
        return iter(self._index.values())

    def __contains__(self, name: object) -> bool:
        return name in self._index

    def names(self) -> list[str]:
        return list(self._index)

    def get(self, name: str) -> OperationSpec:
        try:
            return self._index[name]
        except KeyError:
            raise ApiError(ErrorCode.UNKNOWN_OPERATION, f"Opération inconnue : {name}") from None

    def describe(self) -> dict[str, Any]:
        """Catalogue publié (``schema --version api``) : base d'un futur contrat OpenAPI / JSON-RPC."""
        operations = {spec.name: spec.describe(definitions=False) for spec in self}
        return {
            "api_version": API_VERSION,
            "event_version": EVENT_VERSION,
            "operations": operations,
            "event": EVENT_SCHEMA,
            "errors": [code.value for code in ErrorCode],
            "$defs": used_definitions(operations),
        }


#: États terminaux qu'un résultat peut annoncer lui-même (jobs parents : lots, Hybrid).
_RESULT_STATES = {"partial": JobState.PARTIAL, "failed": JobState.FAILED, "cancelled": JobState.CANCELLED}


def result_state(result: Model) -> JobState:
    """État terminal d'un résultat : celui qu'il annonce (``status`` d'un job parent), sinon réussite."""
    status = getattr(result, "status", None)
    return _RESULT_STATES.get(status, JobState.SUCCEEDED) if isinstance(status, str) else JobState.SUCCEEDED


def execute(spec: OperationSpec, request: Model, ctx: ExecutionContext) -> Model:
    """Exécute une requête déjà validée ; émet l'état, le résultat ou l'erreur.

    Les exceptions sont propagées telles quelles (l'appelant choisit leur rendu) après
    l'émission d'un événement ``error`` portant le code public.
    """
    ctx.events.state(JobState.RUNNING, operation=spec.name)
    try:
        ctx.cancel.raise_if_cancelled()
        result = spec.handler(request, ctx)
        if not isinstance(result, spec.result):
            raise TypeError(f"{spec.name} : résultat {type(result).__name__} au lieu de {spec.result.__name__}")
    except BaseException as exc:
        error = ApiError.from_exception(exc)
        ctx.events.error(error.to_dict())
        final = JobState.CANCELLED if error.code is ErrorCode.CANCELLED else JobState.FAILED
        ctx.events.state(final, operation=spec.name)
        raise
    ctx.events.result(result.to_dict())
    ctx.events.state(result_state(result), operation=spec.name)
    return result


def call(
    catalog: Catalog,
    name: str,
    payload: Any,
    ctx: ExecutionContext,
    *,
    strict: bool = True,
    base_dir: Path | None = None,
    allow_relative: bool = False,
    strict_documents: bool | None = None,
) -> Model:
    """Valide ``payload`` pour l'opération ``name`` puis l'exécute (mode strict par défaut).

    En mode tolérant (requête, ou seulement ses documents : ``strict_documents=False``), chaque
    clé inconnue devient un événement ``warning``.
    """
    # Une seule base : celle de l'appel, sinon celle du contexte ; admission et exécution l'emploient toutes deux.
    effective = Path(base_dir) if base_dir is not None else ctx.base_dir
    if not allow_relative and effective != ctx.base_dir:
        ctx = dataclasses.replace(ctx, base_dir=effective)
    try:
        spec = catalog.get(name)
        request, warnings = spec.request.parse(payload, strict=strict, base_dir=None if allow_relative else effective,
                                               allow_relative=allow_relative, strict_documents=strict_documents)
    except ApiError as error:
        ctx.events.error(error.to_dict())
        ctx.events.state(JobState.FAILED, operation=name)
        raise
    for warning in warnings:
        ctx.events.warning(f"{warning.message} : {warning.path}", code="unknown_key", path=warning.path)
    return execute(spec, request, ctx)
