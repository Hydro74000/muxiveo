from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

import pytest

from core.runner import TaskCancelledError
from core.workflows.remux_models import SourceInput, TrackEntry
from core.workflows.remux_timeline_sync import (
    LiveSyncSession,
    MkvmergeLikeTimelineSync,
    TimelineSyncFallbackHelper,
)


@dataclass
class _MappedTrackFake:
    source_file_index: int
    stream_index: int
    track: TrackEntry


def _track(tid: int, track_type: str) -> TrackEntry:
    return TrackEntry(
        mkv_tid=tid,
        track_type=track_type,
        codec="COPY",
        display_info="",
        language="",
        title="",
        enabled=True,
        file_id="x",
    )


def _source(path: Path, file_index: int, tracks: list[TrackEntry]) -> SourceInput:
    return SourceInput(path=path, file_index=file_index, tracks=tracks)


def test_prepare_from_mapped_tracks_extracts_only_foreign_audio_subtitle(tmp_path, monkeypatch):
    src0 = tmp_path / "src0.mkv"
    src1 = tmp_path / "src1.mkv"
    src0.touch()
    src1.touch()

    mapped = [
        _MappedTrackFake(source_file_index=0, stream_index=0, track=_track(0, "video")),
        _MappedTrackFake(source_file_index=1, stream_index=2, track=_track(2, "audio")),
        _MappedTrackFake(source_file_index=1, stream_index=3, track=_track(3, "subtitle")),
        _MappedTrackFake(source_file_index=1, stream_index=2, track=_track(2, "audio")),  # duplicate
        _MappedTrackFake(source_file_index=0, stream_index=4, track=_track(4, "audio")),
    ]
    sources = [
        _source(src0, 0, [_track(0, "video"), _track(4, "audio")]),
        _source(src1, 1, [_track(2, "audio"), _track(3, "subtitle")]),
    ]

    calls: list[tuple[Path, int, Path]] = []

    def _fake_extract(*, source: Path, stream_index: int, destination: Path) -> None:
        calls.append((source, stream_index, destination))
        destination.write_bytes(b"sync")

    syncer = MkvmergeLikeTimelineSync(ffmpeg_bin="ffmpeg")
    monkeypatch.setattr(syncer, "_extract_stream", _fake_extract)

    prepared = syncer.prepare_from_mapped_tracks(
        mapped_tracks=mapped,
        sources=sources,
        tmp_dir=tmp_path,
        base_input_idx=2,
    )

    assert len(prepared) == 2
    assert [p.input_idx for p in prepared] == [2, 3]
    assert prepared[0].path.suffix == ".mka"
    assert prepared[1].path.suffix == ".mks"
    assert len(calls) == 2


def test_prepare_from_mapped_tracks_honors_cancel_callback(tmp_path, monkeypatch):
    src0 = tmp_path / "src0.mkv"
    src1 = tmp_path / "src1.mkv"
    src0.touch()
    src1.touch()

    mapped = [
        _MappedTrackFake(source_file_index=0, stream_index=0, track=_track(0, "video")),
        _MappedTrackFake(source_file_index=1, stream_index=2, track=_track(2, "audio")),
    ]
    sources = [
        _source(src0, 0, [_track(0, "video")]),
        _source(src1, 1, [_track(2, "audio")]),
    ]

    syncer = MkvmergeLikeTimelineSync(ffmpeg_bin="ffmpeg")
    monkeypatch.setattr(syncer, "_extract_stream", lambda **_: pytest.fail("must not extract when canceled"))

    with pytest.raises(TaskCancelledError):
        syncer.prepare_from_mapped_tracks(
            mapped_tracks=mapped,
            sources=sources,
            tmp_dir=tmp_path,
            base_input_idx=2,
            cancel_cb=lambda: True,
        )


