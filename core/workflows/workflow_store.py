"""Sauvegardes atomiques et relocalisation portable des workflows."""
from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

# Réexport historique : l'écriture atomique est partagée avec les profils.
from core.atomic_io import atomic_write_text


def save_workflow(path: Path, job: dict):
    from cli.contract import validate_job_contract
    validate_job_contract(job, require_version=True)
    atomic_write_text(path, json.dumps(job, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def load_workflow(path: Path, relocate=None) -> dict:
    from cli.contract import validate_job_contract
    path = Path(path).expanduser().resolve()
    job = json.loads(path.read_text(encoding="utf-8-sig"))
    validate_job_contract(job, require_version=True)
    def resolve(raw):
        value = Path(raw).expanduser()
        candidate = value if value.is_absolute() else path.parent / value
        if candidate.is_file():
            return str(candidate.resolve())
        basename = PureWindowsPath(raw).name
        relative = path.parent / basename
        if relative.is_file():
            return str(relative.resolve())
        replacement = relocate(raw) if relocate else None
        if replacement and Path(replacement).is_file():
            return str(Path(replacement).resolve())
        raise FileNotFoundError(raw)
    sources = job.get("sources", [])
    if isinstance(sources, str):
        sources = [sources]
    job["sources"] = [dict(s, path=resolve(s["path"])) if isinstance(s, dict)
                      else {"path": resolve(s)} for s in sources]
    job["extra_attachments"] = [resolve(p) for p in job.get("extra_attachments", [])]
    if job.get("output") and not Path(job["output"]).is_absolute():
        job["output"] = str(path.parent / job["output"])
    return job
