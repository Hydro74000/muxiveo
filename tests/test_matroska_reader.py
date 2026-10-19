from pathlib import Path

import pytest

from core.matroska.ebml import element, encode_vint_size_minimal, float_element, sint_element, uint_element
from core.matroska.ids import EBML_HEADER_ID, INFO_ID, SEGMENT_ID
from core.matroska.reader import MatroskaReader


def test_reader_enumerates_segment_children(tmp_path: Path) -> None:
    path = tmp_path / "sample.mkv"
    # Minimal EBML prefix is sufficient for the boundary reader.
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(INFO_ID, b""))

    reader = MatroskaReader(path)
    assert reader.segment().element_id == SEGMENT_ID
    children = list(reader.top_level())
    assert [child.element_id for child in children] == [INFO_ID]


def test_reader_extracts_track_entry_fields(tmp_path: Path) -> None:
    tracks = bytes.fromhex("1654ae6b")
    entry = bytes.fromhex("ae")
    number, uid, kind = bytes.fromhex("d7"), bytes.fromhex("73c5"), bytes.fromhex("83")
    codec, language, name = bytes.fromhex("86"), bytes.fromhex("22b59c"), bytes.fromhex("536e")
    track = element(entry, b"".join((
        element(number, b"\x01"), element(uid, b"\x02"), element(kind, b"\x01"),
        element(codec, b"V_MPEGH/ISO/HEVC"), element(language, b"fra"), element(name, "Vidéo".encode()),
    )))
    path = tmp_path / "tracks.mkv"
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(tracks, track))
    result = MatroskaReader(path).tracks()
    assert len(result) == 1
    assert (result[0].number, result[0].uid, result[0].codec_id, result[0].language, result[0].name) == (1, 2, "V_MPEGH/ISO/HEVC", "fra", "Vidéo")


def test_reader_extracts_simple_block_timestamp(tmp_path: Path) -> None:
    cluster, timestamp, block = bytes.fromhex("1f43b675"), bytes.fromhex("e7"), bytes.fromhex("a3")
    payload = element(timestamp, b"\x64") + element(block, b"\x81\x00\x05\x80payload")
    path = tmp_path / "blocks.mkv"
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(cluster, payload))
    blocks = list(MatroskaReader(path).simple_blocks())
    assert len(blocks) == 1
    assert (blocks[0].track_number, blocks[0].timestamp_ms, blocks[0].payload) == (1, 105, b"payload")


def _blocks_file(tmp_path: Path, block_payload: bytes, *, grouped: bool = False) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cluster, timestamp = bytes.fromhex("1f43b675"), bytes.fromhex("e7")
    if grouped:
        body = element(bytes.fromhex("a1"), block_payload)
        body += element(bytes.fromhex("9b"), b"\x28")
        body += element(bytes.fromhex("fb"), b"\xff")
        packet = element(bytes.fromhex("a0"), body)
    else:
        packet = element(bytes.fromhex("a3"), block_payload)
    path = tmp_path / ("group.mkv" if grouped else "lace.mkv")
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(cluster, element(timestamp, b"\x00") + packet))
    return path


def test_reader_decodes_fixed_and_xiph_lacing(tmp_path: Path) -> None:
    fixed = _blocks_file(tmp_path, b"\x81\x00\x00\x84\x01aabb")  # 2 frames fixed
    assert [b.payload for b in MatroskaReader(fixed).blocks()] == [b"aa", b"bb"]
    xiph = _blocks_file(tmp_path, b"\x81\x00\x00\x82\x01\x01abb")
    xiph_blocks = list(MatroskaReader(xiph).blocks())
    assert [b.payload for b in xiph_blocks] == [b"a", b"bb"]
    assert [b.timestamp_ns for b in xiph_blocks] == [0, None]


