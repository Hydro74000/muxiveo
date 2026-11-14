"""Aide du CLI : couverture des options et des commandes, exemples valides, traductions, référence à jour."""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

import pytest

from cli.help import FAMILIES, render_markdown, walk
from cli.parser import build_parser

ROOT = Path(__file__).resolve().parents[1]


def _commands():
    return list(walk(build_parser()))


def test_every_option_and_command_is_documented():
    parser = build_parser()
    undocumented = [
        (" ".join(path), action.option_strings or action.dest)
        for path, sub in walk(parser) for action in sub._actions
        if not isinstance(action, (argparse._HelpAction, argparse._SubParsersAction))
        and action.help is not argparse.SUPPRESS and not (action.help or "").strip()
    ]
    assert undocumented == []
    assert [" ".join(path) for path, sub in walk(parser) if not (sub.description or "").strip()] == []
    without_examples = [" ".join(path) for path, sub in walk(parser) if not (sub.epilog or "").startswith("Exemples")]
    assert without_examples == []
    listed = {name for _family, names in FAMILIES for name in names}
    assert {path[0] for path, _sub in walk(parser)} == listed


def test_examples_parse():
    parser = build_parser()
    for path, sub in walk(parser):
        for line in (sub.epilog or "").splitlines()[1:]:
            command = line.strip().removeprefix("muxiveo --cli ").split(" > ")[0].split(" < ")[0]
            if not command:
                continue
            try:
                args = parser.parse_args(shlex.split(command))
            except SystemExit as exc:  # pragma: no cover - message utile en cas d'échec
                pytest.fail(f"Exemple invalide ({' '.join(path)}) : {command} [{exc}]")
            # Règles vérifiées par les commandes elles-mêmes (après argparse).
            if args.command == "batch":
                assert args.template or args.profile, f"batch sans --template ni --profile : {command}"


def test_help_strings_are_translated():
    catalog = json.loads((ROOT / "locales.json").read_text(encoding="utf-8"))
    parser = build_parser()
    texts = {sub.description for _path, sub in walk(parser)}
    for _path, sub in walk(parser):
        for action in sub._actions:
            if isinstance(action, argparse._HelpAction):
                continue
            if isinstance(action, argparse._SubParsersAction):
                texts |= {choice.help for choice in action._choices_actions if choice.help}
            elif action.help and action.help is not argparse.SUPPRESS:
                texts.add(action.help.replace("%%", "%"))
        texts |= {group.title for group in sub._action_groups if group.title}
    missing = sorted(text for text in texts if text and text not in catalog)
    assert missing == []
    assert all(catalog[text].get("eng") for text in texts if text)


def test_reference_markdown_is_up_to_date():
    expected = render_markdown(build_parser(), language="fra")
    assert (ROOT / "docs" / "cli" / "reference.md").read_text(encoding="utf-8") == expected, \
        "docs/cli/reference.md périmée : muxiveo --cli help --format md > docs/cli/reference.md"


def test_documented_examples_are_valid():
    from core.jobs.contract import validate_job_contract
    from core.profiles.decision import validate_decision_profile
    from core.service.ops.admin import ConfigSetRequest
    from core.service.ops.calibration import CalibrationEditRequest
    from core.service.ops.extract import ExtractRequest
    from core.service.ops.hdr_patch import HdrPatchRequest
    from core.workflows.hybrid_matrix import HybridRecipe

    examples = ROOT / "docs" / "cli" / "examples"
    validators = {
        "extract.json": ExtractRequest.from_dict, "hdr-patch.json": HdrPatchRequest.from_dict,
        "calibration-edit.json": CalibrationEditRequest.from_dict, "hybrid-recipe.json": HybridRecipe.from_dict,
        "sync-links.json": validate_job_contract, "config-set.json": ConfigSetRequest.from_dict,
        "profile.json": validate_decision_profile,
    }
    for path in sorted(examples.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        validator = validators.get(path.name)
        if validator is not None:
            validator(document)
        elif "version" in document or "sources" in document:
            validate_job_contract(document)
        if "sources" in document:  # job : contrat strict de l'API (clés, bloc encode)
            from core.jobs.schema import unknown_job_keys
            from core.service.schemas.encode import EncodeBlock

            assert unknown_job_keys(document) == [], path.name
            if "encode" in document:
                EncodeBlock.from_dict(document["encode"])


def test_help_command_and_english_rendering(capsys):
    from cli.help import cmd_help

    args = build_parser().parse_args(["help", "extract", "--language", "eng"])
    assert cmd_help(args, None, None) == 0
    out = capsys.readouterr().out
    assert "Extract" in out and "Examples:" in out and "(default: text)" in out
