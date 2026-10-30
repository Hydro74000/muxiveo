"""Cache de session synchronisé : chargements identiques regroupés, résultats isolés."""
from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class SessionCache(Generic[K, V]):
    """LRU privé sous verrou ; aucun résultat mutable stocké n'est exposé à l'appelant.

    Le chargement et les copies ont lieu hors verrou : des clés différentes peuvent avancer
    ensemble. Un échec libère les demandeurs et autorise une nouvelle tentative.
    """

    def __init__(self, max_entries: int) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[K, V] = OrderedDict()
        self._loading: dict[K, Future[V]] = {}
        self._max_entries = max(0, max_entries)
        self._generation = 0

    def get(self, key: K) -> V | None:
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
        return copy.deepcopy(value)

    def _store(self, key: K, value: V) -> None:
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def put(self, key: K, value: V) -> None:
        snapshot = copy.deepcopy(value)
        with self._lock:
            self._store(key, snapshot)

    def retain(self, predicate: Callable[[K], bool]) -> None:
        """Écarte les entrées inutiles ; les chargements déjà engagés ne les réintroduisent pas."""
        with self._lock:
            self._generation += 1
            for key in list(self._entries):
                if not predicate(key):
                    del self._entries[key]

    def get_or_load(self, key: K, loader: Callable[[], V], *,
                    cache_if: Callable[[V], bool] | None = None,
                    cancel_event: threading.Event | None = None) -> V:
        """Une seule lecture par clé ; chaque demandeur reçoit un résultat indépendant."""
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("Préparation annulée.")
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
            pending = self._loading.get(key)
            leader = cached is None and pending is None
            if leader:
                pending = Future()
                self._loading[key] = pending
            generation = self._generation
        if cached is not None:
            return copy.deepcopy(cached)
        if pending is None:  # impossible : sans entrée, un chargement existe ou vient d'être créé
            raise RuntimeError("Chargement en cours introuvable.")
        if not leader:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError("Préparation annulée.")
                try:
                    return copy.deepcopy(pending.result(timeout=0.05))
                except FutureTimeoutError:
                    if pending.done():
                        return copy.deepcopy(pending.result())
        try:
            value = loader()
            snapshot = copy.deepcopy(value)
            keep = cache_if is None or cache_if(value)
            with self._lock:
                if keep and generation == self._generation:
                    self._store(key, snapshot)
            pending.set_result(snapshot)
            return value
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._loading.pop(key, None)
