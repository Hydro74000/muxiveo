from __future__ import annotations

import json
from pathlib import Path

from cli.main import apply_track_rules, build_parser
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

