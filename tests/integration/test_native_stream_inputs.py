"""Producteurs FFmpeg sur pipes : flux identique au fichier, échecs expliqués, annulation, arrêt volontaire."""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core.matroska import ebml
from core.matroska.ids import CODEC_ID_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.matroska.writer import MatroskaWriter
from core.runner import TaskCancelledError
from core.workflows.common.native_stream_inputs import (
    StreamInputs,
    StreamProducer,
    StreamTrackSpec,
    producer_command,
)

from ._synth import make_av_container, make_mp4_with_mov_text

_MS = 1_000_000
_needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg requis")


def _summary(blocks) -> list[tuple]:
    return [(b.timestamp_ns, b.duration_ns, b.payload) for b in blocks]


def _stream_file(path: Path) -> Path:
    """MKV d'une piste audio, écrit par le writer natif (source des producteurs simulés)."""
    raw = b"".join((
        ebml.uint_element(TRACK_NUMBER_ID, 1), ebml.uint_element(TRACK_UID_ID, 9),
        ebml.uint_element(TRACK_TYPE_ID, 2), ebml.ascii_element(CODEC_ID_ID, "A_AC3"),
    ))
    track = MatroskaTrack(1, 9, 2, "A_AC3", b"", "", "und", "", raw)
    packets = tuple(MatroskaMuxPacket(1, MatroskaBlock(1, ms, 0x80, b"x" * 3000, timestamp_ns=ms * _MS), i)
                    for i, ms in enumerate(range(0, 60_000, 32)))
    MatroskaWriter().write(MatroskaMuxPlan(path, (MatroskaMuxTrack(Path("a.mkv"), track, 1, 9),), packets))
    return path


def _replay(path: Path, *, limit: int | None = None, code: int = 0, delay: float = 0.0, chunk_delay: float = 0.0,
            hang: float = 0.0) -> list[str]:
    """Producteur simulé : rejoue un fichier sur stdout (coupé, lent, silencieux, en échec)."""
    script = (
        "import sys, time\n"
        f"data = open({str(path)!r}, 'rb').read()[:{limit!r}]\n"
        f"time.sleep({delay})\n"
        "for start in range(0, len(data), 65536):\n"
        "    sys.stdout.buffer.write(data[start:start + 65536]); sys.stdout.buffer.flush()\n"
        f"    time.sleep({chunk_delay})\n"
        f"time.sleep({hang})\n"
        # Octets UTF-8, comme FFmpeg (la console Windows coderait sinon en cp1252).
        f"sys.stderr.buffer.write('panne simulée\\n'.encode('utf-8')); sys.stderr.flush(); sys.exit({code})\n"
    )
    return [sys.executable, "-c", script]


def _first_cluster_end(path: Path) -> int:
    cluster = next(e for e in MatroskaReader(path).top_level() if e.element_id == bytes.fromhex("1f43b675"))
    assert cluster.end is not None
    return cluster.end


def test_producer_command_shape() -> None:
    video = producer_command("ffmpeg", StreamTrackSpec(Path("in.mp4"), 0, "video", "hevc", carry_context=True))
    assert video[video.index("-map") + 1] == "0:0"
    assert video[video.index("-avoid_negative_ts") + 1] == "disabled"
    assert video[video.index("-map_metadata") + 1] == "0"
    assert video[-3:] == ["-f", "matroska", "pipe:1"]
    assert "-tag:v:0" in video
    subtitle = producer_command("ffmpeg", StreamTrackSpec(Path("in.mp4"), 3, "subtitle", "mov_text"))
    assert subtitle[subtitle.index("-c:s:0") + 1] == "srt"
    assert subtitle[subtitle.index("-map_chapters") + 1] == "-1"
    assert subtitle[subtitle.index("-map_metadata:g") + 1] == "-1"