def test_start_live_demux_session_uses_fifos_and_no_extract(tmp_path, monkeypatch):
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo non disponible sur cette plateforme")

    src0 = tmp_path / "src0.mkv"
    src1 = tmp_path / "src1.mkv"
    src0.touch()
    src1.touch()

    mapped = [
        _MappedTrackFake(source_file_index=0, stream_index=0, track=_track(0, "video")),
        _MappedTrackFake(source_file_index=1, stream_index=2, track=_track(2, "audio")),
        _MappedTrackFake(source_file_index=1, stream_index=3, track=_track(3, "subtitle")),
    ]
    sources = [
        _source(src0, 0, [_track(0, "video")]),
        _source(src1, 1, [_track(2, "audio"), _track(3, "subtitle")]),
    ]

    syncer = MkvmergeLikeTimelineSync(ffmpeg_bin="ffmpeg")
    popen_cmds: list[list[str]] = []

    monkeypatch.setattr(syncer, "_extract_stream", lambda **_: pytest.fail("must not extract in live mode"))
    monkeypatch.setattr(os, "mkfifo", lambda path, mode: Path(path).touch())

    class _FakeProc:
        def __init__(self, cmd, *_args, **_kwargs):
            popen_cmds.append(list(cmd))
            self._rc = None

        def poll(self):
            return self._rc

        def terminate(self):
            self._rc = 0

        def wait(self, timeout=None):
            self._rc = 0
            return 0

        def kill(self):
            self._rc = -9

    monkeypatch.setattr("core.workflows.remux_timeline_sync.subprocess.Popen", _FakeProc)

    session = syncer.start_live_demux_session(
        mapped_tracks=mapped,
        sources=sources,
        tmp_dir=tmp_path,
        base_input_idx=2,
    )

    assert len(session.inputs) == 2
    assert session.inputs[0].input_idx == 2
    assert session.inputs[1].input_idx == 3
    assert session.inputs[0].path.suffix == ".mka"
    assert session.inputs[1].path.suffix == ".mks"
    assert popen_cmds, "Aucune commande ffmpeg live capturée"
    assert "-y" in popen_cmds[0]
    assert "-start_at_zero" in popen_cmds[0]
    assert "-avoid_negative_ts" in popen_cmds[0]
    assert "-start_at_zero" not in popen_cmds[1]
    assert "-avoid_negative_ts" not in popen_cmds[1]
    session.close()


def test_extract_stream_keeps_audio_rebase_flags(tmp_path, monkeypatch):
    src = tmp_path / "src.mkv"
    src.touch()
    destination = tmp_path / "sync_audio.mka"
    captured_cmd: list[str] = []

    class _Result:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, **_kwargs):
        captured_cmd[:] = list(cmd)
        Path(cmd[-1]).write_bytes(b"audio")
        return _Result()

    monkeypatch.setattr("core.workflows.remux_timeline_sync.subprocess.run", _fake_run)

    syncer = MkvmergeLikeTimelineSync(ffmpeg_bin="ffmpeg")
    syncer._extract_stream(source=src, stream_index=2, destination=destination)

    assert "-start_at_zero" in captured_cmd
    assert "-avoid_negative_ts" in captured_cmd


def test_extract_stream_preserves_subtitle_timestamps(tmp_path, monkeypatch):
    src = tmp_path / "src.mkv"
    src.touch()
    destination = tmp_path / "sync_subtitle.mks"
    captured_cmd: list[str] = []

    class _Result:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, **_kwargs):
        captured_cmd[:] = list(cmd)
        Path(cmd[-1]).write_bytes(b"subtitle")
        return _Result()

    monkeypatch.setattr("core.workflows.remux_timeline_sync.subprocess.run", _fake_run)

    syncer = MkvmergeLikeTimelineSync(ffmpeg_bin="ffmpeg")
    syncer._extract_stream(source=src, stream_index=3, destination=destination)

    assert "-start_at_zero" not in captured_cmd
    assert "-avoid_negative_ts" not in captured_cmd


