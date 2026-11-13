"""Erreurs de construction et d'exécution des jobs.

Une :class:`JobError` est une erreur publique de la couche de service (``ApiError``) qui
garde le code de sortie historique du CLI (``exit_code``) : le CLI l'affiche comme avant,
le service la publie avec son code stable.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.jobs.constants import (
    EXIT_ARGS,
    EXIT_EXISTS,
    EXIT_PARTIAL,
    EXIT_TOOL,
    EXIT_VALIDATION,
    EXIT_WORKFLOW,
)
from core.service.errors import ApiError, ErrorCode

#: Code de sortie du CLI pour chaque code d'erreur public (codes historiques conservés).
EXIT_BY_ERROR: Mapping[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: EXIT_ARGS,
    ErrorCode.UNKNOWN_OPERATION: EXIT_ARGS,
    ErrorCode.VALIDATION: EXIT_VALIDATION,
    ErrorCode.TOOL_MISSING: EXIT_TOOL,
    ErrorCode.OUTPUT_EXISTS: EXIT_EXISTS,
    ErrorCode.BUSY: EXIT_WORKFLOW,
    ErrorCode.WORKFLOW_FAILED: EXIT_WORKFLOW,
    ErrorCode.CANCELLED: EXIT_WORKFLOW,
    ErrorCode.PARTIAL: EXIT_PARTIAL,
}

_ERROR_BY_EXIT: Mapping[int, ErrorCode] = {
    EXIT_ARGS: ErrorCode.INVALID_REQUEST,
    EXIT_VALIDATION: ErrorCode.VALIDATION,
    EXIT_TOOL: ErrorCode.TOOL_MISSING,
    EXIT_EXISTS: ErrorCode.OUTPUT_EXISTS,
    EXIT_WORKFLOW: ErrorCode.WORKFLOW_FAILED,
    EXIT_PARTIAL: ErrorCode.PARTIAL,
}


def exit_code_for(error: ApiError) -> int:
    """Code de sortie du CLI d'une erreur publique."""
    if isinstance(error, JobError):
        return error.exit_code
    return EXIT_BY_ERROR.get(error.code, EXIT_WORKFLOW)


class JobError(ApiError, RuntimeError):
    """Erreur d'un job, avec son code de sortie historique (nom historique côté CLI : ``CliError``)."""

    def __init__(self, message: str, exit_code: int = EXIT_WORKFLOW, *, logged: bool = False) -> None:
        super().__init__(_ERROR_BY_EXIT.get(exit_code, ErrorCode.WORKFLOW_FAILED), message, logged=logged)
        self.exit_code = exit_code


class ContractError(JobError):
    """Document JSON non conforme au contrat (une ligne par écart dans le message)."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("Contrat JSON invalide :\n" + "\n".join(f"- {err}" for err in errors), EXIT_ARGS)
        self.errors = errors
