"""Native Matroska parser (stdlib-only)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..elementary.h264 import parse_avcc
from ..elementary.h265 import parse_hvcc


@dataclass(slots=True)
class MatroskaChapterEntry:
    start_ns: int
    title: str


@dataclass(slots=True)
class MatroskaTrackInfo:
    kind: str
    track_number: int | None = None
    track_uid: int | None = None
    codec_id: str = ""
    language: str = "und"
    name: str = ""
    default: bool = True
    forced: bool = False
    width: int | None = None
    height: int | None = None
    stored_height: int | None = None
    channels: int | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    duration_ms: int | None = None
    default_duration_ns: int | None = None
    frame_rate: float | None = None
    frame_count: int | None = None
    stream_size: int | None = None
    format_profile: str = ""
    format_tier: str = ""
    format_level: str = ""
    format_ref_frames: int | None = None
    color_range: str = ""
    display_aspect_ratio: str = ""
    pixel_format: str = ""
    hdr_format: str = ""
    hdr_format_compatibility: str = ""
    color_primaries: str = ""
    transfer_characteristics: str = ""
    matrix_coefficients: str = ""
    mastering_display_color_primaries: str = ""
    mastering_display_luminance: str = ""
    max_cll: str = ""
    max_fall: str = ""


@dataclass(slots=True)
class MatroskaParseResult:
    container: str
    segment_uid: int | None = None
    track_uid_by_number: dict[int, int] = field(default_factory=dict)
    general_stream_size: int | None = None
    has_level1_crc32: bool = False
    file_size: int | None = None
    duration_ms: int | None = None
    date_utc_unix_ms: int | None = None
    format_version: int | None = None
    writing_application: str = ""
    muxing_application: str = ""
    global_tags: dict[str, str] = field(default_factory=dict)
    track_tags_by_uid: dict[int, dict[str, str]] = field(default_factory=dict)
    chapters: list[MatroskaChapterEntry] = field(default_factory=list)
    tracks: list[MatroskaTrackInfo] = field(default_factory=list)

    def to_metadata(self) -> dict[str, str]:
        meta: dict[str, str] = {"container": self.container}
        if self.segment_uid is not None:
            meta["segment_uid"] = str(self.segment_uid)
        if self.general_stream_size is not None:
            meta["general_stream_size"] = str(self.general_stream_size)
        if self.duration_ms is not None:
            meta["duration_ms"] = str(self.duration_ms)
        if self.date_utc_unix_ms is not None:
            meta["date_utc_unix_ms"] = str(self.date_utc_unix_ms)
        if self.format_version is not None:
            meta["format_version"] = str(self.format_version)
        if self.writing_application:
            meta["writing_application"] = self.writing_application
        if self.muxing_application:
            meta["muxing_application"] = self.muxing_application
        meta["has_level1_crc32"] = "1" if self.has_level1_crc32 else "0"
        if self.file_size is not None:
            meta["file_size"] = str(self.file_size)
        if self.track_uid_by_number:
            for number, uid in sorted(self.track_uid_by_number.items()):
                meta[f"track_uid_{number}"] = str(uid)
        meta["track_count"] = str(len(self.tracks))
        return meta


def _ebml_read_element_id(data: bytes, pos: int) -> tuple[int, int]:
    if pos >= len(data):
        raise ValueError("out of range")
    first = data[pos]
    mask = 0x80
    length = 1
    while length <= 4 and (first & mask) == 0:
        mask >>= 1
        length += 1
    if length > 4 or pos + length > len(data):
        raise ValueError("invalid element id length")
    value = 0
    for i in range(length):
        value = (value << 8) | data[pos + i]
    return value, length


def _ebml_read_element_size(data: bytes, pos: int) -> tuple[int | None, int]:
    if pos >= len(data):
        raise ValueError("out of range")
    first = data[pos]
    mask = 0x80
    length = 1
    while length <= 8 and (first & mask) == 0:
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(data):
        raise ValueError("invalid size length")
    value = first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    unknown = (1 << (7 * length)) - 1
    if value == unknown:
        return None, length
    return value, length


def _ebml_read_vint_value(data: bytes, pos: int) -> tuple[int | None, int]:
    if pos >= len(data):
        return None, 0
    first = data[pos]
    mask = 0x80
    length = 1
    while length <= 8 and (first & mask) == 0:
        mask >>= 1
        length += 1
    if length > 8 or pos + length > len(data):
        return None, 0
    value = first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    return value, length


def _ebml_iter_elements(
    data: bytes,
    start: int,
    end: int,
) -> Iterable[tuple[int, int, int, int]]:
    pos = start
    while pos < end:
        try:
            element_id, id_len = _ebml_read_element_id(data, pos)
            element_size, size_len = _ebml_read_element_size(data, pos + id_len)
        except ValueError:
            break
        payload_offset = pos + id_len + size_len
        if payload_offset > end:
            break
        payload_size = max(0, end - payload_offset) if element_size is None else element_size
        total_size = id_len + size_len + payload_size
        if total_size <= 0 or payload_offset + payload_size > end:
            break
        yield (element_id, payload_offset, payload_size, total_size)
        pos += total_size


def _read_uint(data: bytes, offset: int, size: int) -> int | None:
    if size <= 0 or offset + size > len(data):
        return None
    return int.from_bytes(data[offset : offset + size], byteorder="big", signed=False)


def _read_utf8(data: bytes, offset: int, size: int) -> str:
    if size <= 0 or offset + size > len(data):
        return ""
    return data[offset : offset + size].decode("utf-8", errors="ignore").strip("\x00\n\r\t ")


def _read_float(data: bytes, offset: int, size: int) -> float | None:
    if offset + size > len(data):
        return None
    if size == 4:
        return struct.unpack(">f", data[offset : offset + 4])[0]
    if size == 8:
        return struct.unpack(">d", data[offset : offset + 8])[0]
    return None


def _read_sint(data: bytes, offset: int, size: int) -> int | None:
    if size <= 0 or offset + size > len(data):
        return None
    return int.from_bytes(data[offset : offset + size], byteorder="big", signed=True)


def _find_next_level1_element(data: bytes, start: int, end: int, valid_ids: set[int]) -> int:
    pos = max(start, 0)
    while pos < end:
        try:
            eid, id_len = _ebml_read_element_id(data, pos)
        except ValueError:
            pos += 1
            continue
        if eid not in valid_ids:
            pos += 1
            continue
        try:
            esize, size_len = _ebml_read_element_size(data, pos + id_len)
        except ValueError:
            pos += 1
            continue
        payload_off = pos + id_len + size_len
        if payload_off > end:
            pos += 1
            continue
        if esize is None or payload_off + esize <= end:
            return pos
        pos += 1
    return end


def _iter_segment_level_elements(data: bytes, start: int, end: int) -> Iterable[tuple[int, int, int, int]]:
    valid_ids = {
        0x114D9B74,  # SeekHead
        0x1549A966,  # Info
        0x1654AE6B,  # Tracks
        0x1F43B675,  # Cluster
        0x1C53BB6B,  # Cues
        0x1043A770,  # Chapters
        0x1941A469,  # Attachments
        0x1254C367,  # Tags
        0xEC,        # Void
        0xBF,        # CRC32
    }
    pos = start
    while pos < end:
        try:
            eid, id_len = _ebml_read_element_id(data, pos)
            esize, size_len = _ebml_read_element_size(data, pos + id_len)
        except ValueError:
            break
        payload_off = pos + id_len + size_len
        if payload_off > end:
            break
        if esize is None:
            next_pos = _find_next_level1_element(data, payload_off, end, valid_ids)
            payload_size = max(0, next_pos - payload_off)
        else:
            payload_size = esize
        total_size = id_len + size_len + payload_size
        if total_size <= 0 or payload_off + payload_size > end:
            break
        yield (eid, payload_off, payload_size, total_size)
        pos += total_size


def _kind_from_track_type(track_type: int | None) -> str:
    if track_type == 1:
        return "Video"
    if track_type == 2:
        return "Audio"
    if track_type in {0x11, 17}:
        return "Text"
    return "Other"


def _matroska_laced_frame_count(data: bytes, block_offset: int, block_size: int, track_len: int) -> int:
    header_len = track_len + 3
    if block_size <= header_len:
        return 1
    flags_pos = block_offset + track_len + 2
    if flags_pos >= len(data):
        return 1
    flags = data[flags_pos]
    lacing = (flags >> 1) & 0x03
    if lacing == 0:
        return 1
    lace_pos = flags_pos + 1
    if lace_pos >= len(data) or lace_pos >= block_offset + block_size:
        return 1
    lace_count = int(data[lace_pos]) + 1
    return max(1, lace_count)


def _matroska_block_track_and_size(data: bytes, block_offset: int, block_size: int) -> tuple[int | None, int, int]:
    track_no, track_len = _ebml_read_vint_value(data, block_offset)
    if track_no is None or track_len <= 0:
        return None, 0, 0
    if block_size <= track_len + 3:
        return track_no, 0, 1
    payload_size = max(0, block_size - (track_len + 3))
    frame_count = _matroska_laced_frame_count(data, block_offset, block_size, track_len)
    return track_no, payload_size, frame_count


def _matroska_parse_simple_tag(
    data: bytes,
    start: int,
    end: int,
) -> dict[str, str]:
    name = ""
    value = ""
    out: dict[str, str] = {}
    for se, so, ss, _ in _ebml_iter_elements(data, start, end):
        if se == 0x45A3:  # TagName
            name = _read_utf8(data, so, ss).strip()
        elif se == 0x4487:  # TagString
            value = _read_utf8(data, so, ss).strip()
        elif se == 0x67C8:  # nested SimpleTag
            out.update(_matroska_parse_simple_tag(data, so, so + ss))
    if name and value:
        norm = name.strip()
        if norm == "HANDLER_NAME":
            norm = "TITLE"
        out[norm] = value
    return out


def _matroska_parse_tags(
    data: bytes,
    start: int,
    end: int,
) -> tuple[dict[str, str], dict[int, dict[str, str]]]:
    global_tags: dict[str, str] = {}
    by_uid: dict[int, dict[str, str]] = {}
    for eid, off, size, _ in _ebml_iter_elements(data, start, end):
        if eid != 0x7373:  # Tag
            continue
        target_uid: int | None = None
        tag_end = off + size
        for teid, t_off, t_size, _ in _ebml_iter_elements(data, off, tag_end):
            if teid == 0x63C0:  # Targets
                targets_end = t_off + t_size
                for geid, g_off, g_size, _ in _ebml_iter_elements(data, t_off, targets_end):
                    if geid == 0x63C5:  # TrackUID
                        target_uid = _read_uint(data, g_off, g_size)
            elif teid == 0x67C8:  # SimpleTag
                tags = _matroska_parse_simple_tag(data, t_off, t_off + t_size)
                if not tags:
                    continue
                if target_uid is None:
                    global_tags.update(tags)
                else:
                    current = by_uid.setdefault(int(target_uid), {})
                    current.update(tags)
    return global_tags, by_uid


def _matroska_parse_chapter_atoms(
    data: bytes,
    start: int,
    end: int,
    out: list[MatroskaChapterEntry],
) -> None:
    for eid, off, size, _ in _ebml_iter_elements(data, start, end):
        if eid != 0xB6:  # ChapterAtom
            continue
        c_start_ns: int | None = None
        c_title = ""
        atom_end = off + size
        for aeid, a_off, a_size, _ in _ebml_iter_elements(data, off, atom_end):
            if aeid == 0x91:  # ChapterTimeStart
                c_start_ns = _read_uint(data, a_off, a_size)
            elif aeid == 0x80:  # ChapterDisplay
                display_end = a_off + a_size
                for deid, d_off, d_size, _ in _ebml_iter_elements(data, a_off, display_end):
                    if deid == 0x85:  # ChapString
                        txt = _read_utf8(data, d_off, d_size)
                        if txt:
                            c_title = txt
            elif aeid == 0xB6:  # nested ChapterAtom
                _matroska_parse_chapter_atoms(data, a_off, a_off + a_size, out)
        if c_start_ns is not None and c_title:
            out.append(MatroskaChapterEntry(start_ns=int(c_start_ns), title=c_title))


def _matroska_parse_chapters(
    data: bytes,
    start: int,
    end: int,
) -> list[MatroskaChapterEntry]:
    out: list[MatroskaChapterEntry] = []
    for eid, off, size, _ in _ebml_iter_elements(data, start, end):
        if eid != 0x45B9:  # EditionEntry
            continue
        _matroska_parse_chapter_atoms(data, off, off + size, out)
    out.sort(key=lambda item: item.start_ns)
    return out


def parse_matroska(source: str) -> MatroskaParseResult:
    path = Path(source).expanduser()
    if not path.exists():
        return MatroskaParseResult(container="matroska")
    try:
        data = path.read_bytes()
    except OSError:
        return MatroskaParseResult(container="matroska")
    if len(data) < 12:
        return MatroskaParseResult(container="matroska", file_size=len(data))

    result = MatroskaParseResult(container="matroska", file_size=len(data))
    file_size = len(data)

    # EBML header (DocTypeVersion)
    for eid, off, size, _ in _ebml_iter_elements(data, 0, min(file_size, 4096)):
        if eid == 0x1A45DFA3:
            e_end = off + size
            for heid, h_off, h_size, _ in _ebml_iter_elements(data, off, e_end):
                if heid == 0x4287:
                    result.format_version = _read_uint(data, h_off, h_size)
            break

    segment_data_start: int | None = None
    segment_data_size: int | None = None
    pos = 0
    while pos < file_size:
        try:
            element_id, id_len = _ebml_read_element_id(data, pos)
            element_size, size_len = _ebml_read_element_size(data, pos + id_len)
        except ValueError:
            break
        payload_offset = pos + id_len + size_len
        if payload_offset > file_size:
            break
        payload_size = (file_size - payload_offset) if element_size is None else element_size
        total = id_len + size_len + payload_size
        if element_id == 0x18538067:  # Segment
            segment_data_start = payload_offset
            segment_data_size = payload_size
            break
        if total <= 0:
            break
        pos += total

    if segment_data_start is None:
        return result

    segment_end = min(file_size, segment_data_start + (segment_data_size or (file_size - segment_data_start)))
    non_cluster_sum = 0
    tags_sum = 0
    void_sum = 0

    timecode_scale = 1_000_000  # default ns
    duration_scale_value: float | None = None
    block_bytes_by_track: dict[int, int] = {}
    block_frames_by_track: dict[int, int] = {}

    for eid, payload_off, payload_size, total_size in _iter_segment_level_elements(data, segment_data_start, segment_end):
        if eid != 0x1F43B675:  # Cluster
            non_cluster_sum += total_size
        if eid == 0x1254C367:  # Tags
            tags_sum += total_size
            g_tags, uid_tags = _matroska_parse_tags(data, payload_off, payload_off + payload_size)
            if g_tags:
                result.global_tags.update(g_tags)
            if uid_tags:
                for uid, tag_map in uid_tags.items():
                    cur = result.track_tags_by_uid.setdefault(uid, {})
                    cur.update(tag_map)
        elif eid == 0xEC:  # Void
            void_sum += total_size

        payload_end = payload_off + payload_size
        if eid == 0x1549A966:  # Info
            for ceid, c_off, c_size, _ in _ebml_iter_elements(data, payload_off, payload_end):
                if ceid == 0x73A4 and c_size > 0:
                    result.segment_uid = int.from_bytes(data[c_off : c_off + c_size], byteorder="big", signed=False)
                elif ceid == 0x2AD7B1 and c_size > 0:  # TimecodeScale
                    tc = _read_uint(data, c_off, c_size)
                    if tc is not None:
                        timecode_scale = tc
                elif ceid == 0x4489 and c_size > 0:  # Duration (float)
                    duration_scale_value = _read_float(data, c_off, c_size)
                elif ceid == 0x4461 and c_size > 0:  # DateUTC
                    dt_ns = _read_sint(data, c_off, c_size)
                    if dt_ns is not None:
                        # Matroska DateUTC: ns since 2001-01-01T00:00:00 UTC.
                        result.date_utc_unix_ms = 978_307_200_000 + int(round(dt_ns / 1_000_000.0))
                elif ceid == 0x5741 and c_size > 0:  # WritingApp
                    result.writing_application = _read_utf8(data, c_off, c_size)
                elif ceid == 0x4D80 and c_size > 0:  # MuxingApp
                    result.muxing_application = _read_utf8(data, c_off, c_size)
                elif ceid == 0x7BA9 and c_size > 0:  # Title
                    title = _read_utf8(data, c_off, c_size)
                    if title:
                        result.global_tags.setdefault("TITLE", title)
                elif ceid == 0xBF:
                    result.has_level1_crc32 = True

        elif eid == 0x1654AE6B:  # Tracks
            for ceid, c_off, c_size, _ in _ebml_iter_elements(data, payload_off, payload_end):
                if ceid == 0xBF:
                    result.has_level1_crc32 = True
                if ceid != 0xAE:  # TrackEntry
                    continue

                track_no: int | None = None
                track_uid: int | None = None
                track_type: int | None = None
                codec_id = ""
                language = "und"
                name = ""
                default = True
                forced = False
                width: int | None = None
                height: int | None = None
                display_width: int | None = None
                display_height: int | None = None
                channels: int | None = None
                sample_rate: int | None = None
                bit_depth: int | None = None
                default_duration_ns: int | None = None
                video_bit_depth: int | None = None
                codec_private = b""
                color_range = ""
                transfer_characteristics: int | None = None
                color_primaries: int | None = None
                matrix_coefficients: int | None = None
                has_mastering_metadata = False

                c_end = c_off + c_size
                for teid, t_off, t_size, _ in _ebml_iter_elements(data, c_off, c_end):
                    if teid == 0xD7:  # TrackNumber
                        track_no = _read_uint(data, t_off, t_size)
                    elif teid == 0x73C5:  # TrackUID
                        track_uid = _read_uint(data, t_off, t_size)
                    elif teid == 0x83:  # TrackType
                        track_type = _read_uint(data, t_off, t_size)
                    elif teid == 0x86:  # CodecID
                        codec_id = _read_utf8(data, t_off, t_size)
                    elif teid == 0x22B59C:  # Language
                        lang = _read_utf8(data, t_off, t_size)
                        if lang:
                            language = lang
                    elif teid == 0x536E:  # Name
                        name = _read_utf8(data, t_off, t_size)
                    elif teid == 0x88:  # FlagDefault
                        val = _read_uint(data, t_off, t_size)
                        default = (val != 0) if val is not None else True
                    elif teid == 0x55AA:  # FlagForced
                        val = _read_uint(data, t_off, t_size)
                        forced = (val != 0) if val is not None else False
                    elif teid == 0x23E383:  # DefaultDuration (ns per frame)
                        default_duration_ns = _read_uint(data, t_off, t_size)
                    elif teid == 0x63A2:  # CodecPrivate
                        codec_private = data[t_off : t_off + t_size] if t_size > 0 else b""
                    elif teid == 0xE0:  # Video
                        v_end = t_off + t_size
                        for veid, v_off, v_size, _ in _ebml_iter_elements(data, t_off, v_end):
                            if veid == 0xB0:
                                width = _read_uint(data, v_off, v_size)
                            elif veid == 0xBA:
                                height = _read_uint(data, v_off, v_size)
                            elif veid == 0x54B0:
                                display_width = _read_uint(data, v_off, v_size)
                            elif veid == 0x54BA:
                                display_height = _read_uint(data, v_off, v_size)
                            elif veid == 0x55B0:  # Colour
                                c_end = v_off + v_size
                                for ce2, c2_off, c2_size, _ in _ebml_iter_elements(data, v_off, c_end):
                                    if ce2 == 0x55B2:
                                        video_bit_depth = _read_uint(data, c2_off, c2_size)
                                    elif ce2 == 0x55B9:
                                        range_val = _read_uint(data, c2_off, c2_size)
                                        if range_val == 2:
                                            color_range = "pc"
                                        elif range_val == 1:
                                            color_range = "tv"
                                    elif ce2 == 0x55BA:
                                        transfer_characteristics = _read_uint(data, c2_off, c2_size)
                                    elif ce2 == 0x55BB:
                                        color_primaries = _read_uint(data, c2_off, c2_size)
                                    elif ce2 == 0x55B1:
                                        matrix_coefficients = _read_uint(data, c2_off, c2_size)
                                    elif ce2 in {0x55D0, 0x55BC, 0x55BD}:
                                        has_mastering_metadata = True
                    elif teid == 0xE1:  # Audio
                        a_end = t_off + t_size
                        for aeid, a_off, a_size, _ in _ebml_iter_elements(data, t_off, a_end):
                            if aeid == 0x9F:
                                channels = _read_uint(data, a_off, a_size)
                            elif aeid == 0xB5:
                                sf = _read_float(data, a_off, a_size)
                                if sf is not None:
                                    sample_rate = int(round(sf))
                            elif aeid == 0x6264:
                                bit_depth = _read_uint(data, a_off, a_size)

                if track_no is not None and track_uid is not None:
                    result.track_uid_by_number[track_no] = track_uid

                format_profile = ""
                format_tier = ""
                format_level = ""
                format_ref_frames: int | None = None
                pixel_format = ""
                stored_height: int | None = None
                if codec_id.upper().startswith("V_MPEG4/ISO/AVC") and codec_private:
                    avcc = parse_avcc(codec_private)
                    format_profile = str(avcc.get("profile") or "")
                    level_int = avcc.get("level")
                    if isinstance(level_int, int) and level_int > 0:
                        format_level = f"{(level_int / 10.0):.1f}".rstrip("0").rstrip(".")
                    refs = avcc.get("ref_frames")
                    if isinstance(refs, int) and refs > 0:
                        format_ref_frames = refs
                    sh = avcc.get("stored_height")
                    if isinstance(sh, int) and sh > 0:
                        stored_height = sh
                    if video_bit_depth is None and isinstance(avcc.get("chroma_format_idc"), int):
                        video_bit_depth = 8
                    pixel_format = "yuv420p10le" if (video_bit_depth or 8) > 8 else "yuv420p"
                if codec_id.upper().startswith("V_MPEGH/ISO/HEVC") and codec_private:
                    hvcc = parse_hvcc(codec_private)
                    format_profile = str(hvcc.get("profile") or "")
                    format_tier = str(hvcc.get("tier") or "")
                    format_level = str(hvcc.get("level") or "")
                    if video_bit_depth is None and isinstance(hvcc.get("bit_depth"), int):
                        video_bit_depth = int(hvcc["bit_depth"])
                    pixel_format = str(hvcc.get("pixel_format") or "")

                display_aspect_ratio = ""
                dar_w = display_width or width
                dar_h = display_height or height
                if dar_w and dar_h:
                    display_aspect_ratio = f"{dar_w}:{dar_h}"
                hdr_format = ""
                hdr_compat = ""
                if has_mastering_metadata:
                    hdr_format = "SMPTE ST 2086"
                    hdr_compat = "HDR10"
                elif transfer_characteristics == 16 and (video_bit_depth or 0) >= 10 and color_primaries in {9, 2}:
                    hdr_format = "SMPTE ST 2086"
                    hdr_compat = "HDR10"
                elif transfer_characteristics == 18:
                    hdr_format = "HLG"

                result.tracks.append(
                    MatroskaTrackInfo(
                        kind=_kind_from_track_type(track_type),
                        track_number=track_no,
                        track_uid=track_uid,
                        codec_id=codec_id,
                        language=language,
                        name=name,
                        default=default,
                        forced=forced,
                        width=width,
                        height=height,
                        stored_height=stored_height,
                        channels=channels,
                        sample_rate=sample_rate,
                        bit_depth=bit_depth if bit_depth is not None else video_bit_depth,
                        default_duration_ns=default_duration_ns,
                        format_profile=format_profile,
                        format_tier=format_tier,
                        format_level=format_level,
                        format_ref_frames=format_ref_frames,
                        color_range=color_range,
                        display_aspect_ratio=display_aspect_ratio,
                        pixel_format=pixel_format,
                        hdr_format=hdr_format,
                        hdr_format_compatibility=hdr_compat,
                        color_primaries="BT.2020" if color_primaries == 9 else "",
                        transfer_characteristics="PQ" if transfer_characteristics == 16 else ("HLG" if transfer_characteristics == 18 else ""),
                        matrix_coefficients="BT.2020 non-constant" if matrix_coefficients == 9 else "",
                    )
                )
        elif eid == 0x1043A770:  # Chapters
            result.chapters = _matroska_parse_chapters(data, payload_off, payload_end)
        elif eid == 0x1F43B675:  # Cluster
            cluster_end = payload_off + payload_size
            for ceid, c_off, c_size, _ in _ebml_iter_elements(data, payload_off, cluster_end):
                if ceid == 0xA3:  # SimpleBlock
                    track_no, payload_sz, frame_count = _matroska_block_track_and_size(data, c_off, c_size)
                    if track_no is None:
                        continue
                    block_bytes_by_track[track_no] = block_bytes_by_track.get(track_no, 0) + payload_sz
                    block_frames_by_track[track_no] = block_frames_by_track.get(track_no, 0) + frame_count
                elif ceid == 0xA0:  # BlockGroup
                    group_end = c_off + c_size
                    for geid, g_off, g_size, _ in _ebml_iter_elements(data, c_off, group_end):
                        if geid != 0xA1:  # Block
                            continue
                        track_no, payload_sz, frame_count = _matroska_block_track_and_size(data, g_off, g_size)
                        if track_no is None:
                            continue
                        block_bytes_by_track[track_no] = block_bytes_by_track.get(track_no, 0) + payload_sz
                        block_frames_by_track[track_no] = block_frames_by_track.get(track_no, 0) + frame_count
        elif eid == 0xBF:
            result.has_level1_crc32 = True

    general_size = non_cluster_sum - tags_sum - void_sum
    if general_size >= 0:
        result.general_stream_size = general_size

    if duration_scale_value is not None:
        # Duration unit is TimecodeScale (ns).
        result.duration_ms = int(round(duration_scale_value * timecode_scale / 1_000_000.0))
        for track in result.tracks:
            track.duration_ms = result.duration_ms
            has_measured_frames = track.track_number is not None and track.track_number in block_frames_by_track
            if track.default_duration_ns and track.default_duration_ns > 0:
                track.frame_rate = 1_000_000_000.0 / track.default_duration_ns
            if track.default_duration_ns and track.default_duration_ns > 0 and not has_measured_frames:
                track.frame_count = int(round((track.duration_ms * 1_000_000.0) / track.default_duration_ns))

    for track in result.tracks:
        if track.track_number is None:
            continue
        if track.track_number in block_bytes_by_track:
            track.stream_size = block_bytes_by_track[track.track_number]
        if track.frame_count is None and track.track_number in block_frames_by_track:
            track.frame_count = block_frames_by_track[track.track_number]

    return result
