"""Tests for MatroskaSegmentInfoHeaderEditor in-place behavior."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from core.matroska.ebml import element, string_element, uint_element
from core.matroska.reader import MatroskaReader
from core.matroska.editors.segment_info import (
    MatroskaSegmentInfoHeaderEditor,
    MatroskaSegmentInfoHeaderEditorOptions,
    _AnalyzerState,
    _EbmlElement,
)


_VOID_ID = b"\xec"
_EBML_HDR_ID = b"\x1a\x45\xdf\xa3"
_SEGMENT_ID = b"\x18\x53\x80\x67"
_INFO_ID = b"\x15\x49\xa9\x66"
_MUXING_APP_ID = b"\x4d\x80"
_WRITING_APP_ID = b"\x57\x41"
_SEEKHEAD_ID = b"\x11\x4d\x9b\x74"
_SEEK_ID = b"\x4d\xbb"
_SEEKID_ID = b"\x53\xab"
_SEEKPOS_ID = b"\x53\xac"
_CLUSTER_ID = b"\x1f\x43\xb6\x75"
_TRACKS_ID = b"\x16\x54\xae\x6b"


def encode_vint_size(value: int) -> bytes:
    if value < 0:
        raise ValueError("Taille négative")
    if value <= 126:
        return bytes([0x80 | value])
    if value <= 16382:
        return bytes([0x40 | (value >> 8), value & 0xFF])
    if value <= 2097150:
        return bytes([0x20 | (value >> 16), (value >> 8) & 0xFF, value & 0xFF])
    raise ValueError(f"Taille trop grande pour ce helper: {value}")


def encode_uint(value: int) -> bytes:
    if value < 0:
        raise ValueError("uint négatif")
    if value == 0:
        return b"\x00"
    n = (value.bit_length() + 7) // 8
    return value.to_bytes(n, "big")


def make_mux_element(text: str) -> bytes:
    payload = text.encode("utf-8")
    return _MUXING_APP_ID + encode_vint_size(len(payload)) + payload


def make_writing_element(text: str) -> bytes:
    payload = text.encode("utf-8")
    return _WRITING_APP_ID + encode_vint_size(len(payload)) + payload


def make_void_element(padding_bytes: int) -> bytes:
    return _VOID_ID + encode_vint_size(padding_bytes) + bytes(padding_bytes)


def make_info_element(info_payload: bytes) -> bytes:
    return _INFO_ID + encode_vint_size(len(info_payload)) + info_payload


def make_seek_entry_info(rel_pos: int) -> bytes:
    seek_id = _SEEKID_ID + encode_vint_size(len(_INFO_ID)) + _INFO_ID
    pos_raw = encode_uint(rel_pos)
    seek_pos = _SEEKPOS_ID + encode_vint_size(len(pos_raw)) + pos_raw
    payload = seek_id + seek_pos
    return _SEEK_ID + encode_vint_size(len(payload)) + payload


def make_seek_head_for_info(rel_pos: int) -> bytes:
    payload = make_seek_entry_info(rel_pos)
    return _SEEKHEAD_ID + encode_vint_size(len(payload)) + payload


def make_segment_unknown_size(segment_payload: bytes) -> bytes:
    return _SEGMENT_ID + b"\x01\xff\xff\xff\xff\xff\xff\xff" + segment_payload


def make_segment_known_size(segment_payload: bytes) -> bytes:
    return _SEGMENT_ID + encode_vint_size(len(segment_payload)) + segment_payload


def make_ebml_header() -> bytes:
    payload = (
        b"\x42\x86" + encode_vint_size(1) + b"\x01"
        + b"\x42\xf7" + encode_vint_size(1) + b"\x01"
        + b"\x42\xf2" + encode_vint_size(1) + b"\x04"
        + b"\x42\xf3" + encode_vint_size(1) + b"\x08"
        + b"\x42\x82" + encode_vint_size(8) + b"matroska"
    )
    return _EBML_HDR_ID + encode_vint_size(len(payload)) + payload


def make_fake_cluster(size: int = 64) -> bytes:
    return _CLUSTER_ID + encode_vint_size(size) + bytes(size)


def make_mkv_data(
    muxing_app: str,
    *,
    writing_app: str | None = None,
    void_padding: int = 0,
    segment_known_size: bool = False,
    cluster_size: int = 64,
    with_seek_head: bool = False,
) -> bytes:
    info_payload = make_mux_element(muxing_app)
    if writing_app is not None:
        info_payload += make_writing_element(writing_app)
    if void_padding > 0:
        info_payload += make_void_element(void_padding)

    info = make_info_element(info_payload)
    cluster = make_fake_cluster(cluster_size)

    seg_payload = b""
    if with_seek_head:
        # SeekPosition is relative to Segment payload start.
        seek_pos = len(make_seek_head_for_info(0))
        seg_payload += make_seek_head_for_info(seek_pos)
    seg_payload += info + cluster

    segment = make_segment_known_size(seg_payload) if segment_known_size else make_segment_unknown_size(seg_payload)
    return make_ebml_header() + segment


@pytest.fixture
def editor() -> MatroskaSegmentInfoHeaderEditor:
    return MatroskaSegmentInfoHeaderEditor()


class TestLocateContext:
    def test_minimal_header_parses(self, editor):
        data = make_mkv_data("Lavf61.7.100")
        ctx = editor._locate_context(data)
        assert ctx.muxing_app is not None
        assert ctx.info is not None
        assert ctx.segment is not None

    def test_with_writing_app(self, editor):
        data = make_mkv_data("Lavf61.7.100", writing_app="Muxiveo v1.3.0")
        ctx = editor._locate_context(data)
        assert ctx.writing_app is not None


class TestInfoReplacementHelpers:
    def test_replace_grows_without_void(self, editor):
        data = make_mkv_data("Lavf61.7.100")
        ctx = editor._locate_context(data)
        old_total = ctx.info.end - ctx.info.offset
        new_el = editor._build_replaced_info_element(data, ctx, new_muxing_app_text="Muxiveo v1.3.0")
        assert len(new_el) > old_total

    def test_replace_stays_same_with_inner_void(self, editor):
        data = make_mkv_data("Lavf61.7.100", void_padding=64)
        ctx = editor._locate_context(data)
        old_total = ctx.info.end - ctx.info.offset
        new_el = editor._build_replaced_info_element(data, ctx, new_muxing_app_text="Muxiveo v1.3.0")
        assert len(new_el) == old_total


class TestApplyIntegration:
    def _make_file(self, data: bytes, tmp_path: Path) -> Path:
        p = tmp_path / "test.mkv"
        p.write_bytes(data)
        return p

    def test_apply_replaces_value_with_prefix_only(self, editor, tmp_path):
        path = self._make_file(make_mkv_data("Lavf61.7.100"), tmp_path)

        result = editor.apply_muxing_app_replace_with_header_rebuild(
            path,
            app_prefix="Muxiveo v1.3.0",
        )

        assert result.applied is True
        assert result.muxing_app_before == "Lavf61.7.100"
        assert result.muxing_app_after == "Muxiveo v1.3.0"

        data = path.read_bytes()
        ctx = editor._locate_context(data)
        raw = data[ctx.muxing_app.payload_offset:ctx.muxing_app.end]
        assert raw.decode("utf-8") == "Muxiveo v1.3.0"

    def test_apply_with_inner_void_keeps_file_size(self, editor, tmp_path):
        path = self._make_file(make_mkv_data("Lavf61.7.100", void_padding=64), tmp_path)
        old_size = path.stat().st_size

        result = editor.apply_muxing_app_replace_with_header_rebuild(path, app_prefix="Muxiveo v1.3.0")

        assert result.applied is True
        assert path.stat().st_size == old_size

    def test_apply_without_inner_void_is_still_in_place_file(self, editor, tmp_path):
        path = self._make_file(make_mkv_data("Lavf61.7.100"), tmp_path)

        result = editor.apply_muxing_app_replace_with_header_rebuild(path, app_prefix="Muxiveo v1.3.0")

        assert result.applied is True
        assert not list(path.parent.glob("*.hdrpatch.*"))

    def test_idempotent_second_call(self, editor, tmp_path):
        path = self._make_file(make_mkv_data("Lavf61.7.100", void_padding=64), tmp_path)

        first = editor.apply_muxing_app_replace_with_header_rebuild(path, app_prefix="Muxiveo v1.3.0")
        second = editor.apply_muxing_app_replace_with_header_rebuild(path, app_prefix="Muxiveo v1.3.0")

        assert first.applied is True
        assert second.applied is False
        assert second.skipped is False
        assert "à jour" in second.reason

    def test_known_segment_size_supported(self, editor, tmp_path):
        path = self._make_file(
            make_mkv_data("Lavf61.7.100", segment_known_size=True, void_padding=64),
            tmp_path,
        )

        result = editor.apply_muxing_app_replace_with_header_rebuild(path, app_prefix="Muxiveo v1.3.0")
        assert result.applied is True

    def test_invalid_file_skipped_without_mutation(self, editor, tmp_path):
        path = tmp_path / "invalid.mkv"
        path.write_bytes(b"not a matroska header")
        before = path.read_bytes()

        result = editor.apply_muxing_app_replace_with_header_rebuild(path, app_prefix="Muxiveo v1.3.0")

        assert result.applied is False
        assert result.skipped is True
        assert path.read_bytes() == before


class TestSeekHeadBehavior:
    def _extract_info_seek_positions(self, editor: MatroskaSegmentInfoHeaderEditor, path: Path) -> list[int]:
        with path.open("rb") as fh:
            st = editor._analyze_file(fh, parse_fast=False)
            out: list[int] = []
            for e in st.data:
                if e.element_id != _SEEKHEAD_ID or e.unknown_size:
                    continue
                for target_id, rel_pos in editor._iter_seek_entries(fh, e):
                    if target_id == _INFO_ID:
                        out.append(rel_pos)
            return out

    def test_seekhead_points_to_current_info(self, tmp_path):
        editor = MatroskaSegmentInfoHeaderEditor()
        path = tmp_path / "with_seekhead.mkv"
        path.write_bytes(make_mkv_data("Lavf61.7.100", with_seek_head=True))

        result = editor.apply_muxing_app_replace_with_header_rebuild(path, app_prefix="Muxiveo v1.3.0")
        assert result.applied is True

        data = path.read_bytes()
        ctx = editor._locate_context(data)

        seek_positions = self._extract_info_seek_positions(editor, path)
        assert seek_positions, "Aucun SeekPosition pour Info trouvé"

        # seek positions are relative to segment payload start.
        with path.open("rb") as fh:
            st = editor._analyze_file(fh, parse_fast=False)
        info_rel = ctx.info.offset - st.segment.payload_offset
        assert info_rel in seek_positions


class TestGapOneByteHandling:
    def test_handle_void_gap_of_one_byte(self, tmp_path):
        editor = MatroskaSegmentInfoHeaderEditor()
        path = tmp_path / "gap1.mkv"

        # Build bytes with a 1-byte gap then a valid next element header.
        next_elt = _TRACKS_ID + b"\x80"  # payload size 0, header len 5
        raw = b"\x00" + next_elt
        path.write_bytes(raw)

        removed = _EbmlElement(
            element_id=_INFO_ID,
            offset=0,
            id_len=0,
            size_offset=0,
            size=0,
            size_len=0,
            payload_offset=0,
            unknown_size=False,
        )
        nxt = _EbmlElement(
            element_id=_TRACKS_ID,
            offset=1,
            id_len=4,
            size_offset=5,
            size=0,
            size_len=1,
            payload_offset=6,
            unknown_size=False,
        )
        segment = _EbmlElement(
            element_id=_SEGMENT_ID,
            offset=0,
            id_len=4,
            size_offset=4,
            size=0,
            size_len=1,
            payload_offset=5,
            unknown_size=True,
        )
        state = _AnalyzerState(file_size=path.stat().st_size, segment=segment, data=[removed, nxt])

        with path.open("r+b") as fh:
            editor._handle_void_elements(fh, state, 0)

        assert any(e.element_id == _TRACKS_ID and e.offset == 0 for e in state.data)


def test_edit_muxing_app_disabled_skips(tmp_path):
    opts = MatroskaSegmentInfoHeaderEditorOptions(edit_muxing_app=False)
    editor = MatroskaSegmentInfoHeaderEditor(options=opts)
    path = tmp_path / "x.mkv"
    path.write_bytes(make_mkv_data("Lavf61.7.100"))

    result = editor.apply_muxing_app_replace_with_header_rebuild(path, app_prefix="Muxiveo v1.3.0")
    assert result.applied is False
    assert result.skipped is True


@pytest.mark.parametrize("target_id", [_INFO_ID, _TRACKS_ID])
@pytest.mark.parametrize("remaining", [0, 1, 25])
def test_header_growth_reuses_padding_without_moving_clusters(tmp_path, target_id, remaining):
    """FFmpeg peut séparer le Void réservé des Tracks par l'élément Info."""
    import zlib

    info = make_info_element(make_mux_element("Lavf"))
    track = uint_element(b"\xd7", 1) + uint_element(b"\x83", 1) + string_element(b"\x86", "V_MPEG4/ISO/AVC")
    tracks = element(_TRACKS_ID, element(b"\xae", track))
    old = info if target_id == _INFO_ID else tracks
    replacement = element(target_id, old[5:] + element(_VOID_ID, b"extra metadata"))
    growth = len(replacement) - len(old)
    padding = MatroskaSegmentInfoHeaderEditor()._build_void_element(growth + remaining)

    def seek_head(info_pos, tracks_pos):
        seeks = b"".join(
            element(_SEEK_ID, element(_SEEKID_ID, tid) + element(_SEEKPOS_ID, pos.to_bytes(4, "big")))
            for tid, pos in ((_INFO_ID, info_pos), (_TRACKS_ID, tracks_pos))
        )
        crc = element(b"\xbf", zlib.crc32(seeks).to_bytes(4, "little"))
        return element(_SEEKHEAD_ID, crc + seeks)

    info_pos = len(seek_head(0, 0)) + len(padding)
    seeks = seek_head(info_pos, info_pos + len(info))
    cluster_pos = len(seeks + padding + info + tracks)
    # Cues est indexé après les Clusters et doit rester intact, offset compris.
    cues = element(bytes.fromhex("1c53bb6b"), uint_element(b"\xf1", cluster_pos))
    data = make_ebml_header() + make_segment_known_size(
        seeks + padding + info + tracks + make_fake_cluster() + cues
    )
    path = tmp_path / "compact-header.mkv"
    path.write_bytes(data)
    reader = MatroskaReader(path)
    cluster_offset = next(e.offset for e in reader.top_level() if e.element_id == _CLUSTER_ID)

    editor = MatroskaSegmentInfoHeaderEditor(options=MatroskaSegmentInfoHeaderEditorOptions(
        allow_post_cluster_rebuild=False,
    ))
    assert editor.replace_level1_element(path, element_id=target_id, new_element_bytes=replacement) == 0

    assert path.stat().st_size == len(data)
    assert path.read_bytes()[cluster_offset:] == data[cluster_offset:]
    reader = MatroskaReader(path)
    assert len(reader.tracks()) == 1
    assert all(e.offset < cluster_offset for e in reader.top_level() if e.element_id in (_INFO_ID, _TRACKS_ID))
    with path.open("rb") as handle:
        state = editor._analyze_file(handle, parse_fast=False)
        head = next(e for e in state.data if e.element_id == _SEEKHEAD_ID)
        for tid, position in editor._iter_seek_entries(handle, head):
            target = next(e for e in state.data if e.element_id == tid)
            assert position == target.offset - state.segment.payload_offset
        payload = editor._read_exact(handle, head.payload_offset, head.size)
        assert int.from_bytes(payload[2:6], "little") == zlib.crc32(payload[6:])


