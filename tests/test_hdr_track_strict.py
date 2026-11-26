"""
tests/test_hdr_track_strict.py — Piste vidéo demandée stricte (D1, C3 ; plan de remédiation 5.2.0, lot 2).

Un index ffprobe absent est refusé (``invalid_request``, CLI code 2) avant toute analyse, jamais remplacé par une
autre piste ; B d'une comparaison : index de A s'il existe dans B, sinon première piste de B avec avertissement,
``compare_track`` explicite strict.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

import cli.hdr as cli_hdr
import ui.panels.hdr_studio.panel as panel_module
from cli.constants import EXIT_ARGS
from cli.logging import Logger
from cli.parser import build_parser
from core.config import AppConfig
from core.hdr_inspection import TrackNotFoundError, build_snapshot
from core.service import ExecutionContext, ListSink, call
from core.service.catalog import CATALOG
from core.service.errors import ApiError, ErrorCode
from tests.test_hdr_inspection import _info, _video
from tests.test_hdr_studio import _wait


def _multi(path: Path, *indices: int):
    info = _info(path)
    info.video_tracks = [_video(index=index, side_data=info.video_tracks[0].raw["side_data_list"])
                         for index in indices]
    return info


class _Inspector:
    """Sonde factice : pistes vidéo par nom de fichier."""

    tracks: dict[str, tuple[int, ...]] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def inspect(self, path):
        return _multi(Path(path), *self.tracks.get(Path(path).name, (0,)))


def _media(path: Path) -> Path:
    path.write_bytes(b"\x1a\x45\xdf\xa3")
    return path


def test_build_snapshot_refuses_a_missing_index():
    """R1 du contre-audit : la piste 99 retenait la piste 0."""
    info = _multi(Path("x.mkv"), 0, 1)
    with pytest.raises(TrackNotFoundError) as caught:
        build_snapshot(info, 99)
    assert caught.value.available == (0, 1) and "#99" in str(caught.value) and "#0, #1" in str(caught.value)
    assert build_snapshot(info, 1).stream_index == 1 and build_snapshot(info).stream_index == 0


def _ctx(tmp_path: Path, sink: ListSink | None = None, **ports: Any) -> ExecutionContext:
    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    return ExecutionContext.create(config, sink or ListSink(), ports={"file_inspector": _Inspector, **ports},
                                   base_dir=tmp_path)


def test_hdr_inspect_missing_track_is_invalid_request(tmp_path):
    _Inspector.tracks = {"a.mkv": (0, 1)}
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "hdr.inspect", {"inputs": [str(_media(tmp_path / "a.mkv"))], "level": "static", "track": 99},
             _ctx(tmp_path))
    assert caught.value.code is ErrorCode.INVALID_REQUEST


def test_cli_hdr_track_missing_exits_2(tmp_path, monkeypatch, capsys):
    _Inspector.tracks = {"a.mkv": (0,)}
    monkeypatch.setattr(cli_hdr, "FileInspector", _Inspector)
    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    args = build_parser().parse_args(["inspect", str(_media(tmp_path / "a.mkv")), "--hdr", "static",
                                      "--hdr-track", "99", "--work-dir", str(tmp_path / "work")])
    with pytest.raises(ApiError) as caught:
        args.func(args, config, Logger())
    assert caught.value.code is ErrorCode.INVALID_REQUEST and capsys.readouterr().out == ""


def test_hdr_patch_missing_track_refused_before_any_output(tmp_path):
    _Inspector.tracks = {"film.mkv": (0,)}
    source = _media(tmp_path / "film.mkv")
    output = tmp_path / "fix.mkv"
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "hdr.patch", {"source": str(source), "output": str(output), "track": 99,
                                    "light_levels": {"max_cll": 1000, "max_fall": 400}}, _ctx(tmp_path))
    assert caught.value.code is ErrorCode.INVALID_REQUEST and not output.exists()
    from core.jobs.errors import EXIT_BY_ERROR
    assert EXIT_BY_ERROR[caught.value.code] == EXIT_ARGS


def _compare(tmp_path: Path, sink: ListSink, **request: Any):
    a, b = _media(tmp_path / "a.mkv"), _media(tmp_path / "b.mkv")
    return call(CATALOG, "hdr.inspect", {"inputs": [str(a), str(b)], "level": "static", "compare": True,
                                         **request}, _ctx(tmp_path, sink))


def test_compare_b_without_a_index_takes_first_track_with_warning(tmp_path):
    _Inspector.tracks = {"a.mkv": (0, 1), "b.mkv": (0,)}
    sink = ListSink()
    result: Any = _compare(tmp_path, sink, track=1)
    assert [side["source"]["stream_index"] for side in result.hdr] == [1, 0]
    assert any(event.type.value == "warning" and "#1 absente de B" in str(event.data) for event in sink.events)


def test_compare_b_keeps_a_index_when_present(tmp_path):
    """Non-régression : l'index de A présent dans B reste comparé, sans avertissement."""
    _Inspector.tracks = {"a.mkv": (0, 1), "b.mkv": (0, 1, 2)}
    sink = ListSink()
    result: Any = _compare(tmp_path, sink, track=1)
    assert [side["source"]["stream_index"] for side in result.hdr] == [1, 1]
    assert not any(event.type.value == "warning" for event in sink.events)