def test_grouped_producer_command_maps_every_track_in_order() -> None:
    group = (
        StreamTrackSpec(Path("in.mp4"), 1, "audio", "eac3"),
        StreamTrackSpec(Path("in.mp4"), 0, "video", "hevc"),
        StreamTrackSpec(Path("in.mp4"), 3, "subtitle", "mov_text"),
        StreamTrackSpec(Path("in.mp4"), 4, "subtitle", "mov_text"),
    )
    command = producer_command("ffmpeg", group)
    assert [command[i + 1] for i, arg in enumerate(command) if arg == "-map"] == ["0:1", "0:0", "0:3", "0:4"]
    assert command.count("-i") == 1
    assert "-tag:v:0" in command
    # Conversion des sous-titres par position de sortie parmi les sous-titres.
    assert command[command.index("-c:s:0") + 1] == "srt"
    assert command[command.index("-c:s:1") + 1] == "srt"
    with pytest.raises(ValueError):
        producer_command("ffmpeg", (group[0], StreamTrackSpec(Path("other.mp4"), 0, "video")))


def test_group_stream_specs_keeps_dense_tracks_together_and_subtitles_apart() -> None:
    from core.workflows.common.native_stream_inputs import group_stream_specs

    a_video = StreamTrackSpec(Path("a.mp4"), 0, "video")
    a_sub = StreamTrackSpec(Path("a.mp4"), 2, "subtitle")
    b_audio = StreamTrackSpec(Path("b.m4a"), 0, "audio")
    a_audio = StreamTrackSpec(Path("a.mp4"), 1, "audio")
    groups = group_stream_specs([a_video, a_sub, b_audio, a_audio, a_video])
    assert groups == [(a_video, a_audio), (a_sub,), (b_audio,)]


def test_declared_routes_share_positions_and_reopen_the_same_group(tmp_path: Path) -> None:
    from core.workflows.common.native_stream_inputs import StreamArtifacts

    opened: list[tuple[int, ...]] = []

    class Inputs:
        def open(self, spec, *, duration_ns=None, header_only=False):
            group = (spec,) if isinstance(spec, StreamTrackSpec) else tuple(spec)
            opened.append(tuple(item.stream_index for item in group))
            reader = type("Reader", (), {})()
            reader.path = tmp_path / f"h{len(opened)}.mkv"
            return reader

    streams = StreamArtifacts(Inputs())  # type: ignore[arg-type]
    video, audio, sub = (StreamTrackSpec(Path("a.mp4"), 0, "video"), StreamTrackSpec(Path("a.mp4"), 1, "audio"),
                         StreamTrackSpec(Path("a.mp4"), 2, "subtitle"))
    streams.declare([audio, video, sub])
    header, position = streams.route(video)
    assert (header, position) == (tmp_path / "h1.mkv", 1)
    assert streams.route(audio) == (header, 0)
    assert streams.route(sub) == (tmp_path / "h2.mkv", 0)
    assert streams.metadata(audio) == (header, 0)
    assert opened == [(1, 0), (2,)]
    streams.reopen[header]()
    assert opened[-1] == (1, 0)
    assert streams.route_headers("a.mp4") == [header, tmp_path / "h2.mkv"]


@_needs_ffmpeg
@pytest.mark.parametrize("stream_index", [0, 1])
def test_stream_equals_file_copy(tmp_path: Path, stream_index: int) -> None:
    source = tmp_path / "source.mp4"
    make_av_container(source, duration=3.0)
    spec = StreamTrackSpec(source, stream_index, "video" if stream_index == 0 else "audio", "h264")
    reference = tmp_path / "reference.mkv"
    command = producer_command("ffmpeg", spec)
    subprocess.run([*command[:-1], str(reference)], check=True)
    with StreamInputs("ffmpeg", tmp_path) as inputs:
        reader = inputs.open(spec)
        streamed = _summary(reader.blocks(first_lace_only=True))
    assert streamed == _summary(MatroskaReader(reference).blocks(first_lace_only=True))


