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
    FORBIDDEN = "forbidden"                  # opération privilégiée sans autorisation explicite
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

    def __init__(self, code: ErrorCode, message: str, *, details: Iterable[ErrorDetail] = (),
                 logged: bool = False) -> None:
        super().__init__(message)
        self.code = ErrorCode(code)
        self.message = message
        self.details: tuple[ErrorDetail, ...] = tuple(details)
        #: Message déjà publié au journal par l'opération : l'adaptateur ne le répète pas.
        self.logged = logged

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.details:
            payload["details"] = [detail.to_dict() for detail in self.details]
        return payload

    def render(self) -> str:
        """Message lisible, détails compris (une ligne par champ ; ceux que le message cite déjà sont omis)."""
        extra = [d for d in self.details if d.path or d.message not in self.message]
        if not extra:
            return self.message
        lines = [self.message, *(f"- {d.path or '(requête)'} : {d.message}" for d in extra)]
        return "\n".join(lines)

    @classmethod
    def from_exception(cls, exc: BaseException) -> ApiError:
        """Erreur publique correspondant à une exception quelconque levée par une opération."""
        if isinstance(exc, ApiError):
            return exc
        if isinstance(exc, KeyboardInterrupt):
            return cls(ErrorCode.CANCELLED, "Interrompu.")
        return cls(error_code(exc), str(exc) or type(exc).__name__)


def error_code(exc: object) -> ErrorCode:
    """Code public d'une exception, d'après elle-même ou ses causes (``raise … from``).

    Les workflows enveloppent les refus de destination et les annulations dans leurs propres
    erreurs (``RemuxError``, ``EncodeError``) : la cause d'origine fixe le code.
    """
    from core.output_commit import OutputBusyError, OutputRefusedError
    from core.runner import ToolNotFoundError

    cancelled = _cancellation_types()
    seen: set[int] = set()
    current: BaseException | None = exc if isinstance(exc, BaseException) else None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ApiError):
            return current.code
        if isinstance(current, ToolNotFoundError):
            return ErrorCode.TOOL_MISSING
        if isinstance(current, OutputBusyError):
            return ErrorCode.BUSY
        if isinstance(current, OutputRefusedError):  # sortie apparue ou changée depuis le contrôle / la confirmation
            return ErrorCode.OUTPUT_EXISTS
        if isinstance(current, cancelled):
            return ErrorCode.CANCELLED
        current = current.__cause__
    return ErrorCode.WORKFLOW_FAILED


def _cancellation_types() -> tuple[type[BaseException], ...]:
    """Exceptions d'annulation des outils et workflows (importées à la demande)."""
    from core.fel.engine import FelCancelled
    from core.hdr_inspection.inspector import InspectionCancelled as DynamicInspectionCancelled
    from core.hdr_inspection.patch import PatchCancelled
    from core.inspector import InspectionCancelled
    from core.matroska.hevc.access_units import HevcStreamCancelled
    from core.matroska.writer import MatroskaWriteCancelled
    from core.nvencc_install import NvenccCancelled
    from core.plugins import PluginCancelled
    from core.runner import TaskCancelledError
    from core.subprocess_utils import ProbeCancelledError

    return (KeyboardInterrupt, TaskCancelledError, FelCancelled, DynamicInspectionCancelled, PatchCancelled,
            InspectionCancelled, HevcStreamCancelled, MatroskaWriteCancelled, NvenccCancelled, PluginCancelled,
            ProbeCancelledError)
