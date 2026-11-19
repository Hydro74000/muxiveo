"""Documents et sorties des opérations sans écrasement non autorisé, même entre appels concurrents (audit A01, B02)."""

from __future__ import annotations

import concurrent.futures
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.jobs.errors import JobError
from core.service import ExecutionContext, ListSink, call
from core.service import documents
from core.service.catalog import CATALOG
from core.service.errors import ApiError
from core.service.ops.encode import _deliver
from core.workflows.sync_calibration import SyncCalibration


def _ctx(root: Path) -> ExecutionContext:
    return ExecutionContext.create(SimpleNamespace(app_data_dir=root, profiles_dir=root), ListSink(),
                                   base_dir=root, allow_privileged=True)


def _race(monkeypatch, root: Path, operation: str, requests: list[dict[str, Any]]) -> list[object]:
    """Deux appels qui ont tous deux passé leur pré-contrôle d'existence avant d'écrire."""
    barrier = threading.Barrier(2, timeout=10)
    original = documents.publish_document

    def together(*args: Any, **kwargs: Any) -> Path:
        barrier.wait()
        return original(*args, **kwargs)

    monkeypatch.setattr(documents, "publish_document", together)

    def one(request: dict[str, Any]) -> object:
        try:
            return call(CATALOG, operation, request, _ctx(root))
        except (JobError, ApiError) as exc:
            return exc

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        return [future.result(timeout=20) for future in [pool.submit(one, request) for request in requests]]


def _refused(results: list[object]) -> list[object]:
    return [item for item in results if isinstance(item, (JobError, ApiError))]


def test_concurrent_calibration_exports_keep_first_document(tmp_path, monkeypatch):
    target = tmp_path / "calibration.json"
    requests = [{"calibration": {"value": SyncCalibration.linear(offset).to_dict()}, "output": str(target),
                 "force": False} for offset in (111, 222)]
    results = _race(monkeypatch, tmp_path, "calibration.invert", requests)
    refused = _refused(results)
    assert len(refused) == 1 and getattr(refused[0], "code", None) in ("output_exists", "busy")
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written in [SyncCalibration.linear(-offset).to_dict() for offset in (111, 222)]
    assert not list(tmp_path.glob("*.partial"))


def test_concurrent_preset_exports_keep_first_document(tmp_path, monkeypatch):
    for name in ("audit-A", "audit-B"):
        call(CATALOG, "encode_preset.save", {"preset": {"name": name}}, _ctx(tmp_path))
    export = tmp_path / "export.json"
    requests = [{"name": name, "file": str(export), "force": False} for name in ("audit-A", "audit-B")]
    results = _race(monkeypatch, tmp_path, "encode_preset.export", requests)
    assert len(_refused(results)) == 1
    assert json.loads(export.read_text(encoding="utf-8"))["name"] in ("audit-A", "audit-B")


def test_concurrent_subtitle_shifts_keep_first_document(tmp_path, monkeypatch):
    source = tmp_path / "in.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nbonjour\n", encoding="utf-8")
    output = tmp_path / "out.srt"
    requests = [{"input": str(source), "output": str(output), "offset_ms": offset} for offset in (250, 500)]
    results = _race(monkeypatch, tmp_path, "sync.shift_subtitles", requests)
    assert len(_refused(results)) == 1
    assert output.read_text(encoding="utf-8").startswith("1\n00:00:01,")


def test_subtitle_shift_force_replaces_existing(tmp_path):
    source = tmp_path / "in.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nbonjour\n", encoding="utf-8")
    output = tmp_path / "out.srt"
    output.write_text("ancien", encoding="utf-8")
    with pytest.raises(JobError):
        call(CATALOG, "sync.shift_subtitles", {"input": str(source), "output": str(output), "offset_ms": 250},
             _ctx(tmp_path))
    assert output.read_text(encoding="utf-8") == "ancien"
    call(CATALOG, "sync.shift_subtitles", {"input": str(source), "output": str(output), "offset_ms": 250,
                                           "force": True}, _ctx(tmp_path))
    assert "00:00:01,250" in output.read_text(encoding="utf-8")


def test_concurrent_preview_deliveries_get_distinct_names(tmp_path):
    out = tmp_path / "captures"
    out.mkdir()
    sources = []
    for index in range(2):
        source = tmp_path / f"rendu{index}.png"
        source.write_bytes(f"image {index}".encode())
        sources.append(source)
    barrier = threading.Barrier(2, timeout=10)

    def deliver(source: Path) -> Path:
        barrier.wait()
        return _deliver(source, out, "film.apercu-12.0s.png")

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        delivered = list(pool.map(deliver, sources))
    assert len(set(delivered)) == 2
    assert sorted(path.read_bytes() for path in delivered) == [b"image 0", b"image 1"]
    assert not any(source.exists() for source in sources)  # originaux retirés après publication seulement


def test_hdr_patch_output_created_during_inspection_is_preserved(tmp_path):
    from tests.test_cli_hdr_patch import _ctx as hdr_ctx
    from tests.test_cli_hdr_patch import _info

    source, output = tmp_path / "film.mkv", tmp_path / "fix.mkv"
    source.write_bytes(b"\0" * 1024)

    def inspect(path: Path) -> Any:
        output.write_bytes(b"fichier tiers")  # apparu pendant la sonde initiale, après le pré-contrôle
        return _info(Path(path))

    ctx = hdr_ctx(tmp_path, file_inspector=lambda **_k: SimpleNamespace(inspect=inspect))
    payload = {"source": str(source), "output": str(output), "light_levels": {"max_cll": 1000, "max_fall": 400}}
    with pytest.raises(JobError) as caught:
        call(CATALOG, "hdr.patch", payload, ctx)
    assert caught.value.exit_code == 5
    assert output.read_bytes() == b"fichier tiers"
