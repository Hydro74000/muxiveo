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

import builtins
import collections
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.service.context import CancelToken, ExecutionContext
from core.service.errors import ApiError, ErrorCode
from core.service.events import Emitter, Event, EventSink, EventType, JobState
from core.service.models import Model, api_payload, field
from core.service.operations import Catalog, OperationSpec, authorize, error_state, execute, result_state

#: Jobs en attente au-delà desquels une soumission est refusée (``busy``).
QUEUE_LIMIT = 256
#: Jobs terminés conservés pour ``get`` / ``list``.
HISTORY_LIMIT = 100

_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.PARTIAL}
_TERMINAL_VALUES = {state.value for state in _TERMINAL}

#: Profondeur des rappels en cours dans ce fil (livraison à une destination, rappels d'annulation) : un arrêt
#: demandé depuis l'un d'eux ne peut pas attendre sa propre fin.
_CALLBACK = threading.local()


@contextmanager
def _in_callback() -> Iterator[None]:
    _CALLBACK.depth = getattr(_CALLBACK, "depth", 0) + 1
    try:
        yield
    finally:
        _CALLBACK.depth -= 1


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

    def __init__(self, spec: OperationSpec, request: Model, base_dir: Path | None, sink: EventSink,
                 on_published: Callable[[_Job], None]) -> None:
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
        #: État final livré à la destination : le job peut quitter l'historique (jamais avant).
        self.published = False
        self.thread: threading.Thread | None = None
        self.emitter = Emitter(_ProgressTap(self, sink, on_published))

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
    """Destination du job : retient sa dernière progression, transmet à la destination du gestionnaire et
    signale la livraison effective de l'état final (quel que soit le fil qui livre)."""

    def __init__(self, job: _Job, sink: EventSink, on_published: Callable[[_Job], None]) -> None:
        self._job = job
        self._sink = sink
        self._on_published = on_published

    def emit(self, event: Event) -> None:
        own = event.job == self._job.id
        if event.type is EventType.PROGRESS and own:
            self._job.progress = dict(event.data)  # affectation atomique : lue par get() / list()
        try:
            with _in_callback():
                self._sink.emit(event)
        finally:  # destination en échec comprise : rien d'autre ne sera livré pour ce job
            if own and event.type is EventType.STATE and event.data.get("state") in _TERMINAL_VALUES:
                self._on_published(self._job)


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
        #: Changement d'état attendu par ``shutdown`` (annonce terminée, état final livré, fil terminé).
        self._changed = threading.Condition(self._lock)
        self._jobs: dict[str, _Job] = {}
        self._pending: collections.deque[_Job] = collections.deque()
        self._running: dict[str, int] = {"job": 0, "analysis": 0, "exclusive": 0}
        self._finished: collections.deque[str] = collections.deque()
        #: Fils des jobs démarrés, retirés à leur toute fin (événements publiés, file éventuelle annulée).
        self._threads: set[threading.Thread] = set()
        self._closed = False
        self._draining = False
        #: Annulations dont les rappels (qui peuvent émettre) sont en cours.
        self._cancelling = 0
        self._stopper: threading.Thread | None = None
        self._stop_failure: BaseException | None = None

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
        if base_dir is not None and ("\0" in str(base_dir) or not Path(base_dir).is_absolute()):
            raise ApiError(ErrorCode.INVALID_REQUEST, "base_dir : chemin absolu attendu (sans caractère nul).")
        request, _warnings = spec.request.parse(api_payload(payload), strict=True, base_dir=base_dir)
        job = _Job(spec, request, base_dir, self._sink, self._published)
        with self._lock:  # place réservée dès l'admission : file bornée et ordre FIFO tenus pendant l'annonce
            if self._closed or self._draining:
                raise ApiError(ErrorCode.BUSY, "Gestionnaire en cours d'arrêt : aucune nouvelle soumission.")
            if len(self._pending) >= QUEUE_LIMIT:
                raise ApiError(ErrorCode.BUSY, f"File pleine ({QUEUE_LIMIT} jobs en attente).")
            self._jobs[job.id] = job
            self._pending.append(job)
        try:
            if on_admitted is not None:
                with _in_callback():  # un arrêt demandé ici ne s'attend pas lui-même
                    on_admitted(job.id)
        except BaseException:
            with self._lock:  # identifiant jamais annoncé : le job n'a pas existé
                self._jobs.pop(job.id, None)
                self._pending.remove(job)
                self._schedule()  # il retenait peut-être les jobs suivants
                self._changed.notify_all()
            raise
        # Identifiant communiqué : le job existe ; toute fin de l'annonce le retire de la file ou l'y laisse annoncé.
        failure: ApiError | None = None
        try:
            job.emitter.state(JobState.QUEUED, operation=spec.name)
        except BaseException as exc:  # noqa: BLE001 - destination en échec : job enregistré en échec
            failure = ApiError.from_exception(exc)
        with self._lock:
            job.announced = True
            refused = failure is not None or job.cancel.cancelled or self._closed or self._draining
            if refused:  # échec ou annulation pendant l'annonce, arrêt / drainage (qui laissent les annonces)
                self._pending.remove(job)
                self._finish_locked(job, failure or ApiError(ErrorCode.CANCELLED, "Job annulé avant son démarrage."))
            self._schedule()
            self._changed.notify_all()
        if refused:
            try:
                self._publish_terminal([job])
            except BaseException:  # noqa: BLE001 - identifiant déjà communiqué : la fin reste consultable (get)
                pass
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
            else:
                self._cancelling += 1  # réservé dès la sélection : l'arrêt attend ces rappels et leurs émissions
        if queued:
            self._publish_terminal([target])
            return True
        try:
            with _in_callback():
                target.cancel.cancel()  # en cours, ou en cours d'annonce (submit() le retire) : hors du verrou
        finally:
            with self._lock:
                self._cancelling -= 1
                self._changed.notify_all()
        return True

    def shutdown(self, timeout: float | None = None) -> bool:
        """Refuse les soumissions ; un coordinateur annule la file et les jobs en cours, puis attend la fin de
        chaque ``execute()``, des annonces et annulations en cours et la livraison de chaque état final.

        Attend le coordinateur au plus ``timeout`` s (destination ou rappel bloqué : il poursuit seul) et renvoie
        vrai s'il a fini ; une destination ou un rappel en échec n'interrompt rien (première erreur relancée).
        Depuis un fil dont l'arrêt dépend (job, destination, rappel d'annulation, coordinateur) : arrêt déclenché
        sans attente (``False`` tant qu'il n'est pas fini).
        """
        with self._lock:
            self._closed = True
            if self._stopper is None:
                self._stopper = threading.Thread(target=self._stop, name="job-manager-shutdown", daemon=True)
                self._stopper.start()
            stopper = self._stopper
            current = threading.current_thread()
            reentrant = (getattr(_CALLBACK, "depth", 0) > 0 or current in self._threads or current is stopper)
        stopper.join(0 if reentrant else timeout)
        if stopper.is_alive():
            return False
        if self._stop_failure is not None:
            raise self._stop_failure
        return True

    def _stop(self) -> None:
        with self._lock:
            dropped = self._drop_pending_locked("Arrêt du gestionnaire.")
            running = [job for job in self._jobs.values() if job.state is JobState.RUNNING]
            threads = list(self._threads)  # aucun fil ne démarre après la fermeture
        failure: BaseException | None = None
        try:
            self._publish_terminal(dropped)
        except BaseException as exc:  # noqa: BLE001 - relancée après le nettoyage
            failure = exc
        for job in running:
            try:
                job.cancel.cancel()  # rappels synchrones, qui peuvent émettre
            except BaseException as exc:  # noqa: BLE001 - relancée après le nettoyage
                failure = failure or exc
        with self._changed:
            self._changed.wait_for(self._settled_locked)
        for thread in threads:  # retirés du suivi juste avant leur fin : attendus jusqu'au bout
            thread.join()
        self._stop_failure = failure

    def _settled_locked(self) -> bool:
        """Plus rien en file, en cours ni en annulation, chaque fin livrée (annonces interrompues comprises)."""
        return not self._pending and not self._threads and not self._cancelling and all(
            job.published for job in self._jobs.values() if job.finished_at is not None)

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
        held: set[str] = set()  # classes dont un job plus ancien attend (FIFO par classe)
        for job in list(self._pending):
            queue = job.spec.queue
            if queue == "exclusive":
                if job.announced and not any(self._running.values()):
                    self._start_locked(job)
                break  # rien de soumis après elle ne démarre avant elle, même pendant son annonce
            if queue in held:
                continue
            if job.announced and self._running[queue] < self._limits[queue]:
                self._start_locked(job)
            else:
                held.add(queue)

    def _start_locked(self, job: _Job) -> None:
        self._pending.remove(job)
        self._running[job.spec.queue] += 1
        job.state = JobState.RUNNING
        job.started_at = _now()
        job.thread = threading.Thread(target=self._run, args=(job, self._config), name=f"job-{job.spec.name}",
                                      daemon=True)
        self._threads.add(job.thread)
        job.thread.start()

    def _finish_locked(self, job: _Job, error: ApiError | None = None, result: Model | None = None) -> None:
        """Fin enregistrée (avant ses événements terminaux) ; le job reste connu jusqu'à leur publication."""
        job.result = result
        job.error = error
        job.state = result_state(result) if result is not None else error_state(error) if error else job.state
        job.finished_at = _now()
        self._finished.append(job.id)

    def _drop_pending_locked(self, reason: str) -> Sequence[_Job]:
        """Retire de la file les jobs annoncés (les autres finissent leur annonce puis sont refusés)."""
        dropped = [job for job in self._pending if job.announced]
        for job in dropped:
            self._pending.remove(job)
            self._finish_locked(job, ApiError(ErrorCode.CANCELLED, reason))
        return dropped

    def _published(self, job: _Job) -> None:
        """État final du job livré à la destination : il peut quitter l'historique."""
        with self._lock:
            job.published = True
            self._trim_locked()
            self._changed.notify_all()

    def _trim_locked(self) -> None:
        """Historique borné : les plus anciens jobs terminés dont les événements sont publiés le quittent."""
        excess = len(self._finished) - HISTORY_LIMIT
        if excess <= 0:
            return
        kept: collections.deque[str] = collections.deque()
        for job_id in self._finished:
            job = self._jobs.get(job_id)
            if excess > 0 and (job is None or job.published):
                self._jobs.pop(job_id, None)
                excess -= 1
            else:
                kept.append(job_id)
        self._finished = kept

    # -- Exécution --------------------------------------------------------------------------------------------

    def _publish_terminal(self, jobs: Sequence[_Job]) -> None:
        """Événements terminaux (erreur puis état) de jobs terminés sans exécution, hors verrou ; une destination
        en échec n'empêche pas la publication des autres (première erreur relancée)."""
        failure: BaseException | None = None
        for job in jobs:
            assert job.error is not None
            try:
                try:
                    job.emitter.error(job.error.to_dict())
                finally:  # état final toujours tenté : il libère l'historique et l'arrêt
                    job.emitter.state(error_state(job.error), operation=job.spec.name)
            except BaseException as exc:  # noqa: BLE001 - relancée après les autres
                failure = failure or exc
        if failure is not None:
            raise failure

    def _run(self, job: _Job, config: Any) -> None:
        try:
            self._run_job(job, config)
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())
                self._changed.notify_all()

    def _run_job(self, job: _Job, config: Any) -> None:
        dropped: builtins.list[_Job] = []

        def record(result: Model | None, error: ApiError | None) -> None:
            # Avant les événements terminaux : une destination qui consulte le job le voit terminé.
            with self._lock:
                self._finish_locked(job, error, result)
                if result is not None and getattr(result, "restart_required", False):
                    self._draining = True  # plus d'admission ; la file est annulée
                    dropped.extend(self._drop_pending_locked("Mise à jour installée : arrêt."))

        try:
            ctx = ExecutionContext.create(config, self._sink, cancel=job.cancel, base_dir=job.base_dir,
                                          allow_privileged=self._allow_privileged, emitter=job.emitter)
            execute(job.spec, job.request, ctx, before_terminal=record)
        except BaseException as exc:  # noqa: BLE001 - toute fin de job est enregistrée
            if job.finished_at is None:  # échec avant l'exécution : fin enregistrée et publiée ici
                record(None, ApiError.from_exception(exc))
                try:
                    self._publish_terminal([job])
                except BaseException:  # noqa: BLE001 - destination en échec : la fin reste enregistrée
                    pass
        renewed = None
        if job.spec.queue == "exclusive":  # exclusivité encore tenue : rien d'autre ne tourne pendant la relecture
            try:
                renewed = self._config_factory()
            except Exception:  # noqa: BLE001 - configuration illisible : la précédente reste en vigueur
                renewed = None
        with self._lock:
            if renewed is not None:
                self._config = renewed
            self._running[job.spec.queue] -= 1
            self._schedule()
        try:
            self._publish_terminal(dropped)
        except BaseException:  # noqa: BLE001 - destination en échec : les jobs restent enregistrés annulés
            pass


__all__ = ["HISTORY_LIMIT", "QUEUE_LIMIT", "JobInfo", "JobManager", "JobSummary"]
