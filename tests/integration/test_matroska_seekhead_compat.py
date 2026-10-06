"""Intégration : MKV à second SeekHead non chaîné (RFC 9559 §6.3 non respectée).

Structure produite par d'anciens Muxiveo (Tracks déplacé en fin de fichier,
indexé par un SeekHead orphelin) : ``dovi_tool`` échoue (« can't find Element:
Tracks »). Muxiveo détecte le cas et lit le flux par FFmpeg (Annex B).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.dovi_profile_detector import DoviProfileDetector, DoviSubProfile
from core.matroska.editors.segment_info import (
    MatroskaSegmentInfoHeaderEditor,
    _SEEKHEAD_ID,
    _TRACKS_ID,
)
from core.matroska.reader import strict_demuxer_reads_tracks
from core.runner import TaskSignals, ToolRunner
from core.workflows.encode.runtime.dovi_geometry import extract_dovi_rpu

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("ffmpeg", "dovi_tool")),
    reason="FFmpeg et dovi_tool requis",
)


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def orphan_mkv(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("seekhead")
    config = work / "p81.json"
    config.write_text(json.dumps({"cm_version": "V40", "profile": "8.1", "length": 12}), encoding="utf-8")
    rpu = work / "p81.bin"
    _run(["dovi_tool", "generate", "-j", str(config), "-o", str(rpu)])
    _run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=256x144:r=24", "-frames:v", "12",
          "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-x265-params", "log-level=0:bframes=0",
          "-f", "hevc", str(work / "bl.hevc")])
    _run(["dovi_tool", "inject-rpu", "-i", str(work / "bl.hevc"), "-r", str(rpu), "-o", str(work / "dv.hevc")])
    clean = work / "clean.mkv"
    _run(["ffmpeg", "-y", "-v", "error", "-fflags", "+genpts", "-r", "24", "-i", str(work / "dv.hevc"),
          "-c", "copy", str(clean)])
    broken = work / "orphan.mkv"
    shutil.copy(clean, broken)
    editor = MatroskaSegmentInfoHeaderEditor()
    with broken.open("r+b") as fh:
        state = editor._analyze_file(fh, parse_fast=False)
        segment = state.segment
        seek_head = next(e for e in state.data if e.element_id == _SEEKHEAD_ID)
        void = state.data[state.data.index(seek_head) + 1]
        tracks = next(e for e in state.data if e.element_id == _TRACKS_ID)
        raw_tracks = editor._read_exact(fh, tracks.offset, editor._element_span(tracks))
        end = editor._file_size(fh)
        fh.seek(end)
        fh.write(raw_tracks)
        if not segment.unknown_size:
            fh.seek(segment.size_offset)
            fh.write(editor._encode_ebml_size(segment.size + len(raw_tracks), length=segment.size_len))
        kept = [(i, p) for i, p in editor._iter_seek_entries(fh, seek_head) if i != _TRACKS_ID]
        first = editor._build_seekhead_from_entries([editor._build_seek_entry(i, p) for i, p in kept])
        region = editor._element_span(seek_head) + editor._element_span(void)
        fh.seek(seek_head.offset)
        fh.write(first + editor._build_void_element(region - len(first)))
        second = editor._build_seekhead_from_entries(
            [editor._build_seek_entry(_TRACKS_ID, end - segment.payload_offset)]
        )
        fh.seek(tracks.offset)
        fh.write(second + editor._build_void_element(len(raw_tracks) - len(second)))
    return broken


def test_orphan_seekhead_breaks_dovi_tool_but_not_muxiveo(orphan_mkv, tmp_path, qt_app):
    direct = subprocess.run(
        ["dovi_tool", "extract-rpu", "-i", str(orphan_mkv), "-o", str(tmp_path / "direct.bin")],
        capture_output=True, text=True,
    )
    assert direct.returncode != 0 and "Tracks" in direct.stdout + direct.stderr
    assert not strict_demuxer_reads_tracks(orphan_mkv)

    runner, signals = ToolRunner(), TaskSignals()
    rpu = extract_dovi_rpu(
        source=orphan_mkv, stream_index=0, ffmpeg_bin="ffmpeg", dovi_tool_bin="dovi_tool",
        output_rpu=tmp_path / "rpu.bin", work_dir=tmp_path,
        run_cmd=lambda cmd: runner._run_cmd(cmd, cwd=tmp_path, label="t", signals=signals),
        cleanup_paths=[],
    )
    summary = subprocess.run(["dovi_tool", "info", "-i", str(rpu), "--summary"], capture_output=True, text=True).stdout
    assert "Frames: 12" in summary
    assert DoviProfileDetector().detect_from_dovi_tool(orphan_mkv).sub_profile is not DoviSubProfile.UNKNOWN


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe requis")
def test_orphan_seekhead_full_workflow_x265_with_dolby_vision_copy(orphan_mkv, tmp_path, qt_app):
    """RV7-03 : pipeline mono-piste (MetadataInjectRunner) sur la même source."""
    from core.workflows.encode import EncodeConfig, EncodeWorkflow, VideoEncodeSettings
    from tests.integration._synth import wait_task

    output = tmp_path / "out.mkv"
    video = VideoEncodeSettings(
        codec="libx265", crf=30, preset="ultrafast", copy_dv=True, source_path=orphan_mkv, stream_index=0,
    )
    config = EncodeConfig(
        source=orphan_mkv, output=output, video=video, video_tracks=[video], audio_tracks=[],
        copy_subtitles=False, keep_chapters=False, duration_s=0.5, work_dir=tmp_path / "work",
    )
    workflow = EncodeWorkflow(ffmpeg_bin="ffmpeg", ram_buffer_enabled=False, generate_nfo=False)
    state = wait_task(workflow.run(config), timeout=300.0)
    assert state["failed"] is None, state["failed"]
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_streams", "-of", "json", str(output)],
        capture_output=True, text=True, check=True,
    ).stdout
    stream = json.loads(probe)["streams"][0]
    assert stream["nb_read_frames"] == "12"
    record = next(side for side in stream.get("side_data_list", []) if side.get("side_data_type") == "DOVI configuration record")
    assert record["dv_profile"] == 8 and record["dv_bl_signal_compatibility_id"] == 1
    assert strict_demuxer_reads_tracks(output)
    output_rpu = tmp_path / "output-rpu.bin"
    _run(["dovi_tool", "extract-rpu", "-i", str(output), "-o", str(output_rpu)])
    summary = subprocess.run(
        ["dovi_tool", "info", "-i", str(output_rpu), "--summary"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "Frames: 12" in summary
