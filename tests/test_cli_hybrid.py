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



@pytest.mark.parametrize("recipe_fade, option, expected", [(20, None, 20), (80, "20", 20), (None, None, 80),
                                                         (None, "30", 30), (20, "80", 80)])
def test_crossfade_of_the_recipe_reaches_the_preparation_and_the_saved_recipe(tmp_path, recipe_fade, option,
                                                                             expected):
    """Audit 29 : fondu de la recette utilisé sans option explicite ; option explicite prioritaire ; préparation,
    recette renvoyée et recette sauvegardée identiques."""
    from types import SimpleNamespace as NS

    from core.service import ExecutionContext, ListSink
    from core.service.ops.hybrid import _spec

    argv = ["hybrid", "--ref", str(tmp_path / "m.mkv"), "--donor", str(tmp_path / "d.mkv"), "-o", str(tmp_path),
            "--plan"]
    if recipe_fade is not None:
        path = tmp_path / "r.json"
        path.write_text(json.dumps(HybridRecipe(crossfade_ms=recipe_fade).to_dict()), encoding="utf-8")
        argv += ["--recipe", str(path)]
    if option is not None:
        argv += ["--crossfade-ms", option]
    request = HybridPlanRequest.parse(hybrid_payload(build_parser().parse_args(argv)), allow_relative=True)[0]
    spec = _spec(request, ExecutionContext.create(NS(), ListSink()))
    assert spec.recipe.crossfade_ms == spec.common.crossfade_ms == expected



@pytest.mark.parametrize("explicit", [False, True])
def test_explicit_sync_option_equal_to_the_default_overrides_the_recipe(tmp_path, explicit):
    """Audit 30 : ``--sync-mode physical`` saisi (valeur par défaut) l'emporte sur la recette ``container`` ; absent,
    la recette s'applique."""
    from types import SimpleNamespace as NS

    from core.service import ExecutionContext, ListSink
    from core.service.ops.hybrid import _spec

    path = tmp_path / "r.json"
    path.write_text(json.dumps(HybridRecipe(sync_mode="container").to_dict()), encoding="utf-8")
    argv = ["hybrid", "--ref", str(tmp_path / "m.mkv"), "--donor", str(tmp_path / "d.mkv"), "-o", str(tmp_path),
            "--recipe", str(path), "--plan", *(["--sync-mode", "physical"] if explicit else [])]
    request = HybridPlanRequest.parse(hybrid_payload(build_parser().parse_args(argv)), allow_relative=True)[0]
    spec = _spec(request, ExecutionContext.create(NS(), ListSink()))
    assert spec.recipe.sync_mode == ("physical" if explicit else "container")


@pytest.mark.parametrize("fade", [-10, 5000])
def test_out_of_range_crossfade_is_refused_from_a_recipe_or_an_option(tmp_path, fade):
    from types import SimpleNamespace as NS

    from core.service import ExecutionContext, ListSink
    from core.service.ops.hybrid import _spec

    with pytest.raises(ValueError, match="crossfade_ms"):
        HybridRecipe.from_dict({**HybridRecipe().to_dict(), "crossfade_ms": fade})
    argv = ["hybrid", "--ref", str(tmp_path / "m.mkv"), "--donor", str(tmp_path / "d.mkv"), "-o", str(tmp_path),
            "--plan", "--crossfade-ms", str(fade)]
    request = HybridPlanRequest.parse(hybrid_payload(build_parser().parse_args(argv)), allow_relative=True)[0]
    with pytest.raises(JobError, match="crossfade_ms|Seuils"):
        _spec(request, ExecutionContext.create(NS(), ListSink()))



@pytest.mark.parametrize("given, expected", [
    ([], (False, "asetrate")),
    (["--cadence-auto", "--cadence-method", "auto"], (True, "auto")),
    (["--cadence-method", "atempo"], (False, "atempo")),
])
def test_explicit_cadence_options_override_the_recipe(tmp_path, given, expected):
    """Audit 31 : options de cadence saisies (même égales au défaut) prioritaires sur la recette ; absentes, la
    recette s'applique."""
    from types import SimpleNamespace as NS

    from core.service import ExecutionContext, ListSink
    from core.service.ops.hybrid import _spec

    path = tmp_path / "r.json"
    path.write_text(json.dumps(HybridRecipe(cadence_auto_apply=False, cadence_audio_method="asetrate").to_dict()),
                    encoding="utf-8")
    argv = ["hybrid", "--ref", str(tmp_path / "m.mkv"), "--donor", str(tmp_path / "d.mkv"), "-o", str(tmp_path),
            "--recipe", str(path), "--plan", *given]
    request = HybridPlanRequest.parse(hybrid_payload(build_parser().parse_args(argv)), allow_relative=True)[0]
    spec = _spec(request, ExecutionContext.create(NS(), ListSink()))
    assert (spec.recipe.cadence_auto_apply, spec.recipe.cadence_audio_method) == expected


def test_explicit_cadence_options_in_an_api_request_override_the_recipe(tmp_path):
    from types import SimpleNamespace as NS

    from core.service import ExecutionContext, ListSink
    from core.service.ops.hybrid import _spec

    path = tmp_path / "r.json"
    path.write_text(json.dumps(HybridRecipe(cadence_auto_apply=False, cadence_audio_method="asetrate").to_dict()),
                    encoding="utf-8")
    request = HybridPlanRequest.from_dict({
        "ref": str(tmp_path / "m.mkv"), "donor": str(tmp_path / "d.mkv"), "output_dir": str(tmp_path),
        "recipe": str(path), "options": {"cadence_auto": True, "cadence_method": "auto"}})
    spec = _spec(request, ExecutionContext.create(NS(), ListSink()))
    assert (spec.recipe.cadence_auto_apply, spec.recipe.cadence_audio_method) == (True, "auto")


def test_absent_cadence_options_keep_their_historical_defaults_outside_hybrid():
    from core.service.context import Overrides
    from core.service.ops.media import JobOptions

    common = JobOptions().to_common(Overrides())
    assert (common.cadence_auto, common.cadence_method) == (True, "auto")