@pytest.mark.parametrize("patch", ["language", "enabled", "muxing_app"])
def test_header_growth_without_room_leaves_original_file_intact(tmp_path, patch):
    from core.matroska.editors.language import MatroskaLanguageEditor
    from core.matroska.editors.track_flags import MatroskaTrackEnabledEditor

    info = make_info_element(make_mux_element("Lavf"))
    track = (
        uint_element(b"\xd7", 1) + uint_element(b"\x83", 1)
        + string_element(b"\x86", "V_MPEG4/ISO/AVC")
        + string_element(bytes.fromhex("22b59c"), "fr")
    )
    tracks = element(_TRACKS_ID, element(b"\xae", track))
    data = make_ebml_header() + make_segment_known_size(info + tracks + make_fake_cluster())
    path = tmp_path / "no-room.mkv"
    path.write_bytes(data)
    editor = MatroskaSegmentInfoHeaderEditor(options=MatroskaSegmentInfoHeaderEditorOptions(
        allow_post_cluster_rebuild=False,
    ))
    if patch == "language":
        result = MatroskaLanguageEditor(editor=editor).apply(path)
    elif patch == "enabled":
        result = MatroskaTrackEnabledEditor(editor=editor).apply(path, {0: False})
    else:
        result = editor.apply_muxing_app_replace_with_header_rebuild(path, app_prefix="Muxiveo test")

    assert result.skipped and not result.applied
    assert path.read_bytes() == data
    assert len(MatroskaReader(path).tracks()) == 1


