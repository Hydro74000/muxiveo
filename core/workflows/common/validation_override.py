"""Décision explicite avant de poursuivre après un rejet du contrôle final."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from core.runner import TaskCancelledError


ValidationOverride = Callable[[Path, str, Callable[[], bool]], bool]


@dataclass
class ValidationOverrideRequest:
    path: Path
    message: str
    cancelled: Callable[[], bool]
    _event: threading.Event = field(default_factory=threading.Event, init=False)
    _accepted: bool = field(default=False, init=False)

    def resolve(self, accepted: bool) -> None:
        self._accepted = bool(accepted)
        self._event.set()

    @property
    def resolved(self) -> bool:
        return self._event.is_set()

    def wait(self) -> bool:
        while not self._event.wait(0.05):
            if self.cancelled():
                return False
        return self._accepted and not self.cancelled()


def accept_validation_override(
    override: ValidationOverride | None,
    path: Path,
    message: str,
    cancelled: Callable[[], bool],
    warn: Callable[[str], None] | None = None,
) -> bool:
    """Sans gestionnaire interactif, un rejet reste un rejet (CLI et previews)."""
    if override is None or cancelled():
        return False
    accepted = override(path, message, cancelled)
    if cancelled():
        return False
    if accepted and warn is not None:
        warn(f"Contrôle final ignoré sur décision explicite de l'utilisateur : {message}")
    return accepted


def validate_final_output(
    path: Path,
    errors: list[str],
    probe: Callable[[], object],
    *,
    message_prefix: str,
    override: ValidationOverride | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    warn: Callable[[str], None] | None = None,
) -> None:
    """Regroupe les rejets sémantiques et ffprobe en une seule décision avant commit."""
    failures = list(errors)
    if cancelled():
        raise TaskCancelledError()
    if failures and override is None:
        raise RuntimeError(message_prefix + " ; ".join(failures))
    try:
        probe()
    except Exception as exc:
        failures.append(f"Validation ffprobe : {exc}")
    if cancelled():
        raise TaskCancelledError()
    if failures:
        message = message_prefix + " ; ".join(failures)
        accepted = accept_validation_override(override, path, message, cancelled, warn)
        if cancelled():
            raise TaskCancelledError()
        if not accepted:
            raise RuntimeError(message)
