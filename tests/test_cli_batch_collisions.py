"""
tests/test_cli_batch_collisions.py — Sorties de batch réservées avant exécution (A05).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli.batch as batch_mod
from cli.batch import PlannedBatchJob, assert_unique_batch_outputs, run_batch
from cli.constants import EXIT_ARGS, EXIT_OK, EXIT_PARTIAL
from cli.errors import CliError


class _Logger:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, level: str, message: str, **fields) -> None:
        self.events.append({"level": level, "message": message, **fields})


@pytest.fixture
def doubles(monkeypatch):
    """Construction et exécution remplacées : seuls les chemins sont observés."""
    ran: list[Path] = []

    def build(job, _config, _options, _logger):
        if job.get("fail"):
            raise RuntimeError("source illisible")
        output = Path(str(job["output"])).expanduser()
        return SimpleNamespace(output=output.absolute(), allow_missing_output_dir=bool(job.get("_allow_missing_output_dir")))

    def run(_config, _options, _logger, remux_config, *, force=False):
        ran.append(remux_config.output)
        return EXIT_OK

    monkeypatch.setattr(batch_mod, "build_remux_config", build)
    monkeypatch.setattr(batch_mod, "run_remux_config", run)
    monkeypatch.setattr(batch_mod, "preview_remux_config", lambda *_a, **_k: EXIT_OK)
    return ran


def _run(tmp_path: Path, template: dict, items: list, *, force: bool = False, continue_on_error: bool = False,
         logger: _Logger | None = None) -> int:
    template_path = tmp_path / "template.json"
    template_path.write_text(json.dumps({"version": 1, **template}), encoding="utf-8")
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(json.dumps({"jobs": items}), encoding="utf-8")
    return run_batch(
        template_path=str(template_path), batch_path=str(batch_path), cli_inputs=None, input_dirs=None,
        recursive=False, include_patterns=None, exclude_patterns=None, output_dir=None, dry_run=False,
        force=force, continue_on_error=continue_on_error, summary_path=None,
        config=None, options=None, logger=logger or _Logger(),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("force", [False, True])
def test_inherited_identical_outputs_refused_before_any_job(tmp_path: Path, doubles, force: bool) -> None:
    """Reproduction de l'audit : sortie fixe héritée du template, deux entrées."""
    output = tmp_path / "out" / "film.mkv"
    with pytest.raises(CliError) as excinfo:
        _run(tmp_path, {"output": str(output)},
             [{"sources": ["a.mkv"]}, {"sources": ["b.mkv"]}], force=force)
    assert excinfo.value.exit_code == EXIT_ARGS
    assert "jobs 1 (a.mkv) et 2 (b.mkv)" in str(excinfo.value)
    assert doubles == []
    assert not (tmp_path / "out").exists()


def test_explicit_identical_outputs_refused(tmp_path: Path, doubles) -> None:
    output = str(tmp_path / "film.mkv")
    with pytest.raises(CliError):
        _run(tmp_path, {}, [{"sources": ["a.mkv"], "output": output}, {"sources": ["b.mkv"], "output": output}])
    assert doubles == []


def test_alias_through_symlink_refused(tmp_path: Path, doubles) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("liens symboliques indisponibles")
    with pytest.raises(CliError):
        _run(tmp_path, {}, [
            {"sources": ["a.mkv"], "output": str(real / "film.mkv")},
            {"sources": ["b.mkv"], "output": str(link / "film.mkv")},
        ])
    assert doubles == []


def test_hardlink_alias_of_existing_output_refused(tmp_path: Path, doubles) -> None:
    existing = tmp_path / "film.mkv"
    existing.write_bytes(b"OLD")
    alias = tmp_path / "alias.mkv"
    try:
        os.link(existing, alias)
    except OSError:
        pytest.skip("liens physiques indisponibles")
    with pytest.raises(CliError):
        _run(tmp_path, {}, [
            {"sources": ["a.mkv"], "output": str(existing)},
            {"sources": ["b.mkv"], "output": str(alias)},
        ], force=True)
    assert doubles == []


def test_case_variants_collide_on_case_insensitive_platforms(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    planned = [
        PlannedBatchJob(0, "a.mkv", "", remux_config=SimpleNamespace(output=tmp_path / "Film.mkv")),
        PlannedBatchJob(1, "b.mkv", "", remux_config=SimpleNamespace(output=tmp_path / "FILM.mkv")),
    ]
    with pytest.raises(CliError):
        assert_unique_batch_outputs(planned)


def test_distinct_outputs_run_in_order(tmp_path: Path, doubles) -> None:
    rc = _run(tmp_path, {}, [
        {"sources": ["a.mkv"], "output": str(tmp_path / "a.mkv.out.mkv")},
        {"sources": ["b.mkv"], "output": str(tmp_path / "b.mkv.out.mkv")},
    ])
    assert rc == EXIT_OK
    assert [p.name for p in doubles] == ["a.mkv.out.mkv", "b.mkv.out.mkv"]


def test_independent_job_error_keeps_continue_on_error(tmp_path: Path, doubles) -> None:
    logger = _Logger()
    rc = _run(tmp_path, {}, [
        {"sources": ["a.mkv"], "output": str(tmp_path / "a.out.mkv"), "fail": True},
        {"sources": ["b.mkv"], "output": str(tmp_path / "b.out.mkv")},
    ], continue_on_error=True, logger=logger)
    assert rc == EXIT_PARTIAL
    assert [p.name for p in doubles] == ["b.out.mkv"]
    statuses = [(e["job_index"], e["status"]) for e in logger.events if e.get("event") == "batch_job" and e["status"] != "started"]
    assert statuses == [(0, "failed"), (1, "success")]


def test_job_error_without_continue_stops_after_previous_jobs(tmp_path: Path, doubles) -> None:
    rc = _run(tmp_path, {}, [
        {"sources": ["a.mkv"], "output": str(tmp_path / "a.out.mkv")},
        {"sources": ["b.mkv"], "output": str(tmp_path / "b.out.mkv"), "fail": True},
        {"sources": ["c.mkv"], "output": str(tmp_path / "c.out.mkv")},
    ])
    assert rc == EXIT_PARTIAL
    assert [p.name for p in doubles] == ["a.out.mkv"]
