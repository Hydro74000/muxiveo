"""Native MP4 container parser (stdlib-only)."""

from __future__ import annotations

import struct
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..elementary.h264 import parse_avcc

@dataclass(slots=True)
class Mp4AtomLayout:
    header_size: int | None = None
    data_size: int | None = None
    footer_size: int | None = None
    mdat_header_size: int | None = None


@dataclass(slots=True)
class Mp4TrackInfo:
    kind: str
    track_id: int | None = None
    codec_id: str = ""
    duration_ms: int | None = None
    timescale: int | None = None
    sample_count: int | None = None
    sample_total_size: int | None = None
    frame_rate: float | None = None
    profile: str = ""
    level: int | None = None
    refs: int | None = None
    stored_width: int | None = None
    stored_height: int | None = None
    pixel_format: str = ""
    is_avc: bool = False
    width: int | None = None
    height: int | None = None
    channels: int | None = None
    sample_rate: int | None = None
    language: str = "und"
    default: bool = True
    forced: bool = False


@dataclass(slots=True)
class Mp4ParseResult:
    container: str
    major_brand: str = ""
    compatible_brands: str = ""
    atom_layout: Mp4AtomLayout | None = None
    file_size: int | None = None
    duration_ms: int | None = None
    writing_application: str = ""
    x264_library: str = ""
    x264_name: str = ""
    x264_version: str = ""
    x264_settings: str = ""
    tracks: list[Mp4TrackInfo] = field(default_factory=list)

    def to_metadata(self) -> dict[str, str]:
        meta: dict[str, str] = {"container": self.container}
        if self.major_brand:
            meta["major_brand"] = self.major_brand
        if self.compatible_brands:
            meta["compatible_brands"] = self.compatible_brands
        if self.file_size is not None:
            meta["file_size"] = str(self.file_size)
        if self.duration_ms is not None:
            meta["duration_ms"] = str(self.duration_ms)
        if self.writing_application:
            meta["writing_application"] = self.writing_application
        if self.x264_library:
            meta["x264_library"] = self.x264_library
        if self.atom_layout:
            if self.atom_layout.header_size is not None:
                meta["header_size"] = str(self.atom_layout.header_size)
            if self.atom_layout.data_size is not None:
                meta["data_size"] = str(self.atom_layout.data_size)
            if self.atom_layout.footer_size is not None:
                meta["footer_size"] = str(self.atom_layout.footer_size)
            if self.atom_layout.mdat_header_size is not None:
                meta["mdat_header_size"] = str(self.atom_layout.mdat_header_size)
        meta["track_count"] = str(len(self.tracks))
        return meta


def _iter_atoms(blob: bytes, start: int, end: int) -> Iterable[tuple[str, int, int, int, int]]:
    pos = start
    while pos + 8 <= end:
        size32 = int.from_bytes(blob[pos : pos + 4], "big", signed=False)
        typ = blob[pos + 4 : pos + 8]
        atom_size = size32
        header_size = 8
        if atom_size == 1:
            if pos + 16 > end:
                break
            atom_size = int.from_bytes(blob[pos + 8 : pos + 16], "big", signed=False)
            header_size = 16
        elif atom_size == 0:
            atom_size = end - pos

        if atom_size < header_size or pos + atom_size > end:
            break

        payload_start = pos + header_size
        payload_end = pos + atom_size
        name = typ.decode("ascii", errors="ignore")
        yield name, pos, payload_start, payload_end, atom_size
        pos += atom_size


def _read_ftyp(path: Path) -> tuple[str, str]:
    try:
        with path.open("rb") as fh:
            head = fh.read(4096)
    except OSError:
        return "", ""
    if len(head) < 16:
        return "", ""

    for typ, _pos, payload_start, payload_end, _size in _iter_atoms(head, 0, len(head)):
        if typ != "ftyp":
            continue
        payload = head[payload_start:payload_end]
        if len(payload) < 8:
            return "", ""
        major_brand = payload[0:4].decode("ascii", errors="ignore").strip()
        brands: list[str] = []
        for i in range(8, len(payload), 4):
            b = payload[i : i + 4]
            if len(b) < 4:
                break
            brand = b.decode("ascii", errors="ignore").strip()
            if brand:
                brands.append(brand)
        return major_brand, "/".join(brands)
    return "", ""