# ---------------------------------------------------------------------------
# RFC 9559 §6.3 : jamais de second SeekHead non chaîné (dovi_tool : Tracks introuvable)
# ---------------------------------------------------------------------------

_TAGS_ID = b"\x12\x54\xc3\x67"


def _seek(target: bytes, pos: int) -> bytes:
    return element(_SEEK_ID, element(_SEEKID_ID, target) + uint_element(_SEEKPOS_ID, pos))


def _tight_seekhead_file(path: Path) -> None:
    """SeekHead plein (sans Void), Info, Tags, Tracks, Cluster : Tags à agrandir."""
    info = make_info_element(make_mux_element("Lavf62.19.101"))
    tags = element(_TAGS_ID, element(b"\x73\x73", b"\x00" * 40))
    tracks = element(_TRACKS_ID, element(b"\xae", uint_element(b"\xd7", 1)))
    probe = len(element(_SEEKHEAD_ID, _seek(_INFO_ID, 100) + _seek(_TAGS_ID, 100) + _seek(_TRACKS_ID, 100)))
    sh = element(_SEEKHEAD_ID, _seek(_INFO_ID, probe) + _seek(_TAGS_ID, probe + len(info))
                 + _seek(_TRACKS_ID, probe + len(info) + len(tags)))
    assert len(sh) == probe
    path.write_bytes(make_ebml_header() + make_segment_unknown_size(sh + info + tags + tracks + make_fake_cluster(4096)))


