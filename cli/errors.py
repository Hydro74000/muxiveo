"""Erreurs du CLI : alias historiques des erreurs de job (``core.jobs.errors``)."""

from __future__ import annotations

from core.jobs.errors import ContractError, JobError as CliError

__all__ = ["CliError", "ContractError"]
