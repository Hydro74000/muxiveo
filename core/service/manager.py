"""Gestionnaire de jobs : plusieurs opérations en parallèle dans un même processus (worker, intégration Python).

Admission stricte (opération connue, autorisation, requête valide) avant toute création de job ; puis file
FIFO par classe d'opération (``OperationSpec.queue``) : ``max_jobs`` traitements de médias, ``max_analyses``
analyses (client réactif pendant un long encodage), opérations ``exclusive`` seules — une exclusive en attente
fait barrière. Chaque job reçoit un instantané de la configuration pris à son démarrage ; après une exclusive,
la configuration est relue (``config_factory``). Un résultat ``restart_required`` (mise à jour installée) met
le gestionnaire en drainage : plus aucune admission ni démarrage, jobs en file annulés.

Aucun événement n'est émis sous le verrou du gestionnaire : une destination peut rappeler ``get`` / ``cancel``.
"""

from __future__ import annotations

import collections
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.service.context import CancelToken, ExecutionContext
from core.service.errors import ApiError, ErrorCode
from core.service.events import Emitter, Event, EventSink, EventType, JobState
from core.service.models import Model, field
from core.service.operations import Catalog, OperationSpec, authorize, execute, result_state

#: Jobs en attente au-delà desquels une soumission est refusée (``busy``).
QUEUE_LIMIT = 256
#: Jobs terminés conservés pour ``get`` / ``list``.
HISTORY_LIMIT = 100

_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.PARTIAL}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class JobSummary(Model):
    """État d'un job (liste)."""

    job: str
    operation: str
    state: JobState
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict[str, Any] | None = field(default=None, description="Dernier événement `progress` (données).")
    error: str | None = field(default=None, description="Code d'erreur public d'un job en échec ou annulé.")


@dataclass(frozen=True)
class JobInfo(Model):
    """État complet d'un job : résultat ou erreur une fois terminé."""

    job: str
    operation: str
    state: JobState
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict[str, Any] | None = field(default=None, description="Dernier événement `progress` (données).")
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = field(default=None, description="Erreur publique (`code`, `message`…).")


class _Job:
    """Job suivi par le gestionnaire (attributs modifiés sous son verrou)."""

    def __init__(self, spec: OperationSpec, request: Model, base_dir: Path | None, sink: EventSink) -> None:
        self.spec = spec
        self.request = request
        self.base_dir = base_dir
        self.cancel = CancelToken()
        self.state = JobState.QUEUED
        self.created_at = _now()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.result: Model | None = None
        self.error: ApiError | None = None
        self.progress: dict[str, Any] | None = None
        #: Annoncé au client (``on_admitted`` puis ``queued``) : seul un job annoncé peut démarrer.
        self.announced = False
        self.thread: threading.Thread | None = None
        self.emitter = Emitter(_ProgressTap(self, sink))

    @property
    def id(self) -> str:
        return self.emitter.job

    def summary(self) -> JobSummary:
        return JobSummary(job=self.id, operation=self.spec.name, state=self.state, created_at=self.created_at,
                          started_at=self.started_at, finished_at=self.finished_at, progress=self.progress,
                          error=self.error.code.value if self.error is not None else None)

    def info(self) -> JobInfo:
        return JobInfo(job=self.id, operation=self.spec.name, state=self.state, created_at=self.created_at,
                       started_at=self.started_at, finished_at=self.finished_at, progress=self.progress,
                       result=self.result.to_dict() if self.result is not None else None,
                       error=self.error.to_dict() if self.error is not None else None)


class _ProgressTap:
    """Destination du job : retient sa dernière progression puis transmet à la destination du gestionnaire."""

    def __init__(self, job: _Job, sink: EventSink) -> None:
        self._job = job
        self._sink = sink

    def emit(self, event: Event) -> None:
        if event.type is EventType.PROGRESS and event.job == self._job.id:
            self._job.progress = dict(event.data)  # affectation atomique : lue par get() / list()
        self._sink.emit(event)


