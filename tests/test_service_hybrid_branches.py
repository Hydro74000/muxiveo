"""Branches de ``hybrid.run`` restées sans exécution (couverture résiduelle de l'audit 4effdd1) : arrêt au premier
épisode en échec, annulation avant tout épisode, export des exact-jobs, aperçu d'un remux sans encodage."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.service import ExecutionContext, ListSink
from core.service.ops import batch as batch_ops
from core.service.ops import hybrid as hybrid_ops


def _items(tmp_path: Path, count: int, failing: tuple[int, ...] = ()) -> list[Any]:
    return [SimpleNamespace(error=RuntimeError(f"préparation E{i}") if i in failing else None, calibration=None,
                            remux_config=None if i in failing else SimpleNamespace(output=tmp_path / f"E{i}.mkv"),
                            episode=SimpleNamespace(display_name=f"E{i}", master_file=tmp_path / f"m{i}.mkv",
                                                    donor_files=[]))
            for i in range(count)]


def _run(monkeypatch, tmp_path: Path, ctx: ExecutionContext, items: list[Any], **request: Any):
    spec = SimpleNamespace(common=None, output_template=None)
    monkeypatch.setattr(hybrid_ops, "_prepare", lambda request, ctx, cache=None: (spec, items))
    monkeypatch.setattr(hybrid_ops, "check_outputs", lambda *a, **k: None)
    payload = {"output_dir": str(tmp_path), **request}
    return hybrid_ops.hybrid_run.handler(hybrid_ops.HybridRequest.from_dict(payload), ctx)


def test_first_preparation_failure_stops_without_continue_on_error(monkeypatch, tmp_path):
    ctx = ExecutionContext.create(SimpleNamespace(), ListSink())
    result = _run(monkeypatch, tmp_path, ctx, _items(tmp_path, 3, failing=(0,)), dry_run=True)
    assert [entry["status"] for entry in result.episodes] == ["failed"]  # épisodes suivants non traités
    assert result.status == "failed"


def test_cancellation_before_the_first_episode(monkeypatch, tmp_path):
    ctx = ExecutionContext.create(SimpleNamespace(), ListSink())
    ctx.cancel.cancel()
    result = _run(monkeypatch, tmp_path, ctx, _items(tmp_path, 2), dry_run=True)
    assert result.status == "cancelled" and result.episodes == ()


def test_export_writes_one_exact_job_per_episode_with_the_encode_block(monkeypatch, tmp_path):
    from core.profiles import selectors

    monkeypatch.setattr(selectors, "remux_config_to_exact_job",
                        lambda config: {"version": 1, "output": str(config.output)})
    ctx = ExecutionContext.create(SimpleNamespace(), ListSink(), base_dir=tmp_path)
    exports = tmp_path / "jobs"
    result = _run(monkeypatch, tmp_path, ctx, _items(tmp_path, 2), export_workflow=str(exports),
                  encode={"video": {"default": {"codec": "libx265"}}})
    assert [entry["status"] for entry in result.episodes] == ["exported", "exported"]
    written = sorted(exports.glob("*.exact-job.json"))
    assert [path.name for path in written] == ["E0.exact-job.json", "E1.exact-job.json"]
    document = json.loads(written[0].read_text(encoding="utf-8"))
    assert document["output"].endswith("E0.mkv") and document["encode"]["video"]["default"]["codec"] == "libx265"


def test_dry_run_without_encode_previews_the_remux(monkeypatch, tmp_path):
    seen: list[Any] = []

    def preview(self, config, common, child, remux_config):
        seen.append(remux_config.output)
        return 0

    monkeypatch.setattr(batch_ops._Children, "preview", preview)
    ctx = ExecutionContext.create(SimpleNamespace(), ListSink())
    result = _run(monkeypatch, tmp_path, ctx, _items(tmp_path, 2), dry_run=True)
    assert [entry["status"] for entry in result.episodes] == ["planned", "planned"]
    assert seen == [tmp_path / "E0.mkv", tmp_path / "E1.mkv"]
