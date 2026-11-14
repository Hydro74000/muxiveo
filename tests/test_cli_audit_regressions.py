"""Correctifs de l'audit du chantier CLI ↔ API (export / aperçu avec profil, priorités, états, annulation…)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from cli.errors import CliError
from cli.parser import build_parser
from core.service import ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.ops import profile as profile_ops


def _ctx(**config: Any) -> ExecutionContext:
    return ExecutionContext.create(SimpleNamespace(**config), ListSink())


# --- Groupe 1 : export et aperçu avec profil -----------------------------------------------------


def test_profile_apply_exports_instead_of_running(monkeypatch, tmp_path):
    remux = SimpleNamespace(output=tmp_path / "film.mkv")
    from core.jobs.options import CommonOptions

    monkeypatch.setattr(profile_ops, "_build", lambda request, ctx, preview: (remux, {}, CommonOptions()))
    monkeypatch.setattr(profile_ops, "run_remux", lambda *a, **k: pytest.fail("aucun remux attendu"))
    monkeypatch.setattr("core.profiles.selectors.remux_config_to_exact_job", lambda config: {"version": 1})
    destination = tmp_path / "job.json"
    result: Any = call(CATALOG, "profile.apply", {"profile": "P", "inputs": [str(tmp_path / "a.mkv")],
                                                  "export_workflow": str(destination),
                                                  "encode": {"video": {"default": {"codec": "libx265"}}}}, _ctx())
    assert result.status == "exported" and result.output == str(destination)
    assert json.loads(destination.read_text(encoding="utf-8"))["encode"]["video"]["default"]["codec"] == "libx265"


def test_cli_profile_apply_forwards_export_workflow(monkeypatch, tmp_path):
    from cli import profile as cli_profile
    from cli.options import common_options

    seen: dict[str, Any] = {}
    monkeypatch.setattr(cli_profile, "run_operation", lambda name, payload, **kwargs: (
        seen.update(name=name, payload=payload) or (SimpleNamespace(), SimpleNamespace(events=SimpleNamespace(
            job="j")))))
    args = build_parser().parse_args(["run", "--profile", "P", "-i", "a.mkv", "-o", "b.mkv",
                                      "--export-workflow", str(tmp_path / "job.json")])
    cli_profile.profile_apply("P", inputs=["a.mkv"], output="b.mkv", force=False, dry_run=False,
                              config=SimpleNamespace(), options=common_options(args),  # type: ignore[arg-type]
                              logger=SimpleNamespace(fmt="text"))  # type: ignore[arg-type]
    assert seen["name"] == "profile.apply" and seen["payload"]["export_workflow"] == str(tmp_path / "job.json")


def test_previews_with_profile_are_refused():
    from cli.commands import cmd_run

    args = build_parser().parse_args(["encode", "--profile", "P", "-i", "a.mkv", "-o", "b.mkv",
                                      "--preview-image", "10"])
    with pytest.raises(CliError, match="aperçus") as caught:
        cmd_run(args, SimpleNamespace(), SimpleNamespace(fmt="text"))  # type: ignore[arg-type]
    assert caught.value.exit_code == 2