def test_reader_decodes_ebml_lacing_and_block_group(tmp_path: Path) -> None:
    ebml = _blocks_file(tmp_path, b"\x81\x00\x00\x86\x01\x81abb")
    assert [b.payload for b in MatroskaReader(ebml).blocks()] == [b"a", b"bb"]
    grouped = list(MatroskaReader(_blocks_file(tmp_path, b"\x81\x00\x05\x00frame", grouped=True)).blocks())
    assert grouped[0].timestamp_ms == 5
    assert grouped[0].duration_ms == 40
    assert grouped[0].references == (-1,)
    assert grouped[0].is_keyframe is False


def test_reader_resumes_after_unknown_size_clusters(tmp_path: Path) -> None:
    cluster, timestamp, block = bytes.fromhex("1f43b675"), bytes.fromhex("e7"), bytes.fromhex("a3")
    first = cluster + b"\xff" + element(timestamp, b"\x00") + element(block, b"\x81\x00\x00\x80a")
    second = cluster + b"\xff" + element(timestamp, b"\x0a") + element(block, b"\x81\x00\x00\x80b")
    path = tmp_path / "unknown-clusters.mkv"
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + first + second)
    reader = MatroskaReader(path)
    assert [item.element_id for item in reader.top_level()] == [cluster, cluster]
    assert [(item.timestamp_ms, item.payload) for item in reader.blocks()] == [(0, b"a"), (10, b"b")]


@pytest.mark.parametrize("scale_ns", [1_000, 1_000_000, 10_000_000])
@pytest.mark.parametrize("track_numbers", [None, {129}, {2}, set()])
def test_packet_reader_matches_stream_for_laces_and_block_group_metadata(
    tmp_path, scale_ns, track_numbers,
):
    """Même ordre, nanosecondes et payloads : mmap, flux et mux lacé."""
    def raw(track, relative, flags, payload):
        return encode_vint_size_minimal(track) + relative.to_bytes(2, "big", signed=True) + bytes([flags]) + payload

    packets = b"".join(
        element(b"\xa3", raw(129, relative, flags, payload))
        for relative, flags, payload in (
            (3, 0x89, b"plain"), (-2, 0x82, b"\x01\x01abb"),
            (1, 0x84, b"\x01aabb"), (0, 0x86, b"\x01\x81abb"),
            (4, 0x82, b"\x02\xff\x2d\xff\x2d" + b"x" * 900),
        )
    )
    additions = element(b"\xa6", uint_element(b"\xee", 1) + element(b"\xa5", b"extra"))
    group = element(b"\xa0", b"".join((
        element(b"\xa1", raw(2, -3, 0x0C, b"\x01ccdd")),
        uint_element(b"\x9b", 5), sint_element(b"\xfb", -2), sint_element(b"\xfb", 3),
        sint_element(bytes.fromhex("75a2"), -123456),
        element(b"\xa4", b"codec-state"), element(bytes.fromhex("75a1"), additions),
    )))
    cluster = bytes.fromhex("1f43b675")
    path = tmp_path / "packet-parity.mkv"
    path.write_bytes(
        element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff"
        + element(INFO_ID, uint_element(bytes.fromhex("2ad7b1"), scale_ns))
        + cluster + b"\xff" + uint_element(b"\xe7", 40) + packets + group
        + cluster + b"\xff" + uint_element(b"\xe7", 80) + packets + group
    )
    reader = MatroskaReader(path)
    streamed = list(reader._blocks_stream(track_numbers=track_numbers))
    assert list(reader.blocks(track_numbers=track_numbers)) == streamed
    first_frames = [block for block in streamed if block.lace_index == 0]
    assert list(reader.blocks(track_numbers=track_numbers, first_lace_only=True)) == first_frames
    assert list(reader._blocks_stream(track_numbers=track_numbers, first_lace_only=True)) == first_frames
    if track_numbers is None:
        grouped = next(block for block in first_frames if block.track_number == 2)
        assert grouped.timestamp_ns == 37 * scale_ns
        assert grouped.references_ns == (-2 * scale_ns, 3 * scale_ns)
        assert grouped.duration_ns == 5 * scale_ns
        assert grouped.discard_padding_ns == -123456
        assert grouped.codec_state == b"codec-state"
        assert grouped.block_additions == additions


