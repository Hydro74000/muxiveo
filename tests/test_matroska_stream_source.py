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
    # Trois modes de lacing (Xiph, taille fixe, EBML) et un BlockGroup complet.
    for i, (flags, encoded) in enumerate(((0x82, b"\x01\x01abb"), (0x84, b"\x01abcd"), (0x86, b"\x01\x81abb"))):
        packets.append(MatroskaMuxPacket(2, MatroskaBlock(
            2, 300 + i * 20, flags, encoded[-2:], lace_index=0, lace_count=2, lacing_mode=(flags >> 1) & 3,
            encoded_frames_payload=encoded, timestamp_ns=(300 + i * 20) * _MS,
        ), 100 + i))
    packets.append(MatroskaMuxPacket(2, MatroskaBlock(
        2, 400, 0x80, b"extra", timestamp_ns=400 * _MS, duration_ms=20, duration_ns=20 * _MS,
        discard_padding_ns=1_234, codec_state=b"state", block_additions=b"\xa6\x84\xa5\x82ok",
    ), 200))
    MatroskaWriter().write(MatroskaMuxPlan(
        path,
        (
            MatroskaMuxTrack(Path("v.mkv"), _track(1, 11, 1, video_codec, codec_private), 1, 11),
            MatroskaMuxTrack(Path("a.mkv"), _track(2, 12, 2, "A_AC3"), 2, 12),
            MatroskaMuxTrack(Path("s.mkv"), _track(3, 13, 17, "S_TEXT/UTF8"), 3, 13),
        ),
        tuple(packets),
        regenerate_statistics=True,
        opaque_top_level=(ebml.element(bytes.fromhex("1043a770"), ebml.element(bytes.fromhex("45b9"), ebml.element(
            bytes.fromhex("b6"), ebml.uint_element(bytes.fromhex("73c4"), 7) + ebml.uint_element(bytes.fromhex("91"), 0),
        ))),),
    ))
    return path


def _summary(blocks) -> list[tuple]:
    return [(b.track_number, b.timestamp_ns, b.duration_ns, b.references_ns, b.payload, b.is_keyframe,
             b.lace_count, b.lacing_mode, b.encoded_frames_payload, b.discard_padding_ns, b.codec_state,
             b.block_additions) for b in blocks]


class _Fragmented(io.RawIOBase):
    """Flux qui ne rend que quelques octets par lecture (pipe lent)."""

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        return self._data.read(min(3, size if size >= 0 else 3))


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


def test_fragmented_reads_and_metadata_before_clusters(tmp_path: Path) -> None:
    from core.matroska.ids import CHAPTERS_ID

    source = _write(tmp_path / "src.mkv")
    reader = open_stream_reader(_Fragmented(source.read_bytes()), tmp_path / "header.mkv")  # type: ignore[arg-type]
    assert _summary(reader.blocks(first_lace_only=True)) == _summary(MatroskaReader(source).blocks(first_lace_only=True))
    assert reader.raw_top_level(CHAPTERS_ID) == MatroskaReader(source).raw_top_level(CHAPTERS_ID)


def _crafted(clusters: list[bytes], *, segment_size: int | None = None, trailing: bytes = b"") -> bytes:
    from core.matroska.ebml import element, encode_unknown_size_marker, encode_vint_size_minimal, uint_element

    ebml_header = element(bytes.fromhex("1a45dfa3"), ebml.ascii_element(bytes.fromhex("4282"), "matroska"))
    info = element(bytes.fromhex("1549a966"), uint_element(bytes.fromhex("2ad7b1"), 1_000_000))
    tracks = element(bytes.fromhex("1654ae6b"), element(bytes.fromhex("ae"), _track(1, 11, 2, "A_AC3").raw_entry))
    body = info + tracks + b"".join(clusters)
    size = encode_unknown_size_marker(length=8) if segment_size is None else encode_vint_size_minimal(segment_size)
    return ebml_header + bytes.fromhex("18538067") + size + body + trailing


def _block(offset_ms: int, payload: bytes = b"a") -> bytes:
    from core.matroska.ebml import element

    return element(bytes.fromhex("a3"), b"\x81" + offset_ms.to_bytes(2, "big", signed=True) + b"\x80" + payload)


def _cluster(content: bytes, *, declared: int | None = None) -> bytes:
    from core.matroska.ebml import encode_vint_size_minimal

    return bytes.fromhex("1f43b675") + encode_vint_size_minimal(len(content) if declared is None else declared) + content


def _timestamp(ms: int) -> bytes:
    return ebml.uint_element(bytes.fromhex("e7"), ms)


def test_block_overflowing_its_cluster_is_refused_before_being_yielded(tmp_path: Path) -> None:
    content = _timestamp(0) + _block(0)
    data = _crafted([_cluster(content, declared=len(content) - 1) + b"\x00"])
    blocks = open_stream_reader(io.BytesIO(data), tmp_path / "header.mkv").blocks()
    with pytest.raises(ValueError, match="débordant de son Cluster"):
        next(blocks)


def test_block_before_timestamp_and_cluster_without_timestamp_are_refused(tmp_path: Path) -> None:
    before = _crafted([_cluster(_block(0) + _timestamp(0))])
    with pytest.raises(ValueError, match="avant le Timestamp"):
        list(open_stream_reader(io.BytesIO(before), tmp_path / "a.mkv").blocks())
    without = _crafted([_cluster(_timestamp(0) + _block(0)), _cluster(ebml.element(bytes.fromhex("ec"), b"\0"))])
    with pytest.raises(ValueError, match="Cluster sans Timestamp"):
        list(open_stream_reader(io.BytesIO(without), tmp_path / "b.mkv").blocks())


def test_declared_segment_size_bounds_the_stream(tmp_path: Path) -> None:
    clusters = [_cluster(_timestamp(0) + _block(0)), _cluster(_timestamp(40) + _block(0))]
    full = _crafted(clusters)
    body_size = len(full) - full.index(bytes.fromhex("1549a966"))  # Info + Tracks + Clusters
    # Taille déclarée : la suite du flux (hors Segment) n'est pas lue.
    bounded = _crafted(clusters, segment_size=body_size, trailing=_cluster(_timestamp(80) + _block(0)))
    reader = open_stream_reader(io.BytesIO(bounded), tmp_path / "a.mkv")
    assert [b.timestamp_ns for b in reader.blocks()] == [0, 40 * _MS]
    # Flux coupé avant la fin déclarée : troncature signalée.
    truncated = _crafted(clusters, segment_size=body_size + 100)
    with pytest.raises(ValueError, match="tronqué"):
        list(open_stream_reader(io.BytesIO(truncated), tmp_path / "b.mkv").blocks())


def test_position_based_operations_are_refused(tmp_path: Path) -> None:
    source = _write(tmp_path / "src.mkv")
    with source.open("rb") as handle:
        reader = open_stream_reader(handle, tmp_path / "header.mkv")
        assert reader.cue_block_positions({1}) is None
        for operation in (reader.block_summaries, reader.cluster_elements):
            with pytest.raises(ValueError, match="lu séquentiellement"):
                operation()
        with pytest.raises(ValueError, match="lu séquentiellement"):
            reader.indexed_blocks(set())
