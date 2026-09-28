"""Tests for MatroskaSegmentInfoHeaderEditor in-place behavior."""

from __future__ import annotations

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
