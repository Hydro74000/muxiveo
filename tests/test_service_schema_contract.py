"""Contrat publié des requêtes et résultats : définitions partagées, références résolues, schéma = admission."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from core.service.catalog import CATALOG
from core.service.errors import ApiError
from tests import _json_schema as schema_check

_ROOT = str(Path.cwd().anchor)
_SOURCE = str(Path(_ROOT, "media", "film.mkv"))
_OUTPUT = str(Path(_ROOT, "media", "sortie.mkv"))


def test_every_reference_resolves_in_its_published_document():
    catalog = CATALOG.describe()
    for ref in schema_check.refs(catalog):
        schema_check.resolve(ref, catalog)
    for spec in CATALOG:
        described = spec.describe()
        for ref in schema_check.refs(described):
            schema_check.resolve(ref, described)  # chaque description d'opération est autonome
    assert not any("$defs" in spec for spec in catalog["operations"].values())  # jointes une fois au catalogue


def test_documents_are_linked_to_their_shared_definitions():
    catalog = CATALOG.describe()
    request = catalog["operations"]["media.run"]["request"]
    assert request["properties"]["job"]["$ref"] == "#/$defs/job"
    assert catalog["$defs"]["job"]["properties"]["encode"] == {"$ref": "#/$defs/encode_block"}
    for name in ("media.batch", "profile.batch", "profile.apply", "hybrid.run"):
        encode = catalog["operations"][name]["request"]["properties"]["encode"]
        assert {"$ref": "#/$defs/encode_block"} in encode["anyOf"], name
    assert catalog["operations"]["profile.save"]["request"]["properties"]["profile"]["$ref"] == \
        "#/$defs/decision_profile_input"
    for name in ("profile.show", "profile.save", "profile.capture"):
        assert catalog["operations"][name]["result"]["properties"]["profile"]["$ref"] == \
            "#/$defs/profile_document", name
    template = catalog["operations"]["media.inspect"]["result"]["properties"]["template"]
    assert {"$ref": "#/$defs/job"} in template["anyOf"]
    assert "description" not in catalog["operations"]["profile.show"]["result"]  # signature générée : omise


def _job(**extra: Any) -> dict[str, Any]:
    return {"sources": [_SOURCE], "output": _OUTPUT, **extra}


_CALIBRATION = {"segments": [{"start_ms": 0, "shift_ms": 120}], "confidence": 1.0, "samples": []}
_AUDIO = {"type": "audio", "position": 0}

_ACCEPTED = [
    _job(),
    _job(version=1),
    {"version": 1, "kind": "exact-job", "sources": [{"path": _SOURCE, "attachments": "none", "copy_tags": False}],
     "output": _OUTPUT, "chapters": {"source_index": 0}},
    _job(_batch_output_dir="/x", **{"$comment": "ignorée"}),
    _job(variables={"saison": "1", "aliases": {"fre": {"VFF": "Français"}}, "libre": 1}),
    _job(tracks=[{"selector": {"type": "audio", "position": 0}, "_note": "interne"}]),
    _job(tag_overrides={"COLLECTION": "Muxiveo"}),
    _job(chapters=False, tmdb=True),
    _job(fallback_profile={"name": "x", "_note": 1}, chapters={"source_index": 0, "_note": 1}),
    _job(sync_calibrations={"1": _CALIBRATION}, tracks=[{"selector": _AUDIO, "sync_calibration": _CALIBRATION}]),
    _job(sync_calibrations={"0": {**_CALIBRATION, "cadence_mismatch": {
        "cadence_type": "pal_to_film_23976", "source_fps": 25, "target_fps": 23.976, "speed_factor": 0.959}}}),
    _job(track_order=[[0, 1], [0, 2, "e1"], {"selector": _AUDIO}, {"source": 0, "id": 3}]),
    _job(chapters={"add": [{"timestamp": "00:01:00", "name": "Début"}, {"timecode_s": 61.5}]}),
    _job(audio_variants=[{"selector": _AUDIO, "codec": "aac"}, {"source_selector": _AUDIO}]),
    _job(sources=[{"path": _SOURCE, "sync": {"reference_track": "a:1", "calibration": _CALIBRATION}}]),
    _job(sources=[{"path": _SOURCE, "attachments": "none"}], tmdb={"enabled": False}),
    _job(version=1.0, crossfade_ms=1.0),  # entiers au sens JSON (requête de l'API)
    _job(tmdb_cover=["https://image.tmdb.org/t/p/original/x.jpg", "cover.jpg"]),
    _job(encode={"video": {"default": {"codec": "copy"}}}),
]
_REFUSED = [
    _job(inconnue=1),
    _job(kind="decision-profile"),
    _job(tracks=[{"selector": {"type": "audio", "position": 0}, "bogus": True}]),
    _job(tracks=[{"selector": {"type": "audio", "index": 0}}]),
    _job(sources=[{"path": _SOURCE, "sync": {"calibration": "/c.json", "bogus": 1}}]),
    _job(chapters={"source_index": 0, "bogus": 1}),
    _job(sources=[{"path": _SOURCE, "sync": {"offset_ms": 0, "_note": 1}}]),  # objets fermés : clés internes aussi
    _job(tracks=[{"selector": {"type": "audio", "position": 0}, "flags": {"default": True, "$note": 1}}]),
    _job(tracks=[{"selector": {"type": "audio", "flags": {"default": True, "_note": 1}}}]),
    _job(tmdb={"enabled": True, "bogus": 1}),
    _job(fallback_profile={"name": "x", "bogus": 1}),
    _job(encode={"video": {"default": {"codec": "copy", "bogus": 1}}}),
    _job(encode={"bogus": 1}),
    _job(audio_variants=[{"source_selector": {}, "selector": {"flags": {"_note": 1}}}]),
    *(_job(tracks=[{"selector": {"type": "video", **selector}}]) for selector in (
        {"resolution": {"height": "1080"}}, {"resolution": 42}, {"type": "bogus"}, {"position_order": "bogus"},
        {"video_flags": []})),
    _job(audio_variants=[{}]),
    _job(chapters={"add": [{}]}),
    _job(track_order=[{}]),
    _job(track_order=[{"selector": {}, "source": "bad"}]),
    _job(track_order=[[0, 1, "x", "extra"]]),
    _job(variables={"aliases": 42}),
    _job(variables={"codec_names": {"h264": 42}}),
    _job(sources=[{"path": _SOURCE, "sync": {"reference_track": "bogus"}}]),
    _job(sources=[{"path": _SOURCE, "sync": {"calibration": "/c.json", "offset_ms": 0}}]),
    _job(tracks=[{"selector": _AUDIO, "sync_calibration": {}}]),
    _job(sources=[{"path": _SOURCE, "sync": {"calibration": {}}}]),
    _job(sync_calibrations={"0": {}}),
    _job(sync_calibrations={"1": _CALIBRATION, "_note": _CALIBRATION}),
    _job(encode=None),
    _job(tmdb=None),
    _job(tmdb=0),
    _job(sources=[{"path": _SOURCE, "attachments": None}]),
    _job(sources=[{"path": _SOURCE, "attachments": 0}]),
    _job(sources=[{"path": _SOURCE, "sync": {"calibration": None}}]),
    _job(tmdb_cover=["https://example.com/x.jpg", "x.jpg"]),
    _job(sync_calibrations={"0": {"segments": []}}),
    _job(sync_calibrations={"0": {"segments": [{"start_ms": 1, "shift_ms": 0}]}}),
    _job(sync_calibrations={"0\n": _CALIBRATION}),
    _job(sync_calibrations={"0": {**_CALIBRATION, "samples": 1}}),
    _job(sync_calibrations={"0": {**_CALIBRATION, "cadence_mismatch": True}}),
    _job(sync_calibrations={"0": {**_CALIBRATION, "cadence_mismatch": {"cadence_type": "bogus"}}}),
    _job(sync_calibrations={"0": {**_CALIBRATION, "cadence_mismatch": {"speed_factor": [1]}}}),
    _job(sync_calibrations={"0": {**_CALIBRATION, "confidence": 2}}),
]


def _admitted(job: dict[str, Any]) -> bool:
    """Admission d'une requête de l'API (``call`` / ``serve`` : entiers JSON normalisés, lecture stricte)."""
    from core.service.models import api_payload

    try:
        CATALOG.get("media.validate").request.parse(api_payload({"job": copy.deepcopy(job)}))
    except ApiError:
        return False
    return True


@pytest.mark.parametrize("job", _ACCEPTED + _REFUSED)
def test_published_job_schema_and_admission_agree(job):
    catalog = CATALOG.describe()
    request = catalog["operations"]["media.validate"]["request"]
    verdict = schema_check.errors({"job": job}, request, catalog)
    assert (not verdict) is _admitted(job), verdict
    assert (job in _ACCEPTED) is _admitted(job)


def test_inspection_template_satisfies_the_published_job():
    from types import SimpleNamespace

    from core.jobs.inspection import config_template_from_info

    catalog = CATALOG.describe()
    template = config_template_from_info(SimpleNamespace(path=Path(_SOURCE)), output=_OUTPUT)
    assert not schema_check.errors(template, catalog["$defs"]["job"], catalog)
    assert _admitted(template)


def test_tolerant_documents_still_report_keys_next_to_required_alternatives():
    """Combinateurs de champs requis : les propriétés voisines restent parcourues (avertissements historiques)."""
    from core.jobs.schema import unknown_job_keys

    job = _job(audio_variants=[{"selector": _AUDIO, "bogus": 1}], chapters={"add": [{"timestamp": "0", "bogus": 1}]},
               track_order=[{"id": 1, "bogus": 1}])
    assert sorted(unknown_job_keys(job)) == ["job.audio_variants[0].bogus", "job.chapters.add[0].bogus",
                                             "job.track_order[0].bogus"]


def test_one_cause_gives_one_diagnostic():
    cases = {"audio_variants": [{}], "chapters": {"add": [{}]}, "tmdb_cover": ["https://example.com/x", "x"],
             "sync_calibrations": {"0": {"segments": []}}}
    for key, value in cases.items():
        with pytest.raises(ApiError) as caught:
            CATALOG.get("media.validate").request.parse({"job": _job(**{key: value})})
        assert len(caught.value.details) == 1, (key, caught.value.details)
    with pytest.raises(ApiError) as caught:
        CATALOG.get("media.validate").request.parse({"job": _job(audio_variants=[{"source_selector": False}])})
    assert [detail.path for detail in caught.value.details] == ["job.audio_variants[0].source_selector"]


_RULE = {"id": "r", "match": {"field": "type", "op": "eq", "value": "audio"}, "actions": []}
_PROFILE_ACCEPTED = [
    {"name": "demo"},
    {"name": "demo", "version": 1, "kind": "decision-profile", "rules": [_RULE], "extra": {"libre": True}},
    {"name": "demo", "rules": [{**_RULE, "match": {"field": "language", "expr": "fre | eng"}}]},
]
_PROFILE_REFUSED = [
    {},
    {"name": " "},
    {"name": "demo", "version": 2},
    {"name": "demo", "rules": None},
    {"name": "demo", "rules": [{"id": "r", "match": {}}]},
    {"name": "demo", "rules": [{**_RULE, "id": ""}]},
    {"name": "demo", "rules": [{**_RULE, "match": {"value": 1, "expr": "audio"}}]},
    {"name": "demo", "rules": [{**_RULE, "match": {"expr": " "}}]},
    {"name": "demo", "rules": [{**_RULE, "actions": [{"type": "set_title", "clear": ["title", "title"]}]}]},
]


def _profile_admitted(profile: dict[str, Any]) -> bool:
    try:
        CATALOG.get("profile.save").request.parse({"profile": copy.deepcopy(profile)})
    except ApiError:
        return False
    return True


@pytest.mark.parametrize("profile", _PROFILE_ACCEPTED + _PROFILE_REFUSED)
def test_published_profile_schema_and_admission_agree(profile):
    """``version`` / ``kind`` facultatifs (complétés par l'opération) ; règles des profils publiées."""
    catalog = CATALOG.describe()
    request = catalog["operations"]["profile.save"]["request"]
    verdict = schema_check.errors({"profile": profile}, request, catalog)
    assert (not verdict) is _profile_admitted(profile), verdict
    assert (profile in _PROFILE_ACCEPTED) is _profile_admitted(profile)


def test_profile_rules_checked_beyond_the_schema_are_refused_at_admission():
    """Limites documentées : identifiants uniques, syntaxe des expressions et `field` d'une expression de valeurs
    littérales, vérifiés hors schéma."""
    for profile in ({"name": "demo", "rules": [_RULE, _RULE]},
                    {"name": "demo", "rules": [{**_RULE, "match": {"field": "language", "expr": "((("}}]},
                    {"name": "demo", "rules": [{**_RULE, "match": {"expr": "fre"}}]}):
        assert not _profile_admitted(profile)


def test_profile_documents_published_as_rendered():
    """Profils rendus tels quels (fichier écrit à la main, capture sans nom) conformes au résultat publié."""
    catalog = CATALOG.describe()
    result = catalog["operations"]["profile.show"]["result"]
    for profile in ({"name": "main", "rules": None}, {"name": "main", "rules": [{"id": "r", "match": {}}]},
                    {"name": "", "version": 1, "kind": "decision-profile", "rules": []}):
        assert not schema_check.errors({"profile": profile, "path": None}, result, catalog), profile


def test_admission_of_many_faulty_entries_stays_linear():
    """Un écart par entrée (contrat puis schéma) : diagnostics triés en temps linéaire, sans comparaison deux à deux."""
    import time

    started = time.monotonic()
    with pytest.raises(ApiError) as caught:
        CATALOG.get("media.validate").request.parse({"job": _job(tracks=[{"id": False}] * 50_000)})
    details = caught.value.details
    assert len(details) == 101 and details[-1].message == "49900 autre(s) écart(s) non détaillé(s)"
    assert time.monotonic() - started < 20


def test_diagnostics_under_a_long_key_stay_bounded():
    """Clés de 256 caractères au plus ; écarts détaillés bornés : chemins jamais recopiés par erreur."""
    import time

    from core.service.models import api_payload

    with pytest.raises(ApiError) as caught:
        api_payload({"job": {"variables": {"aliases": {"k" * 8_000_000: {}}}}})
    assert "clé de plus de 256 caractères" in caught.value.message
    aliases = {"k" * 256: {str(index): False for index in range(100_000)}}
    started = time.monotonic()
    with pytest.raises(ApiError) as caught:
        CATALOG.get("media.validate").request.parse(api_payload({"job": _job(variables={"aliases": aliases})}))
    assert len(caught.value.details) <= 101 and time.monotonic() - started < 20


def test_relative_paths_against_a_long_reference_folder_are_bounded():
    """Chaque chemin relatif recopie le dossier de référence : volume résolu borné avant toute construction."""
    import time

    base = Path("/" + "b" * 200_000)
    started = time.monotonic()
    for operation, payload in (("media.validate", {"job": {"sources": ["a.mkv"] * 400, "output": _OUTPUT}}),
                               ("media.inspect", {"inputs": ["a.mkv"] * 400})):
        with pytest.raises(ApiError) as caught:
            CATALOG.get(operation).request.parse(payload, base_dir=base)
        assert any("chemins relatifs trop nombreux" in detail.message for detail in caught.value.details), operation
    assert time.monotonic() - started < 10
    CATALOG.get("media.inspect").request.parse({"inputs": ["a.mkv"] * 400}, base_dir=Path("/media"))


def test_every_document_diagnostic_is_bounded_and_truncation_announced():
    audio = [{"select": {}, "codec": "copy", "unknown": 0}] * 150
    with pytest.raises(ApiError) as caught:
        CATALOG.get("media.validate").request.parse({"job": _job(encode={"audio": audio})})
    details = caught.value.details
    assert len(details) == 101 and details[-1].message == "50 autre(s) écart(s) non détaillé(s)"
    with pytest.raises(ApiError) as caught:
        CATALOG.get("profile.save").request.parse({"profile": {"name": "p", "tags": [False] * 150}})
    details = caught.value.details
    assert len(details) == 100 and details[-1].message == "51 autre(s) écart(s) non détaillé(s)"


def test_deeply_nested_criteria_are_refused_not_crashed():
    rule = {**_RULE, "match": {"field": "type", "expr": "(" * 300 + "audio" + ")" * 300}}
    with pytest.raises(ApiError) as caught:
        CATALOG.get("profile.save").request.parse({"profile": {"name": "p", "rules": [rule]}})
    assert "trop imbriquee" in caught.value.details[0].message
    negated = {**_RULE, "match": {"field": "type", "expr": "!" * 300 + "audio"}}
    with pytest.raises(ApiError):
        CATALOG.get("profile.save").request.parse({"profile": {"name": "p", "rules": [negated]}})