def _seekhead_offsets(path: Path) -> list[int]:
    data = path.read_bytes()
    offsets, start = [], 0
    while (found := data.find(_SEEKHEAD_ID, start)) >= 0:
        offsets.append(found)
        start = found + 1
    return offsets


def test_relocated_element_stays_indexed_by_the_only_seekhead(tmp_path: Path) -> None:
    from core.matroska.reader import strict_demuxer_reads_tracks

    path = tmp_path / "tight.mkv"
    _tight_seekhead_file(path)
    editor = MatroskaSegmentInfoHeaderEditor(
        options=MatroskaSegmentInfoHeaderEditorOptions(allow_post_cluster_rebuild=False, fallback_mode="skip"),
    )
    editor.replace_level1_element(
        path, element_id=_TAGS_ID, new_element_bytes=element(_TAGS_ID, element(b"\x73\x73", b"\x01" * 400)),
    )
    # Un seul SeekHead, resté premier élément du segment (agrandi sur place), qui indexe Tags et Tracks.
    assert len(_seekhead_offsets(path)) == 1
    assert next(MatroskaReader(path).top_level()).element_id == _SEEKHEAD_ID
    reader = MatroskaReader(path)
    assert reader.raw_top_level(_TAGS_ID)[0].endswith(b"\x01" * 400)
    assert reader.tracks_indexed_for_strict_readers()
    assert strict_demuxer_reads_tracks(path)


