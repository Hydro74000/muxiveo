"""Diagnostics en WARN : entrelacement des sorties et pistes « default » multiples."""
from __future__ import annotations

from pathlib import Path

from core.matroska import ebml
from core.matroska.editors.statistics import MatroskaTrackStatisticsEditor
from core.matroska.ids import CODEC_ID_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaBlock, MatroskaTrack
from core.matroska.validation import (
    InterleaveTracker,
    MatroskaPacketValidation,
    interleave_warnings,
    mux_track_labels,
    scan_interleave,
)
from core.matroska.writer import MatroskaWriter
from core.workflows.common.matroska_finalize import ffmpeg_output_interleave_warnings
from core.workflows.common.metadata import duplicate_default_track_warnings, output_track_label

_MS = 1_000_000


def _track(number: int, uid: int, kind: int, codec: str) -> MatroskaTrack:
    raw = b"".join((
        ebml.uint_element(TRACK_NUMBER_ID, number),
        ebml.uint_element(TRACK_UID_ID, uid),
        ebml.uint_element(TRACK_TYPE_ID, kind),
        ebml.ascii_element(CODEC_ID_ID, codec),
    ))
    return MatroskaTrack(number, uid, kind, codec, b"", "", "fre" if kind == 2 else "und", "", raw)


def _mux_tracks() -> tuple[MatroskaMuxTrack, ...]:
    return (
        MatroskaMuxTrack(Path("v.mkv"), _track(1, 101, 1, "V_MPEG4/ISO/AVC"), 1, 101),
        MatroskaMuxTrack(Path("a.mkv"), _track(2, 102, 2, "A_AC3"), 2, 102, language="fre", name="Français"),
    )


def _bad_packets() -> list[MatroskaMuxPacket]:
    """Audio écrit d'un bloc avant la vidéo (60 s d'avance) : la vidéo arrive en retard."""
    audio = [MatroskaMuxPacket(2, MatroskaBlock(2, ms, 0x80, b"a", timestamp_ns=ms * _MS), i)
             for i, ms in enumerate(range(0, 60_000, 500))]
    video = [MatroskaMuxPacket(1, MatroskaBlock(1, ms, 0x80, b"v", timestamp_ns=ms * _MS), i)
             for i, ms in enumerate(range(0, 60_000, 1000))]
    return audio + video


def test_tracker_reports_lagging_track_with_label() -> None:
    tracker = InterleaveTracker()
    for ms in range(0, 10_000, 100):
        tracker.packet(2, ms * _MS)
    for ms in range(0, 10_000, 1000):
        tracker.packet(1, ms * _MS)
    report = tracker.report()
    assert report.max_lag_ns[1] == 9_900 * _MS
    assert report.lag_at_ns[1] == 0
    assert report.lagged_packets == {1: 8}
    [message] = interleave_warnings(report, track_labels={1: "piste #1 (vidéo)"})
    assert message.startswith("Entrelacement : piste #1 (vidéo) écrite jusqu'à 9,9 s")
    assert "8 paquet(s)" in message


def test_clean_order_and_missing_report_give_no_warning() -> None:
    tracker = InterleaveTracker()
    for ms in range(0, 10_000, 40):
        tracker.packet(1 + (ms // 40) % 2, ms * _MS)
    assert interleave_warnings(tracker.report()) == []
    assert interleave_warnings(None) == []


def test_native_writer_measures_interleave_of_written_order(tmp_path: Path) -> None:
    received: list[MatroskaPacketValidation] = []
    plan = MatroskaMuxPlan(tmp_path / "bad.mkv", _mux_tracks(), iter(_bad_packets()))
    MatroskaWriter().write(plan, external_validator=lambda _path, summary: received.append(summary))
    report = received[0].interleave
    assert report is not None and report.lagged_packets.get(1)
    labels = mux_track_labels(plan.tracks)
    assert labels == {1: "piste #1 (vidéo)", 2: "piste #2 (audio fre « Français »)"}
    assert any("piste #1 (vidéo)" in message for message in interleave_warnings(report, track_labels=labels))


def test_ffmpeg_outputs_measured_by_statistics_pass_or_header_scan(tmp_path: Path) -> None:
    output = tmp_path / "bad.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(output, _mux_tracks(), iter(_bad_packets())))
    result = MatroskaTrackStatisticsEditor().apply(output, writing_app="test")
    summary = result.packet_validation
    assert summary is not None and summary.interleave is not None
    assert summary.interleave.lagged_packets.get(1)
    from_statistics = ffmpeg_output_interleave_warnings(output, summary)
    from_scan = ffmpeg_output_interleave_warnings(output, None)
    assert from_statistics and from_scan
    assert scan_interleave(output).backward_clusters >= 1


def test_duplicate_default_tracks_warn_per_type() -> None:
    messages = duplicate_default_track_warnings([
        ("video", True, output_track_label(1, "und", "")),
        ("audio", True, output_track_label(2, "fre", "French DDP 5.1")),
        ("subtitle", True, output_track_label(4, "fre", "French Forced")),
        ("subtitle", True, output_track_label(6, "eng", "English")),
        ("subtitle", False, output_track_label(7, "eng", "English SDH")),
    ])
    assert messages == [
        "Plusieurs pistes sous-titres marquées par défaut : #4 (fre « French Forced »), #6 (eng « English »). "
        "Le lecteur choisit selon ses préférences de langue, sinon la première."
    ]
