from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.main import ContractError, apply_track_rules, build_parser, validate_job_contract
from core.workflows.remux_models import TrackEntry


def test_cli_parser_exposes_expected_subcommands() -> None:
    parser = build_parser()
    args = parser.parse_args(["preview", "--config", "job.json"])
    assert args.command == "preview"
    args = parser.parse_args(["batch", "--template", "template.json", "-i", "a.mkv"])
    assert args.command == "batch"


def test_apply_track_rules_filters_languages_and_renames() -> None:
    tracks = [
        TrackEntry(
            mkv_tid=1,
            track_type="audio",
            codec="EAC3",
            display_info="5.1  768 kbps  Atmos",
            language="fra",
            title="VF",
            orig_language="fra",
            orig_title="VF",
            flag_default=True,
            orig_flag_default=True,
        ),
        TrackEntry(
            mkv_tid=2,
            track_type="audio",
            codec="AAC",
            display_info="Stereo",
            language="spa",
            title="Spanish",
            orig_language="spa",
            orig_title="Spanish",
        ),
    ]

    apply_track_rules(
        tracks,
        {
            "normalize_languages": True,
            "tracks": {
                "audio": {
                    "languages": ["fr-FR"],
                    "rename_pattern": "{LangName} {codec} {channels} {atmos} {tag_default}",
                }
            },
        },
    )

    assert tracks[0].enabled is True
    assert tracks[0].language == "fr-FR"
    assert tracks[0].title == "French EAC3 5.1 Atmos Default"
    assert tracks[1].enabled is False


def test_apply_track_rules_filters_original_flags() -> None:
    tracks = [
        TrackEntry(
            mkv_tid=3,
            track_type="subtitle",
            codec="PGS",
            display_info="",
            language="eng",
            title="SDH",
            orig_flag_hearing_impaired=True,
            flag_hearing_impaired=True,
        ),
        TrackEntry(
            mkv_tid=4,
            track_type="subtitle",
            codec="PGS",
            display_info="",
            language="eng",
            title="Regular",
        ),
    ]

    apply_track_rules(
        tracks,
        {
            "tracks": {
                "subtitle": {
                    "languages": ["en-US"],
                    "flags": {"hearing_impaired": False},
                }
            }
        },
    )

    assert tracks[0].enabled is False
    assert tracks[1].enabled is True
    assert tracks[1].language == "en-US"


def test_documented_cli_json_examples_are_valid() -> None:
    root = Path(__file__).resolve().parent.parent
    for path in (root / "docs" / "cli").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)


def test_validate_job_contract_reports_field_paths() -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_job_contract(
            {
                "version": 1,
                "sources": [{"path": "source.mkv"}],
                "output": "out.mkv",
                "rules": {
                    "tracks": {
                        "audio": {
                            "languages": ["fr-FR", 42],
                            "flags": {"commentary": "no"},
                        }
                    }
                },
            },
            require_version=True,
        )

    message = str(excinfo.value)
    assert "rules.tracks.audio.languages[1]" in message
    assert "rules.tracks.audio.flags.commentary" in message


def test_validate_job_contract_requires_version_for_json_files() -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_job_contract(
            {"sources": [{"path": "source.mkv"}], "output": "out.mkv"},
            require_version=True,
        )

    assert "$.version: champ requis" in str(excinfo.value)


def test_validate_job_contract_rejects_invalid_chapter_shape() -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_job_contract(
            {
                "version": 1,
                "sources": [{"path": "source.mkv"}],
                "output": "out.mkv",
                "chapters": {"add": [{"chaptername": "No timestamp"}]},
            },
            require_version=True,
        )

    assert "chapters.add[0].timestamp" in str(excinfo.value)


def test_apply_track_rules_supports_presets_conditions_limits_and_defaults() -> None:
    tracks = [
        TrackEntry(
            mkv_tid=1,
            track_type="audio",
            codec="EAC3",
            display_info="5.1 Atmos",
            language="fra",
            title="Main",
            orig_language="fra",
            orig_title="Main",
        ),
        TrackEntry(
            mkv_tid=2,
            track_type="audio",
            codec="EAC3",
            display_info="2.0",
            language="fra",
            title="Commentary",
            orig_language="fra",
            orig_title="Commentary",
            flag_commentary=True,
            orig_flag_commentary=True,
        ),
        TrackEntry(
            mkv_tid=3,
            track_type="audio",
            codec="AAC",
            display_info="Stereo",
            language="eng",
            title="English",
            orig_language="eng",
            orig_title="English",
        ),
    ]

    apply_track_rules(
        tracks,
        {
            "presets": {
                "series": {
                    "tracks": {
                        "audio": {
                            "include": True,
                            "languages": ["fr-FR"],
                            "fallback_languages": ["en-US"],
                        }
                    }
                }
            },
            "use_presets": ["series"],
            "tracks": {
                "audio": {
                    "conditions": {"not": {"flags": {"commentary": True}}},
                    "limit_per_language": 1,
                    "default": "first",
                }
            },
        },
    )

    assert tracks[0].enabled is True
    assert tracks[0].flag_default is True
    assert tracks[1].enabled is False
    assert tracks[2].enabled is False


def test_apply_track_rules_prioritizes_within_track_type_only() -> None:
    tracks = [
        TrackEntry(mkv_tid=0, track_type="video", codec="HEVC", display_info="2160p", language="", title=""),
        TrackEntry(mkv_tid=1, track_type="audio", codec="AAC", display_info="Stereo", language="eng", title=""),
        TrackEntry(mkv_tid=2, track_type="audio", codec="EAC3", display_info="5.1 Atmos", language="fra", title=""),
        TrackEntry(mkv_tid=3, track_type="subtitle", codec="PGS", display_info="", language="fra", title=""),
    ]

    ordered = apply_track_rules(
        tracks,
        {
            "tracks": {
                "audio": {
                    "priority": [
                        {"languages": ["fr-FR"], "codec": "EAC3", "channels": "5.1"},
                        {"languages": ["en-US"]},
                    ]
                }
            }
        },
    )

    assert [track.track_type for track in ordered] == ["video", "audio", "audio", "subtitle"]
    assert [track.mkv_tid for track in ordered] == [0, 2, 1, 3]


def test_apply_track_rules_uses_fallback_languages_when_primary_missing() -> None:
    tracks = [
        TrackEntry(
            mkv_tid=1,
            track_type="audio",
            codec="AAC",
            display_info="Stereo",
            language="eng",
            title="English",
            orig_language="eng",
            orig_title="English",
        )
    ]

    apply_track_rules(
        tracks,
        {
            "tracks": {
                "audio": {
                    "languages": ["fr-FR"],
                    "fallback_languages": ["en-US"],
                }
            }
        },
    )

    assert tracks[0].enabled is True
    assert tracks[0].language == "en-US"


def test_validate_job_contract_checks_advanced_rule_shapes() -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_job_contract(
            {
                "version": 1,
                "sources": [{"path": "source.mkv"}],
                "output": "out.mkv",
                "rules": {
                    "use_presets": ["series"],
                    "presets": {"series": {"tracks": {"audio": {"limit_per_language": "one"}}}},
                    "tracks": {"audio": {"conditions": {"all": [{"atmos": "yes"}]}, "priority": "bad"}},
                },
            },
            require_version=True,
        )

    message = str(excinfo.value)
    assert "rules.presets.series.tracks.audio.limit_per_language" in message
    assert "rules.tracks.audio.conditions.all[0].atmos" in message
    assert "rules.tracks.audio.priority" in message
