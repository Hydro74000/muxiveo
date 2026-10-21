"""Cache de préparation : inspections et réponses TMDB réutilisées, jamais périmées."""
from __future__ import annotations

import json
import os

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