def test_explicit_compare_track_missing_refused_before_analysing_a(tmp_path):
    _Inspector.tracks = {"a.mkv": (0,), "b.mkv": (0,)}
    analysed: list[Any] = []
    a, b = _media(tmp_path / "a.mkv"), _media(tmp_path / "b.mkv")
    ctx = _ctx(tmp_path, dynamic_report=lambda *args, **kwargs: analysed.append(args))
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "hdr.inspect", {"inputs": [str(a), str(b)], "compare": True, "compare_track": 5}, ctx)
    assert caught.value.code is ErrorCode.INVALID_REQUEST and analysed == []
    sink = ListSink()
    result: Any = _compare(tmp_path, sink, compare_track=0)
    assert [side["source"]["stream_index"] for side in result.hdr] == [0, 0]


def test_compare_track_requires_compare(tmp_path):
    """Non-régression : ``compare_track`` sans ``compare`` est refusé (``invalid_request``)."""
    _Inspector.tracks = {"a.mkv": (0,)}
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "hdr.inspect", {"inputs": [str(_media(tmp_path / "a.mkv"))], "compare_track": 0},
             _ctx(tmp_path))
    assert caught.value.code is ErrorCode.INVALID_REQUEST


def test_studio_requested_track_missing_shows_error(qt_app, tmp_path, monkeypatch):
    from ui.panels.hdr_studio import HdrStudio

    _Inspector.tracks = {"film.mkv": (0, 1)}
    monkeypatch.setattr(panel_module, "FileInspector", _Inspector)
    config = AppConfig()
    config.app_data_dir = tmp_path / "appdata"
    studio = HdrStudio(config)
    messages: list[tuple[str, str]] = []
    studio.log_message.connect(lambda level, text: messages.append((level, text)))
    try:
        studio.load_source(_media(tmp_path / "film.mkv"), 7)
        assert _wait(qt_app, lambda: any(level == "ERROR" for level, _text in messages))
        assert any("#7" in text and "#0, #1" in text for level, text in messages if level == "ERROR")
        assert studio._info is None and studio._report is None  # rien affiché à la place
        studio.load_compare(_media(tmp_path / "film.mkv"), 9)
        assert _wait(qt_app, lambda: sum(level == "ERROR" for level, _text in messages) >= 2)
    finally:
        studio.close()
        _wait(qt_app, lambda: getattr(studio, "_shutdown", None) is not None and studio._shutdown.done.is_set(), 2.0)


def test_snapshot_index_survives_json_report(tmp_path):
    """Non-régression : la piste retenue apparaît dans le rapport (``source.stream_index``)."""
    info = _multi(tmp_path / "x.mkv", 0, 3)
    from core.hdr_inspection.report import report_data
    from core.hdr_inspection import snapshot_report

    data = report_data(snapshot_report(info, 3))
    assert json.loads(json.dumps(data))["source"]["stream_index"] == 3
    assert dataclasses.is_dataclass(build_snapshot(info, 3))
