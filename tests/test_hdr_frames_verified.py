"""
tests/test_hdr_frames_verified.py — Nombre d'images vérifié (D2, C1, C2, C4 ; plan de remédiation 5.2.0, lot 4).

Règle unique : compte rapide absent ou différent → compte exact ; écriture image par image (HDR10+ recalculé) →
compte exact égal exigé ; encodage à HDR10+ généré → compte source exact ; écart de longueur sur un HDR10+
recalculé → refus ; compte vérifié mémorisé par le cache.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import core.service.ops.hdr_patch as hdr_patch_ops
import ui.panels.hdr_studio.panel as panel_module
from core.hdr_inspection import InspectionCache
from core.hdr_inspection.measure import (
    FrameStatus,
    ImageMeasurement,
    MeasurementError,
    frame_count_check,
    frames_status,
)
from core.hdr_inspection.patch import Hdr10PlusEdit, MetadataPatcher, PatchError, PatchPlan
from core.hdr_inspection.report import report_data
from core.service.catalog import CATALOG
from core.service.errors import ApiError
from core.service import call
from tests.test_cli_hdr_patch import _ctx, _measurement, _report
from tests.test_hdr_studio import _wait


def _m(frames: int, **kwargs: Any) -> ImageMeasurement:
    return ImageMeasurement("base", np.zeros((frames, 3), np.float32), np.zeros(frames, np.float32),
                            np.zeros(frames, np.float32), np.zeros(frames, np.float32),
                            np.zeros((frames, 7), np.float32), np.array([0], np.int32), np.zeros(1024, np.int64),
                            (0, 0, 16, 16), **kwargs)


def _counter(value: int | None, calls: list[int]):
    return lambda: calls.append(1) or value


def test_missing_quick_count_asks_for_the_exact_count():
    """R2 du contre-audit : mesure de 38 images, compte rapide absent → acceptée sans aucun comptage."""
    calls: list[int] = []
    check = frame_count_check(None, _counter(50, calls))
    assert check(_m(38)) is not None and calls == [1]
    assert check(_m(38)) is not None and calls == [1]  # compté une fois


def test_status_without_counting():
    assert frames_status(_m(10), 10) is FrameStatus.QUICK_MATCH
    assert frames_status(_m(10), None) is FrameStatus.UNVERIFIED
    assert frames_status(_m(10, verified_frames=10)) is FrameStatus.VERIFIED
    assert frames_status(_m(10, sampled=3)) is FrameStatus.UNVERIFIED


def test_require_verified_refuses_quick_match_contradicted_and_unreadable():
    with pytest.raises(MeasurementError):
        frame_count_check(38, lambda: 50).require_verified(_m(38))  # compte rapide égal, compte exact différent
    with pytest.raises(MeasurementError):
        frame_count_check(38, lambda: None).require_verified(_m(38))  # compte exact illisible
    assert frame_count_check(38, lambda: 38).require_verified(_m(38)) == 38
    calls: list[int] = []
    assert frame_count_check(None, _counter(0, calls)).require_verified(_m(5, verified_frames=5)) == 5
    assert calls == []  # vérifiée lors d'un enregistrement précédent : aucun comptage


def test_cache_remembers_verified_frames(tmp_path):
    """C4 : compte exact obtenu → mémorisé dans l'entrée ; ancienne entrée : statut recalculé."""
    source = tmp_path / "film.mkv"
    source.write_bytes(b"x" * 16)
    cache = InspectionCache(tmp_path / "cache")
    cache.store_measurement(source, 0, _m(10))
    loaded = cache.load_measurement(source, 0)
    assert loaded is not None and loaded.verified_frames is None
    check = frame_count_check(None, lambda: 10)
    assert check(loaded) is None
    cache.store_measurement(source, 0, check.verified(loaded))
    again = cache.load_measurement(source, 0)
    assert again is not None and again.verified_frames == 10
    calls: list[int] = []
    assert frame_count_check(None, _counter(10, calls))(again) is None and calls == []


def _cached_measurement(tmp_path: Path) -> ImageMeasurement:
    source = tmp_path / "film.mkv"
    source.write_bytes(b"\x1a\x45\xdf\xa3")
    measurement = _measurement(tmp_path)
    InspectionCache(tmp_path / "data" / "hdr_inspection").store_measurement(source, 0, measurement)
    return measurement


def test_hdr_patch_recompute_refused_when_exact_count_differs(tmp_path, monkeypatch):
    """Compte de l'inspection égal à la mesure (10), compte exact 12 → HDR10+ recalculé refusé, rien écrit."""
    measurement = _cached_measurement(tmp_path)
    assert measurement.frames == 10
    monkeypatch.setattr(hdr_patch_ops, "packet_counter", lambda *a, **k: (lambda: 12))
    output = tmp_path / "fix.mkv"
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "hdr.patch", {"source": str(tmp_path / "film.mkv"), "output": str(output),
                                    "hdr10plus": {"recompute": True}, "measurement": "cached"}, _ctx(tmp_path))
    assert re.search(r"\b10\b.*\b12\b", str(caught.value)) and not output.exists()


