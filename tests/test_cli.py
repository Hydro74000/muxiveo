from __future__ import annotations

import json
from pathlib import Path

import pytest

import cli.main
from cli.batch import discover_direct_batch_jobs, run_batch
from cli.contract import validate_batch_contract, validate_job_contract
from cli.errors import CliError, ContractError
from cli.parser import build_parser
from cli.rules import apply_track_rules
from cli.schema import (
    build_cli_json_schema,
    build_cli_json_schema_bundle,
    build_cli_json_schema_v2,
    build_decision_profile_schema_v1,
    build_exact_job_schema_v1,
)
from core.workflows.remux_models import TrackEntry


def test_cli_parser_exposes_expected_subcommands() -> None:
    parser = build_parser()
    args = parser.parse_args(["preview", "--config", "job.json", "--json"])
    assert args.command == "preview"
    assert args.json_output is True
    args = parser.parse_args(["validate", "--config", "job.json", "--json"])
    assert args.json_output is True
    args = parser.parse_args(["batch", "--template", "template.json", "-i", "a.mkv"])
    assert args.command == "batch"
    args = parser.parse_args(["batch", "--template", "template.json", "-i", "a.mkv", "--summary", "summary.json"])
    assert args.summary == "summary.json"
    args = parser.parse_args(
        [
            "batch",
            "--template",
            "template.json",
            "--input-dir",
            "season",
            "--recursive",
            "--include",
            "*.mkv",
            "--exclude",
            "*sample*",
        ]
    )
    assert args.input_dir == ["season"]
    assert args.recursive is True
    assert args.include == ["*.mkv"]
    assert args.exclude == ["*sample*"]
    args = parser.parse_args(["inspect", "a.mkv", "--rules-preview"])
    assert args.rules_preview is True
    args = parser.parse_args(["schema", "--output", "schema.json"])
    assert args.command == "schema"
    assert args.output == "schema.json"
    args = parser.parse_args(["schema", "--version", "2"])
    assert args.schema_version == "2"
    args = parser.parse_args(["schema", "--version", "decision-profile"])
    assert args.schema_version == "decision-profile"
    args = parser.parse_args(["run", "--config", "job.json", "--dry-run"])
    assert args.command == "run"
    assert args.dry_run is True
    args = parser.parse_args(["preview", "--profile", "p.json", "-i", "a.mkv", "--json"])
    assert args.command == "preview"
    assert args.profile == "p.json"
    args = parser.parse_args(["batch", "--profile", "p.json", "--input-dir", "season", "--output-dir", "out"])
    assert args.command == "batch"
    assert args.profile == "p.json"
    args = parser.parse_args(["profile", "preview", "--profile", "p.json", "-i", "a.mkv", "--json"])
    assert args.command == "profile"
    assert args.profile_command == "preview"
    assert args.json_output is True


def test_cli_main_only_exposes_entrypoint() -> None:
    assert cli.main.__all__ == ["main"]
    assert callable(cli.main.main)
    for moved_name in (
        "apply_track_rules",
        "build_cli_json_schema",
        "build_parser",
        "validate_batch_contract",
        "validate_job_contract",
    ):
        assert not hasattr(cli.main, moved_name)


def test_cli_json_schema_exposes_public_contract_keys() -> None:
    schema = build_cli_json_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["version"] == {"const": 1}
    for key in ("sources", "output", "rules", "tracks", "track_order", "chapters", "tmdb"):
        assert key in schema["properties"]
    for definition in ("source", "rules", "condition", "track_edit", "track_order_item", "chapters", "tmdb"):
        assert definition in schema["$defs"]


def test_cli_json_schema_v2_exposes_hybrid_contract_keys() -> None:
    schema = build_cli_json_schema_v2()
    assert schema["properties"]["version"] == {"const": 2}
    for key in ("sources", "output", "tracks", "track_order", "audio_variants", "encode"):
        assert key in schema["properties"]
    for definition in ("selector", "audio_variant", "encode", "rules", "chapters"):
        assert definition in schema["$defs"]
    bundle = build_cli_json_schema_bundle()
    assert len(bundle["oneOf"]) == 4


def test_profile_schemas_expose_exact_and_decision_contracts() -> None:
    exact = build_exact_job_schema_v1()
    assert exact["properties"]["kind"] == {"const": "exact-job"}
    decision = build_decision_profile_schema_v1()
    assert decision["properties"]["kind"] == {"const": "decision-profile"}
    assert "rule" in decision["$defs"]
    assert "action" in decision["$defs"]


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


