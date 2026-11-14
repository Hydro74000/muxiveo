"""Correctifs de l'audit du chantier CLI ↔ API (export / aperçu avec profil, priorités, états, annulation…)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli.errors import CliError
from core.jobs.errors import JobError
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


# --- Groupe 2 : priorité des options, ordre des synchros ------------------------------------------


def test_cli_encode_options_override_per_track_settings():
    from core.jobs.batch import merge_encode_block

    job = {"encode": {"video": {"tracks": [{"select": {"source": 0}, "crf": 18, "codec": "libx265",
                                            "restoration": {"enabled": True, "mode": "fast"}}]}}}
    merged = merge_encode_block(job, {"video": {"default": {"crf": 28, "restoration": {"mode": "normal"}}}})
    track = merged["encode"]["video"]["tracks"][0]
    assert track == {"select": {"source": 0}, "codec": "libx265", "restoration": {"enabled": True}}
    assert merged["encode"]["video"]["default"]["crf"] == 28


def _source(index: int, *types: str) -> Any:
    from core.workflows.remux_models import SourceInput, TrackEntry

    tracks = [TrackEntry(mkv_tid=tid, track_type=kind, codec="x", display_info="", language="fre", title="")
              for tid, kind in enumerate(types)]
    return SourceInput(path=Path(f"/s{index}.mkv"), file_index=index, tracks=tracks)


def test_source_linked_to_a_track_with_its_own_sync_follows_it():
    from core.jobs.remux_config import apply_donor_calibration
    from core.jobs.source_sync import resolve_syncs

    sources = [_source(0, "video", "audio"), _source(1, "audio", "subtitle"), _source(2, "audio")]
    job: dict[str, Any] = {"sync_mode": "container", "sync_subtitles": "none"}

    def apply(calibration, index):
        job["sync_mode"], job["sync_calibrations"] = apply_donor_calibration(
            calibration, sync_mode=job["sync_mode"], sources=sources, sync_subtitles="none",
            sync_calibrations=job.get("sync_calibrations"), source_index=index)

    track_pairs = [({"sync": {"reference": 0, "offset_ms": 1000}}, sources[1].tracks[0])]
    specs = {2: {"reference": 1, "reference_track": "a:0", "offset_ms": 200}}
    logger = SimpleNamespace(emit=lambda *a, **k: None)
    resolve_syncs(job, sources, specs, track_pairs, config=None, options=None, logger=logger, apply=apply)
    assert sources[1].tracks[0].time_shift_ms == 1000 and sources[2].tracks[0].time_shift_ms == 1200
    # Boucle entre une piste et une source : refusée.
    loop_pairs = [({"sync": {"reference": 2}}, sources[1].tracks[0])]
    with pytest.raises(JobError, match="boucle"):
        resolve_syncs(job, sources, specs, loop_pairs, config=None, options=None, logger=logger, apply=apply)