@pytest.mark.parametrize("error", [OSError, ValueError, OverflowError])
def test_packet_reader_falls_back_when_mapping_unavailable(tmp_path, monkeypatch, error):
    import core.matroska.reader as module

    path = _blocks_file(tmp_path, b"\x81\x00\x00\x86\x01\x81abb", grouped=True)
    reader = MatroskaReader(path)
    expected = list(reader._blocks_stream(first_lace_only=True))

    def unavailable(*args, **kwargs):
        raise error("mapping unavailable")

    monkeypatch.setattr(module.mmap, "mmap", unavailable)
    assert list(reader.blocks(first_lace_only=True)) == expected


def test_packet_reader_consumes_unknown_clusters_without_boundary_prescan(tmp_path, monkeypatch):
    cluster = bytes.fromhex("1f43b675")
    path = tmp_path / "unknown-streaming.mkv"
    path.write_bytes(
        element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(INFO_ID, b"")
        + cluster + b"\xff" + element(b"\xa3", b"\x81\x00\x00\x80first")
        + cluster + b"\xff" + uint_element(b"\xe7", 10)
        + element(b"\xa3", b"\x81\x00\x00\x80second")
        + element(bytes.fromhex("1254c367"), b"")
        + element(cluster, uint_element(b"\xe7", 20) + element(b"\xa3", b"\x81\x00\x00\x80third"))
    )
    reader = MatroskaReader(path)
    expected = list(reader._blocks_stream())

    def unexpected_prescan():
        pytest.fail("Le lecteur de paquets doit trouver les frontières pendant sa passe média")

    monkeypatch.setattr(reader, "top_level", unexpected_prescan)
    assert list(reader.blocks()) == expected
    assert [block.timestamp_ns for block in expected] == [0, 10_000_000, 20_000_000]


@pytest.mark.parametrize("unknown_clusters", [False, True])
def test_packet_reader_window_boundaries_and_oversized_packets(tmp_path, monkeypatch, unknown_clusters):
    import core.matroska.reader as module

    # Fenêtre réduite : plusieurs remappings, un paquet plus grand que la
    # fenêtre, des en-têtes traversant sa frontière et des métadonnées groupées.
    monkeypatch.setattr(module._MappedInput, "_WINDOW_BYTES", 4096)
    cluster = bytes.fromhex("1f43b675")
    media = b""
    for index in range(12):
        block = b"\x81\x00\x00\x80" + bytes([index]) * (5000 if index == 4 else 1021)
        group = element(b"\xa0", element(b"\xa1", block) + uint_element(b"\x9b", 40))
        body = uint_element(b"\xe7", index * 40) + group + element(b"\xa3", block)
        media += cluster + b"\xff" + body if unknown_clusters else element(cluster, body)
    path = tmp_path / "windows.mkv"
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(INFO_ID, b"") + media)
    reader = MatroskaReader(path)
    expected = list(reader._blocks_stream())
    observed = list(reader.blocks())
    assert observed == expected
    # Les bytes déjà émis restent valides après fermeture de toutes les fenêtres.
    assert observed[8].payload == bytes([4]) * 5000


def test_packet_reader_element_header_crosses_mapping_window(tmp_path, monkeypatch):
    import core.matroska.reader as module

    monkeypatch.setattr(module._MappedInput, "_WINDOW_BYTES", 4096)
    prefix = element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + element(INFO_ID, b"")
    timestamp = uint_element(b"\xe7", 0)
    packet = element(b"\xa3", b"\x81\x00\x00\x80" + b"f" * 128)
    # Cluster : ID 4 + VINT 2 ; Void : ID 1 + VINT 2.
    padding = element(b"\xec", b"\x00" * (4095 - len(prefix) - 6 - len(timestamp) - 3))
    document = prefix + element(bytes.fromhex("1f43b675"), timestamp + padding + packet)
    assert document.index(packet) == 4095
    path = tmp_path / "header-boundary.mkv"
    path.write_bytes(document)
    reader = MatroskaReader(path)
    assert list(reader.blocks()) == list(reader._blocks_stream())


