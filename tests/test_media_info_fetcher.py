from __future__ import annotations

from core.media_info_fetcher import MediaDetails, MediaSearchResult, TmdbError, TmdbFetcher


def test_get_details_tv_prefers_episode_overview_when_available(monkeypatch):
    fetcher = TmdbFetcher(api_key="dummy")

    def fake_get(endpoint: str, extra: dict | None = None) -> dict:
        if endpoint == "/tv/100":
            return {
                "name": "My Show",
                "first_air_date": "2020-01-01",
                "genres": [],
                "credits": {"cast": [], "crew": []},
                "created_by": [],
                "origin_country": ["US"],
                "overview": "Series overview",
                "external_ids": {},
            }
        if endpoint == "/tv/100/season/1/episode/2":
            return {"overview": "Episode overview", "name": "Episode Title"}
        raise AssertionError(f"Unexpected endpoint: {endpoint}")

    monkeypatch.setattr(fetcher, "_get", fake_get)

    result = MediaSearchResult(tmdb_id=100, title="My Show", year="2020", kind="tv")
    details = fetcher.get_details(result, season="1", episode="2")

    assert details.synopsis == "Episode overview"
    assert details.season == "1"
    assert details.episode == "2"
    assert details.episode_title == "Episode Title"
    assert details.formatted_container_title() == "My Show - S01E02 - Episode Title"
    assert details.to_mkv_tags()["SUBTITLE"] == "My Show - S01E02 - Episode Title"


def test_get_details_tv_falls_back_to_series_overview_when_episode_unavailable(monkeypatch):
    fetcher = TmdbFetcher(api_key="dummy")

    def fake_get(endpoint: str, extra: dict | None = None) -> dict:
        if endpoint == "/tv/101":
            return {
                "name": "My Show",
                "first_air_date": "2020-01-01",
                "genres": [],
                "credits": {"cast": [], "crew": []},
                "created_by": [],
                "origin_country": ["US"],
                "overview": "Series overview",
                "external_ids": {},
            }
        if endpoint == "/tv/101/season/1/episode/99":
            raise TmdbError("not found")
        raise AssertionError(f"Unexpected endpoint: {endpoint}")

    monkeypatch.setattr(fetcher, "_get", fake_get)

    result = MediaSearchResult(tmdb_id=101, title="My Show", year="2020", kind="tv")
    details = fetcher.get_details(result, season="1", episode="99")

    assert details.synopsis == "Series overview"


def test_get_details_movie_does_not_call_episode_endpoint(monkeypatch):
    fetcher = TmdbFetcher(api_key="dummy")
    calls: list[str] = []

    def fake_get(endpoint: str, extra: dict | None = None) -> dict:
        calls.append(endpoint)
        if endpoint == "/movie/200":
            return {
                "title": "My Movie",
                "release_date": "2022-06-01",
                "genres": [],
                "credits": {"cast": [], "crew": []},
                "overview": "Movie overview",
                "external_ids": {},
            }
        raise AssertionError(f"Unexpected endpoint: {endpoint}")

    monkeypatch.setattr(fetcher, "_get", fake_get)

    result = MediaSearchResult(tmdb_id=200, title="My Movie", year="2022", kind="movie")
    details = fetcher.get_details(result, season="1", episode="2")

    assert details.synopsis == "Movie overview"
    assert calls == ["/movie/200"]
    assert details.formatted_container_title() == "My Movie (2022)"
    assert details.to_mkv_tags()["SUBTITLE"] == "My Movie"


def test_media_details_subtitle_and_container_title_formats():
    movie = MediaDetails(title="Inception", year="2010")
    assert movie.formatted_container_title() == "Inception (2010)"
    assert movie.formatted_subtitle_tag() == "Inception"

    episode = MediaDetails(
        title="Daredevil: Born Again",
        season="2",
        episode="1",
        episode_title="The Northern Star",
    )
    assert episode.formatted_container_title() == (
        "Daredevil: Born Again - S02E01 - The Northern Star"
    )
    assert episode.formatted_subtitle_tag() == (
        "Daredevil: Born Again - S02E01 - The Northern Star"
    )