def _read_writing_application(path: Path) -> str:
    try:
        blob = path.read_bytes()
    except OSError:
        return ""
    limit = len(blob) - 8
    pos = 0
    while pos <= limit:
        if blob[pos + 4 : pos + 8] != b"\xA9too":
            pos += 1
            continue
        size = int.from_bytes(blob[pos : pos + 4], "big", signed=False)
        if size < 8:
            pos += 1
            continue
        end = pos + size
        if end > len(blob):
            pos += 1
            continue
        payload_start = pos + 8
        for _name, child_pos, child_payload_start, child_payload_end, _child_size in _iter_atoms(
            blob, payload_start, end
        ):
            if blob[child_pos + 4 : child_pos + 8] != b"data":
                continue
            data = blob[child_payload_start:child_payload_end]
            if len(data) <= 8:
                continue
            text = data[8:].decode("utf-8", errors="ignore").strip("\x00 \n\r\t")
            if not text:
                text = data[8:].decode("latin1", errors="ignore").strip("\x00 \n\r\t")
            if text:
                return text
        pos = end
    return ""


def _read_x264_metadata(path: Path) -> tuple[str, str, str, str]:
    try:
        blob = path.read_bytes()
    except OSError:
        return "", "", "", ""
    marker = b"x264 - core "
    idx = blob.find(marker)
    if idx < 0:
        return "", "", "", ""
    window = blob[idx : min(len(blob), idx + 32768)]
    text = "".join(chr(c) if 32 <= c < 127 else "\x00" for c in window)
    start = text.find("x264 - core ")
    if start < 0:
        return "", "", "", ""
    sub = text[start:]
    sub = sub.split("\x00", 1)[0]
    core = ""
    m = re.search(r"x264\s*-\s*core\s+([0-9]+)", sub)
    if m:
        core = m.group(1)
    settings = ""
    mopt = re.search(r"options:\s*(.+)$", sub)
    if mopt:
        settings = mopt.group(1).strip()
        if settings and "/" not in settings:
            settings = " / ".join(settings.split())
    if not core:
        return "", "", "", settings
    return f"x264 - core {core}", "x264", f"core {core}", settings


def _read_atom_layout(path: Path) -> Mp4AtomLayout | None:
    if not path.exists():
        return None
    file_size = path.stat().st_size
    layout = Mp4AtomLayout()
    try:
        with path.open("rb") as fh:
            offset = 0
            while offset + 8 <= file_size:
                fh.seek(offset)
                header = fh.read(8)
                if len(header) < 8:
                    break
                size32, atom = struct.unpack(">I4s", header)
                atom_size = size32
                header_size = 8
                if atom_size == 1:
                    ext = fh.read(8)
                    if len(ext) < 8:
                        break
                    atom_size = struct.unpack(">Q", ext)[0]
                    header_size = 16
                elif atom_size == 0:
                    atom_size = file_size - offset
                if atom_size < header_size:
                    break

                atom_name = atom.decode("ascii", errors="ignore")
                if atom_name == "mdat":
                    layout.header_size = offset
                    layout.data_size = atom_size
                    layout.mdat_header_size = header_size
                    layout.footer_size = max(0, file_size - (offset + atom_size))
                    return layout
                offset += atom_size
    except OSError:
        return None
    return None


def _language_from_mdhd(code: int) -> str:
    if code == 0:
        return "und"
    c1 = ((code >> 10) & 0x1F) + 0x60
    c2 = ((code >> 5) & 0x1F) + 0x60
    c3 = (code & 0x1F) + 0x60
    try:
        out = bytes([c1, c2, c3]).decode("ascii")
    except Exception:
        return "und"
    return out if out.isalpha() else "und"