def test_packet_reader_closes_mapping_when_iteration_stops(tmp_path, monkeypatch):
    import core.matroska.reader as module

    path = _blocks_file(tmp_path, b"\x81\x00\x00\x80frame")
    mappings = []
    original = module.mmap.mmap

    def record_mapping(*args, **kwargs):
        result = original(*args, **kwargs)
        mappings.append(result)
        return result

    monkeypatch.setattr(module.mmap, "mmap", record_mapping)
    iterator = MatroskaReader(path).blocks()
    assert next(iterator).payload == b"frame"
    assert mappings and not mappings[0].closed
    iterator.close()
    assert all(mapping.closed for mapping in mappings)


@pytest.mark.parametrize("corrupt", [b"\x00", b"\xa3", b"\xa3\x40", b"\xa3\x88", b"\xa3\x82\x81\x00"])
def test_packet_reader_rejects_corruption_without_replaying_previous_packets(tmp_path, monkeypatch, corrupt):
    path = _blocks_file(tmp_path, b"\x81\x00\x00\x80good")
    # Remplace le Cluster par un master valide contenant un enfant tronqué.
    path.write_bytes(
        element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff"
        + element(bytes.fromhex("1f43b675"),
                  element(b"\xa3", b"\x81\x00\x00\x80good") + corrupt)
    )
    reader = MatroskaReader(path)

    def unexpected_replay(*args, **kwargs):
        pytest.fail("Une erreur de parsing ne doit pas relancer la lecture")

    monkeypatch.setattr(reader, "_blocks_stream", unexpected_replay)
    iterator = reader.blocks()
    assert next(iterator).payload == b"good"
    with pytest.raises(ValueError):
        next(iterator)


def test_reader_models_nested_video_audio_colour_and_dovi_mapping(tmp_path: Path) -> None:
    track_entry = element(bytes.fromhex("ae"), b"".join((
        element(bytes.fromhex("d7"), b"\x01"),
        element(bytes.fromhex("73c5"), b"\x02"),
        element(bytes.fromhex("83"), b"\x01"),
        element(bytes.fromhex("86"), b"V_MPEGH/ISO/HEVC"),
        element(bytes.fromhex("e0"), b"".join((
            element(bytes.fromhex("b0"), b"\x07\x80"),
            element(bytes.fromhex("ba"), b"\x04\x38"),
            element(bytes.fromhex("55b0"), b"".join((
                element(bytes.fromhex("55bb"), b"\x09"),
                element(bytes.fromhex("55ba"), b"\x10"),
                element(bytes.fromhex("55bc"), b"\x03\xe8"),
                element(bytes.fromhex("55d0"), float_element(bytes.fromhex("55d9"), 1000.0)),
            ))),
        ))),
        element(bytes.fromhex("41e4"), b"".join((
            element(bytes.fromhex("41f0"), b"\x01"),
            element(bytes.fromhex("41a4"), b"Dolby Vision configuration"),
            element(bytes.fromhex("41e7"), b"\x07"),
            element(bytes.fromhex("41ed"), b"\x01\x08\x06"),
        ))),
    )))
    path = tmp_path / "metadata.mkv"
    path.write_bytes(
        element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff"
        + element(bytes.fromhex("1654ae6b"), track_entry)
    )

    track = MatroskaReader(path).tracks()[0]

    assert track.video["pixel_width"] == 1920
    assert track.video["pixel_height"] == 1080
    assert track.video["primaries"] == 9
    assert track.video["transfer_characteristics"] == 16
    assert track.video["max_cll"] == 1000
    assert track.video["luminance_max"] == 1000.0
    assert track.block_addition_mappings[0]["name"] == "Dolby Vision configuration"
    assert track.block_addition_mappings[0]["extra_data"] == b"\x01\x08\x06"