@_needs_ffmpeg
def test_mov_text_converted_and_context_carried(tmp_path: Path) -> None:
    source = tmp_path / "subs.mp4"
    make_mp4_with_mov_text(source)
    with StreamInputs("ffmpeg", tmp_path) as inputs:
        reader = inputs.open(StreamTrackSpec(source, 2, "subtitle", "mov_text", carry_context=True))
        assert reader.tracks()[0].codec_id == "S_TEXT/UTF8"
        assert list(reader.blocks())


def test_slow_and_silent_producer_gives_the_same_blocks(tmp_path: Path) -> None:
    source = _stream_file(tmp_path / "src.mkv")
    producer = StreamProducer(_replay(source, delay=0.3, chunk_delay=0.01), tmp_path / "h.mkv", label="lent")
    assert _summary(producer.reader.blocks()) == _summary(MatroskaReader(source).blocks())
    assert producer.done


def test_failure_at_a_clean_boundary_reports_exit_code(tmp_path: Path) -> None:
    source = _stream_file(tmp_path / "src.mkv")
    producer = StreamProducer(_replay(source, limit=_first_cluster_end(source), code=3), tmp_path / "h.mkv",
                              label="piste test")
    with pytest.raises(RuntimeError, match=r"piste test : FFmpeg a échoué \(3\).*panne simulée"):
        list(producer.reader.blocks())


def test_truncated_stream_reports_producer_failure(tmp_path: Path) -> None:
    source = _stream_file(tmp_path / "src.mkv")
    producer = StreamProducer(_replay(source, limit=_first_cluster_end(source) + 1000, code=1), tmp_path / "h.mkv",
                              label="piste test")
    with pytest.raises(RuntimeError, match=r"FFmpeg a échoué \(1\)"):
        list(producer.reader.blocks())


def test_cancellation_kills_producer_and_raises_cancelled(tmp_path: Path) -> None:
    source = _stream_file(tmp_path / "src.mkv")
    cancelled = threading.Event()
    registered: list[subprocess.Popen] = []
    producer = StreamProducer(
        _replay(source, limit=_first_cluster_end(source), hang=30), tmp_path / "h.mkv", label="bloqué",
        cancelled=cancelled.is_set, register=registered.append, unregister=registered.remove,
    )
    process = registered[0]
    blocks = producer.reader.blocks()
    next(blocks)
    # Annulation comme TaskSignals.cancel : processus enregistrés tués, puis lecture interrompue.
    cancelled.set()
    threading.Timer(0.2, lambda: process.kill()).start()
    started = time.monotonic()
    with pytest.raises(TaskCancelledError):
        list(blocks)
    assert time.monotonic() - started < 10
    assert process.poll() is not None
    assert registered == []


def test_header_only_producer_is_stopped_on_purpose(tmp_path: Path) -> None:
    source = _stream_file(tmp_path / "src.mkv")
    logs: list[str] = []
    inputs = StreamInputs(sys.executable, tmp_path, ffprobe_bin=None, log=lambda _level, message: logs.append(message))
    # Producteur simulé à la place de FFmpeg : seule la mécanique d'arrêt est testée ici.
    command = _replay(source, hang=30)
    from core.workflows.common import native_stream_inputs as module

    original = module.producer_command
    module.producer_command = lambda _ffmpeg, _spec: command  # type: ignore[assignment]
    try:
        reader = inputs.open(StreamTrackSpec(source, 0, "audio"), header_only=True)
    finally:
        module.producer_command = original  # type: ignore[assignment]
    assert reader.tracks()[0].codec_id == "A_AC3"
    assert inputs._producers[0].stopped
    assert inputs._producers[0]._process.poll() is not None
    assert any("arrêté volontairement" in message for message in logs)
    inputs.close()


