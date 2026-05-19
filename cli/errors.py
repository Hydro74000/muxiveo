"""CLI exception types."""

from __future__ import annotations

from cli.constants import EXIT_ARGS, EXIT_WORKFLOW


class CliError(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_WORKFLOW) -> None:
        self.exit_code = exit_code
        super().__init__(message)


class ContractError(CliError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("Contrat JSON invalide :\n" + "\n".join(f"- {err}" for err in errors), EXIT_ARGS)
        self.errors = errors