def test_reader_reports_content_encryption_as_native_blocker(tmp_path: Path) -> None:
    encoded = element(bytes.fromhex("6d80"), element(
        bytes.fromhex("6240"), element(bytes.fromhex("5035"), b"\x01"),
    ))
    track = element(bytes.fromhex("ae"), b"".join((
        element(bytes.fromhex("d7"), b"\x01"), element(bytes.fromhex("73c5"), b"\x02"),
        element(bytes.fromhex("83"), b"\x02"), element(bytes.fromhex("86"), b"A_AAC"), encoded,
    )))
    path = tmp_path / "encrypted.mka"
    path.write_bytes(
        element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff"
        + element(bytes.fromhex("1654ae6b"), track)
    )
    assert MatroskaReader(path).content_encoding_capabilities() == (False, True)


def _aggregate_from_blocks(reader: MatroskaReader) -> dict:
    """Compteurs par piste obtenus en matérialisant tous les blocs."""
    totals: dict[int, list[int]] = {}
    for block in reader.blocks():
        item = totals.setdefault(block.track_number, [0, 0, 0])
        timestamp_ns = (
            block.timestamp_ns if block.timestamp_ns is not None
            else block.timestamp_ms * 1_000_000
        )
        item[0] += 1
        item[1] += len(block.payload)
        item[2] = max(item[2], timestamp_ns + (block.duration_ns or 0))
    return totals


def _aggregate_from_summaries(reader: MatroskaReader) -> dict:
    totals: dict[int, list[int]] = {}
    for block in reader.block_summaries():
        item = totals.setdefault(block.track_number, [0, 0, 0])
        item[0] += block.frame_count
        item[1] += block.payload_bytes
        item[2] = max(item[2], block.timestamp_ns + (block.duration_ns or 0))
    return totals


def test_block_summaries_match_full_scan_for_every_lacing_mode(tmp_path: Path) -> None:
    """Le parcours en-têtes seuls compte comme le parcours complet."""
    cases = {
        "none": _blocks_file(tmp_path / "none", b"\x81\x00\x00\x80payload"),
        "fixed": _blocks_file(tmp_path / "fixed", b"\x81\x00\x00\x84\x01aabb"),
        "xiph": _blocks_file(tmp_path / "xiph", b"\x81\x00\x00\x82\x01\x01abb"),
        "ebml": _blocks_file(tmp_path / "ebml", b"\x81\x00\x00\x86\x01\x81abb"),
        "group": _blocks_file(tmp_path / "group", b"\x81\x00\x05\x00frame", grouped=True),
    }
    for label, path in cases.items():
        reader = MatroskaReader(path)
        assert _aggregate_from_summaries(reader) == _aggregate_from_blocks(reader), label


def test_block_summaries_handle_long_xiph_lacing_tables(tmp_path: Path) -> None:
    """Une table de lacing plus longue que la sonde initiale est relue."""
    frame_count = 40
    frame = b"x" * 300
    sizes = b"".join(b"\xff" + bytes([300 - 255]) for _ in range(frame_count - 1))
    block = b"\x81\x00\x00\x82" + bytes([frame_count - 1]) + sizes + frame * frame_count
    path = _blocks_file(tmp_path / "long-xiph", block)

    reader = MatroskaReader(path)
    summaries = list(reader.block_summaries())

    assert len(summaries) == 1
    assert summaries[0].frame_count == frame_count
    assert _aggregate_from_summaries(reader) == _aggregate_from_blocks(reader)


def test_parallel_block_summaries_match_the_sequential_scan(tmp_path: Path) -> None:
    """Les tranches parallèles sont émises dans l'ordre du fichier."""
    cluster, timestamp, block = bytes.fromhex("1f43b675"), bytes.fromhex("e7"), bytes.fromhex("a3")
    clusters = b"".join(
        element(
            cluster,
            element(timestamp, bytes([index]))
            + element(block, b"\x81\x00\x00\x80" + b"f" * (index + 1))
            + element(block, b"\x82\x00\x02\x80" + b"a" * (index + 2)),
        )
        for index in range(40)
    )
    path = tmp_path / "many-clusters.mkv"
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + clusters)

    reader = MatroskaReader(path)
    sequential = list(reader.block_summaries())
    for workers in (2, 4, 8):
        assert list(reader.block_summaries(workers=workers)) == sequential, workers
    assert len(sequential) == 80


