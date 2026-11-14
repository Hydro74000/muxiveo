"""Hybridation sur le moteur du Studio : recette (JSON), sources à rôles, paires manuelles, requête du CLI."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.hybrid import hybrid_payload
from cli.parser import build_parser
from core.jobs.errors import JobError
from core.jobs.hybrid_studio import (
    StudioSpec,
    load_recipe,
    parse_pairs,
    parse_sources,
    plan_episodes,
    recipe_with_overrides,
)
from core.service.ops.hybrid import HybridPlanRequest
from core.workflows.hybrid_matrix import HybridRecipe, HybridTrackChoice, SourceRole


def _choice(**fields) -> HybridTrackChoice:
    base = dict(source_id="src_2", track_type="audio", language="fr", codec="eac3", title="", forced=False,
                hearing_impaired=False, commentary=False, visual_impaired=False, original=False, audio_channels="6",
                audio_object="")
    return HybridTrackChoice(**{**base, **fields})


def test_recipe_round_trip_and_validation(tmp_path):
    recipe = HybridRecipe(track_choices=(_choice(), _choice(track_type="subtitle", codec="subrip", occurrence=1)),
                          track_overrides={_choice(): {"title": "VFF"}}, sync_strategy="manual", manual_shift_ms=-120)
    path = tmp_path / "r.json"
    path.write_text(json.dumps(recipe.to_dict()), encoding="utf-8")
    assert load_recipe(path) == recipe
    for broken in ({"format": "autre"}, {"sync_mode": "x"}, {"donor_audio_langs": "fre"}, {"inconnu": 1},
                   {"track_overrides": [{"track": {}, "set": {"codec": "x"}}]}):
        with pytest.raises(ValueError):
            HybridRecipe.from_dict(broken)
    merged = recipe_with_overrides(recipe, donor_audio_langs=("eng",), sync_strategy=None, manual_shift_ms=50)
    assert merged.donor_audio_langs == ["eng"] and merged.sync_strategy == "manual" and merged.manual_shift_ms == 50
    assert merged.track_choices == recipe.track_choices
    with pytest.raises(JobError):
        recipe_with_overrides(recipe, sync_strategy="jamais")


def test_sources_and_pairs_parsing(tmp_path):
    assert parse_sources(["master=/a", "donor-sub=/b"]) == ((SourceRole.MASTER, Path("/a")),
                                                              (SourceRole.DONOR_SUBTITLE, Path("/b")))
    for bad in (["/a"], ["chef=/a"], ["master="]):
        with pytest.raises(JobError):
            parse_sources(bad)
    assert parse_pairs(["/m.mkv::/d1.mkv::/d2.mkv"]) == ((Path("/m.mkv"), (Path("/d1.mkv"), Path("/d2.mkv"))),)
    with pytest.raises(JobError):
        parse_pairs(["/m.mkv"])


def test_plan_episodes_rules(tmp_path):
    logger = SimpleNamespace(emit=lambda *a, **k: None)
    with pytest.raises(JobError, match="master"):
        plan_episodes(StudioSpec(sources=((SourceRole.DONOR, tmp_path),), output_dir=tmp_path), logger)
    master, donor = tmp_path / "Show.S01E03.mkv", tmp_path / "d.mkv"
    master.write_bytes(b"x")
    donor.write_bytes(b"x")
    with pytest.raises(JobError, match="exclut"):
        plan_episodes(StudioSpec(sources=((SourceRole.MASTER, master),), pairs=((master, (donor,)),),
                                 output_dir=tmp_path), logger)
    episodes = plan_episodes(StudioSpec(sources=(), pairs=((master, (donor,)),), output_dir=tmp_path), logger)
    assert (episodes[0].season, episodes[0].episode) == (1, 3)
    assert [source.source_id for source, _path in episodes[0].donor_files] == ["src_2"]
    with pytest.raises(JobError, match="introuvable"):
        plan_episodes(StudioSpec(sources=(), pairs=((master, (tmp_path / "absent.mkv",)),), output_dir=tmp_path),
                      logger)


def test_cli_payload_is_a_valid_request():
    args = build_parser().parse_args([
        "hybrid", "--source", "master=/uhd", "--source", "donor-audio=/vf", "--ref", "/r.mkv", "-o", "/out",
        "--matching", "fuzzy", "--keep-master-audio", "eng, jpn", "--donor-sub-langs", "fre", "--sync", "manual",
        "--shift-ms", "-80", "--recipe", "/r.json", "--plan", "--export-jobs", "/jobs", "--video-codec", "libx265"])
    payload = hybrid_payload(args)
    assert payload["sources"] == [{"role": "master", "path": "/uhd"}, {"role": "donor-audio", "path": "/vf"}]
    assert payload["keep_master_audio"] == ["eng", "jpn"] and payload["encode"]["video"]["default"]["codec"] == "libx265"
    request = HybridPlanRequest.parse(payload, allow_relative=True)[0]
    assert request.matching == "fuzzy" and request.export_jobs == Path("/jobs") and request.shift_ms == -80
