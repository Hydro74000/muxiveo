"""
tests/test_json_documents.py — Documents JSON, chemins relatifs et contrat (A11, A12, A14, A15).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from cli.chapters import chapter_entries, parse_timecode
from cli.constants import EXIT_ARGS, MUX_BACKEND_CHOICES, SYNC_MODES, SYNC_REWRITE_MODES, SYNC_SUBTITLE_MODES
from cli.contract import validate_job_contract
from cli.errors import CliError, ContractError
from cli.json_io import load_json
from cli.jobs import load_job
from cli.options import JobOverrides
from cli.schema import build_cli_json_schema
from core.json_documents import JsonDocumentError, read_json_document, resolve_document_paths
from core.workflows.remux_models import MUX_BACKENDS
from core.workflows.workflow_store import load_workflow


def _write(path: Path, payload: dict, *, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding=encoding)
    return path


# ---------------------------------------------------------------------------
# A12 — BOM ; A14 — constantes non finies
# ---------------------------------------------------------------------------

def test_utf8_bom_accepted_by_cli_and_workflow_loaders(tmp_path: Path) -> None:
    """Reproduction de l'audit : même JSON BOM, accepté partout."""
    source = tmp_path / "source.mkv"
    source.write_bytes(b"x")
    document = _write(tmp_path / "job.json", {"version": 1, "sources": ["source.mkv"]}, encoding="utf-8-sig")
    assert document.read_bytes().startswith(b"\xef\xbb\xbf")
    assert load_json(document)["version"] == 1
    assert load_workflow(document)["sources"] == [{"path": str(source.resolve())}]


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_refused(tmp_path: Path, constant: str) -> None:
    document = tmp_path / "job.json"
    document.write_text(f'{{"version": 1, "crossfade_ms": {constant}}}', encoding="utf-8")
    with pytest.raises(JsonDocumentError):
        read_json_document(document)
    with pytest.raises(CliError) as excinfo:
        load_json(document)
    assert excinfo.value.exit_code == EXIT_ARGS


# ---------------------------------------------------------------------------
# A11 — chemins relatifs au document
# ---------------------------------------------------------------------------

def test_resolve_document_paths_covers_all_path_fields(tmp_path: Path) -> None:
    job = {
        "sources": ["a.mkv", {"path": "sub/b.mkv", "attachments": "none"}, "/abs/c.mkv", "C:\\\\win\\\\d.mkv"],
        "input": "e.mkv",
        "extra_attachments": ["cover.jpg", "~/f.jpg"],
        "chapters": {"import": "chapters.txt", "include_source": True},
        "output": "out/film.mkv",
        "output_template": "{title}.mkv",
    }
    resolved = resolve_document_paths(job, tmp_path)
    assert resolved["sources"][0] == str(tmp_path / "a.mkv")
    assert resolved["sources"][1] == {"path": str(tmp_path / "sub/b.mkv"), "attachments": "none"}
    assert resolved["sources"][2] == "/abs/c.mkv"
    assert resolved["sources"][3] == "C:\\\\win\\\\d.mkv"
    assert resolved["input"] == str(tmp_path / "e.mkv")
    assert resolved["extra_attachments"] == [str(tmp_path / "cover.jpg"), "~/f.jpg"]
    assert resolved["chapters"] == {"import": str(tmp_path / "chapters.txt"), "include_source": True}
    assert resolved["output"] == str(tmp_path / "out/film.mkv")
    assert resolved["output_template"] == "{title}.mkv"
    assert job["sources"][0] == "a.mkv"  # document d'origine intact


def test_cli_job_paths_follow_each_document_not_cwd(tmp_path: Path, monkeypatch) -> None:
    """Reproduction de l'audit : même document, lancé depuis un autre dossier."""
    job_dir = tmp_path / "jobs"
    template_dir = tmp_path / "templates"
    config = _write(job_dir / "job.json", {"version": 1, "sources": ["source.mkv"]})
    template = _write(template_dir / "template.json", {"version": 1, "output": "out.mkv",
                                                       "extra_attachments": ["cover.jpg"]})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    job = load_job(JobOverrides(config=str(config), template=str(template)))
    assert job["sources"] == [str(job_dir / "source.mkv")]
    assert job["output"] == str(template_dir / "out.mkv")
    assert job["extra_attachments"] == [str(template_dir / "cover.jpg")]