def test_strict_reader_check_detects_unchained_second_seekhead(tmp_path: Path) -> None:
    """Structure produite par l'ancien éditeur : Tracks indexé par un second SeekHead orphelin."""
    info = make_info_element(make_mux_element("Lavf"))
    tracks = element(_TRACKS_ID, element(b"\xae", uint_element(b"\xd7", 1)))
    sh1 = element(_SEEKHEAD_ID, _seek(_INFO_ID, 0))
    sh1_len = len(sh1)
    sh1 = element(_SEEKHEAD_ID, _seek(_INFO_ID, sh1_len))
    cluster = make_fake_cluster(64)
    tracks_pos = len(sh1) + len(info) + 64 + len(cluster)
    sh2 = element(_SEEKHEAD_ID, _seek(_TRACKS_ID, tracks_pos))
    orphan = sh1 + info + sh2 + make_void_element(64 - len(sh2) - 2) + cluster + tracks
    path = tmp_path / "orphan.mkv"
    path.write_bytes(make_ebml_header() + make_segment_unknown_size(orphan))
    assert not MatroskaReader(path).tracks_indexed_for_strict_readers()
    # Second SeekHead chaîné depuis le premier : lisible.
    chained_sh1 = element(_SEEKHEAD_ID, _seek(_INFO_ID, 0) + _seek(_SEEKHEAD_ID, 0))
    base = len(chained_sh1)
    chained_sh1 = element(_SEEKHEAD_ID, _seek(_INFO_ID, base) + _seek(_SEEKHEAD_ID, base + len(info)))
    assert len(chained_sh1) == base
    chained = chained_sh1 + info + sh2 + make_void_element(64 - len(sh2) - 2) + cluster + tracks
    tracks_pos = len(chained_sh1) + len(info) + 64 + len(cluster)
    chained = chained.replace(sh2, element(_SEEKHEAD_ID, _seek(_TRACKS_ID, tracks_pos)))
    path.write_bytes(make_ebml_header() + make_segment_unknown_size(chained))
    assert MatroskaReader(path).tracks_indexed_for_strict_readers()
    # Sans SeekHead : Tracks avant les Clusters.
    path.write_bytes(make_ebml_header() + make_segment_unknown_size(info + tracks + cluster))
    assert MatroskaReader(path).tracks_indexed_for_strict_readers()


def test_strict_reader_check_verifies_the_pointed_tracks_element(tmp_path: Path) -> None:
    """RV7-04 : une entrée Tracks qui désigne un autre élément ne rend pas Tracks trouvable."""
    info = make_info_element(make_mux_element("Lavf"))
    tracks = element(_TRACKS_ID, element(b"\xae", uint_element(b"\xd7", 1)))
    cluster = make_fake_cluster(64)
    probe = len(element(_SEEKHEAD_ID, _seek(_INFO_ID, 100) + _seek(_TRACKS_ID, 100)))
    # Tracks annoncé à la position du SeekHead lui-même (0), présent seulement après les Clusters.
    sh = element(_SEEKHEAD_ID, _seek(_INFO_ID, probe) + _seek(_TRACKS_ID, 0))
    assert len(sh) == probe
    path = tmp_path / "bad_pointer.mkv"
    path.write_bytes(make_ebml_header() + make_segment_unknown_size(sh + info + cluster + tracks))
    assert not MatroskaReader(path).tracks_indexed_for_strict_readers()
    # Position hors segment : ignorée aussi.
    far = element(_SEEKHEAD_ID, _seek(_INFO_ID, probe) + _seek(_TRACKS_ID, 10_000_000))
    path.write_bytes(make_ebml_header() + make_segment_unknown_size(far + info + cluster + tracks))
    assert not MatroskaReader(path).tracks_indexed_for_strict_readers()



