"""Progression du mux natif : métriques pour l'interface, détails pour le verbose."""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from .writer import MatroskaWriteProgress


_PROGRESS_RE = re.compile(
    r"^(?:Assemblage|Écriture) Matroska(?: \([a-z]+\))? : "
    r"(?:(\d+)% )?\(?\d+ paquets, ([\d.]+ Mio)\)?$"
)


def native_mux_progress_label(line: str) -> str | None:
    """Reconnaît uniquement les métriques, sans masquer les messages d'étape."""
    match = _PROGRESS_RE.fullmatch(line.strip())
    if match is None:
        return None
    percent, size = match.groups()
    return f"{percent + '%' if percent is not None else '…'} - {size}"


def native_mux_progress_callback(
    emit: Callable[[str], None], *, label: str = "Assemblage Matroska",
) -> Callable[[MatroskaWriteProgress], None]:
    """Émet ensemble pourcentage et taille, au plus quatre fois par seconde.

    Les changements d'étape et la fin sont toujours transmis. Un unique
    signal évite qu'un pourcentage seul écrase la taille dans la légende.
    """
    last_time: float | None = None
    last_stage = ""

    def on_progress(progress: MatroskaWriteProgress) -> None:
        nonlocal last_time, last_stage
        now = time.monotonic()
        if progress.stage == last_stage and last_time is not None and now - last_time < 0.25:
            return
        last_time, last_stage = now, progress.stage
        stage = f" ({progress.stage})" if progress.stage != "clusters" else ""
        percent = f"{progress.percent}% " if progress.percent is not None else ""
        emit(
            f"{label}{stage} : {percent}({progress.packets_written} paquets, "
            f"{progress.bytes_written / (1024 * 1024):.1f} Mio)"
        )

    return on_progress