def test_hdr_patch_recompute_carries_the_verified_count(tmp_path, monkeypatch):
    _cached_measurement(tmp_path)
    monkeypatch.setattr(hdr_patch_ops, "packet_counter", lambda *a, **k: (lambda: 10))
    executed: list[PatchPlan] = []
    monkeypatch.setattr(hdr_patch_ops, "_execute", lambda plan, ctx, logger, warnings=None: executed.append(plan)
                        or plan.output)
    call(CATALOG, "hdr.patch", {"source": str(tmp_path / "film.mkv"), "output": str(tmp_path / "fix.mkv"),
                                "hdr10plus": {"recompute": True}, "measurement": "cached"}, _ctx(tmp_path))
    assert executed and executed[0].expected_frames == 10
    stored = InspectionCache(tmp_path / "data" / "hdr_inspection").load_measurement(tmp_path / "film.mkv", 0)
    assert stored is not None and stored.verified_frames == 10


def test_hdr_patch_static_only_never_counts(tmp_path, monkeypatch):
    """Non-régression : correction statique seule, aucun comptage (mesure en cache concordante)."""
    _cached_measurement(tmp_path)
    counted: list[int] = []
    monkeypatch.setattr(hdr_patch_ops, "packet_counter", lambda *a, **k: (lambda: counted.append(1) or 10))
    call(CATALOG, "hdr.patch", {"source": str(tmp_path / "film.mkv"), "output": str(tmp_path / "fix.mkv"),
                                "light_levels": {"max_cll": 1000, "max_fall": 400}, "dry_run": True},
         _ctx(tmp_path))
    assert counted == []


# ── Exécution du patcher (C2) ───────────────────────────────────────────────


def test_patcher_refuses_recomputed_hdr10plus_of_another_length(tmp_path, monkeypatch):
    from core.hdr_inspection import patch as patch_module

    monkeypatch.setattr(patch_module, "transform_hdr10plus", lambda source, target, edit: 9)
    plan = PatchPlan(tmp_path / "film.hevc", 0, tmp_path / "out.hevc",
                     hdr10plus=Hdr10PlusEdit(recompute=(_m(10), np.array([0]))), expected_frames=10)
    patcher = MetadataPatcher(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool", hdr10plus_bin="hdr10plus_tool",
                              run_cmd=lambda cmd: "", work_dir=tmp_path)
    (tmp_path / "film.hevc").write_bytes(b"\x00\x00\x00\x01\x40\x01" + b"\x0c" * 16)
    with pytest.raises(PatchError, match=r"\b9\b.*\b10\b"):
        patcher.run(plan)
    assert not (tmp_path / "out.hevc").exists()


def test_tool_length_mismatch_is_fatal_for_recomputed_hdr10plus(tmp_path):
    patcher = MetadataPatcher(ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool", hdr10plus_bin="hdr10plus_tool",
                              run_cmd=lambda cmd: "Warning: mismatched lengths. video 12, HDR10+ JSON 10",
                              work_dir=tmp_path)
    with pytest.raises(PatchError):
        patcher._tool(["hdr10plus_tool", "inject"], strict=True)
    warnings: list[str] = []
    patcher._warn = lambda code, message, fields: warnings.append(code)
    patcher._tool(["hdr10plus_tool", "inject"])  # HDR10+ extrait : avertissement seulement
    assert warnings == ["tool_length_mismatch"]


# ── Rapport ─────────────────────────────────────────────────────────────────


def test_report_exposes_frames_status(tmp_path):
    report = _report(tmp_path)
    data = report_data(report, measurement=_measurement(tmp_path))
    assert data["measurement"]["frames_status"] == "quick_match"
    data = report_data(report, measurement=_m(10, verified_frames=10))
    assert json.loads(json.dumps(data))["measurement"]["frames_status"] == "verified"


# ── HDR Studio ──────────────────────────────────────────────────────────────


def _studio(qt_app, tmp_path):
    from core.config import AppConfig
    from ui.panels.hdr_studio import HdrStudio

    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    return HdrStudio(config)


def _close(qt_app, studio) -> None:
    studio.close()
    _wait(qt_app, lambda: getattr(studio, "_shutdown", None) is not None and studio._shutdown.done.is_set(), 2.0)