def test_blocks_track_filtering(tmp_path: Path) -> None:
    """blocks(track_numbers=...) filtre les pistes et ignore les autres sans lire leur payload."""
    cluster, timestamp, block = bytes.fromhex("1f43b675"), bytes.fromhex("e7"), bytes.fromhex("a3")
    data = (
        element(EBML_HEADER_ID, b"")
        + SEGMENT_ID
        + b"\xff"
        + element(
            cluster,
            element(timestamp, b"\x00")
            + element(block, b"\x81\x00\x00\x80video_frame")
            + element(block, b"\x82\x00\x05\x80audio_frame")
            + element(block, b"\x83\x00\x0a\x80sub_frame"),
        )
    )
    path = tmp_path / "filtered.mkv"
    path.write_bytes(data)
    reader = MatroskaReader(path)

    all_blocks = list(reader.blocks())
    assert [b.track_number for b in all_blocks] == [1, 2, 3]

    audio_only = list(reader.blocks(track_numbers={2}))
    assert len(audio_only) == 1
    assert audio_only[0].track_number == 2
    assert audio_only[0].payload == b"audio_frame"

    non_video = list(reader.blocks(track_numbers={2, 3}))
    assert [b.track_number for b in non_video] == [2, 3]


def test_blocks_read_payload_false(tmp_path: Path) -> None:
    """blocks(read_payload=False) renvoie payload=b"" tout en conservant payload_bytes et les métadonnées."""
    cluster, timestamp, block = bytes.fromhex("1f43b675"), bytes.fromhex("e7"), bytes.fromhex("a3")
    payload = b"big_video_payload_bytes"
    data = (
        element(EBML_HEADER_ID, b"")
        + SEGMENT_ID
        + b"\xff"
        + element(
            cluster,
            element(timestamp, b"\x00")
            + element(block, b"\x81\x00\x00\x80" + payload),
        )
    )
    path = tmp_path / "no_payload.mkv"
    path.write_bytes(data)
    reader = MatroskaReader(path)

    full = list(reader.blocks())[0]
    assert full.payload == payload
    assert full.payload_bytes == len(payload)

    header_only = list(reader.blocks(read_payload=False))[0]
    assert header_only.payload == b""
    assert header_only.payload_bytes == len(payload)
    assert header_only.track_number == full.track_number
    assert header_only.timestamp_ms == full.timestamp_ms
    assert header_only.flags == full.flags



# ── Index level-1 via SeekHead (Tracks relocalisé après les Clusters) ─────────

_SEEK_HEAD, _SEEK, _SEEK_ID, _SEEK_POS = (bytes.fromhex(v) for v in ("114d9b74", "4dbb", "53ab", "53ac"))
_TRACKS, _ENTRY, _CLUSTER, _TAGS = (bytes.fromhex(v) for v in ("1654ae6b", "ae", "1f43b675", "1254c367"))
_VOID = bytes.fromhex("ec")


