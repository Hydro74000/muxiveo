"""Lecteur Matroska séquentiel (pipe) : en-tête recopié, blocs à la demande, erreurs explicites."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from core.matroska import ebml
from core.matroska.ids import CODEC_ID_ID, TRACK_NUMBER_ID, TRACK_TYPE_ID, TRACK_UID_ID
from core.matroska.mux_plan import MatroskaMuxPacket, MatroskaMuxPlan, MatroskaMuxTrack
from core.matroska.reader import MatroskaBlock, MatroskaReader, MatroskaTrack
from core.matroska.stream_source import MatroskaStreamSource, open_stream_reader
from core.matroska.writer import MatroskaWriter

_MS = 1_000_000


def _track(number: int, uid: int, kind: int, codec: str, codec_private: bytes = b"") -> MatroskaTrack:
    raw = b"".join((
        ebml.uint_element(TRACK_NUMBER_ID, number),
        ebml.uint_element(TRACK_UID_ID, uid),
        ebml.uint_element(TRACK_TYPE_ID, kind),
        ebml.ascii_element(CODEC_ID_ID, codec),
        ebml.binary_element(bytes.fromhex("63a2"), codec_private) if codec_private else b"",
    ))
    return MatroskaTrack(number, uid, kind, codec, codec_private, "", "und", "", raw)


def _write(path: Path, *, video_codec: str = "V_MS/VFW/FOURCC", codec_private: bytes = b"x") -> Path:
    packets: list[MatroskaMuxPacket] = []
    decode_order = [0, 120, 40, 80, 240, 160, 200]
    for i, ms in enumerate(decode_order):
        references = () if i in (0, 4) else (-40 * _MS,)
        packets.append(MatroskaMuxPacket(1, MatroskaBlock(
            1, ms, 0, f"v{ms}".encode(), timestamp_ns=ms * _MS, references_ns=references,
            references=tuple(round(value / _MS) for value in references), is_keyframe=not references,
        ), i))
    packets += [MatroskaMuxPacket(2, MatroskaBlock(2, ms, 0x80, b"a" * 100, timestamp_ns=ms * _MS), i)
                for i, ms in enumerate(range(0, 280, 20))]
    packets.append(MatroskaMuxPacket(3, MatroskaBlock(3, 50, 0x80, b"sub", timestamp_ns=50 * _MS,
                                                      duration_ms=1000, duration_ns=1000 * _MS), 0))
    MatroskaWriter().write(MatroskaMuxPlan(
        path,
        (
            MatroskaMuxTrack(Path("v.mkv"), _track(1, 11, 1, video_codec, codec_private), 1, 11),
            MatroskaMuxTrack(Path("a.mkv"), _track(2, 12, 2, "A_AC3"), 2, 12),
            MatroskaMuxTrack(Path("s.mkv"), _track(3, 13, 17, "S_TEXT/UTF8"), 3, 13),
        ),
        tuple(packets),
        regenerate_statistics=True,
    ))
    return path


def _summary(blocks) -> list[tuple]:
    return [(b.track_number, b.timestamp_ns, b.duration_ns, b.references_ns, b.payload, b.is_keyframe)
            for b in blocks]


def test_stream_matches_file_reading(tmp_path: Path) -> None:
    source = _write(tmp_path / "src.mkv")
    with source.open("rb") as handle:
        reader = open_stream_reader(handle, tmp_path / "header.mkv", duration_ns=280 * _MS)
        assert [(t.number, t.uid, t.codec_id) for t in reader.tracks()] == [
            (t.number, t.uid, t.codec_id) for t in MatroskaReader(source).tracks()
        ]
        assert reader.timestamp_scale_ns() == _MS
        assert reader.segment_duration_ns() == 280 * _MS
        streamed = _summary(reader.blocks(first_lace_only=True))
    assert streamed == _summary(MatroskaReader(source).blocks(first_lace_only=True))
    # Le fichier d'en-tête s'arrête avant le premier Cluster.
    assert (tmp_path / "header.mkv").stat().st_size < source.stat().st_size


def test_blocks_are_single_use_and_filtered(tmp_path: Path) -> None:
    source = _write(tmp_path / "src.mkv")
    with source.open("rb") as handle:
        reader = open_stream_reader(handle, tmp_path / "header.mkv")
        assert {b.track_number for b in reader.blocks(track_numbers={3})} == {3}
        with pytest.raises(ValueError, match="lecture unique"):
            list(reader.blocks())


def test_missing_codec_private_is_refused(tmp_path: Path) -> None:
    source = _write(tmp_path / "src.mkv", video_codec="V_MPEGH/ISO/HEVC", codec_private=b"")
    with source.open("rb") as handle, pytest.raises(ValueError, match="CodecPrivate absent"):
        open_stream_reader(handle, tmp_path / "header.mkv")


def test_truncated_stream_is_refused(tmp_path: Path) -> None:
    source = _write(tmp_path / "src.mkv")
    data = source.read_bytes()
    first_cluster = next(e for e in MatroskaReader(source).top_level() if e.element_id == bytes.fromhex("1f43b675"))
    truncated = io.BytesIO(data[:first_cluster.payload_offset + 20])
    reader = open_stream_reader(truncated, tmp_path / "header.mkv")
    with pytest.raises(ValueError, match="tronqué"):
        list(reader.blocks())


def test_unknown_size_cluster_is_refused(tmp_path: Path) -> None:
    source = _write(tmp_path / "src.mkv")
    data = bytearray(source.read_bytes())
    cluster = next(e for e in MatroskaReader(source).top_level() if e.element_id == bytes.fromhex("1f43b675"))
    size_offset = cluster.offset + 4
    size_length = cluster.payload_offset - size_offset
    data[size_offset:cluster.payload_offset] = bytes([0x01 << (8 - size_length) | ((1 << (8 - size_length)) - 1)]) \
        + b"\xff" * (size_length - 1)
    with pytest.raises(ValueError, match="taille inconnue"):
        MatroskaStreamSource(io.BytesIO(bytes(data)), tmp_path / "header.mkv").read_header()


def test_oversized_element_is_refused(tmp_path: Path) -> None:
    source = _write(tmp_path / "src.mkv")
    small = MatroskaStreamSource(io.BytesIO(source.read_bytes()), tmp_path / "header.mkv", max_element_bytes=8)
    with pytest.raises(ValueError, match="au-delà de la borne"):
        small.read_header()