def test_studio_verifies_frames_at_write_and_refuses_a_mismatch(qt_app, tmp_path, monkeypatch):
    """Onglet Édition : compte exact demandé au lancement de l'écriture (jamais pendant la saisie)."""
    counted: list[int] = []
    monkeypatch.setattr(panel_module, "packet_counter", lambda *a, **k: (lambda: counted.append(1) or 12))
    studio = _studio(qt_app, tmp_path)
    try:
        report = _report(tmp_path)
        measurement = _measurement(tmp_path)
        studio._editor.set_report(report, measurement)
        studio._editor.h10_recompute.setChecked(True)
        studio._editor.output.setText(str(tmp_path / "fix.mkv"))
        studio._editor._sync()
        assert counted == []  # formulaire modifié : aucun comptage
        plan = studio._editor.plan()
        assert plan is not None and plan.writes_per_frame
        with pytest.raises(MeasurementError, match=r"\b10\b.*\b12\b"):
            studio._verified_plan(plan, report, 1, threading.Event())
        assert counted == [1] and not (tmp_path / "fix.mkv").exists()
    finally:
        _close(qt_app, studio)


def test_studio_cancel_during_the_count_writes_nothing(qt_app, tmp_path, monkeypatch):
    from core.fel.engine import FelCancelled

    started = threading.Event()

    def blocking(_ffprobe, _path, _stream, cancel=None):
        def count() -> int:
            started.set()
            assert cancel is not None and cancel.wait(5)
            raise FelCancelled("Comptage des images annulé")
        return count

    monkeypatch.setattr(panel_module, "packet_counter", blocking)
    studio = _studio(qt_app, tmp_path)
    try:
        report = _report(tmp_path)
        studio._editor.set_report(report, _measurement(tmp_path))
        studio._editor.h10_recompute.setChecked(True)
        studio._editor.output.setText(str(tmp_path / "fix.mkv"))
        studio._editor._sync()
        plan = studio._editor.plan()
        assert plan is not None
        cancel = threading.Event()
        result: list[Any] = []
        worker = threading.Thread(target=lambda: result.append(studio._verified_plan(plan, report, 1, cancel)))
        worker.start()
        assert started.wait(5)
        cancel.set()
        worker.join(5)
        assert result == [None] and not (tmp_path / "fix.mkv").exists()
    finally:
        _close(qt_app, studio)


# ── Encodage à HDR10+ généré (C1) ───────────────────────────────────────────


class _Guard:
    """Garde de comptage dont les lecteurs sont simulés (compte rapide, paquets)."""

    def __init__(self, quick: int, packets: int) -> None:
        from core.workflows.encode.runtime.frame_count_guard import FrameCountGuard

        self.guard = FrameCountGuard()
        self.quick, self.packets = quick, packets
        self.guard._read_video_frame_count = lambda path: self.quick  # type: ignore[method-assign]
        self.guard._recount_video_frames = lambda path: self.packets  # type: ignore[method-assign]
        self.guard._hdr10p_json_frame_count = lambda path: 38  # type: ignore[method-assign]


@pytest.mark.parametrize("stream", [None, 0])
def test_generated_hdr10plus_audit_counts_source_packets_on_the_main_track(tmp_path, stream):
    """38 images encodées, HDR10+ généré de 38 entrées, source de 50 paquets (compte rapide 38) → refus."""
    from core.workflows.encode.runtime.frame_count_guard import FrameCountAuditError, MetadataAdjustment

    double = _Guard(quick=38, packets=50)
    json_path = tmp_path / "hdr10plus.json"
    json_path.write_text("{}", encoding="utf-8")
    audit = double.guard.audit(source=tmp_path / "s.mkv", encoded=tmp_path / "e.hevc", hdr10p_json=json_path,
                               known_encoded_frames=38, source_stream_index=stream, exact_source=True)
    assert audit.source == 50 and audit.source_basis == "exact"
    with pytest.raises(FrameCountAuditError):
        double.guard.enforce(audit, adjustment=MetadataAdjustment.TRIM_TAIL, hdr10p_json=json_path)


def test_extracted_metadata_shortcut_unchanged(tmp_path):
    """Non-régression : HDR10+ extrait de la source égal au flux encodé → raccourci « metadata » (source non relue)."""
    double = _Guard(quick=38, packets=50)
    json_path = tmp_path / "hdr10plus.json"
    json_path.write_text("{}", encoding="utf-8")
    audit = double.guard.audit(source=tmp_path / "s.hevc", encoded=tmp_path / "e.hevc", hdr10p_json=json_path,
                               known_encoded_frames=38)
    assert audit.source_basis == "metadata" and audit.source == 38


def test_generated_hdr10plus_audit_on_a_secondary_track_counts_its_packets(tmp_path, monkeypatch):
    """Non-régression : piste secondaire, paquets comptés (déjà le cas) — mode exact identique."""
    from core.workflows.encode.runtime import frame_count_guard as guard_module

    monkeypatch.setattr(guard_module, "ffprobe_packet_count", lambda *a, **k: 50)
    double = _Guard(quick=38, packets=50)
    json_path = tmp_path / "hdr10plus.json"
    json_path.write_text("{}", encoding="utf-8")
    audit = double.guard.audit(source=tmp_path / "s.mkv", encoded=tmp_path / "e.hevc", hdr10p_json=json_path,
                               known_encoded_frames=38, source_stream_index=2, exact_source=True)
    assert audit.source == 50
