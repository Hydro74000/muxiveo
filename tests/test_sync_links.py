"""Liens de synchro en chaîne (core.workflows.sync_links), contrat des blocs ``sync`` d'un job."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.jobs.contract import validate_job_contract
from core.jobs.errors import JobError
from core.jobs.source_sync import sync_specs, validate_sync_spec
from core.workflows.sync_calibration import SyncCalibration, SyncSegment
from core.workflows.sync_links import (
    SyncLink,
    SyncLoopError,
    dependency_order,
    depends_on,
    follows_track,
    is_negligible,
    measure_track,
    propagate,
)


def _link(reference: str, entry: str = "", offset: float = 0.0) -> SyncLink:
    return SyncLink(reference, entry, SyncCalibration.linear(offset))


def test_dependencies_and_loops():
    links = {"b": _link("a", "a-audio"), "c": _link("b", "b-audio")}
    assert dependency_order(links) == ["b", "c"]
    assert depends_on(links, "c", "a") and not depends_on(links, "a", "c")
    assert follows_track(links, "c", "a-audio") and not follows_track(links, "b", "b-audio")
    with pytest.raises(SyncLoopError, match="a → b → a|b → a → b"):
        dependency_order({"a": _link("b"), "b": _link("a")})
    with pytest.raises(SyncLoopError):
        dependency_order({"a": _link("a")})


def test_propagation_follows_the_reference_in_cascade():
    links = {"b": _link("a", offset=-400), "c": _link("b", offset=-300)}
    applied = {"a": SyncCalibration.linear(100), "b": SyncCalibration.linear(0), "c": SyncCalibration.linear(0)}

    def apply(file_id, calibration, negligible):
        applied[file_id] = SyncCalibration.linear(0) if negligible else calibration

    updates = propagate(links, {"a"}, reference_position=lambda link: applied[link.reference_file_id],
                        current=lambda file_id: applied[file_id], apply=apply)
    assert updates == [("b", "a", -300), ("c", "b", -600)]
    assert propagate(links, {"a"}, reference_position=lambda link: applied[link.reference_file_id],
                     current=lambda file_id: applied[file_id], apply=apply) == []
    assert is_negligible(SyncCalibration.linear(4)) and not is_negligible(SyncCalibration.linear(6))
    assert not is_negligible(SyncCalibration((SyncSegment(0, 0), SyncSegment(1000, 3))))


def test_measure_track_prefers_the_reference_language():
    def track(file_id, language, kind="audio", commentary=False):
        return SimpleNamespace(file_id=file_id, track_type=kind, language=language, flag_commentary=commentary)

    target = track("b", "eng")
    french = track("b", "fre")
    tracks = [target, track("b", "fre", commentary=True), french, track("c", "fre")]
    assert measure_track(target, track("a", "fra"), tracks) is french
    assert measure_track(target, track("a", "eng"), tracks) is target
    assert measure_track(target, track("a", "ger"), tracks) is target
    assert measure_track(target, track("a", "fra"), tracks, usable=lambda t: t is not french) is target


def test_job_contract_and_specs():
    errors: list[str] = []
    validate_sync_spec(errors, "s", {"reference": -1, "mode": "x", "track": True, "calibration": 3, "nope": 1})
    assert len(errors) == 5
    errors = []
    validate_sync_spec(errors, "t", {"track": "a:0"}, track_level=True)
    assert errors == ["t.track: champ inconnu"]
    errors = []
    validate_sync_spec(errors, "s", {"offset_ms": 5, "calibration": {"segments": [{"start_ms": 0, "shift_ms": 1}]}})
    assert errors == ["s: calibration et offset_ms s'excluent"]
    job = {"version": 1, "sources": ["a.mkv", {"path": "b.mkv", "sync": {"reference": 0, "mode": "deep"}}, "c.mkv"]}
    validate_job_contract(job)
    with pytest.raises(JobError):
        validate_job_contract({"version": 1, "sources": [{"path": "a", "sync": {"mode": "?"}}]})
    assert sync_specs(job) == {1: {"reference": 0, "mode": "deep"}}
    assert sync_specs(job, auto_sync_all=True) == {1: {"reference": 0, "mode": "deep"}, 2: {"reference": 0}}
