"""Cache et chronométrage de la préparation des plans (Hybrid Studio, saison).

Les résultats sont liés à l'identité du fichier (chemin, taille, date de
modification) : un fichier modifié n'est jamais servi depuis le cache. Le
cache appartient à une session (un panneau) et peut être partagé entre les
threads de préparation.
"""
from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generator

if TYPE_CHECKING:
    from core.inspector import FileInfo, FileInspector

FileIdentity = tuple[str, int, int]


def file_identity(path: Path) -> FileIdentity | None:
    """Chemin résolu, taille et date de modification ; None si le fichier est illisible."""
    try:
        stat = path.stat()
        return str(path.resolve()), stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


@dataclass(frozen=True)
class SubtitleTextSummary:
    """Ce que la détection forcés / SDH retient du texte d'une piste."""

    count: int
    #: Première description sonore trouvée (``[musique]``…), vide sinon.
    sound_description: str


class PreparationCache:
    """Inspections, résumés de sous-titres et réponses TMDB d'une session."""

    def __init__(self, *, max_entries: int = 1024) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        self._max_entries = max_entries
        #: Réponses JSON TMDB par URL (lecture seule pour le fetcher).
        self.tmdb_responses: dict[str, Any] = {}

    def _get(self, key: tuple[Any, ...]) -> Any | None:
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def _put(self, key: tuple[Any, ...], value: Any) -> None:
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def inspect(self, inspector: FileInspector, path: Path) -> FileInfo:
        """``inspector.inspect(path)`` mémorisé ; chaque appel reçoit sa propre copie."""
        identity = file_identity(path)
        key = ("inspection", identity, inspector.tool_signature)
        cached = self._get(key) if identity is not None else None
        if cached is not None:
            return copy.deepcopy(cached)
        info = inspector.inspect(path)
        # Fichier modifié pendant l'inspection : résultat rendu, jamais mémorisé.
        if identity is not None and file_identity(path) == identity:
            self._put(key, copy.deepcopy(info))
        return info

    def subtitle_summary(self, identity: FileIdentity, stream_index: int) -> SubtitleTextSummary | None:
        return self._get(("subtitle", identity, stream_index))

    def store_subtitle_summary(self, identity: FileIdentity, stream_index: int, summary: SubtitleTextSummary) -> None:
        self._put(("subtitle", identity, stream_index), summary)


@dataclass
class StepTimings:
    """Durées cumulées par étape d'un plan (un thread)."""

    values: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def measure(self, step: str) -> Generator[None, None, None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.values[step] = self.values.get(step, 0.0) + time.perf_counter() - start

    def summary(self) -> str:
        return " · ".join(f"{step} {seconds:.1f} s" for step, seconds in self.values.items())


@dataclass
class PreparationContext:
    """Cache, chronométrage et annulation transmis aux constructeurs de plan."""

    cache: PreparationCache | None = None
    timings: StepTimings = field(default_factory=StepTimings)
    cancel_event: threading.Event | None = None

    @contextmanager
    def measure(self, step: str) -> Generator[None, None, None]:
        with self.timings.measure(step):
            yield
