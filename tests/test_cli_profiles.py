"""Profils enregistrés (profile.list/show/save/delete/capture), TMDB (tmdb.search/details) et titres Blu-ray."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.parser import build_parser
from core.jobs.errors import JobError
from core.media_info_fetcher import MediaDetails, MediaSearchResult
from core.service import ApiError, ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.ops import library
from tests.test_bluray import _make_bluray_tree, _write_playlist


def _ctx(tmp_path: Path, **ports: Any) -> ExecutionContext:
    config = SimpleNamespace(profiles_dir=tmp_path / "profiles", tmdb_api_key="", tmdb_bearer_token="")
    return ExecutionContext.create(config, ListSink(), ports=ports)


def _profile(name: str = "Films") -> dict[str, Any]:
    return {"name": name, "description": "VF puis VO", "rules": []}


def test_profile_store_lifecycle(tmp_path):
    saved: Any = call(CATALOG, "profile.save", {"profile": _profile()}, _ctx(tmp_path))
    assert Path(saved.path).is_file() and saved.profile["kind"] == "decision-profile"
    with pytest.raises(JobError) as caught:
        call(CATALOG, "profile.save", {"profile": _profile()}, _ctx(tmp_path))
    assert caught.value.exit_code == 5
    call(CATALOG, "profile.save", {"profile": _profile(), "force": True}, _ctx(tmp_path))
    listed: Any = call(CATALOG, "profile.list", {}, _ctx(tmp_path))
    assert [p["name"] for p in listed.profiles] == ["Films"] and listed.profiles[0]["rules"] == 0
    shown: Any = call(CATALOG, "profile.show", {"name": "Films"}, _ctx(tmp_path))
    assert shown.profile["description"] == "VF puis VO"
    call(CATALOG, "profile.delete", {"name": "Films"}, _ctx(tmp_path))
    with pytest.raises(JobError):
        call(CATALOG, "profile.show", {"name": "Films"}, _ctx(tmp_path))
    historical = ExecutionContext.create(_ctx(tmp_path).config, ListSink(), api=False)
    with pytest.raises(JobError, match="invalide") as caught:  # commande historique : refus à l'exécution
        call(CATALOG, "profile.save", {"profile": {"name": "", "rules": "x"}}, historical, allow_relative=True,
             strict_documents=False)
    assert caught.value.exit_code == 3
    with pytest.raises(ApiError) as refused:  # requête de l'API : refus dès la lecture, champs désignés
        call(CATALOG, "profile.save", {"profile": {"name": "", "rules": "x"}}, _ctx(tmp_path))
    assert {detail.path for detail in refused.value.details} == {"profile.name", "profile.rules"}


def test_profile_capture_uses_a_placeholder_output(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}

    def build(request, ctx):
        seen["job"] = request.job
        return SimpleNamespace(sources=[]), None, None

    monkeypatch.setattr(library, "_build_cached", build)
    monkeypatch.setattr("core.profiles.decision.remux_config_to_decision_profile",
                        lambda config, name="", **flags: {"name": name, "rules": [], "flags": flags})
    result: Any = call(CATALOG, "profile.capture", {"job": {"sources": ["/a.mkv"]}, "name": "Capturé",
                                                    "include_order": False, "save": True}, _ctx(tmp_path))
    assert seen["job"]["_allow_missing_output_dir"] and Path(result.path).is_file()
    assert result.profile["flags"]["include_order"] is False
    with pytest.raises(JobError, match="name"):
        call(CATALOG, "profile.capture", {"job": {"sources": ["/a.mkv"]}, "save": True}, _ctx(tmp_path))


class _Fetcher:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def search(self, query, *, kind, year):
        return [MediaSearchResult(27205, query, year or "2010", "movie", "Rêves")]

    def get_details(self, result, *, season, episode):
        return MediaDetails(title="Inception", year="2010", genre="Science-Fiction", synopsis="Rêves",
                            cover_bytes=b"\x00", season=season, episode=episode)


def test_tmdb_search_and_details_with_a_fake_fetcher(tmp_path):
    ctx = _ctx(tmp_path, tmdb_fetcher=_Fetcher)
    found: Any = call(CATALOG, "tmdb.search", {"query": "Inception", "year": "2010"}, ctx)
    assert found.results == ({"tmdb_id": 27205, "title": "Inception", "year": "2010", "kind": "movie",
                              "overview": "Rêves"},)
    details: Any = call(CATALOG, "tmdb.details", {"tmdb_id": 27205}, _ctx(tmp_path, tmdb_fetcher=_Fetcher))
    assert details.container_title == "Inception (2010)" and "cover_bytes" not in details.details
    assert details.mkv_tags["GENRE"] == "Science-Fiction"


def test_bluray_titles_lists_playlists(tmp_path):
    root, _playlist = _make_bluray_tree(tmp_path, playlist_id=800)
    _write_playlist(root, playlist_id=1, items=[("00001", 2.0)])
    result: Any = call(CATALOG, "media.bluray_titles", {"path": str(root / "BDMV")}, _ctx(tmp_path))
    assert result.disc_root == str(root)
    assert [title["playlist"] for title in result.titles] == [800, 1]  # du plus long au plus court
    assert result.titles[0]["segments"] == ["00001", "00002"] and result.titles[0]["duration_s"] == 15.5
    long_only: Any = call(CATALOG, "media.bluray_titles", {"path": str(root), "min_duration_s": 10}, _ctx(tmp_path))
    assert [title["playlist"] for title in long_only.titles] == [800]
    with pytest.raises(JobError):
        call(CATALOG, "media.bluray_titles", {"path": str(tmp_path / "rien")}, _ctx(tmp_path))


def test_cli_parsers():
    parser = build_parser()
    args = parser.parse_args(["profile", "capture", "-i", "a.mkv", "--name", "P", "--no-order", "--save"])
    assert args.profile_name == "P" and args.no_order and args.save
    assert parser.parse_args(["profile", "save", "p.json", "--force"]).func.__name__ == "cmd_profile_store"
    assert parser.parse_args(["tmdb", "details", "1399", "--kind", "tv", "--season", "1"]).season == 1
    assert parser.parse_args(["bluray-titles", "/disque", "--min-duration", "600"]).min_duration == 600
    assert isinstance(parser.parse_args(["tmdb", "search", "Dune"]), argparse.Namespace)


def test_cli_profile_save_from_file(tmp_path, monkeypatch, capsys):
    from cli.library import cmd_profile_store
    from cli.logging import Logger

    document = tmp_path / "p.json"
    document.write_text(json.dumps(_profile("Séries")), encoding="utf-8")
    args = build_parser().parse_args(["profile", "save", str(document), "--name", "Renommé", "--json"])
    config = SimpleNamespace(profiles_dir=tmp_path / "profiles", tmdb_api_key="", tmdb_bearer_token="")
    assert cmd_profile_store(args, config, Logger(fmt="text")) == 0  # type: ignore[arg-type]
    assert json.loads(capsys.readouterr().out)["profile"]["name"] == "Renommé"
