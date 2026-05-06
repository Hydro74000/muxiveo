from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration._synth import make_av_container


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe requis pour les tests d'intégration CLI",
)


def _run_cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(root / "mediarecode_cli.py"),
            *args,
            "--ffmpeg",
            "ffmpeg",
            "--ffprobe",
            "ffprobe",
            "--mediainfo",
            "/bin/false",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_inspect_validate_preview_on_synthetic_media(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    src = tmp_path / "source.mkv"
    out = tmp_path / "out.mkv"
    make_av_container(src, duration=0.3)

    inspect = _run_cli(root, "inspect", str(src))
    assert inspect.returncode == 0, inspect.stderr
    payload = json.loads(inspect.stdout)
    assert payload["files"][0]["tracks"]

    config = tmp_path / "job.json"
    config.write_text(
        json.dumps({"version": 1, "sources": [{"path": str(src)}], "output": str(out)}),
        encoding="utf-8",
    )

    validate = _run_cli(root, "validate", "--config", str(config))
    assert validate.returncode == 0, validate.stderr

    preview = _run_cli(root, "preview", "--config", str(config))
    assert preview.returncode == 0, preview.stderr
    assert "ffmpeg" in preview.stdout
    assert str(out) in preview.stdout


def test_cli_remux_dry_run_refuses_invalid_json_before_inspection(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    config = tmp_path / "bad.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [{"path": str(tmp_path / "missing.mkv")}],
                "output": str(tmp_path / "out.mkv"),
                "rules": {"tracks": {"audio": {"languages": ["fr-FR", 7]}}},
            }
        ),
        encoding="utf-8",
    )

    result = _run_cli(root, "remux", "--config", str(config), "--dry-run")
    assert result.returncode == 2
    assert "rules.tracks.audio.languages[1]" in result.stderr


def test_cli_batch_dry_run_jsonl_reports_job_status(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    src = tmp_path / "source.mkv"
    out = tmp_path / "out.mkv"
    make_av_container(src, duration=0.3)

    template = tmp_path / "template.json"
    template.write_text(
        json.dumps({"version": 1, "rules": {"normalize_languages": True}}),
        encoding="utf-8",
    )
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps({"jobs": [{"sources": [{"path": str(src)}], "output": str(out)}]}),
        encoding="utf-8",
    )

    result = _run_cli(
        root,
        "batch",
        "--template",
        str(template),
        "--batch",
        str(batch),
        "--dry-run",
        "--log-format",
        "jsonl",
    )
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
    assert any(event.get("event") == "batch_job" and event.get("status") == "success" for event in events)
    assert any(event.get("event") == "batch_summary" for event in events)


def test_cli_remux_headless_runs_and_refuses_existing_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    src = tmp_path / "source.mkv"
    out = tmp_path / "out.mkv"
    make_av_container(src, duration=0.3)

    first = _run_cli(root, "remux", "-i", str(src), "-o", str(out), "--no-nfo")
    assert first.returncode == 0, first.stderr
    assert out.exists() and out.stat().st_size > 0

    second = _run_cli(root, "remux", "-i", str(src), "-o", str(out), "--no-nfo")
    assert second.returncode == 5
    assert "Sortie déjà existante" in second.stderr

    forced = _run_cli(root, "remux", "-i", str(src), "-o", str(out), "--no-nfo", "--force")
    assert forced.returncode == 0, forced.stderr