def _two_seekheads_file(path: Path) -> None:
    """Premier SeekHead plein (Info, Tags, Tracks, second SeekHead) ; second en fin, Clusters seuls."""
    info = make_info_element(make_mux_element("mkvmerge-like"))
    tags = element(_TAGS_ID, element(b"\x73\x73", b"\x00" * 40))
    tracks = element(_TRACKS_ID, element(b"\xae", uint_element(b"\xd7", 1)))
    cluster = make_fake_cluster(4096)
    probe = len(element(_SEEKHEAD_ID, b"".join(_seek(i, 100) for i in (_INFO_ID, _TAGS_ID, _TRACKS_ID))
                        + _seek(_SEEKHEAD_ID, 5000)))
    head = probe + len(info) + len(tags) + len(tracks)
    second = element(_SEEKHEAD_ID, _seek(_CLUSTER_ID, head))
    first = element(_SEEKHEAD_ID, _seek(_INFO_ID, probe) + _seek(_TAGS_ID, probe + len(info))
                    + _seek(_TRACKS_ID, probe + len(info) + len(tags)) + _seek(_SEEKHEAD_ID, head + len(cluster)))
    assert len(first) == probe
    path.write_bytes(make_ebml_header() + make_segment_unknown_size(first + info + tags + tracks + cluster + second))


def test_first_seekhead_stays_first_with_a_conforming_second_seekhead(tmp_path: Path) -> None:
    """RV7-02 : agrandi sur place, le premier SeekHead précède Info et garde la référence au second."""
    path = tmp_path / "two.mkv"
    _two_seekheads_file(path)
    reader = MatroskaReader(path)
    assert reader.tracks_indexed_for_strict_readers()
    editor = MatroskaSegmentInfoHeaderEditor(
        options=MatroskaSegmentInfoHeaderEditorOptions(allow_post_cluster_rebuild=False, fallback_mode="skip"),
    )
    editor.replace_level1_element(
        path, element_id=_TAGS_ID, new_element_bytes=element(_TAGS_ID, element(b"\x73\x73", b"\x02" * 300)),
    )
    reader = MatroskaReader(path)
    top = list(reader.top_level())
    assert top[0].element_id == _SEEKHEAD_ID
    heads = [item for item in top if item.element_id == _SEEKHEAD_ID]
    assert len(heads) == 2
    first_entries = dict(reader._seek_entries(heads[0]))
    segment = reader.segment()
    # Le premier référence le second à sa position réelle ; le second ne liste que des Clusters.
    assert segment.payload_offset + first_entries[_SEEKHEAD_ID] == heads[1].offset
    assert {target for target, _ in reader._seek_entries(heads[1])} == {_CLUSTER_ID}
    assert reader.tracks_indexed_for_strict_readers()
    assert reader.raw_top_level(_TAGS_ID)[0].endswith(b"\x02" * 300)


@pytest.mark.parametrize("trailing_tags", [False, True])
def test_failed_edit_restores_file_byte_for_byte(tmp_path: Path, monkeypatch, trailing_tags: bool) -> None:
    """RV7-01 : un échec après écritures (et troncature) rend le fichier intact."""
    path = tmp_path / "rollback.mkv"
    if trailing_tags:
        info = make_info_element(make_mux_element("Lavf"))
        tags = element(_TAGS_ID, element(b"\x73\x73", b"\x00" * 40))
        probe = len(element(_SEEKHEAD_ID, _seek(_INFO_ID, 100) + _seek(_TAGS_ID, 100)))
        cluster = make_fake_cluster(256)
        sh = element(_SEEKHEAD_ID, _seek(_INFO_ID, probe) + _seek(_TAGS_ID, probe + len(info) + len(cluster)))
        path.write_bytes(make_ebml_header() + make_segment_unknown_size(sh + info + cluster + tags))
    else:
        _tight_seekhead_file(path)
    original = path.read_bytes()
    editor = MatroskaSegmentInfoHeaderEditor(
        options=MatroskaSegmentInfoHeaderEditorOptions(allow_post_cluster_rebuild=False, fallback_mode="skip"),
    )

    def boom(*_args, **_kwargs):
        raise ValueError("échec simulé après écriture")

    # Tags en fin : premier SeekHead plein et aucun Void avant les Clusters → refus réel
    # après mise en Void / troncature / réécriture. Sinon : échec simulé en fin d'édition.
    monkeypatch.setattr(editor, "_resync_meta_seeks", boom)
    with pytest.raises(ValueError, match="Premier SeekHead plein" if trailing_tags else "échec simulé"):
        editor.replace_level1_element(
            path, element_id=_TAGS_ID, new_element_bytes=element(_TAGS_ID, element(b"\x73\x73", b"\x03" * 500)),
        )
    assert path.read_bytes() == original


