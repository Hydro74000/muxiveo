"""Cache de préparation : inspections et réponses TMDB réutilisées, jamais périmées."""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from core.inspector import FileInfo, FileInspector
from core.media_info_fetcher import MediaSearchResult, TmdbFetcher
from core.workflows.preparation_cache import PreparationCache, StepTimings


class _Inspector(FileInspector):
    def __init__(self, ffprobe_bin="ffprobe"):
        super().__init__(ffprobe_bin)
        self.calls = 0

    def inspect(self, path):
        self.calls += 1
        return FileInfo(path, "matroska", 1, path.stat().st_size, None, title=f"lecture {self.calls}")


def test_inspection_is_reused_as_an_independent_copy_until_the_file_changes(tmp_path):
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"x")
    cache, inspector = PreparationCache(), _Inspector()
    first = cache.inspect(inspector, path)
    first.title = "modifié par l'appelant"
    again = cache.inspect(inspector, path)
    assert inspector.calls == 1 and again.title == "lecture 1" and again is not first
    os.utime(path, ns=(1, 1))
    assert cache.inspect(inspector, path).title == "lecture 2"


def test_replaced_file_of_same_size_and_date_is_inspected_again(tmp_path):
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"x")
    cache, inspector = PreparationCache(), _Inspector()
    cache.inspect(inspector, path)
    stat = path.stat()
    replacement = tmp_path / "nouveau.mkv"
    replacement.write_bytes(b"y")
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(replacement, path)
    assert cache.inspect(inspector, path).title == "lecture 2"


def test_inspection_depends_on_the_tools(tmp_path):
    path = tmp_path / "episode.mkv"
    path.write_bytes(b"x")
    cache = PreparationCache()
    for tool in ("ffprobe", "autre-ffprobe"):
        inspector = _Inspector(tool)
        cache.inspect(inspector, path)
        assert inspector.calls == 1


def test_tmdb_responses_are_shared_between_fetchers(monkeypatch):
    requests = []

    class _Response:
        headers: dict[str, str] = {}

        def __init__(self, url):
            self.url = url

        def read(self):
            return json.dumps({"results": [{"id": 7, "name": "Série", "media_type": "tv"}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def urlopen(_self, request, timeout):
        requests.append(request.full_url)
        return _Response(request.full_url)

    monkeypatch.setattr(TmdbFetcher, "_urlopen", urlopen)
    cache = PreparationCache()
    for _ in range(3):
        results = TmdbFetcher(api_key="k", response_cache=cache.tmdb_responses).search("Série", kind="tv")
        assert results == [MediaSearchResult(tmdb_id=7, title="Série", year="", kind="tv")]
        results[0].title = "modifié"
    assert len(requests) == 1
    TmdbFetcher(api_key="k").search("Série", kind="tv")
    assert len(requests) == 2


def test_step_timings_accumulate_per_step():
    timings = StepTimings()
    for _ in range(2):
        with timings.measure("inspection"):
            pass
    with timings.measure("TMDB"):
        pass
    assert list(timings.values) == ["inspection", "TMDB"]
    assert timings.summary().startswith("inspection 0.0 s · TMDB")


def test_retain_keeps_only_the_files_of_the_new_matrix(tmp_path):
    from core.workflows.preparation_cache import SubtitleTextSummary, file_identity

    kept, dropped = tmp_path / "kept.mkv", tmp_path / "dropped.mkv"
    kept.write_bytes(b"k")
    dropped.write_bytes(b"d")
    cache = PreparationCache()
    for path in (kept, dropped):
        cache.store_subtitle_summary(file_identity(path), 2, SubtitleTextSummary(3, ""))
    cache.retain([kept])
    assert cache.subtitle_summary(file_identity(kept), 2) is not None
    assert cache.subtitle_summary(file_identity(dropped), 2) is None


def test_tmdb_responses_keep_the_most_recent():
    from core.media_info_fetcher import TmdbResponseCache

    responses = TmdbResponseCache(max_entries=2)
    responses.put("a", {"id": 1})
    responses.put("b", {"id": 2})
    assert responses.get("a") == {"id": 1}          # « a » redevient la plus récente
    responses.put("c", {"id": 3})
    assert responses.get("b") is None and responses.get("a") and responses.get("c")


def test_simultaneous_inspections_are_shared_and_nested_json_is_isolated(tmp_path):
    path = tmp_path / "in.mkv"
    path.touch()
    entered, release = threading.Event(), threading.Event()
    cache = PreparationCache()

    class Inspector(_Inspector):
        def inspect(self, path):
            self.calls += 1
            entered.set()
            assert release.wait(5)
            return FileInfo(path, "mkv", 1, 0, None, ffprobe_json={"streams": [{"tags": {"language": "fra"}}]})

    inspector = Inspector()
    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(cache.inspect, inspector, path)
        try:
            assert entered.wait(5)
            second = executor.submit(cache.inspect, inspector, path)
        finally:
            release.set()
        left, right = first.result(timeout=5), second.result(timeout=5)
    left.ffprobe_json["streams"][0]["tags"]["language"] = "eng"
    assert right.ffprobe_json["streams"][0]["tags"]["language"] == "fra"
    assert inspector.calls == 1


def test_simultaneous_tmdb_requests_are_shared(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Response:
        headers = {}
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return b'{"results": [{"id": 7, "name": "Serie", "media_type": "tv"}]}'

    def urlopen(*_args, **_kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return Response()

    monkeypatch.setattr(TmdbFetcher, "_urlopen", urlopen)
    cache = PreparationCache()
    with ThreadPoolExecutor(2) as executor:
        fetchers = [TmdbFetcher(api_key="k", response_cache=cache.tmdb_responses) for _ in range(2)]
        first = executor.submit(fetchers[0].search, "Serie", kind="tv")
        try:
            assert entered.wait(5)
            second = executor.submit(fetchers[1].search, "Serie", kind="tv")
        finally:
            release.set()
        left, right = first.result(timeout=5), second.result(timeout=5)
    left[0].title = "Modifié"
    assert right[0].title == "Serie" and len(calls) == 1