def _script_command(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def test_origin_is_explicit_and_probed_once(tmp_path: Path) -> None:
    from core.workflows.common.native_stream_inputs import probe_start_time

    command = producer_command("ffmpeg", StreamTrackSpec(Path("in.mp4"), 1, "audio", origin_s=-0.021333))
    assert command.index("-copyts") < command.index("-i")
    assert command[command.index("-output_ts_offset") + 1] == "0.021333"
    if shutil.which("ffprobe"):
        source = tmp_path / "s.mp4"
        make_av_container(source, duration=1.0)
        assert isinstance(probe_start_time(source, "ffprobe"), float)


def test_stderr_flood_never_blocks_the_stream(tmp_path: Path) -> None:
    source = _stream_file(tmp_path / "src.mkv")
    replay = _replay(source)[2]
    flood = "import sys\nsys.stderr.write('x' * 1_000_000 + '\\n'); sys.stderr.flush()\n" + replay
    producer = StreamProducer(_script_command(flood), tmp_path / "h.mkv", label="bavard")
    assert len(list(producer.reader.blocks())) == len(list(MatroskaReader(source).blocks()))


def test_close_kills_descendants_holding_stderr(tmp_path: Path) -> None:
    source = _stream_file(tmp_path / "src.mkv")
    spawn = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        + _replay(source, limit=_first_cluster_end(source))[2]
    )
    producer = StreamProducer(_script_command(spawn), tmp_path / "h.mkv", label="descendant")
    started = time.monotonic()
    producer.close()
    producer.close()  # idempotent
    assert time.monotonic() - started < 10


def test_cancel_through_task_signals_while_reading_header(tmp_path: Path, monkeypatch) -> None:
    from core.runner import TaskSignals
    from core.workflows.common import native_stream_inputs as module

    source = _stream_file(tmp_path / "src.mkv")
    signals = TaskSignals()
    monkeypatch.setattr(module, "producer_command", lambda _ffmpeg, _spec: _replay(source, delay=30))
    inputs = StreamInputs(sys.executable, tmp_path, ffprobe_bin=None, cancelled=signals._cancel_event.is_set,
                          register=signals._register_proc, unregister=signals._unregister_proc)
    threading.Timer(0.3, signals.cancel).start()
    started = time.monotonic()
    with pytest.raises(TaskCancelledError):
        inputs.open(StreamTrackSpec(source, 0, "audio"))
    assert time.monotonic() - started < 10
    inputs.close()


def test_failure_while_cancelled_is_reported_as_cancellation(tmp_path: Path) -> None:
    source = _stream_file(tmp_path / "src.mkv")
    cancelled = threading.Event()
    producer = StreamProducer(_replay(source, limit=_first_cluster_end(source), code=3), tmp_path / "h.mkv",
                              label="annulé", cancelled=cancelled.is_set)
    blocks = producer.reader.blocks()
    next(blocks)
    cancelled.set()
    with pytest.raises(TaskCancelledError):
        list(blocks)


def test_prefetched_pipe_is_bounded_and_returns_the_same_bytes() -> None:
    import io

    from core.workflows.common import native_stream_inputs as module

    data = bytes(range(256)) * (40 * 1024 * 16)  # 10 Mio
    pipe = module._PrefetchedPipe(io.BufferedReader(io.BytesIO(data)), label="test", limit=1024 * 1024)
    deadline = time.monotonic() + 5
    while pipe._pending < 1024 * 1024 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    # Jamais plus de la borne + un morceau en attente.
    assert pipe._pending <= 1024 * 1024 + module._PREFETCH_CHUNK
    received = bytearray()
    sizes = (1, 3, 7, 4096, 200_000)
    index = 0
    while True:
        chunk = pipe.read(sizes[index % len(sizes)])
        index += 1
        if not chunk:
            break
        received += chunk
    assert bytes(received) == data
    pipe.close()


def test_prefetched_pipe_close_stops_a_blocked_filler() -> None:
    import io

    from core.workflows.common import native_stream_inputs as module

    pipe = module._PrefetchedPipe(io.BufferedReader(io.BytesIO(b"x" * (8 * 1024 * 1024))), label="test", limit=4096)
    assert pipe.read(10) == b"x" * 10
    started = time.monotonic()
    pipe.close()
    assert time.monotonic() - started < 5
    assert not pipe._thread.is_alive()