def _parse_tkhd(payload: bytes) -> tuple[int | None, int | None, int | None]:
    if len(payload) < 84:
        return None, None, None
    version = payload[0]
    try:
        if version == 1:
            track_id = int.from_bytes(payload[20:24], "big", signed=False)
            width_fixed = int.from_bytes(payload[92:96], "big", signed=False)
            height_fixed = int.from_bytes(payload[96:100], "big", signed=False)
        else:
            track_id = int.from_bytes(payload[12:16], "big", signed=False)
            width_fixed = int.from_bytes(payload[76:80], "big", signed=False)
            height_fixed = int.from_bytes(payload[80:84], "big", signed=False)
    except Exception:
        return None, None, None
    return track_id, width_fixed >> 16, height_fixed >> 16


def _parse_mdhd(payload: bytes) -> tuple[int | None, int | None, str]:
    if len(payload) < 24:
        return None, None, "und"
    version = payload[0]
    try:
        if version == 1:
            timescale = int.from_bytes(payload[20:24], "big", signed=False)
            duration = int.from_bytes(payload[24:32], "big", signed=False)
            lang_raw = int.from_bytes(payload[32:34], "big", signed=False)
        else:
            timescale = int.from_bytes(payload[12:16], "big", signed=False)
            duration = int.from_bytes(payload[16:20], "big", signed=False)
            lang_raw = int.from_bytes(payload[20:22], "big", signed=False)
    except Exception:
        return None, None, "und"
    language = _language_from_mdhd(lang_raw & 0x7FFF)
    return timescale, duration, language


def _parse_hdlr(payload: bytes) -> str:
    if len(payload) < 12:
        return ""
    return payload[8:12].decode("ascii", errors="ignore").strip()


def _find_sample_box_payload(sample: bytes, box_type: bytes) -> bytes:
    for pos in range(0, max(0, len(sample) - 8)):
        if sample[pos + 4 : pos + 8] != box_type:
            continue
        size = int.from_bytes(sample[pos : pos + 4], "big", signed=False)
        if size < 8:
            continue
        end = pos + size
        if end <= len(sample):
            return sample[pos + 8 : end]
    return b""


def _parse_stsd(
    payload: bytes,
) -> tuple[
    str,
    int | None,
    int | None,
    str,
    int | None,
    int | None,
    int | None,
    int | None,
    str,
    bool,
]:
    if len(payload) < 16:
        return "", None, None, "", None, None, None, None, "", False
    entry_count = int.from_bytes(payload[4:8], "big", signed=False)
    if entry_count <= 0:
        return "", None, None, "", None, None, None, None, "", False

    entry = payload[8:]
    if len(entry) < 16:
        return "", None, None, "", None, None, None, None, "", False
    entry_size = int.from_bytes(entry[0:4], "big", signed=False)
    if entry_size < 16 or entry_size > len(entry):
        entry_size = len(entry)
    sample = entry[:entry_size]
    codec_id = sample[4:8].decode("ascii", errors="ignore").strip()

    channels: int | None = None
    sample_rate: int | None = None
    profile = ""
    level: int | None = None
    refs: int | None = None
    stored_width: int | None = None
    stored_height: int | None = None
    pixel_format = ""
    is_avc = False

    avcc_payload = _find_sample_box_payload(sample, b"avcC")
    if avcc_payload:
        is_avc = True
        avcc_meta = parse_avcc(avcc_payload)
        profile = str(avcc_meta.get("profile") or "")
        level_obj = avcc_meta.get("level")
        if isinstance(level_obj, int):
            level = level_obj
        refs_obj = avcc_meta.get("ref_frames")
        if isinstance(refs_obj, int):
            refs = refs_obj
        sw = avcc_meta.get("stored_width")
        sh = avcc_meta.get("stored_height")
        if isinstance(sw, int):
            stored_width = sw
        if isinstance(sh, int):
            stored_height = sh
        chroma = avcc_meta.get("chroma_format_idc")
        if isinstance(chroma, int):
            if chroma == 1:
                pixel_format = "yuv420p"
            elif chroma == 2:
                pixel_format = "yuv422p"
            elif chroma == 3:
                pixel_format = "yuv444p"

    # Audio sample-entry quick parse.
    if len(sample) >= 36:
        channels = int.from_bytes(sample[24:26], "big", signed=False)
        sr_fixed = int.from_bytes(sample[32:36], "big", signed=False)
        if sr_fixed:
            sample_rate = sr_fixed >> 16
    return codec_id, channels, sample_rate, profile, level, refs, stored_width, stored_height, pixel_format, is_avc