def test_validate_job_contract_accepts_hybrid_v2_selectors() -> None:
    validate_job_contract(
        {
            "version": 2,
            "kind": "exact-template",
            "sources": [{"path": "source.mkv"}],
            "output": "out.mkv",
            "tracks": [
                {
                    "selector": {"source": 0, "type": "audio", "position": 0, "codec": "EAC3"},
                    "enabled": True,
                    "language": "fr-FR",
                }
            ],
            "track_order": [{"selector": {"source": 0, "type": "video", "position": 0}}],
            "audio_variants": [
                {
                    "source_selector": {"source": 0, "type": "audio", "position": 0},
                    "codec": "eac3",
                    "bitrate_kbps": 640,
                }
            ],
            "encode": {
                "video": {
                    "selector": {"source": 0, "type": "video", "position": 0},
                    "codec": "copy",
                }
            },
            "fallback_profile": {"name": "series-fr"},
        },
        require_version=True,
    )


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


def test_validate_job_contract_reports_track_order_paths() -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_job_contract(
            {
                "version": 1,
                "sources": [{"path": "source.mkv"}],
                "output": "out.mkv",
                "track_order": [
                    {"source": "zero"},
                    [0],
                    [0, "audio"],
                    7,
                ],
            },
            require_version=True,
        )

    message = str(excinfo.value)
    assert "track_order[0].source" in message
    assert "track_order[0].id" in message
    assert "track_order[1]" in message
    assert "track_order[2][1]" in message
    assert "track_order[3]" in message


def test_validate_batch_contract_reports_job_paths() -> None:
    with pytest.raises(ContractError) as excinfo:
        validate_batch_contract(
            {
                "jobs": [
                    {"sources": [{"path": "source.mkv"}], "tracks": "bad"},
                    42,
                ]
            }
        )

    message = str(excinfo.value)
    assert "$.jobs[0].tracks" in message
    assert "$.jobs[1]" in message


def test_batch_rejects_json_batch_mixed_with_direct_inputs() -> None:
    with pytest.raises(CliError) as excinfo:
        run_batch(
            template_path="missing-template.json",
            batch_path="batch.json",
            cli_inputs=["a.mkv"],
            input_dirs=None,
            recursive=False,
            include_patterns=None,
            exclude_patterns=None,
            output_dir=None,
            dry_run=True,
            force=False,
            continue_on_error=False,
            summary_path=None,
            config=None,  # type: ignore[arg-type]
            options=None,  # type: ignore[arg-type]
            logger=None,  # type: ignore[arg-type]
        )

    assert "--batch" in str(excinfo.value)
    assert "--input-dir" in str(excinfo.value)


def test_discover_direct_batch_jobs_filters_video_and_preserves_relative_outputs(tmp_path: Path) -> None:
    root = tmp_path / "in"
    nested = root / "Saison 01"
    nested.mkdir(parents=True)
    for path in (
        root / "E00.sample.mkv",
        root / "E01.mkv",
        root / "E01.srt",
        root / "audio.flac",
        nested / "E02.mp4",
        nested / "E03.avi",
    ):
        path.write_bytes(b"")

    non_recursive = discover_direct_batch_jobs(
        input_dirs=[str(root)],
        output_dir=str(tmp_path / "out"),
        recursive=False,
    )
    assert [Path(job["sources"][0]["path"]).name for job in non_recursive.jobs] == [
        "E00.sample.mkv",
        "E01.mkv",
    ]

    recursive = discover_direct_batch_jobs(
        input_dirs=[str(root)],
        output_dir=str(tmp_path / "out"),
        recursive=True,
        include_patterns=["*.mkv", "Saison 01/*.mp4"],
        exclude_patterns=["*sample*"],
    )

    assert [Path(job["sources"][0]["path"]).as_posix().split("/in/")[-1] for job in recursive.jobs] == [
        "E01.mkv",
        "Saison 01/E02.mp4",
    ]
    assert [Path(job["output"]).as_posix().split("/out/")[-1] for job in recursive.jobs] == [
        "E01.mkv",
        "Saison 01/E02.mkv",
    ]
    assert recursive.scanned == 6
    assert recursive.selected == 2


def test_discover_direct_batch_jobs_errors_when_no_video_matches(tmp_path: Path) -> None:
    root = tmp_path / "in"
    root.mkdir()
    (root / "subtitle.srt").write_text("1\n", encoding="utf-8")
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"version": 1, "rules": {}}), encoding="utf-8")

    with pytest.raises(CliError) as excinfo:
        run_batch(
            template_path=str(template),
            batch_path=None,
            cli_inputs=None,
            input_dirs=[str(root)],
            recursive=False,
            include_patterns=None,
            exclude_patterns=None,
            output_dir=None,
            dry_run=True,
            force=False,
            continue_on_error=False,
            summary_path=None,
            config=None,  # type: ignore[arg-type]
            options=None,  # type: ignore[arg-type]
            logger=None,  # type: ignore[arg-type]
        )

    assert "Aucun fichier vidéo compatible" in str(excinfo.value)