def _late_tracks_file(
    tmp_path: Path,
    *,
    seek_targets: tuple[bytes, ...] | None,
    wrong_id: bool = False,
    tags: bool = True,
    void_gap: bool = False,
) -> Path:
    """Segment ``[SeekHead] Info Cluster×3 Tracks [Void] [Tags]`` : Tracks et Tags après les Clusters."""
    track = element(_ENTRY, element(bytes.fromhex("d7"), b"\x01") + element(bytes.fromhex("83"), b"\x01")
                    + element(bytes.fromhex("86"), b"V_MPEGH/ISO/HEVC"))
    cluster = element(_CLUSTER, element(bytes.fromhex("e7"), b"\x00") + element(bytes.fromhex("a3"), b"\x81\x00\x00\x80x"))
    info = element(INFO_ID, b"")
    late = {_TRACKS: element(_TRACKS, track), _VOID: element(_VOID, b"\x00" * 4),
            _TAGS: element(_TAGS, element(bytes.fromhex("7373"), b""))}
    tail = (_TRACKS,) + ((_VOID,) if void_gap else ()) + ((_TAGS,) if tags else ())

    def seek_head(positions: dict[bytes, int]) -> bytes:
        return element(_SEEK_HEAD, b"".join(
            element(_SEEK, element(_SEEK_ID, target) + element(_SEEK_POS, positions.get(target, 0).to_bytes(8, "big")))
            for target in seek_targets or ()
        ))

    head = seek_head({}) if seek_targets is not None else b""
    cursor = len(head) + len(info) + 3 * len(cluster)
    positions: dict[bytes, int] = {}
    for target in tail:
        positions[target] = cursor
        cursor += len(late[target])
    if wrong_id:
        positions[_TRACKS] = len(head)  # pointe sur Info
    if seek_targets is not None:
        head = seek_head(positions)
    path = tmp_path / "late-tracks.mkv"
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + head + info
                     + 3 * cluster + b"".join(late[target] for target in tail))
    return path


def test_reader_finds_relocated_tracks_through_seekhead_without_walking_clusters(tmp_path: Path, monkeypatch) -> None:
    path = _late_tracks_file(tmp_path, seek_targets=(_TRACKS, _TAGS))

    def no_walk(_self):
        raise AssertionError("parcours des Clusters inattendu")

    monkeypatch.setattr(MatroskaReader, "top_level", no_walk)
    reader = MatroskaReader(path)
    assert [track.codec_id for track in reader.tracks()] == ["V_MPEGH/ISO/HEVC"]
    assert len(reader.raw_top_level(_TAGS)) == 1


def test_reader_without_seekhead_falls_back_to_linear_walk(tmp_path: Path) -> None:
    path = _late_tracks_file(tmp_path, seek_targets=None)
    reader = MatroskaReader(path)
    assert [track.codec_id for track in reader.tracks()] == ["V_MPEGH/ISO/HEVC"]
    assert len(reader.raw_top_level(_TAGS)) == 1


def test_reader_distrusts_seekhead_pointing_to_another_element(tmp_path: Path) -> None:
    path = _late_tracks_file(tmp_path, seek_targets=(_TRACKS, _TAGS), wrong_id=True)
    reader = MatroskaReader(path)
    assert [track.codec_id for track in reader.tracks()] == ["V_MPEGH/ISO/HEVC"]
    assert len(reader.raw_top_level(_TAGS)) == 1


def test_reader_seekhead_index_matches_linear_walk(tmp_path: Path) -> None:
    path = _late_tracks_file(tmp_path, seek_targets=(_TRACKS, _TAGS))
    reader = MatroskaReader(path)
    expected = [item for item in reader.top_level() if item.element_id != _CLUSTER]
    assert list(MatroskaReader(path)._metadata_elements()) == expected


def _early_tracks_file(tmp_path: Path, *, unknown_clusters: bool = False, bad_seek: bool = False) -> Path:
    """Tracks avant les Clusters, Tags après ; l'index optionnel annonce Tags à la position de Tracks."""
    tracks = element(_TRACKS, element(_ENTRY, element(bytes.fromhex("d7"), b"\x01")
                     + element(bytes.fromhex("83"), b"\x01") + element(bytes.fromhex("86"), b"V_MPEGH/ISO/HEVC")))
    info = element(INFO_ID, float_element(bytes.fromhex("4489"), 2000.0))
    cluster_payload = element(bytes.fromhex("e7"), b"\x00") + element(bytes.fromhex("a3"), b"\x81\x00\x00\x80x")
    cluster = _CLUSTER + b"\xff" + cluster_payload if unknown_clusters else element(_CLUSTER, cluster_payload)
    tags = element(_TAGS, element(bytes.fromhex("7373"), b""))

    def seek_head(position: int) -> bytes:
        return element(_SEEK_HEAD, element(_SEEK, element(_SEEK_ID, _TAGS)
                       + element(_SEEK_POS, position.to_bytes(8, "big"))))

    head = seek_head(len(seek_head(0)) + len(info)) if bad_seek else b""
    path = tmp_path / "early-tracks.mkv"
    path.write_bytes(element(EBML_HEADER_ID, b"") + SEGMENT_ID + b"\xff" + head + info + tracks + 100 * cluster + tags)
    return path


