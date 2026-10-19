"""Origine commune, pré-roll et cycle de vie des sondes audio du runtime."""
import threading
import time

import pytest

from core.runner import TaskCancelledError
from core.workflows.common.audio_timestamps import (
    adjust_input_timestamp, common_timestamp_origin, preroll_input_offset, probe_timestamp_payload,
)
from core.workflows.common.sync_rewrite import SyncRewriteService
from core.workflows.physical_sync import preparation_commands
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry
from tests.test_probe_lifecycle import _fake_tool


@pytest.mark.parametrize("start,padding,rate,expected", [
    ("602.834667", 0, 48000, 602.834667),
    ("-0.005", 256, 48000, 0),
    ("-0.021", 1024, 48000, 0),
    ("0.5", 256, 48000, 0.5),
    ("-2.0", 256, 48000, -2),
    ("N/A", 0, 48000, 0),
    ("nan", 0, 0, 0),
])
def test_common_origin_distinguishes_container_and_codec_delay(start, padding, rate, expected):
    payload = {"format": {"start_time": start}, "streams": [
        {"codec_type": "video", "start_time": "0.5"},
        {"codec_type": "audio", "start_time": "0.625", "initial_padding": padding, "sample_rate": rate},
    ]}
    assert common_timestamp_origin(payload) == expected
    raw = 0 if start in {"N/A", "nan"} else float(start)
    assert preroll_input_offset(payload) == pytest.approx(raw - expected)


def test_input_correction_composes_offsets_without_touching_other_inputs():
    cmd = ["ffmpeg", "-itsoffset", "0.25", "-f", "matroska", "-i", "one.mkv",
           "-i", "two.ts", "-i", "three.mkv"]
    adjust_input_timestamp(cmd, 0, -0.005)
    adjust_input_timestamp(cmd, 2, -0.021)
    assert cmd == ["ffmpeg", "-itsoffset", "0.245000000", "-f", "matroska", "-i", "one.mkv",
                   "-i", "two.ts", "-itsoffset", "-0.021000000", "-i", "three.mkv"]


@pytest.mark.parametrize("kind", ["source", "artifact"])
def test_cancellation_stops_the_timestamp_probe(tmp_path, kind):
    tool = _fake_tool(tmp_path, "silent-ffprobe", "import time; time.sleep(30)\n")
    media = tmp_path / "source.mkv"
    media.write_bytes(b"placeholder")
    cancelled = threading.Event()
    timer = threading.Timer(0.2, cancelled.set)
    started = time.monotonic()
    timer.start()
    try:
        with pytest.raises(TaskCancelledError):
            if kind == "source":
                SyncRewriteService(ffmpeg_bin="ffmpeg", ffprobe_bin=tool)._probe_stream(media, 1, cancel_cb=cancelled.is_set)
            else:
                probe_timestamp_payload(media, tool, cancel_cb=cancelled.is_set)
        assert time.monotonic() - started < 5
    finally:
        timer.cancel()


def test_physical_preview_never_probes_or_executes(tmp_path, monkeypatch):
    def no_process(*_args, **_kwargs):
        pytest.fail("Un aperçu ne doit lancer aucun outil")

    monkeypatch.setattr("subprocess.Popen", no_process)
    monkeypatch.setattr("subprocess.run", no_process)
    track = TrackEntry(1, "audio", "AC3", "", "fra", "", time_shift_ms=125)
    config = RemuxConfig([SourceInput(tmp_path / "source.ts", 0, [track])], tmp_path / "out.mkv",
                         [(0, 1)], sync_mode="physical")
    commands = preparation_commands(config, tmp_path, "ffmpeg")
    assert len(commands) == 1


def test_physical_normalization_keeps_the_bluray_playlist_input(tmp_path):
    from tests.test_bluray import _make_bluray_tree
    root, playlist = _make_bluray_tree(tmp_path, playlist_id=12)
    track = TrackEntry(1, "audio", "AC3", "", "fra", "", time_shift_ms=125)
    config = RemuxConfig([SourceInput(playlist, 0, [track])], tmp_path / "out.mkv", [(0, 1)], sync_mode="physical")
    _, _, command, _ = preparation_commands(config, tmp_path, "ffmpeg", source_origins={playlist: 600})[0]
    assert command[command.index("-i")+1] == f"bluray:{root}"
    assert command[command.index("-playlist")+1] == "12"
    assert command[command.index("-itsoffset")+1] == "-600.000000000"
    assert command.index("-itsoffset") < command.index("-i")
