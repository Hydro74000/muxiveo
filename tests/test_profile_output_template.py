"""Template de nom de sortie porté par un profil décisionnel (CLI)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cli.errors import CliError
from cli.logging import Logger
from cli.options import CommonOptions
from cli.profile import build_profile_remux_config, profile_batch
from core.config import AppConfig
from core.profiles.decision import DecisionProfileError, profile_output_template, validate_decision_profile
from core.workflows.remux_models import SourceInput, TrackEntry


def _profile(**extra) -> dict:
    return {"version": 1, "kind": "decision-profile", "name": "Nommage", "rules": [], **extra}


def test_profile_output_template_is_optional_string():
    assert profile_output_template(_profile()) == ""
    assert profile_output_template(_profile(output_template="  {title}.{year}  ")) == "{title}.{year}"
    assert profile_output_template(_profile(output_template=12)) == ""
    with pytest.raises(DecisionProfileError, match="output_template"):
        validate_decision_profile(_profile(output_template=["{title}"]))


@pytest.fixture
def build(tmp_path, monkeypatch):
    source = tmp_path / "Film.2024.2160p.BluRay-GRP.mkv"
    source.touch()
    video = TrackEntry(0, "video", "HEVC", "2160p", "und", "", file_id="src0")
    audio_fr = TrackEntry(1, "audio", "EAC3", "5.1", "fra", "VF", file_id="src0")
    audio_en = TrackEntry(2, "audio", "EAC3", "5.1", "eng", "VO", file_id="src0")
    sources = [SourceInput(path=source, file_index=0, tracks=[video, audio_fr, audio_en])]
    monkeypatch.setattr(
        "cli.profile.inspect_sources",
        lambda job, conf, opts, log, cli_inputs=None, **_kwargs: (sources, [MagicMock()], [video, audio_fr, audio_en]),
    )
    monkeypatch.chdir(tmp_path)

    def run(profile: dict, *, cli_output: str | None = None, metadata_job: dict | None = None) -> Path:
        config, _ = build_profile_remux_config(
            profile, cli_inputs=[str(source)], cli_output=cli_output, config=AppConfig(),
            options=CommonOptions(), logger=Logger(fmt="text"), preview=True, metadata_job=metadata_job,
        )
        return config.output

    return run


TEMPLATE = "{source_name}.{audio-multi}.{video-codec:best}"


def test_profile_template_names_output_without_cli_output(build, tmp_path):
    assert build(_profile(output_template=TEMPLATE)) == tmp_path / "Film.2024.2160p.BluRay-GRP.MULTi.HEVC.mkv"


def test_output_directory_is_base_of_profile_template(build, tmp_path):
    (tmp_path / "out").mkdir()
    expected = tmp_path / "out" / "Film.2024.2160p.BluRay-GRP.MULTi.HEVC.mkv"
    assert build(_profile(output_template=TEMPLATE), cli_output=str(tmp_path / "out")) == expected
    assert build(_profile(output_template=TEMPLATE), cli_output="out/") == expected


def test_explicit_output_file_and_cli_template_win_over_profile(build, tmp_path):
    profile = _profile(output_template=TEMPLATE)
    assert build(profile, cli_output=str(tmp_path / "choisi.mkv")) == tmp_path / "choisi.mkv"
    assert build(profile, metadata_job={"output_template": "{group}"}) == tmp_path / "GRP.mkv"


def test_profile_batch_uses_profile_template(tmp_path, monkeypatch):
    captured: dict = {}

    def discover(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(jobs=[], selected=0, scanned=0, roots=[])

    monkeypatch.setattr("cli.profile.load_decision_profile", lambda _path, _config: _profile(output_template=TEMPLATE))
    monkeypatch.setattr("cli.profile.discover_direct_batch_jobs", discover)
    with pytest.raises(CliError):
        profile_batch(
            "Nommage", cli_inputs=None, input_dirs=[str(tmp_path)], recursive=False, include_patterns=None,
            exclude_patterns=None, output_dir=str(tmp_path / "out"), dry_run=True, force=False,
            continue_on_error=False, summary_path=None, config=AppConfig(), options=CommonOptions(),
            logger=Logger(fmt="text"),
        )
    assert captured["output_template"] == TEMPLATE