@pytest.mark.parametrize("unknown_clusters", [False, True])
def test_reader_reads_early_tracks_and_info_without_seekhead_or_cluster_walk(tmp_path: Path, monkeypatch, unknown_clusters: bool) -> None:
    path = _early_tracks_file(tmp_path, unknown_clusters=unknown_clusters)

    def no_walk(_self):
        raise AssertionError("parcours des Clusters inattendu")

    monkeypatch.setattr(MatroskaReader, "top_level", no_walk)
    reader = MatroskaReader(path)
    assert [track.codec_id for track in reader.tracks()] == ["V_MPEGH/ISO/HEVC"]
    assert reader.segment_duration_ns() == 2_000_000_000


def test_reader_rejects_wrong_seek_id_at_already_known_offset(tmp_path: Path) -> None:
    path = _early_tracks_file(tmp_path, bad_seek=True)
    reader = MatroskaReader(path)
    # Tags pointe sur Tracks, déjà rencontré avant les Clusters. Le repli
    # doit retrouver les vrais Tags même sans demander tracks() d'abord.
    assert len(reader.raw_top_level(_TAGS)) == 1


def test_reader_trusts_coherent_seekhead_for_missing_optional_elements(tmp_path: Path, monkeypatch) -> None:
    """Élément facultatif non indexé = absent : pas de parcours des Clusters (gros fichiers)."""
    path = _late_tracks_file(tmp_path, seek_targets=(_TRACKS,), tags=False, void_gap=True)

    def no_walk(_self):
        raise AssertionError("parcours des Clusters inattendu")

    monkeypatch.setattr(MatroskaReader, "top_level", no_walk)
    reader = MatroskaReader(path)
    assert [track.codec_id for track in reader.tracks()] == ["V_MPEGH/ISO/HEVC"]
    assert reader.raw_top_level(_TAGS) == ()
    assert reader.attachment_headers() == []


@pytest.mark.parametrize("void_gap", [False, True])
def test_reader_finds_tags_appended_after_indexed_tail(tmp_path: Path, void_gap: bool) -> None:
    """Tags non indexés juste après Tracks (Void éventuel) : index incomplet, repli."""
    path = _late_tracks_file(tmp_path, seek_targets=(_TRACKS,), void_gap=void_gap)
    reader = MatroskaReader(path)
    assert [track.codec_id for track in reader.tracks()] == ["V_MPEGH/ISO/HEVC"]
    assert len(reader.raw_top_level(_TAGS)) == 1


def test_reader_walks_clusters_for_mandatory_tracks_missing_from_seekhead(tmp_path: Path) -> None:
    path = _late_tracks_file(tmp_path, seek_targets=(_TAGS,))
    reader = MatroskaReader(path)
    assert [track.codec_id for track in reader.tracks()] == ["V_MPEGH/ISO/HEVC"]
    assert len(reader.raw_top_level(_TAGS)) == 1


@pytest.mark.parametrize("unknown_clusters", [False, True])
def test_reader_without_seekhead_enumerates_tags_before_and_after_clusters(tmp_path: Path, unknown_clusters: bool) -> None:
    path = _early_tracks_file(tmp_path, unknown_clusters=unknown_clusters)
    raw = path.read_bytes()
    tags = element(_TAGS, element(bytes.fromhex("7373"), b""))
    segment_offset = len(element(EBML_HEADER_ID, b"")) + len(SEGMENT_ID) + 1
    path.write_bytes(raw[:segment_offset] + tags + raw[segment_offset:])
    reader = MatroskaReader(path)
    assert len(reader.tracks()) == 1
    assert reader.raw_top_level(_TAGS) == (tags, tags)
