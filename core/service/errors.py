"""Erreurs de la couche de service : codes stables, indépendants du transport.

Le CLI traduit chaque code en code de sortie (``cli/service_adapter.py``) ; un futur
daemon le traduira en statut de son protocole. Les messages sont destinés à l'humain,
les codes et chemins de champ aux programmes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """Codes d'erreur publics (stables d'une version à l'autre)."""

    INVALID_REQUEST = "invalid_request"      # requête mal formée (types, clés, champs requis)
    UNKNOWN_OPERATION = "unknown_operation"  # opération absente du catalogue
    VALIDATION = "validation"                # requête bien formée mais refusée par les règles métier
    TOOL_MISSING = "tool_missing"            # outil externe introuvable
    OUTPUT_EXISTS = "output_exists"          # sortie existante sans autorisation d'écrasement
    BUSY = "busy"                            # ressource déjà utilisée (destination verrouillée…)
    WORKFLOW_FAILED = "workflow_failed"      # échec d'exécution
    CANCELLED = "cancelled"                  # annulation demandée
    PARTIAL = "partial"                      # job parent : une partie seulement a réussi


@dataclass(frozen=True)
class ErrorDetail:
    """Précision rattachée à un champ de la requête (chemin pointé, ex. ``inputs[0]``)."""

    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


class ApiError(Exception):
    """Erreur publique d'une opération."""

    def __init__(self, code: ErrorCode, message: str, *, details: Iterable[ErrorDetail] = ()) -> None:
        super().__init__(message)
        self.code = ErrorCode(code)
        self.message = message
        self.details: tuple[ErrorDetail, ...] = tuple(details)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.details:
            payload["details"] = [detail.to_dict() for detail in self.details]
        return payload

    def render(self) -> str:
        """Message lisible, détails compris (une ligne par champ)."""
        if not self.details:
            return self.message
        lines = [self.message, *(f"- {d.path or '(requête)'} : {d.message}" for d in self.details)]
        return "\n".join(lines)

    @classmethod
    def from_exception(cls, exc: BaseException) -> ApiError:
        """Erreur publique correspondant à une exception quelconque levée par une opération."""
        if isinstance(exc, ApiError):
            return exc
        from core.runner import ToolNotFoundError

        if isinstance(exc, ToolNotFoundError):
            return cls(ErrorCode.TOOL_MISSING, str(exc))
        if isinstance(exc, KeyboardInterrupt):
            return cls(ErrorCode.CANCELLED, "Interrompu.")
        return cls(ErrorCode.WORKFLOW_FAILED, str(exc) or type(exc).__name__)
