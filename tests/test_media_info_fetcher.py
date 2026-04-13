from __future__ import annotations

from pathlib import Path

from core.media_info_fetcher import (
    MediaDetails,
    MediaSearchResult,
    TmdbError,
    TmdbFetcher,
    clean_filename_for_search,
    clean_text_for_search,
    extract_year_from_filename,
    extract_year_from_text,
    normalize_tmdb_search_query,
)


def test_clean_text_for_search_strips_year_and_episode_suffixes_from_title():
    assert clean_text_for_search("Daredevil: Born Again - S02E01 - The Northern Star") == (
        "Daredevil: Born Again"
    )
    assert clean_text_for_search("The.Last.of.Us.01x02.2023.1080p.WEB-DL") == "The Last of Us"


def test_normalize_tmdb_search_query_extracts_clean_query_and_year():
    assert normalize_tmdb_search_query("Inception (2010)") == ("Inception", "2010")
    assert normalize_tmdb_search_query("The Last of Us - S01E01 - When You're Lost in the Darkness (2023)") == (
        "The Last of Us",
        "2023",
    )


def test_filename_helpers_delegate_to_text_cleaning():
    path = Path("Daredevil.Born.Again.S02E01.Le.Northern.Star.2025.2160p.WEB-DL.mkv")

    assert clean_filename_for_search(path) == "Daredevil Born Again"
    assert extract_year_from_filename(path) == "2025"
    assert extract_year_from_text("Daredevil: Born Again (2025)") == "2025"


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


def test_get_details_tv_uses_episode_translations_when_detail_is_empty(monkeypatch):
    fetcher = TmdbFetcher(api_key="dummy", language="fr-FR")

    def fake_get(endpoint: str, extra: dict | None = None) -> dict:
        if endpoint == "/tv/102":
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
        if endpoint == "/tv/102/season/1/episode/4":
            if extra and extra.get("language") == "en-US":
                return {"overview": "English episode overview", "name": "English Episode Title"}
            return {"overview": "", "name": ""}
        if endpoint == "/tv/102/season/1/episode/4/translations":
            return {
                "translations": [
                    {
                        "iso_639_1": "fr",
                        "iso_3166_1": "FR",
                        "data": {
                            "name": "Titre d'episode FR",
                            "overview": "Synopsis episode FR",
                        },
                    }
                ]
            }
        raise AssertionError(f"Unexpected endpoint: {endpoint} extra={extra}")

    monkeypatch.setattr(fetcher, "_get", fake_get)

    result = MediaSearchResult(tmdb_id=102, title="My Show", year="2020", kind="tv")
    details = fetcher.get_details(result, season="1", episode="4")

    assert details.synopsis == "Synopsis episode FR"
    assert details.episode_title == "Titre d'episode FR"
    assert details.formatted_container_title() == "My Show - S01E04 - Titre d'episode FR"


def test_get_details_tv_falls_back_to_english_episode_metadata_when_locale_is_empty(monkeypatch):
    fetcher = TmdbFetcher(api_key="dummy", language="fr-FR")

    def fake_get(endpoint: str, extra: dict | None = None) -> dict:
        if endpoint == "/tv/102":
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
        if endpoint == "/tv/102/season/1/episode/4":
            if extra and extra.get("language") == "en-US":
                return {"overview": "English episode overview", "name": "English Episode Title"}
            return {"overview": "", "name": ""}
        if endpoint == "/tv/102/season/1/episode/4/translations":
            return {"translations": []}
        raise AssertionError(f"Unexpected endpoint: {endpoint} extra={extra}")

    monkeypatch.setattr(fetcher, "_get", fake_get)

    result = MediaSearchResult(tmdb_id=102, title="My Show", year="2020", kind="tv")
    details = fetcher.get_details(result, season="1", episode="4")

    assert details.synopsis == "English episode overview"
    assert details.episode_title == "English Episode Title"
    assert details.formatted_container_title() == "My Show - S01E04 - English Episode Title"


def test_get_details_fetches_cover_from_tmdb(monkeypatch):
    """
    Depuis le téléchargement différé, get_details() ne télécharge plus la cover :
    il construit uniquement l'URL (cover_url) et le nom de fichier (cover_filename).
    cover_bytes reste vide — le téléchargement réel est effectué au lancement du workflow.
    """
    fetcher = TmdbFetcher(api_key="dummy")

    def fake_get(endpoint: str, extra: dict | None = None) -> dict:
        if endpoint == "/movie/300":
            return {
                "title": "Poster Film",
                "release_date": "2024-06-01",
                "genres": [],
                "credits": {"cast": [], "crew": []},
                "overview": "Movie overview",
                "external_ids": {},
                "poster_path": "/poster.jpg",
            }
        raise AssertionError(f"Unexpected endpoint: {endpoint} extra={extra}")

    binary_called = []

    monkeypatch.setattr(fetcher, "_get", fake_get)
    monkeypatch.setattr(fetcher, "_build_image_url", lambda *args, **kwargs: "https://img.test/poster.jpg")
    monkeypatch.setattr(fetcher, "_get_binary", lambda url: binary_called.append(url) or (b"jpeg-bytes", "image/jpeg"))

    result = MediaSearchResult(tmdb_id=300, title="Poster Film", year="2024", kind="movie")
    details = fetcher.get_details(result)

    # L'URL est construite mais le binaire n'est pas téléchargé immédiatement.
    assert details.cover_url == "https://img.test/poster.jpg"
    assert details.cover_filename == "cover.jpg"
    assert details.cover_bytes == b""   # téléchargement différé
    assert binary_called == []          # _get_binary ne doit pas être appelé


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
