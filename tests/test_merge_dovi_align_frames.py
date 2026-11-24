"""Merge DoVi — alignement : table d'images, analyse des signatures (sans média)."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from core.workflows.merge_dovi_align.frames import (
    FrameTable,
    FrameTableError,
    VideoProps,
    parse_packet_pts,
    parse_stream_props,
    read_frame_table,
)
from core.workflows.merge_dovi_align import probe as probe_module
from core.workflows.merge_dovi_align.presence import PresenceError, _gaps
from core.workflows.merge_dovi_align.process import ProcessHooks
from core.workflows.merge_dovi_align.probe import (
    STRIP,
    VIGNETTE_H,
    _Accumulator,
    black_threshold,
    filter_graph,
    parse_showinfo,
)


def _props(width: int = 320, height: int = 180) -> VideoProps:
    return VideoProps("hevc", width, height, Fraction(1), "tv", "smpte2084", "bt2020", Fraction(1, 1000),
                      "yuv420p10le")


def test_stream_props_parsed_with_defaults() -> None:
    props = parse_stream_props(json.dumps({"format": {"start_time": "5.000000"}, "streams": [{
        "codec_name": "hevc", "width": 3840, "height": 2160, "sample_aspect_ratio": "1:1",
        "pix_fmt": "yuv420p10le", "color_range": "tv", "color_transfer": "smpte2084",
        "color_primaries": "bt2020", "time_base": "1/1000"}]}))
    assert (props.width, props.height, props.sar, props.time_base) == (3840, 2160, 1, Fraction(1, 1000))
    assert props.pq and not props.full_range and props.start_time == 5
    assert parse_stream_props(json.dumps({"streams": [{"width": 8, "height": 8}]})).sar == 1


def test_stream_props_refuse_missing_video() -> None:
    with pytest.raises(FrameTableError):
        parse_stream_props(json.dumps({"streams": []}))
    with pytest.raises(FrameTableError):
        parse_stream_props(json.dumps({"streams": [{"width": 0, "height": 10}]}))


def test_packet_pts_sorted_to_display_order() -> None:
    assert parse_packet_pts("0\n167\n83\n42\n125\n") == ((0, 42, 83, 125, 167), "")


@pytest.mark.parametrize(("payload", "reason"), [
    ("0\nN/A\n42\n", "paquet vidéo sans PTS"),
    ("0\n42\n42\n", "PTS répétés"),
    ("", "aucun paquet vidéo"),
])
def test_packet_pts_refusals(payload: str, reason: str) -> None:
    assert parse_packet_pts(payload) == ((), reason)


def test_frame_table_ranks_and_exact_seek() -> None:
    table = FrameTable(Path("a.mkv"), _props(), True, "", (1000, 1042, 1083, 1125))
    assert table.frames == 4 and table.frame_interval == 41
    assert [table.rank_of(value) for value in (1000, 1083, 1084)] == [0, 2, None]
    # Demi-image avant le PTS visé, depuis le départ du conteneur.
    assert table.seek_seconds(2) == "1.062500000"
    shifted = FrameTable(Path("a.mkv"), replace(_props(), start_time=Fraction(1)), True, "", table.pts)
    assert shifted.seek_seconds(2) == "0.062500000" and shifted.seek_seconds(0) == "0.000000000"
    with pytest.raises(ValueError):
        table.seek_seconds(4)


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def test_read_frame_table_non_indexable_container_skips_packets() -> None:
    stream = json.dumps({"streams": [{"width": 320, "height": 180, "time_base": "1/90000"}]})
    with patch("core.workflows.merge_dovi_align.frames.run_cancellable_capture",
               return_value=_completed(stream)) as run:
        table = read_frame_table(Path("film.m2ts"))
    assert not table.indexable and table.frames is None and "m2ts" in table.reason
    assert run.call_count == 1


def test_read_frame_table_indexable() -> None:
    stream = json.dumps({"streams": [{"width": 320, "height": 180, "time_base": "1/1000"}]})
    with patch("core.workflows.merge_dovi_align.frames.run_cancellable_capture",
               side_effect=[_completed(stream), _completed("0\n83\n42\n")]):
        table = read_frame_table(Path("film.mkv"))
    assert table.indexable and table.pts == (0, 42, 83)


def test_showinfo_parsing_keeps_output_order() -> None:
    lines = [
        "[Parsed_showinfo_0 @ 0x1] n:   0 pts:1200032 pts_time:1200.032 duration: 41",
        "[Parsed_showinfo_0 @ 0x1] config in time_base: 1/1000",
        "[Parsed_showinfo_0 @ 0x1] n:   1 pts:1200074 pts_time:1200.074",
        "[Parsed_showinfo_0 @ 0x1] n:   3 pts:1200157 pts_time:1200.157",
    ]
    assert parse_showinfo(lines) == [1200032, 1200074]


def test_filter_graph_masks_before_any_reduction() -> None:
    graph = filter_graph(3840, 2160, black_threshold(False))
    assert graph.index("lut=c0='gt(val,80)*1023'") < graph.index("scale=64:2160")
    assert "transpose=dir=clock" in graph and "vstack=inputs=3" in graph
    assert black_threshold(True) == 16
    assert filter_graph(64, 64, 80, cuda_format="p010le").startswith("[0:v:0]hwdownload,format=p010le,")


def _stacked(width: int, height: int, rows: slice, cols: slice, luma: int = 400) -> bytes:
    plane = np.zeros((height + width + VIGNETTE_H, STRIP), dtype="<u2")
    plane[:height][rows, 3] = 65535
    plane[height:height + width][cols, 60] = 65535
    plane[height + width:, :24] = luma * 64
    return plane.tobytes()


def test_accumulator_bars_and_blank_frames() -> None:
    acc = _Accumulator(320, 180)
    acc.add(_stacked(320, 180, slice(20, 160), slice(40, 290)))
    acc.add(_stacked(320, 180, slice(0, 0), slice(0, 0), luma=64))
    result = acc.result([0, 1], [0, 42], "logiciel")
    assert (result.top[0], result.bottom[0], result.left[0], result.right[0]) == (20, 20, 40, 30)
    assert (result.top[1], result.bottom[1]) == (-1, -1)
    assert result.mean[0] == pytest.approx(400) and result.vignettes[0, 0, 0] == 100


# --- Lot 1, relecture : CUDA, relances, annulation, présence ---------------------------------------

_MASTER = frozenset({"nv12", "p010le", "p012le", "p016le", "nv16", "p210le", "p212le", "p216le", "yuv444p",
                     "yuv444p10msble", "yuv444p12msble", "yuv444p16le"})
_OLDER = frozenset({"nv12", "p010le", "p016le", "nv16", "p210le", "p216le", "yuv444p", "yuv444p16le"})


@pytest.mark.parametrize(("pix_fmt", "master", "older"), [
    ("yuv420p", "nv12", "nv12"), ("yuv420p10le", "p010le", "p010le"), ("yuv420p12le", "p012le", "p016le"),
    ("yuv422p10le", "p210le", "p210le"), ("yuv422p12le", "p212le", "p216le"), ("yuv444p", "yuv444p", "yuv444p"),
    ("yuv444p10le", "yuv444p10msble", "yuv444p16le"), ("yuv444p12le", "yuv444p12msble", "yuv444p16le"),
    ("gray", None, None), ("yuv410p", None, None), ("", None, None),
])
def test_cuda_download_format_follows_chroma_depth_and_ffmpeg(pix_fmt: str, master: str | None,
                                                               older: str | None) -> None:
    assert probe_module.cuda_format(pix_fmt, _MASTER) == master
    assert probe_module.cuda_format(pix_fmt, _OLDER) == older
    assert probe_module.cuda_format(pix_fmt, frozenset()) is None


def test_cuda_failure_disables_cuda_for_the_whole_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    table = FrameTable(Path("a.mkv"), _props(), True, "", tuple(range(0, 420, 42)))
    commands: list[list[str]] = []

    def fake_run(command, table, hooks, progress):
        commands.append(command)
        acc = _Accumulator(320, 180)
        if "-hwaccel" in command:
            return acc, [], 1, "hwdownload: Impossible to convert"
        for _ in range(3):
            acc.add(_stacked(320, 180, slice(10, 170), slice(0, 320)))
        return acc, [0, 42, 84], 0, ""

    monkeypatch.setattr(probe_module, "_run", fake_run)
    monkeypatch.setattr(probe_module, "_pixel_formats", lambda ffmpeg, hooks: _MASTER)
    preference = probe_module.DecoderPreference("cuda")
    first = probe_module.decode_window(table, 0, 3, hwaccel=preference)
    second = probe_module.decode_window(table, 0, 3, hwaccel=preference)
    assert first.decoder == second.decoder == "logiciel"
    assert preference.hwaccel is None and "hwdownload" in preference.failure
    assert ["-hwaccel" in command for command in commands] == [True, False, False]


def test_window_retries_any_anomaly_earlier(monkeypatch: pytest.MonkeyPatch) -> None:
    table = FrameTable(Path("a.mkv"), _props(), True, "", tuple(range(0, 420, 42)))
    seeks: list[str] = []
    answers = iter([
        [9999, 42 * 3, 42 * 4],        # PTS inconnu : relance
        [42 * 3, 42 * 3, 42 * 4],      # image dupliquée : relance
        [42 * 2, 42 * 3, 42 * 4, 42 * 5],  # départ plus tôt, fenêtre complète
    ])

    def fake_decode(table, ffmpeg, hooks, *, seek, frames, hwaccel, progress):
        seeks.append(seek)
        pts = next(answers)
        acc = _Accumulator(320, 180)
        for _ in pts:
            acc.add(_stacked(320, 180, slice(10, 170), slice(0, 320)))
        return acc, pts, "logiciel"

    monkeypatch.setattr(probe_module, "_decode", fake_decode)
    window = probe_module.decode_window(table, 3, 3)
    assert window.ranks.tolist() == [3, 4, 5] and len(seeks) == 3
    assert seeks[0] == table.seek_seconds(3) and seeks[2] == table.seek_seconds(0)


def test_cancellation_stops_a_blocked_reader_and_cleans_up() -> None:
    """Annulation pendant une lecture bloquante : processus tué, flux fermés, on_end appelé."""
    table = FrameTable(Path("a.mkv"), _props(64, 64), True, "", (0, 42))
    command = [sys.executable, "-c", "import sys, time; sys.stdout.write('x'); sys.stdout.flush(); time.sleep(60)"]
    started: list = []
    ended: list = []
    cancel = threading.Event()
    hooks = ProcessHooks(cancelled=cancel.is_set, on_start=started.append, on_end=ended.append)
    threading.Timer(0.3, cancel.set).start()
    begin = time.monotonic()
    with pytest.raises(InterruptedError):
        probe_module._run(command, table, hooks, None)
    assert time.monotonic() - begin < 10
    assert started and ended and started[0] is ended[0]
    proc = ended[0]
    assert proc.poll() is not None and proc.stdout.closed and proc.stderr.closed


def test_equal_timestamps_make_missing_ranks_ambiguous() -> None:
    with pytest.raises(PresenceError):
        _gaps([0, 40, 40], [2])
    assert _gaps([0, 40, 40], []).missing == ()
    assert _gaps([0, 80, 40], [1]).missing == (2,)


def test_cancellation_during_stderr_drain_by_a_descendant() -> None:
    """Un descendant garde stderr ouvert après la sortie de stdout : groupe tué, annulation remontée."""
    table = FrameTable(Path("a.mkv"), _props(64, 64), True, "", (0, 42))
    child = "import time; time.sleep(60)"
    command = [sys.executable, "-c",
               "import subprocess, sys; subprocess.Popen([sys.executable, '-c', %r], stderr=sys.stderr, "
               "stdout=subprocess.DEVNULL); sys.stdout.close()" % child]
    ended: list = []
    cancel = threading.Event()
    hooks = ProcessHooks(cancelled=cancel.is_set, on_end=ended.append)
    threading.Timer(0.5, cancel.set).start()
    begin = time.monotonic()
    with pytest.raises(InterruptedError):
        probe_module._run(command, table, hooks, None)
    assert time.monotonic() - begin < 12 and ended


def test_pixel_format_probe_is_cancellable_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    listing = ("Pixel formats:\nI.... = Supported Input  format for conversion\n-----\n"
               "IO... yuv420p                3            12      8-8-8\n"
               "IO... p010le                 3            24      10-10-10\n")

    def fake_capture(command, *, cancel_cb, check_cancelled, on_start=None, on_end=None, **kwargs):
        calls.append(command)
        check_cancelled()
        return subprocess.CompletedProcess(command, 0, listing, "")

    monkeypatch.setattr(probe_module, "run_cancellable_capture", fake_capture)
    monkeypatch.setattr(probe_module, "_PIXEL_FORMATS", {})
    with pytest.raises(InterruptedError):
        probe_module._pixel_formats("ffmpeg-x", ProcessHooks(cancelled=lambda: True))
    hooks = ProcessHooks()
    assert probe_module._pixel_formats("ffmpeg-x", hooks) == frozenset({"yuv420p", "p010le"})
    assert probe_module._pixel_formats("ffmpeg-x", hooks) == frozenset({"yuv420p", "p010le"})
    assert len(calls) == 2  # annulée puis lue une seule fois (cache)


def test_cuda_failure_after_some_frames_restarts_in_software(monkeypatch: pytest.MonkeyPatch) -> None:
    table = FrameTable(Path("a.mkv"), _props(), True, "", tuple(range(0, 420, 42)))
    calls: list[bool] = []

    def fake_run(command, table, hooks, progress):
        cuda = "-hwaccel" in command
        calls.append(cuda)
        acc = _Accumulator(320, 180)
        for _ in range(2 if cuda else 10):
            acc.add(_stacked(320, 180, slice(10, 170), slice(0, 320)))
        return acc, list(range(0, 420, 42)), (1 if cuda else 0), "cuda error" if cuda else ""

    monkeypatch.setattr(probe_module, "_run", fake_run)
    monkeypatch.setattr(probe_module, "_pixel_formats", lambda ffmpeg, hooks: _MASTER)
    preference = probe_module.DecoderPreference("cuda")
    full = probe_module.decode_all(table, hwaccel=preference)
    assert len(full) == 10 and full.decoder == "logiciel" and calls == [True, False]
    assert preference.hwaccel is None
    assert "-noautorotate" in probe_module._command("ffmpeg", table, seek=None, frames=None, download=None)