def test_cli_argv_paths_stay_relative_to_cwd(tmp_path: Path) -> None:
    config = _write(tmp_path / "jobs" / "job.json", {"version": 1, "sources": ["source.mkv"]})
    job = load_job(JobOverrides(config=str(config), input=["argv.mkv"], output="argv-out.mkv"))
    assert job["sources"] == [{"path": "argv.mkv"}]
    assert job["output"] == "argv-out.mkv"


def test_workflow_loader_resolves_chapter_import_from_document(tmp_path: Path) -> None:
    (tmp_path / "source.mkv").write_bytes(b"x")
    document = _write(tmp_path / "wf" / "job.json", {
        "version": 1,
        "sources": [str(tmp_path / "source.mkv")],
        "chapters": {"import": "chapters.txt"},
        "output": "film.mkv",
    })
    job = load_workflow(document)
    assert job["chapters"]["import"] == str(document.parent / "chapters.txt")
    assert job["output"] == str(document.parent / "film.mkv")


# ---------------------------------------------------------------------------
# A14 — chapitres
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["nan", "inf", "-inf", float("nan"), float("inf"), -1, "-1", True])
def test_parse_timecode_refuses_non_finite_or_negative(value) -> None:
    with pytest.raises(ValueError):
        parse_timecode(value)


def test_chapter_entries_report_invalid_values_as_cli_errors() -> None:
    with pytest.raises(CliError) as excinfo:
        chapter_entries({"chapters": {"add": [{"timestamp": "inf", "name": "x"}]}}, [])
    assert excinfo.value.exit_code == EXIT_ARGS


@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_remux_validation_refuses_non_finite_chapters(tmp_path: Path, value: float) -> None:
    """Reproduction de l'audit : ChapterEntry NaN/infini dans un RemuxConfig réel."""
    from core.inspector import ChapterEntry
    from core.workflows.remux import RemuxWorkflow
    from core.workflows.remux_models import RemuxConfig, SourceInput
    from tests.test_remux_hardening import _track

    source = tmp_path / "in.mkv"
    source.write_bytes(b"src")
    config = RemuxConfig(
        sources=[SourceInput(source, 0, [_track(0)])], output=tmp_path / "out.mkv", track_order=[(0, 0)],
        chapter_overrides=[ChapterEntry(timecode_s=value, name="x")],
    )
    errors = RemuxWorkflow(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe").validate(config)
    assert any("Chapitre #1 invalide : timecode non fini" in error for error in errors)


# ---------------------------------------------------------------------------
# A15 — schéma public et validateur
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("job", [
    {"version": True},
    {"version": 1.0},
    {"version": 1, "mux_backend": "garbage"},
    {"version": 1, "sync_mode": "garbage"},
    {"version": 1, "sync_subtitles": "garbage"},
    {"version": 1, "tracks": [{"id": 0, "sync_rewrite_mode": "garbage"}]},
    {"version": 1, "tmdb": {"kind": "garbage"}},
    {"version": 1, "chapters": {"add": [{"timestamp": math.nan}]}},
])
def test_contract_refuses_values_outside_public_schema(job: dict) -> None:
    with pytest.raises(ContractError):
        validate_job_contract(job, require_version=True)


@pytest.mark.parametrize("job", [
    {"version": 1, "mux_backend": "native", "sync_mode": "physical", "sync_subtitles": "none"},
    {"version": 1, "tracks": [{"id": 0, "sync_rewrite_mode": "offset"}]},
    {"version": 1, "chapters": {"add": [{"timestamp": "00:01:00", "name": "x"}, {"timestamp": 12.5}]}},
])
def test_contract_accepts_documented_values(job: dict) -> None:
    validate_job_contract(job, require_version=True)


def test_schema_and_validator_share_enumerations() -> None:
    schema = build_cli_json_schema()
    properties = schema["properties"]
    assert properties["mux_backend"]["enum"] == list(MUX_BACKEND_CHOICES)
    assert set(MUX_BACKEND_CHOICES) == set(MUX_BACKENDS)
    assert properties["sync_mode"]["enum"] == list(SYNC_MODES)
    assert properties["sync_subtitles"]["enum"] == list(SYNC_SUBTITLE_MODES)
    track_edit = schema["$defs"]["track_edit"]["properties"]
    assert track_edit["sync_rewrite_mode"]["enum"] == list(SYNC_REWRITE_MODES)


@pytest.mark.parametrize("value", ["1e999", "-1e999"])
def test_overflowing_json_number_is_rejected(value):
    from core.json_documents import JsonDocumentError, parse_json_text

    with pytest.raises(JsonDocumentError, match="non fini"):
        parse_json_text('{"duration": ' + value + '}')