class JobManager:
    """Admission, ordonnancement, annulation et suivi des jobs d'un processus."""

    def __init__(self, catalog: Catalog, config_factory: Callable[[], Any], sink: EventSink, *, max_jobs: int = 1,
                 max_analyses: int = 2, allow_privileged: bool = False) -> None:
        if max_jobs < 1 or max_analyses < 1:
            raise ValueError("max_jobs et max_analyses : au moins 1")
        self._catalog = catalog
        self._config_factory = config_factory
        self._config = config_factory()
        self._sink = sink
        self._limits = {"job": max_jobs, "analysis": max_analyses}
        self._allow_privileged = allow_privileged
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._pending: collections.deque[_Job] = collections.deque()
        self._running: dict[str, int] = {"job": 0, "analysis": 0, "exclusive": 0}
        self._finished: collections.deque[str] = collections.deque()
        self._closed = False
        self._draining = False

    @property
    def draining(self) -> bool:
        """Vrai après un résultat ``restart_required`` : le processus doit s'arrêter pour appliquer la mise à jour."""
        with self._lock:
            return self._draining

    # -- Admission --------------------------------------------------------------------------------------------

    def submit(self, operation: str, payload: Any, *, base_dir: Path | None = None,
               on_admitted: Callable[[str], None] | None = None) -> str:
        """Admet un job et renvoie son identifiant ; refus (``ApiError``) sans job créé.

        Ordre garanti : admission → ``on_admitted(job)`` (le client reçoit l'identifiant) → ``queued`` →
        ordonnancement : aucun événement du job ne précède l'annonce de son identifiant.
        """
        spec = self._catalog.get(operation)
        authorize(spec, self._allow_privileged)
        if base_dir is not None and not Path(base_dir).is_absolute():
            raise ApiError(ErrorCode.INVALID_REQUEST, "base_dir : chemin absolu attendu.")
        request, _warnings = spec.request.parse(payload, strict=True, base_dir=base_dir)
        job = _Job(spec, request, base_dir, self._sink)
        with self._lock:
            if self._closed or self._draining:
                raise ApiError(ErrorCode.BUSY, "Gestionnaire en cours d'arrêt : aucune nouvelle soumission.")
            if len(self._pending) >= QUEUE_LIMIT:
                raise ApiError(ErrorCode.BUSY, f"File pleine ({QUEUE_LIMIT} jobs en attente).")
            self._jobs[job.id] = job
        try:
            if on_admitted is not None:
                on_admitted(job.id)
        except BaseException:
            with self._lock:  # identifiant jamais annoncé : le job n'a pas existé
                self._jobs.pop(job.id, None)
            raise
        job.emitter.state(JobState.QUEUED, operation=spec.name)
        with self._lock:
            job.announced = True
            refused = job.cancel.cancelled or self._closed or self._draining
            if refused:
                self._finish_locked(job, ApiError(ErrorCode.CANCELLED, "Job annulé avant son démarrage."))
            else:
                self._pending.append(job)
                self._schedule()
        if refused:
            self._announce_cancelled(job)
        return job.id

    # -- Suivi ------------------------------------------------------------------------------------------------

    def get(self, job: str) -> JobInfo:
        with self._lock:
            return self._find(job).info()

    def list(self) -> tuple[JobSummary, ...]:
        """Jobs connus (en file, en cours, terminés récents), dans l'ordre d'admission."""
        with self._lock:
            return tuple(item.summary() for item in self._jobs.values())

    def cancel(self, job: str) -> bool:
        """En file : retiré et annoncé annulé ; en cours : jeton levé ; terminé : ``False``."""
        with self._lock:
            target = self._find(job)
            if target.state in _TERMINAL:
                return False
            queued = target.announced and target.state is JobState.QUEUED
            if queued:
                self._pending.remove(target)
                self._finish_locked(target, ApiError(ErrorCode.CANCELLED, "Job annulé avant son démarrage."))
                self._schedule()  # une exclusive retirée ne fait plus barrière
        if queued:
            self._announce_cancelled(target)
        else:
            target.cancel.cancel()  # en cours, ou en cours d'annonce (submit() le retire) : hors du verrou
        return True

    def shutdown(self, timeout: float | None = None) -> None:
        """Refuse les soumissions, annule la file et les jobs en cours, attend la fin de chaque ``execute()``."""
        with self._lock:
            self._closed = True
            dropped = list(self._pending)
            self._pending.clear()
            for job in dropped:
                self._finish_locked(job, ApiError(ErrorCode.CANCELLED, "Arrêt du gestionnaire."))
            running = [job for job in self._jobs.values() if job.state is JobState.RUNNING]
        for job in dropped:
            self._announce_cancelled(job)
        for job in running:
            job.cancel.cancel()
        for job in running:
            if job.thread is not None:
                job.thread.join(timeout)

    # -- Ordonnancement (sous le verrou) ----------------------------------------------------------------------

    def _find(self, job: str) -> _Job:
        try:
            return self._jobs[job]
        except KeyError:
            raise ApiError(ErrorCode.INVALID_REQUEST, f"Job inconnu : {job}") from None

    def _schedule(self) -> None:
        """Démarre les jobs admissibles (FIFO par classe ; une exclusive attend que tout soit fini et fait
        barrière). Fils démarrés sous le verrou : un job en cours a toujours un fil à attendre."""
        if self._closed or self._draining or self._running["exclusive"]:
            return
        for job in list(self._pending):
            queue = job.spec.queue
            if queue == "exclusive":
                if not any(self._running.values()):
                    self._start_locked(job)
                break  # rien de soumis après elle ne démarre avant elle
            if self._running[queue] < self._limits[queue]:
                self._start_locked(job)

    def _start_locked(self, job: _Job) -> None:
        self._pending.remove(job)
        self._running[job.spec.queue] += 1
        job.state = JobState.RUNNING
        job.started_at = _now()
        job.thread = threading.Thread(target=self._run, args=(job, self._config), name=f"job-{job.spec.name}",
                                      daemon=True)
        job.thread.start()

    def _finish_locked(self, job: _Job, error: ApiError | None = None) -> None:
        if error is not None:
            job.error = error
            job.state = JobState.CANCELLED if error.code is ErrorCode.CANCELLED else JobState.FAILED
        job.finished_at = _now()
        self._finished.append(job.id)
        while len(self._finished) > HISTORY_LIMIT:
            self._jobs.pop(self._finished.popleft(), None)

    # -- Exécution --------------------------------------------------------------------------------------------

    @staticmethod
    def _announce_cancelled(job: _Job) -> None:
        assert job.error is not None
        job.emitter.error(job.error.to_dict())
        job.emitter.state(JobState.CANCELLED, operation=job.spec.name)

    def _run(self, job: _Job, config: Any) -> None:
        ctx = ExecutionContext.create(config, self._sink, cancel=job.cancel, base_dir=job.base_dir,
                                      allow_privileged=self._allow_privileged, emitter=job.emitter)
        result: Model | None = None
        error: ApiError | None = None
        try:
            result = execute(job.spec, job.request, ctx)
        except BaseException as exc:  # noqa: BLE001 - toute fin de job est enregistrée (événements déjà émis)
            error = ApiError.from_exception(exc)
        renewed = None
        if job.spec.queue == "exclusive":  # exclusivité encore tenue : rien d'autre ne tourne pendant la relecture
            try:
                renewed = self._config_factory()
            except Exception:  # noqa: BLE001 - configuration illisible : la précédente reste en vigueur
                renewed = None
        with self._lock:
            if renewed is not None:
                self._config = renewed
            job.result = result
            if result is not None:
                job.state = result_state(result)
            self._finish_locked(job, error)
            self._running[job.spec.queue] -= 1
            dropped: list[_Job] = []
            if result is not None and getattr(result, "restart_required", False):
                self._draining = True
                dropped = list(self._pending)
                self._pending.clear()
                for item in dropped:
                    self._finish_locked(item, ApiError(ErrorCode.CANCELLED, "Mise à jour installée : arrêt."))
            self._schedule()
        for item in dropped:
            self._announce_cancelled(item)


__all__ = ["HISTORY_LIMIT", "QUEUE_LIMIT", "JobInfo", "JobManager", "JobSummary"]