def _parse_stsz(payload: bytes) -> tuple[int | None, int | None]:
    if len(payload) < 12:
        return None, None
    sample_size = int.from_bytes(payload[4:8], "big", signed=False)
    sample_count = int.from_bytes(payload[8:12], "big", signed=False)
    if sample_count <= 0:
        return 0, 0
    if sample_size > 0:
        return sample_count, sample_size * sample_count
    total_size = 0
    expected = 12 + (4 * sample_count)
    if len(payload) < expected:
        sample_count = max(0, (len(payload) - 12) // 4)
    offset = 12
    for _ in range(sample_count):
        if offset + 4 > len(payload):
            break
        total_size += int.from_bytes(payload[offset : offset + 4], "big", signed=False)
        offset += 4
    return sample_count, total_size


def _parse_stts(payload: bytes) -> tuple[int | None, int | None]:
    if len(payload) < 8:
        return None, None
    entry_count = int.from_bytes(payload[4:8], "big", signed=False)
    offset = 8
    total_samples = 0
    total_delta = 0
    for _ in range(entry_count):
        if offset + 8 > len(payload):
            break
        count = int.from_bytes(payload[offset : offset + 4], "big", signed=False)
        delta = int.from_bytes(payload[offset + 4 : offset + 8], "big", signed=False)
        total_samples += count
        total_delta += count * delta
        offset += 8
    return total_samples, total_delta


def _parse_mvhd(payload: bytes) -> tuple[int | None, int | None]:
    if len(payload) < 24:
        return None, None
    version = payload[0]
    try:
        if version == 1:
            timescale = int.from_bytes(payload[20:24], "big", signed=False)
            duration = int.from_bytes(payload[24:32], "big", signed=False)
        else:
            timescale = int.from_bytes(payload[12:16], "big", signed=False)
            duration = int.from_bytes(payload[16:20], "big", signed=False)
    except Exception:
        return None, None
    return timescale, duration


def _kind_from_handler(handler: str) -> str:
    if handler == "vide":
        return "Video"
    if handler == "soun":
        return "Audio"
    if handler in {"text", "sbtl", "subt", "clcp"}:
        return "Text"
    return "Other"


def _track_duration_ms(timescale: int | None, duration: int | None) -> int | None:
    if not timescale or not duration:
        return None
    if timescale <= 0:
        return None
    return int(round(duration * 1000.0 / timescale))


def _parse_mp4_tracks(path: Path) -> tuple[list[Mp4TrackInfo], int | None]:
    try:
        blob = path.read_bytes()
    except OSError:
        return [], None

    moov_start = None
    moov_end = None
    for typ, _pos, payload_start, payload_end, _size in _iter_atoms(blob, 0, len(blob)):
        if typ == "moov":
            moov_start, moov_end = payload_start, payload_end
            break
    if moov_start is None or moov_end is None:
        return [], None

    tracks: list[Mp4TrackInfo] = []
    max_duration: int | None = None
    movie_duration_ms: int | None = None

    for typ, _pos, cstart, cend, _size in _iter_atoms(blob, moov_start, moov_end):
        if typ != "mvhd":
            continue
        mv_timescale, mv_duration_units = _parse_mvhd(blob[cstart:cend])
        movie_duration_ms = _track_duration_ms(mv_timescale, mv_duration_units)
        break

    for typ, _pos, trak_start, trak_end, _size in _iter_atoms(blob, moov_start, moov_end):
        if typ != "trak":
            continue

        track_id: int | None = None
        width: int | None = None
        height: int | None = None
        handler = ""
        timescale: int | None = None
        duration_units: int | None = None
        language = "und"
        codec_id = ""
        channels: int | None = None
        sample_rate: int | None = None
        profile = ""
        level: int | None = None
        refs: int | None = None
        stored_width: int | None = None
        stored_height: int | None = None
        pixel_format = ""
        is_avc = False
        sample_count: int | None = None
        sample_total_size: int | None = None
        stts_total_samples: int | None = None
        stts_total_delta: int | None = None

        for ctyp, _cpos, cstart, cend, _csize in _iter_atoms(blob, trak_start, trak_end):
            payload = blob[cstart:cend]
            if ctyp == "tkhd":
                track_id, width, height = _parse_tkhd(payload)
            elif ctyp == "mdia":
                for mtyp, _mpos, mstart, mend, _msize in _iter_atoms(blob, cstart, cend):
                    mpayload = blob[mstart:mend]
                    if mtyp == "hdlr":
                        handler = _parse_hdlr(mpayload)
                    elif mtyp == "mdhd":
                        timescale, duration_units, language = _parse_mdhd(mpayload)
                    elif mtyp == "minf":
                        for styp, _spos, sstart, send, _ssize in _iter_atoms(blob, mstart, mend):
                            if styp != "stbl":
                                continue
                            for ttyp, _tpos, tstart, tend, _tsize in _iter_atoms(blob, sstart, send):
                                if ttyp == "stsd":
                                    (
                                        codec_id,
                                        channels,
                                        sample_rate,
                                        profile,
                                        level,
                                        refs,
                                        stored_width,
                                        stored_height,
                                        pixel_format,
                                        is_avc,
                                    ) = _parse_stsd(blob[tstart:tend])
                                elif ttyp == "stsz":
                                    sample_count, sample_total_size = _parse_stsz(blob[tstart:tend])
                                elif ttyp == "stts":
                                    stts_total_samples, stts_total_delta = _parse_stts(blob[tstart:tend])

        kind = _kind_from_handler(handler)
        duration_ms = _track_duration_ms(timescale, duration_units) or movie_duration_ms
        frame_rate: float | None = None
        if timescale and stts_total_samples and stts_total_delta and stts_total_delta > 0:
            frame_rate = (timescale * stts_total_samples) / stts_total_delta
        if duration_ms is not None:
            max_duration = max(max_duration or 0, duration_ms)
        tracks.append(
            Mp4TrackInfo(
                kind=kind,
                track_id=track_id,
                codec_id=codec_id,
                duration_ms=duration_ms,
                timescale=timescale,
                sample_count=sample_count or stts_total_samples,
                sample_total_size=sample_total_size,
                frame_rate=frame_rate,
                profile=profile,
                level=level,
                refs=refs,
                stored_width=stored_width,
                stored_height=stored_height,
                pixel_format=pixel_format,
                is_avc=is_avc,
                width=width,
                height=height,
                channels=channels,
                sample_rate=sample_rate,
                language=language,
            )
        )

    return tracks, movie_duration_ms or max_duration


def parse_mp4(source: str) -> Mp4ParseResult:
    path = Path(source).expanduser()
    major_brand, compatible_brands = _read_ftyp(path)
    writing_application = _read_writing_application(path) if path.exists() else ""
    x264_library, x264_name, x264_version, x264_settings = _read_x264_metadata(path) if path.exists() else ("", "", "", "")
    file_size = path.stat().st_size if path.exists() else None
    layout = _read_atom_layout(path)
    tracks, duration_ms = _parse_mp4_tracks(path) if path.exists() else ([], None)
    return Mp4ParseResult(
        container="mp4",
        major_brand=major_brand,
        compatible_brands=compatible_brands,
        atom_layout=layout,
        file_size=file_size,
        duration_ms=duration_ms,
        writing_application=writing_application,
        x264_library=x264_library,
        x264_name=x264_name,
        x264_version=x264_version,
        x264_settings=x264_settings,
        tracks=tracks,
    )