def test_start_live_demux_session_routes_to_windows_backend(tmp_path, monkeypatch):
    syncer = MkvmergeLikeTimelineSync(ffmpeg_bin="ffmpeg")
    called = {"ok": False}

    def _fake_windows(**_kwargs):
        called["ok"] = True
        return LiveSyncSession(inputs=[], processes=[])

    monkeypatch.setattr("core.workflows.remux_timeline_sync.os.name", "nt", raising=False)
    monkeypatch.setattr(syncer, "_start_windows_named_pipe_session", _fake_windows)

    session = syncer.start_live_demux_session(
        mapped_tracks=[],
        sources=[],
        tmp_dir=tmp_path,
        base_input_idx=0,
    )
    assert called["ok"] is True
    assert session.inputs == []


def test_prepare_from_mapped_tracks_mmap_uses_mmap_extractor(tmp_path, monkeypatch):
    src0 = tmp_path / "src0.mkv"
    src1 = tmp_path / "src1.mkv"
    src0.touch()
    src1.touch()

    mapped = [
        _MappedTrackFake(source_file_index=0, stream_index=0, track=_track(0, "video")),
        _MappedTrackFake(source_file_index=1, stream_index=2, track=_track(2, "audio")),
    ]
    sources = [
        _source(src0, 0, [_track(0, "video")]),
        _source(src1, 1, [_track(2, "audio")]),
    ]

    calls: list[tuple[Path, int, Path]] = []

    def _fake_extract_mmap(*, source: Path, stream_index: int, destination: Path) -> None:
        calls.append((source, stream_index, destination))
        destination.write_bytes(b"mmap-sync")

    syncer = MkvmergeLikeTimelineSync(ffmpeg_bin="ffmpeg")
    monkeypatch.setattr(syncer, "_extract_stream_via_mmap", _fake_extract_mmap)

    prepared = syncer.prepare_from_mapped_tracks_mmap(
        mapped_tracks=mapped,
        sources=sources,
        tmp_dir=tmp_path,
        base_input_idx=2,
    )

    assert len(prepared) == 1
    assert prepared[0].input_idx == 2
    assert prepared[0].path.suffix == ".mka"
    assert len(calls) == 1


def test_fallback_helper_prefers_live_when_available(tmp_path):
    sync_path = tmp_path / "sync_live.mka"
    sync_path.touch()
    expected = [type("P", (), {"key": (1, 1, "audio"), "path": sync_path, "input_idx": 2})()]

    class _FakeSyncer:
        def start_live_demux_session(self, **_kwargs):
            return LiveSyncSession(inputs=expected, processes=[])

        def prepare_from_mapped_tracks_mmap(self, **_kwargs):
            pytest.fail("mmap fallback should not run when live is available")

        def prepare_from_mapped_tracks(self, **_kwargs):
            pytest.fail("file fallback should not run when live is available")

    result = TimelineSyncFallbackHelper(
        syncer=_FakeSyncer(),
        work_dir=tmp_path,
        ram_dir=tmp_path / "ram",
    ).prepare(
        mapped_tracks=[],
        sources=[],
        base_input_idx=2,
        allow_live=True,
    )

    assert result.live_session is not None
    assert result.prepared_inputs == expected


def test_fallback_helper_prefers_ram_before_disk(tmp_path):
    work_dir = tmp_path / "work"
    ram_dir = tmp_path / "ram"
    work_dir.mkdir()
    ram_dir.mkdir()
    calls: list[Path] = []

    class _FakeSyncer:
        def start_live_demux_session(self, **_kwargs):
            raise RuntimeError("live unavailable")

        def prepare_from_mapped_tracks_mmap(self, **kwargs):
            calls.append(Path(kwargs["tmp_dir"]))
            return [type("P", (), {"key": (1, 1, "audio"), "path": ram_dir / "sync_ram.mka", "input_idx": 2})()]

        def prepare_from_mapped_tracks(self, **_kwargs):
            pytest.fail("file fallback should not run when RAM mmap works")

    result = TimelineSyncFallbackHelper(
        syncer=_FakeSyncer(),
        work_dir=work_dir,
        ram_dir=ram_dir,
    ).prepare(
        mapped_tracks=[],
        sources=[],
        base_input_idx=2,
        allow_live=True,
    )

    assert calls == [ram_dir]
    assert result.live_session is None
    assert len(result.prepared_inputs) == 1
