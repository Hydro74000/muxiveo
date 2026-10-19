"""Les CC sont retirés seuls : images, HDR et décisions UI restent cohérents."""
from __future__ import annotations

from dataclasses import replace
import shutil
import subprocess
import time

import pytest
from PySide6.QtCore import Qt

from core.closed_captions import CaptionFilter, detect_matroska_captions
from core.h26x_sei import build_sei_rbsp
from core.inspector import FileInspector
from core.i18n import translate_text
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaReader
from core.matroska.writer import MatroskaWriter
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry, tracks_from_file_info

CC = b"\xb5\x00\x31GA94\x03\x41\x00\xfc\x94\x20\xff"
HDR10PLUS = b"\xb5\x00\x3c\x00\x01\x04\x01\x00\x00\x01"


def sei(codec, messages, *, suffix=False):
    header = bytes([80 if suffix else 78, 1]) if codec == "hevc" else b"\x06"
    return header + build_sei_rbsp(messages)


@pytest.mark.parametrize("codec", ["hevc", "h264"])
@pytest.mark.parametrize("framing", ["annexb", "length"])
def test_selective_filter_preserves_images_hdr_and_unknown_sei(codec, framing):
    # HDR10+ partage le type SEI 4 avec les CC ; supprimer le type entier
    # serait une régression. Même les SEI groupés et les RPU restent intacts.
    messages = [(137, bytes(24)), (4, CC), (4, HDR10PLUS), (144, b"\x03\xe8\x01\x90"),
                (5, bytes(range(16)) + b"encoder"), (300, b"unknown" * 50)]
    mixed = sei(codec, messages)
    cleaned = sei(codec, [message for message in messages if message[1] != CC])
    image = b"\x26\x01\x80\x11\x22" if codec == "hevc" else b"\x65\x80\x11\x22"
    rpu = b"\x7c\x01\x00\x00\x03\x01" if codec == "hevc" else b"\x09\x10"
    private = bytes(21) + b"\x03\x00" if codec == "hevc" else bytes(4) + b"\x03"
    private = b"\x01" + private[1:]
    filter_ = CaptionFilter(codec, private if framing == "length" else b"")
    def pack(nals):
        return b"".join((len(nal).to_bytes(4, "big") if framing == "length" else b"\x00\x00\x00\x01") + nal for nal in nals)
    original = pack([mixed, image, rpu])
    assert filter_.packet(original) == pack([cleaned, image, rpu])
    assert filter_.packet(pack([cleaned, image, rpu])) == pack([cleaned, image, rpu])


def test_hvcc_and_suffix_sei_are_filtered_without_losing_hdr_arrays():
    caption = sei("hevc", [(4, CC)], suffix=True)
    hdr = sei("hevc", [(4, HDR10PLUS)])
    arrays = [bytes([0x80 | kind]) + b"\x00\x01" + len(nal).to_bytes(2, "big") + nal
              for kind, nal in [(40, caption), (39, hdr), (32, b"\x40\x01parameter")]]
    header = b"\x01" + bytes(20) + b"\x03"
    private = header + bytes([len(arrays)]) + b"".join(arrays)
    filter_ = CaptionFilter("hevc", private)
    assert filter_.private(private) == header + b"\x02" + b"".join(arrays[1:])
    assert filter_.nal(caption) == b""
    assert filter_.nal(sei("hevc", [(4, b"\xb5\x00\x31DTG1\x03afd")])) != b""


def test_malformed_caption_sei_fails_without_dropping_unknown_data():
    filter_ = CaptionFilter("hevc")
    with pytest.raises(ValueError, match="tronqué"):
        filter_.nal(b"\x4e\x01\x04\xff\xff\x00" + CC)
    unknown = b"\x4e\x01\x04\xff\xff\x00unknown"
    assert filter_.nal(unknown) == unknown


def test_remux_caption_row_is_unchecked_linked_and_never_mapped(qt_app):
    from ui.panels.remux_panel.widgets.track_table import _TrackTable
    table = _TrackTable()
    try:
        video = TrackEntry(0, "video", "HEVC", "", "", "", file_id="src0", has_closed_captions=True)
        plain = TrackEntry(1, "video", "HEVC", "", "", "", file_id="src0")
        table.append_tracks("#5588ff", [video, plain])
        rows = table.current_tracks(include_embedded=True)
        assert len(rows) == 3
        embedded = next(track for track in rows if track.embedded_caption_parent_id)
        assert not embedded.enabled and video.remove_closed_captions
        assert embedded.display_info == translate_text("Sous-titre embarqué EIA-608/708")
        assert table.current_tracks() == [video, plain]
        row = next(row for row in range(table.rowCount())
                   if table.item(row, table.COL_CHECK).data(Qt.ItemDataRole.UserRole) is embedded)
        assert not table.item(row, table.COL_TITLE).flags() & Qt.ItemFlag.ItemIsEditable
        table.item(row, table.COL_CHECK).setCheckState(Qt.CheckState.Checked)
        table.current_tracks()
        assert video.remove_closed_captions is False
        table.clear_all()
        table.append_tracks("#5588ff", [video])  # Restauration : conserver la décision sauvegardée.
        assert next(t for t in table.current_tracks(include_embedded=True) if t.embedded_caption_parent_id).enabled
        assert table.remove_track_by_entry_id(video.entry_id)
        assert not table.rowCount()
    finally:
        table.close()
        table.deleteLater()


