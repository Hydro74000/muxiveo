"""Calibrations de synchro (calibration.show/edit/compose/invert), forme d'onde et commandes CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from cli.calibration import add_calibration_parsers, build_calibration_request, parse_time_ms
from core.jobs.errors import JobError
from core.service import ApiError, ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.workflows.sync_calibration import SyncCalibration, SyncSegment


def _ctx() -> ExecutionContext:
    return ExecutionContext.create(SimpleNamespace(tool_ffmpeg="ffmpeg", tool_ffprobe="ffprobe"), ListSink())


def _file(tmp_path: Path, calibration: SyncCalibration, name: str = "c.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(calibration.to_dict()), encoding="utf-8")
    return path


def test_segment_shift_moves_following_segments():
    calibration = SyncCalibration((SyncSegment(0, 100), SyncSegment(60000, 300), SyncSegment(90000, 250)))
    moved = calibration.with_segment_shift(1, 200)
    assert [s.shift_ms for s in moved.segments] == [100, 200, 150]
    with pytest.raises(ValueError):
        calibration.with_segment_shift(3, 0)


def test_show_and_edit_in_order(tmp_path):
    source = _file(tmp_path, SyncCalibration.linear(-400))
    shown: Any = call(CATALOG, "calibration.show", {"calibration": {"file": str(source)}}, _ctx())
    assert shown.cuts == 0 and shown.segments[0]["shift_ms"] == -400 and shown.summary
    out = tmp_path / "edit.json"
    edited: Any = call(CATALOG, "calibration.edit", {
        "calibration": {"file": str(source)}, "output": str(out),
        "edits": [{"cut": [10000, 12000]}, {"shift_from_segment": {"index": 1, "shift_ms": -2500}},
                  {"shift_ms": -350}]}, _ctx())
    assert edited.file == str(out) and edited.cuts == 1
    assert [s["shift_ms"] for s in edited.segments] == [-350, -2450]
    assert SyncCalibration.from_dict(json.loads(out.read_text())).segments == \
        SyncCalibration.from_dict(edited.calibration).segments
    removed: Any = call(CATALOG, "calibration.edit", {"calibration": {"value": edited.calibration},
                                                      "edits": [{"remove_junction": 1}]}, _ctx())
    assert removed.cuts == 0


@pytest.mark.parametrize("payload, error", [
    ({"edits": [{"cut": [5, 1]}]}, JobError),
    ({"edits": [{"cut": [1, 5], "shift_ms": 3}]}, ApiError),
    ({"edits": [{"cadence_method": "atempo"}]}, JobError),
    ({"edits": []}, ApiError),
])
def test_edit_refusals(tmp_path, payload, error):
    with pytest.raises(error):
        call(CATALOG, "calibration.edit", {"calibration": {"value": SyncCalibration.linear(0).to_dict()}, **payload},
             _ctx())


def test_compose_invert_and_existing_output(tmp_path):
    inner, outer = _file(tmp_path, SyncCalibration.linear(-300), "i.json"), _file(tmp_path, SyncCalibration.linear(-400), "o.json")
    composed: Any = call(CATALOG, "calibration.compose", {"inner": {"file": str(inner)}, "outer": {"file": str(outer)}},
                         _ctx())
    assert composed.segments[0]["shift_ms"] == -700
    inverted: Any = call(CATALOG, "calibration.invert", {"calibration": {"file": str(outer)}}, _ctx())
    assert inverted.segments[0]["shift_ms"] == 400
    with pytest.raises(JobError) as caught:
        call(CATALOG, "calibration.invert", {"calibration": {"file": str(outer)}, "output": str(inner)}, _ctx())
    assert caught.value.exit_code == 5
    with pytest.raises(ApiError):
        call(CATALOG, "calibration.show", {"calibration": {}}, _ctx())


def _args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_calibration_parsers(parser.add_subparsers())
    return parser.parse_args(list(argv))


def test_cli_requests_keep_the_edit_order():
    assert parse_time_ms("1:02:03.5") == 3723500 and parse_time_ms("1500") == 1500
    name, request = build_calibration_request(_args(
        "calibration", "edit", "c.json", "--silence", "1:00-1:05", "--cut", "600000-602500",
        "--shift-from-segment", "2:-120", "--remove-junction", "1", "--cadence-method", "auto",
        "--resolve-cadence", "ref.mkv:1", "cible.mkv:2", "-o", "o.json"))
    assert name == "calibration.edit" and request["output"] == "o.json"
    assert request["edits"] == [{"silence": [60000.0, 65000.0]}, {"cut": [600000.0, 602500.0]},
                                {"shift_from_segment": {"index": 2, "shift_ms": -120.0}}, {"remove_junction": 1},
                                {"cadence_method": "auto"}]
    assert request["resolve_cadence"] == [{"path": "ref.mkv", "stream": 1}, {"path": "cible.mkv", "stream": 2}]
    for argv in (("calibration", "edit", "c.json"), ("calibration", "edit", "c.json", "--cut", "5"),
                 ("calibration", "edit", "c.json", "--shift-from-segment", "x")):
        with pytest.raises(JobError):
            build_calibration_request(_args(*argv))
    assert build_calibration_request(_args("calibration", "compose", "a.json", "b.json"))[0] == "calibration.compose"


def test_waveform_peaks(monkeypatch):
    from core.workflows import sync_waveform

    samples = np.array([0.1, -0.8, 0.2, 0.3] * 40, dtype="<f4")  # 160 échantillons à 8 kHz = 20 ms
    seen: list[list[str]] = []

    def fake_run(command, **_kwargs):
        seen.append(command)
        return SimpleNamespace(returncode=0, stdout=samples.tobytes(), stderr=b"")

    monkeypatch.setattr(sync_waveform, "run_cancellable_capture", fake_run)
    result: Any = call(CATALOG, "sync.waveform", {"source": "/a.mkv", "stream": "3", "start_s": 5, "duration_s": 0.5,
                                                  "resolution_ms": 10}, _ctx())
    assert result.peaks == (0.8, 0.8) and seen[0][seen[0].index("-map") + 1] == "0:3"
    assert sync_waveform.stream_map("a:1") == "0:a:1" and sync_waveform.stream_map("0:a:2") == "0:a:2"