def test_journal_bounds_reads_and_restores_overlapping_writes(monkeypatch) -> None:
    """Le journal reste borné, même après plusieurs écritures, une troncature et un ajout."""
    from core.matroska.editors.segment_info import _WriteJournal
    from core.runner import TaskCancelledError

    monkeypatch.setattr(_WriteJournal, "_COPY_CHUNK", 16)

    class BoundedFile(BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 16
            return super().read(size)

    original = bytes(range(128))
    fh = BoundedFile(original)
    editor = MatroskaSegmentInfoHeaderEditor()
    with pytest.raises(TaskCancelledError), editor._journaled(fh):
        editor._write_at(fh, 5, b"a" * 80)
        editor._write_at(fh, 20, b"b" * 60)
        editor._truncate(fh, 40)
        editor._write_at(fh, 40, b"c" * 150)
        raise TaskCancelledError()
    assert fh.getvalue() == original


def test_journals_are_isolated_for_files_using_the_same_editor() -> None:
    """Deux éditions qui se chevauchent ne sauvegardent jamais les octets du mauvais fichier."""
    editor = MatroskaSegmentInfoHeaderEditor()
    first, second = BytesIO(b"first file"), BytesIO(b"second file")
    with pytest.raises(ValueError, match="premier"), editor._journaled(first):
        with pytest.raises(ValueError, match="second"), editor._journaled(second):
            editor._write_at(first, 0, b"AAAAA")
            editor._write_at(second, 0, b"BBBBBB")
            raise ValueError("second")
        assert second.getvalue() == b"second file"
        editor._write_at(first, 5, b"CCCCC")
        raise ValueError("premier")
    assert first.getvalue() == b"first file"


def test_journal_rolls_back_when_the_commit_flush_fails() -> None:
    """Une erreur d'écriture différée survient avant la fermeture du journal."""
    class FailedFlush(BytesIO):
        failed = False

        def flush(self):
            if not self.failed:
                self.failed = True
                raise OSError("flush simulé")
            super().flush()

    fh = FailedFlush(b"original")
    editor = MatroskaSegmentInfoHeaderEditor()
    with pytest.raises(OSError, match="flush simulé"), editor._journaled(fh):
        editor._write_at(fh, 0, b"changed!")
    assert fh.getvalue() == b"original"


@pytest.mark.parametrize("front_void", [False, True])
def test_new_seekhead_is_complete_and_can_only_be_created_at_the_front(tmp_path, front_void):
    """Un fichier sans index reste lisible ; aucun SeekHead partiel ne masque Tracks."""
    info = make_info_element(make_mux_element("Lavf"))
    tracks = element(_TRACKS_ID, element(b"\xae", uint_element(b"\xd7", 1)))
    padding = make_void_element(100)
    before = padding + info + tracks if front_void else info + padding + tracks
    path = tmp_path / "nohead.mkv"
    path.write_bytes(make_ebml_header() + make_segment_unknown_size(before + make_fake_cluster(100)))
    MatroskaSegmentInfoHeaderEditor().replace_level1_element(
        path, element_id=_TAGS_ID, new_element_bytes=element(_TAGS_ID, b"x" * 300),
    )
    reader = MatroskaReader(path)
    heads = [e for e in reader.top_level() if e.element_id == _SEEKHEAD_ID]
    assert len(heads) == int(front_void)
    if front_void:
        assert next(reader.top_level()) == heads[0]
        assert {target for target, _ in reader._seek_entries(heads[0])} == {_INFO_ID, _TRACKS_ID, _TAGS_ID}
    assert reader.tracks_indexed_for_strict_readers()
    assert reader.raw_top_level(_TAGS_ID)[0].endswith(b"x" * 300)


def test_second_seekhead_preserves_an_index_containing_only_the_second_cluster(tmp_path):
    """Un index partiel de Clusters conserve ses positions, pas leur rang dans l'analyse."""
    info = make_info_element(make_mux_element("Lavf"))
    tracks = element(_TRACKS_ID, element(b"\xae", uint_element(b"\xd7", 1)))
    tags = element(_TAGS_ID, b"x" * 50)
    cluster = make_fake_cluster(100)
    span = len(element(_SEEKHEAD_ID, _seek(_INFO_ID, 100) + _seek(_TRACKS_ID, 100) + _seek(_SEEKHEAD_ID, 5000)))
    cluster_pos = span + len(info) + len(tags) + len(tracks)
    second = element(_SEEKHEAD_ID, _seek(_CLUSTER_ID, cluster_pos + len(cluster)))
    first = element(_SEEKHEAD_ID, _seek(_INFO_ID, span) + _seek(_TRACKS_ID, span + len(info) + len(tags))
                    + _seek(_SEEKHEAD_ID, cluster_pos + 2 * len(cluster)))
    assert len(first) == span
    path = tmp_path / "partial-cluster-index.mkv"
    path.write_bytes(make_ebml_header() + make_segment_unknown_size(
        first + info + tags + tracks + cluster + cluster + second,
    ))
    MatroskaSegmentInfoHeaderEditor().replace_level1_element(
        path, element_id=_TAGS_ID, new_element_bytes=element(_TAGS_ID, b"y" * 300),
    )
    reader = MatroskaReader(path)
    heads = [e for e in reader.top_level() if e.element_id == _SEEKHEAD_ID]
    assert reader._seek_entries(heads[1]) == [(_CLUSTER_ID, cluster_pos + len(cluster))]
    assert [reader.raw_element(e) for e in reader.cluster_elements()] == [cluster, cluster]


def test_padded_seekpositions_allow_header_compaction_without_moving_tracks(tmp_path):
    """Une nouvelle entrée tient grâce à la compaction des uint paddés ; la CRC est conservée."""
    import zlib

    info = make_info_element(make_mux_element("Lavf"))
    tracks = element(_TRACKS_ID, element(b"\xae", uint_element(b"\xd7", 1)))
    cues_id = b"\x1c\x53\xbb\x6b"
    cues = element(cues_id, b"")
    padding = make_void_element(200)

    def padded_seek(target, pos):
        return element(_SEEK_ID, element(_SEEKID_ID, target) + element(_SEEKPOS_ID, pos.to_bytes(8, "big")))

    probe = element(_SEEKHEAD_ID, element(b"\xbf", bytes(4)) + b"".join(
        padded_seek(i, 0) for i in (_INFO_ID, _TRACKS_ID, cues_id)
    ))
    entries = padded_seek(_INFO_ID, len(probe)) + padded_seek(_TRACKS_ID, len(probe) + len(info) + len(padding))
    entries += padded_seek(cues_id, len(probe) + len(info) + len(padding) + len(tracks))
    first = element(_SEEKHEAD_ID, element(b"\xbf", zlib.crc32(entries).to_bytes(4, "little")) + entries)
    assert len(first) == len(probe)
    path = tmp_path / "padded.mkv"
    path.write_bytes(make_ebml_header() + make_segment_unknown_size(
        first + info + padding + tracks + cues + make_fake_cluster(100),
    ))
    reader = MatroskaReader(path)
    tracks_offset = next(e.offset for e in reader.top_level() if e.element_id == _TRACKS_ID)
    assert reader.tracks_indexed_for_strict_readers()
    MatroskaSegmentInfoHeaderEditor().replace_level1_element(
        path, element_id=_TAGS_ID, new_element_bytes=element(_TAGS_ID, b"x" * 300),
    )
    reader = MatroskaReader(path)
    assert next(e.offset for e in reader.top_level() if e.element_id == _TRACKS_ID) == tracks_offset
    assert reader.tracks_indexed_for_strict_readers()
    payload = reader.payload(next(reader.top_level()))
    assert payload[:2] == b"\xbf\x84"
    assert int.from_bytes(payload[2:6], "little") == zlib.crc32(payload[6:])


def test_seekhead_growth_moves_a_chained_second_head_and_preserves_both_crcs(tmp_path):
    """Le premier garde sa place ; le second décalé et son index de Cluster restent valides."""
    import zlib

    info = make_info_element(make_mux_element("Lavf"))
    tracks = element(_TRACKS_ID, element(b"\xae", uint_element(b"\xd7", 1)))
    padding = make_void_element(200)
    cluster = make_fake_cluster(100)

    def crc_head(entries):
        return element(_SEEKHEAD_ID, element(b"\xbf", zlib.crc32(entries).to_bytes(4, "little")) + entries)

    def padded_seek(target, pos):
        return element(_SEEK_ID, element(_SEEKID_ID, target) + element(_SEEKPOS_ID, pos.to_bytes(4, "big")))

    first_span = len(crc_head(b"".join(padded_seek(i, 0) for i in (_INFO_ID, _TRACKS_ID, _SEEKHEAD_ID))))
    second_pos = first_span + len(info)
    second_span = len(crc_head(_seek(_CLUSTER_ID, 5000)))
    tracks_pos = second_pos + second_span + len(padding)
    cluster_pos = tracks_pos + len(tracks)
    second = crc_head(_seek(_CLUSTER_ID, cluster_pos))
    first = crc_head(padded_seek(_INFO_ID, first_span) + padded_seek(_TRACKS_ID, tracks_pos)
                     + padded_seek(_SEEKHEAD_ID, second_pos))
    assert len(first) == first_span and len(second) == second_span
    path = tmp_path / "shifted-second-head.mkv"
    path.write_bytes(make_ebml_header() + make_segment_unknown_size(first + info + second + padding + tracks + cluster))
    original_reader = MatroskaReader(path)
    original_heads = [e for e in original_reader.top_level() if e.element_id == _SEEKHEAD_ID]
    assert original_reader.tracks_indexed_for_strict_readers()

    MatroskaSegmentInfoHeaderEditor().replace_level1_element(
        path, element_id=_TAGS_ID, new_element_bytes=element(_TAGS_ID, b"x" * 300),
    )

    reader = MatroskaReader(path)
    heads = [e for e in reader.top_level() if e.element_id == _SEEKHEAD_ID]
    assert len(heads) == 2 and heads[0].offset == original_heads[0].offset
    assert heads[1].offset > original_heads[1].offset
    assert reader.segment().payload_offset + dict(reader._seek_entries(heads[0]))[_SEEKHEAD_ID] == heads[1].offset
    assert reader._seek_entries(heads[1]) == [(_CLUSTER_ID, cluster_pos)]
    assert reader.raw_element(heads[1]) == second
    assert reader.cluster_elements()[0].offset == reader.segment().payload_offset + cluster_pos
    assert reader.tracks_indexed_for_strict_readers()
    for head in heads:
        payload = reader.payload(head)
        assert int.from_bytes(payload[2:6], "little") == zlib.crc32(payload[6:])
