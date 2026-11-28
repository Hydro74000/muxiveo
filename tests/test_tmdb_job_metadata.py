"""Métadonnées TMDB d'un job (``resolve_tmdb_metadata``) : authentification, identifiant ou recherche, jaquette.

Couverture résiduelle de l'audit 4effdd1 : chemin exécuté sans réseau, fournisseur TMDB simulé.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.jobs import remux_config as module
from core.jobs.errors import JobError


class _Details:
    cover_url = "https://image.tmdb.org/t/p/original/x.jpg"
    cover_filename = "cover.jpg"

    def formatted_container_title(self) -> str:
        return "Le Film (2024)"

    def to_mkv_tags(self) -> dict[str, str]:
        return {"TITLE": "Le Film", "GENRE": "Animation"}


class _Fetcher:
    instances: list["_Fetcher"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.searches: list[tuple] = []
        self.details: list[tuple] = []
        _Fetcher.instances.append(self)

    def search(self, query: str, *, kind: str, year: str):
        self.searches.append((query, kind, year))
        return [] if query == "introuvable" else [SimpleNamespace(title="Le Film", year="2024", tmdb_id=42)]

    def get_details(self, result: Any, *, season: str, episode: str):
        self.details.append((result, season, episode))
        return _Details()


@pytest.fixture
def fetcher(monkeypatch):
    _Fetcher.instances.clear()
    monkeypatch.setattr(module, "TmdbFetcher", _Fetcher)
    return _Fetcher


def _logger() -> Any:
    messages: list[tuple[str, str]] = []
    return SimpleNamespace(emit=lambda level, message, **_k: messages.append((level, message)), messages=messages)


def _config(**values: str) -> Any:
    return SimpleNamespace(tmdb_api_key=values.get("api_key", ""), tmdb_bearer_token=values.get("bearer", ""))


def test_no_tmdb_block_returns_nothing(tmp_path, fetcher):
    assert module.resolve_tmdb_metadata({}, _config(), tmp_path / "f.mkv", _logger()) == ("", None, None, None)
    assert fetcher.instances == []


def test_missing_authentication_is_refused(tmp_path, fetcher, monkeypatch):
    monkeypatch.setattr(module, "default_tmdb_bearer_token", lambda: "")
    with pytest.raises(JobError) as caught:
        module.resolve_tmdb_metadata({"tmdb": True}, _config(), tmp_path / "f.mkv", _logger())
    assert caught.value.exit_code == 3 and "TMDB" in caught.value.message


def test_search_uses_first_result_and_returns_cover(tmp_path, fetcher):
    logger = _logger()
    title, tags, cover, details = module.resolve_tmdb_metadata(
        {"tmdb": {"query": "Le Film", "year": "2024", "language": "en-US"}}, _config(api_key="clé"),
        tmp_path / "Le.Film.2024.mkv", logger)
    assert title == "Le Film (2024)" and tags == {"TITLE": "Le Film", "GENRE": "Animation"}
    assert cover == (_Details.cover_url, _Details.cover_filename) and details is not None
    used = fetcher.instances[0]
    assert used.kwargs["api_key"] == "clé" and used.kwargs["language"] == "en-US"
    assert used.searches == [("Le Film", "all", "2024")]
    assert any("premier résultat" in message for _level, message in logger.messages)


def test_search_without_result_is_refused(tmp_path, fetcher):
    with pytest.raises(JobError) as caught:
        module.resolve_tmdb_metadata({"tmdb": {"query": "introuvable"}}, _config(bearer="jeton"),
                                     tmp_path / "f.mkv", _logger())
    assert "introuvable" in caught.value.message


@pytest.mark.parametrize("season, episode, kind", [("", "", "movie"), ("1", "2", "tv")])
def test_explicit_id_skips_search_and_infers_kind(tmp_path, fetcher, season, episode, kind):
    tmdb = {"id": 1234, "title": "Série", "cover": False, **({"season": season, "episode": episode} if season else {})}
    _title, _tags, cover, _details = module.resolve_tmdb_metadata({"tmdb": tmdb}, _config(api_key="k"),
                                                                  Path(tmp_path / "f.mkv"), _logger())
    used = fetcher.instances[0]
    assert used.searches == [] and cover is None  # jaquette désactivée
    result, used_season, used_episode = used.details[0]
    assert (result.tmdb_id, result.kind, result.title) == (1234, kind, "Série")
    assert (used_season, used_episode) == (season, episode)
