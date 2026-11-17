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
from typing import Any, Literal

from core.service.context import ExecutionContext
from core.service.errors import ApiError, ErrorCode
from core.service.events import EVENT_SCHEMA, EVENT_VERSION, JobState
from core.service.models import Model, api_payload
from core.service.schemas.documents import used_definitions

#: Version du catalogue (ajout d'opération ou de champ facultatif : inchangée ;
#: retrait ou changement incompatible : incrémentée).
API_VERSION = 1

Handler = Callable[[Any, ExecutionContext], Model]
#: File d'ordonnancement d'un gestionnaire de jobs : ``analysis`` (inspections, aperçus, analyses : client réactif
#: pendant un long traitement), ``job`` (produit ou transforme des médias), ``exclusive`` (configuration,
#: extensions, mise à jour : exécutée seule).
Queue = Literal["analysis", "job", "exclusive"]


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
    #: Modifie la configuration, les extensions ou l'installation : refusée sans autorisation explicite
    #: (``ExecutionContext.allow_privileged``).
    privileged: bool = False
    #: File d'ordonnancement (déclarée par l'opération, jamais déduite des autres indicateurs).
    queue: Queue = "analysis"

    def describe(self, *, definitions: bool = True) -> dict[str, Any]:
        """Description publiée ; ``definitions`` : document autonome (``$defs`` partagées qu'il utilise jointes),
        faux quand le catalogue les joint une fois pour toutes."""
        described = {
            "summary": self.summary,
            "long_running": self.long_running,
            "mutating": self.mutating,
            "uses_gpu": self.uses_gpu,
            "privileged": self.privileged,
            "queue": self.queue,
            "errors": sorted({ErrorCode.INVALID_REQUEST.value, *(code.value for code in self.errors),
                              *([ErrorCode.FORBIDDEN.value] if self.privileged else [])}),
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
    privileged: bool = False,
    queue: Queue = "analysis",
) -> Callable[[Handler], OperationSpec]:
    """Décorateur : transforme un gestionnaire en :class:`OperationSpec`."""

    def wrap(handler: Handler) -> OperationSpec:
        return OperationSpec(name, summary, request, result, handler, long_running, mutating, uses_gpu,
                             tuple(errors), progress, privileged, queue)

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


def authorize(spec: OperationSpec, allow_privileged: bool) -> None:
    """Opération privilégiée sans autorisation explicite → ``forbidden`` (``call``, gestionnaire de jobs)."""
    if spec.privileged and not allow_privileged:
        raise ApiError(ErrorCode.FORBIDDEN, f"Opération privilégiée non autorisée : {spec.name} "
                                            "(autorisation explicite requise, ex. `call --allow-privileged`).")


def execute(spec: OperationSpec, request: Model, ctx: ExecutionContext, *,
            before_terminal: Callable[[Model | None, ApiError | None], None] | None = None) -> Model:
    """Exécute une requête déjà validée ; émet l'état, le résultat ou l'erreur.

    Les exceptions sont propagées telles quelles (l'appelant choisit leur rendu) après
    l'émission d'un événement ``error`` portant le code public. ``before_terminal(result, error)`` est
    appelé avant les événements terminaux (un gestionnaire y enregistre la fin du job : une destination qui
    le consulte en recevant ces événements la voit déjà).
    """
    try:
        ctx.events.state(JobState.RUNNING, operation=spec.name)  # refusée par la destination : état final tenté
        ctx.cancel.raise_if_cancelled()
        result = spec.handler(request, ctx)
        if not isinstance(result, spec.result):
            raise TypeError(f"{spec.name} : résultat {type(result).__name__} au lieu de {spec.result.__name__}")
    except BaseException as exc:
        error = ApiError.from_exception(exc)
        if before_terminal is not None:
            before_terminal(None, error)
        try:
            ctx.events.error(error.to_dict())
        finally:  # état final toujours publié, même si la destination refuse l'erreur
            ctx.events.state(error_state(error), operation=spec.name)
        raise
    if before_terminal is not None:
        before_terminal(result, None)
    try:
        ctx.events.result(result.to_dict())
    finally:  # état final toujours publié, même si la destination refuse le résultat
        ctx.events.state(result_state(result), operation=spec.name)
    return result


def error_state(error: ApiError) -> JobState:
    """État final d'un job en erreur : annulé ou en échec."""
    return JobState.CANCELLED if error.code is ErrorCode.CANCELLED else JobState.FAILED


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
        authorize(spec, ctx.allow_privileged)  # avant toute lecture de la requête
        if ctx.api and not allow_relative and effective is not None and (
                "\0" in str(effective) or not effective.is_absolute()):
            # Requête de l'API : dossier de référence absolu, sans caractère nul (jamais le dossier courant).
            raise ApiError(ErrorCode.INVALID_REQUEST, "base_dir : chemin absolu attendu (sans caractère nul).")
        if ctx.api:
            payload = api_payload(payload)  # sémantique JSON du schéma publié (1.0 entier, nombres représentables)
        request, warnings = spec.request.parse(payload, strict=strict, base_dir=None if allow_relative else effective,
                                               allow_relative=allow_relative, strict_documents=strict_documents)
        for warning in warnings:  # destination en échec : traitée comme les autres échecs d'admission
            ctx.events.warning(f"{warning.message} : {warning.path}", code="unknown_key", path=warning.path)
    except BaseException as exc:  # refus d'admission, défaut imprévu, interruption : publié avant d'être propagé
        error = ApiError.from_exception(exc)
        try:
            ctx.events.error(error.to_dict())
        finally:  # état final toujours tenté (annulé ou en échec), même si la destination refuse l'erreur
            ctx.events.state(error_state(error), operation=name)
        raise
    return execute(spec, request, ctx)
