"""Documents écrits par les opérations (calibrations, presets exportés, sous-titres recalés) : publication sous
réservation de destination, sans écrasement non autorisé même entre appels concurrents."""

from __future__ import annotations

from pathlib import Path

from core.jobs.constants import EXIT_EXISTS
from core.jobs.errors import JobError
from core.output_commit import OutputBusyError, OutputExistsError, publish_document
from core.service.errors import ApiError, ErrorCode


def publish_request_document(path: Path, content: bytes, *, force: bool, exists_message: str) -> Path:
    """Publie ``content`` sur ``path`` ; destination présente sans ``force`` → ``output_exists``
    (``exists_message``), autre écrivain en cours → ``busy``. Premier document toujours conservé."""
    try:
        return publish_document(Path(path), content, force=force)
    except OutputExistsError as exc:
        raise JobError(exists_message, EXIT_EXISTS) from exc
    except OutputBusyError as exc:
        raise ApiError(ErrorCode.BUSY, str(exc)) from exc


__all__ = ["publish_request_document"]
