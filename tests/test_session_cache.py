"""Chargements de cache concurrents : isolation, erreurs, annulation et rétention."""
import threading
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor

import pytest

from core.session_cache import SessionCache


def _observe_waiters(monkeypatch, count):
    joined, enough = set(), threading.Event()
    lock = threading.Lock()
    original = Future.result

    def result(future, *args, **kwargs):
        if threading.current_thread().name.startswith("cache-test"):
            with lock:
                joined.add(threading.get_ident())
                if len(joined) >= count:
                    enough.set()
        return original(future, *args, **kwargs)

    monkeypatch.setattr(Future, "result", result)
    return enough


@pytest.mark.parametrize("fail", [False, True])
def test_identical_loads_share_work_but_not_mutable_results(monkeypatch, fail):
    cache = SessionCache(2)
    release, entered = threading.Event(), threading.Event()
    waiting = _observe_waiters(monkeypatch, 3)
    calls = []

    def load():
        calls.append(True)
        entered.set()
        assert release.wait(5)
        if fail:
            raise ValueError("Lecture impossible")
        return {"raw": {"value": 1}}

    with ThreadPoolExecutor(4, thread_name_prefix="cache-test") as executor:
        first = executor.submit(cache.get_or_load, "key", load)
        try:
            assert entered.wait(5)
            followers = [executor.submit(cache.get_or_load, "key", load) for _ in range(3)]
            assert waiting.wait(5)
        finally:
            release.set()
        if fail:
            for future in [first, *followers]:
                with pytest.raises(ValueError, match="Lecture impossible"):
                    future.result(timeout=5)
            assert cache.get_or_load("key", lambda: {"retry": True}) == {"retry": True}
        else:
            values = [future.result(timeout=5) for future in [first, *followers]]
            values[0]["raw"]["value"] = 9
            assert all(value["raw"]["value"] == 1 for value in values[1:])
            cached = cache.get("key")
            assert cached == {"raw": {"value": 1}}
            cached["raw"]["value"] = 7
            assert cache.get("key") == {"raw": {"value": 1}}
        assert len(calls) == 1


def test_different_keys_load_in_parallel():
    cache = SessionCache(2)
    both = threading.Barrier(2)

    def load(value):
        both.wait(timeout=5)
        return value

    with ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(cache.get_or_load, key, lambda key=key: load(key)) for key in (1, 2)]
        assert [future.result(timeout=5) for future in futures] == [1, 2]


def test_cancelling_a_waiter_keeps_the_shared_load_alive(monkeypatch):
    cache = SessionCache(2)
    entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
    waiting = _observe_waiters(monkeypatch, 1)

    def load():
        entered.set()
        assert release.wait(5)
        return {"value": 1}

    with ThreadPoolExecutor(2, thread_name_prefix="cache-test") as executor:
        leader = executor.submit(cache.get_or_load, "key", load)
        try:
            assert entered.wait(5)
            follower = executor.submit(cache.get_or_load, "key", load, cancel_event=cancel)
            assert waiting.wait(5)
            cancel.set()
            with pytest.raises(CancelledError):
                follower.result(timeout=1)
            assert not leader.done()
        finally:
            release.set()
        assert leader.result(timeout=5) == {"value": 1}
    assert cache.get("key") == {"value": 1}


def test_retain_does_not_reintroduce_a_pruned_inflight_result():
    cache = SessionCache(2)
    entered, release = threading.Event(), threading.Event()

    def load():
        entered.set()
        assert release.wait(5)
        return {"value": 1}

    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(cache.get_or_load, "old-season", load)
        try:
            assert entered.wait(5)
            cache.retain(lambda _key: False)
        finally:
            release.set()
        assert future.result(timeout=5) == {"value": 1}
    assert cache.get("old-season") is None
