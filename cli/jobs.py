"""Load and merge CLI job JSON with command-line overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cli.contract import validate_job_contract
from cli.json_io import csv_values, deep_merge, load_json
from cli.options import JobOverrides


def load_job(overrides: JobOverrides) -> dict[str, Any]:
    job: dict[str, Any] = {}
    require_version = False
    if overrides.config:
        loaded = load_json(Path(overrides.config).expanduser())
        validate_job_contract(loaded, require_version=True)
        job = deep_merge(job, loaded)
        require_version = True
    if overrides.template:
        template = load_json(Path(overrides.template).expanduser())
        validate_job_contract(template, require_version=True)
        job = deep_merge(template, job)
        require_version = True
    if overrides.input:
        job["sources"] = [{"path": item} for item in overrides.input]
    if overrides.output:
        job["output"] = overrides.output
    if overrides.languages:
        langs = csv_values(overrides.languages)
        job.setdefault("rules", {}).setdefault("tracks", {}).setdefault("audio", {})["languages"] = langs
        job.setdefault("rules", {}).setdefault("tracks", {}).setdefault("subtitle", {})["languages"] = langs
    if overrides.tmdb:
        raw_tmdb = job.get("tmdb", {})
        tmdb: dict[str, Any] = raw_tmdb if isinstance(raw_tmdb, dict) else {"enabled": True}
        job["tmdb"] = tmdb
        tmdb["enabled"] = True
    if overrides.tmdb_id is not None:
        raw_tmdb = job.get("tmdb", {})
        tmdb = raw_tmdb if isinstance(raw_tmdb, dict) else {"enabled": True}
        job["tmdb"] = tmdb
        tmdb["enabled"] = True
        tmdb["id"] = overrides.tmdb_id
    validate_job_contract(job, require_version=require_version)
    return job
