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
        "#/$defs/decision_profile"
    for name in ("profile.show", "profile.save", "profile.capture"):
        assert catalog["operations"][name]["result"]["properties"]["profile"]["$ref"] == \
            "#/$defs/decision_profile", name
    template = catalog["operations"]["media.inspect"]["result"]["properties"]["template"]
    assert {"$ref": "#/$defs/job"} in template["anyOf"]
    assert "description" not in catalog["operations"]["profile.show"]["result"]  # signature générée : omise


def _job(**extra: Any) -> dict[str, Any]:
    return {"sources": [_SOURCE], "output": _OUTPUT, **extra}


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
]


def _admitted(job: dict[str, Any]) -> bool:
    try:
        CATALOG.get("media.validate").request.parse({"job": copy.deepcopy(job)})
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