@pytest.fixture
def caption_source(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg requis")
    plain = tmp_path / "plain.mkv"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64:r=24:d=0.5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5",
        "-c:v", "libx264", "-threads", "1", "-c:a", "ac3", str(plain),
    ], check=True, capture_output=True, timeout=20)
    reader = MatroskaReader(plain)
    tracks = reader.tracks()
    video = next(track for track in tracks if track.track_type == 1)
    length_size = CaptionFilter(video.codec_id, video.codec_private).length_size
    caption = sei("h264", [(4, CC)])
    prefix = len(caption).to_bytes(length_size, "big") + caption
    output = tmp_path / "captions.mkv"
    MatroskaWriter().write(MatroskaMuxPlan(
        output, tuple(MatroskaMuxTrack(plain, track, track.number, track.uid,
                      patch_language=False, patch_name=False, patch_flags=False) for track in tracks),
        tuple(MatroskaMuxPacket(block.track_number, replace(block, payload=prefix + block.payload)
                                if block.track_number == video.number else block, index)
              for index, block in enumerate(reader.blocks())),
    ))
    return output, plain


def test_non_matroska_detection_uses_compressed_packets(caption_source, tmp_path):
    source, _ = caption_source
    mp4 = tmp_path / "captions.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:v:0", "-c", "copy", str(mp4)],
                   check=True, capture_output=True, timeout=15)
    assert FileInspector().inspect(mp4).primary_video.has_closed_captions


def test_filter_handles_all_frames_in_laced_video_blocks(caption_source, tmp_path):
    from core.matroska.closed_captions import filter_caption_plan
    from core.matroska.reader import _split_laces
    source, _ = caption_source
    reader = MatroskaReader(source)
    video = reader.tracks()[0]
    block = next(reader.blocks(track_numbers={video.number}))
    laced = replace(block, flags=(block.flags & ~6) | 4, lace_count=2, lacing_mode=2,
                    encoded_frames_payload=b"\x01" + block.payload * 2)
    plan = MatroskaMuxPlan(tmp_path / "unused.mkv", (MatroskaMuxTrack(
        source, video, 1, video.uid, remove_closed_captions=True,
    ),), (MatroskaMuxPacket(1, laced),))
    rewritten = next(iter(filter_caption_plan(plan).packets)).block
    expected = CaptionFilter(video.codec_id, video.codec_private).packet(block.payload)
    assert _split_laces(rewritten.encoded_frames_payload, rewritten.flags) == (expected, expected)
    assert rewritten.timestamp_ns == block.timestamp_ns


def test_caption_rewrite_preserves_container_application_metadata(caption_source, tmp_path):
    from core.matroska.closed_captions import strip_candidate_captions
    source, _ = caption_source
    candidate = tmp_path / "candidate.mkv"
    shutil.copy2(source, candidate)
    original_apps = MatroskaReader(candidate).segment_info_apps()
    strip_candidate_captions(candidate, {0})
    assert MatroskaReader(candidate).segment_info_apps() == original_apps
    assert not detect_matroska_captions(candidate)


def test_stopping_filtered_stream_closes_its_source(caption_source, tmp_path):
    from core.matroska.closed_captions import filter_caption_plan
    source, _ = caption_source
    reader = MatroskaReader(source)
    track = reader.tracks()[0]
    closed = []
    def packets():
        try:
            for block in reader.blocks(track_numbers={track.number}):
                yield MatroskaMuxPacket(1, block)
        finally:
            closed.append(True)
    plan = MatroskaMuxPlan(tmp_path / "unused.mkv", (MatroskaMuxTrack(
        source, track, 1, track.uid, remove_closed_captions=True,
    ),), packets())
    filtered = filter_caption_plan(plan).packets
    next(iter(filtered))
    filtered.close()
    assert closed == [True]


@pytest.mark.parametrize("backend", ["native", "ffmpeg"])
@pytest.mark.parametrize("remove", [False, True])
def test_real_remux_retains_or_removes_cc_without_reencoding(qt_app, caption_source, tmp_path, backend, remove):
    from core.workflows.remux import RemuxWorkflow
    from core.profiles.selectors import remux_config_to_exact_job
    from cli.remux_config import build_remux_config
    from cli.options import CommonOptions
    from cli.logging import Logger
    from core.config import AppConfig

    source, plain = caption_source
    info = FileInspector().inspect(source)
    assert info.primary_video.has_closed_captions
    assert not FileInspector().inspect(plain).primary_video.has_closed_captions
    tracks = tracks_from_file_info(info, "src0")
    tracks[0].remove_closed_captions = remove
    config = RemuxConfig([SourceInput(source, 0, tracks)], tmp_path / f"out-{backend}-{remove}.mkv",
                         [(0, track.mkv_tid, track.entry_id) for track in tracks], mux_backend=backend)
    job = remux_config_to_exact_job(config)
    restored = build_remux_config(job, AppConfig(), CommonOptions(), Logger())
    assert restored.sources[0].tracks[0].remove_closed_captions is remove
    workflow = RemuxWorkflow(generate_nfo=False)
    result = []
    signals = workflow.run(config)
    signals.finished.connect(lambda path: result.append((True, path)))
    signals.failed.connect(lambda message, error: result.append((False, message)))
    deadline = time.monotonic() + 20
    while not result and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert result and result[0][0], result
    assert bool(detect_matroska_captions(config.output)) is not remove
    before = list(MatroskaReader(source).blocks())
    after = list(MatroskaReader(config.output).blocks())
    assert len(before) == len(after)
    filter_ = CaptionFilter("h264", MatroskaReader(source).tracks()[0].codec_private)
    for number in (1, 2):
        expected = [filter_.packet(block.payload) if remove and number == 1 else block.payload
                    for block in before if block.track_number == number]
        assert [block.payload for block in after if block.track_number == number] == expected
        # Comparaison dans l'ordre de décodage de chaque piste : FFmpeg peut
        # changer l'entrelacement audio/vidéo, sans réencoder leurs données.
