"""Injection de metadonnees HDR statiques dans un flux HEVC annexB."""

from __future__ import annotations

import mmap
import re
import shutil
import struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path


_START_CODE_4 = b"\x00\x00\x00\x01"
_START_CODE_3 = b"\x00\x00\x01"

_VCL_RANGE = range(0, 32)
_IRAP_TYPES = frozenset({16, 17, 18, 19, 20, 21})
_PREFIX_BOUNDARY_TYPES = frozenset({32, 33, 34, 35, 39, 62, 63})
_PREFIX_SEI_NAL_TYPE = 39

_MASTERING_DISPLAY_PAYLOAD_TYPE = 137
_CONTENT_LIGHT_LEVEL_PAYLOAD_TYPE = 144

_MASTER_DISPLAY_RE = re.compile(
    r"^G\((\d+),(\d+)\)"
    r"B\((\d+),(\d+)\)"
    r"R\((\d+),(\d+)\)"
    r"WP\((\d+),(\d+)\)"
    r"L\((\d+),(\d+)\)$"
)
_MAX_CLL_RE = re.compile(r"^(\d+),(\d+)$")


@dataclass(frozen=True)
class StaticHdrSeiInjectionResult:
    access_units: int
    targeted_access_units: int
    injected_access_units: int
    preserved_access_units: int

    @property
    def applied(self) -> bool:
        return self.injected_access_units > 0


@dataclass(frozen=True)
class _NalRef:
    payload_offset: int
    end_offset: int
    nal_type: int
    first_slice_in_pic: bool


def inject_static_hdr_sei(
    stream: bytes,
    *,
    master_display: str = "",
    max_cll: str = "",
) -> tuple[bytes, StaticHdrSeiInjectionResult]:
    """Injecte les SEI HDR statiques dans un buffer HEVC annexB."""
    sei_nal, requested_types = _build_static_hdr_sei_nal(
        master_display=master_display,
        max_cll=max_cll,
    )
    writer = BytesIO()
    result = _rewrite_stream(
        stream,
        writer,
        sei_nal=sei_nal,
        requested_types=requested_types,
    )
    if not result.applied:
        return stream, result
    return writer.getvalue(), result


def inject_static_hdr_sei_file(
    input_path: Path,
    output_path: Path,
    *,
    master_display: str = "",
    max_cll: str = "",
) -> StaticHdrSeiInjectionResult:
    """Injecte les SEI HDR statiques dans un fichier HEVC annexB."""
    sei_nal, requested_types = _build_static_hdr_sei_nal(
        master_display=master_display,
        max_cll=max_cll,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    file_size = input_path.stat().st_size
    if file_size == 0:
        shutil.copyfile(input_path, output_path)
        return StaticHdrSeiInjectionResult(0, 0, 0, 0)

    with input_path.open("rb") as src, output_path.open("wb") as dst:
        with mmap.mmap(src.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            result = _rewrite_stream(
                mm,
                dst,
                sei_nal=sei_nal,
                requested_types=requested_types,
            )

    if not result.applied:
        shutil.copyfile(input_path, output_path)
    return result


def _rewrite_stream(
    data: bytes | mmap.mmap,
    writer,
    *,
    sei_nal: bytes,
    requested_types: frozenset[int],
) -> StaticHdrSeiInjectionResult:
    current_au: list[_NalRef] = []
    access_units = 0
    targeted_access_units = 0
    injected_access_units = 0
    preserved_access_units = 0
    has_slice = False
    current_is_keyframe = False

    def _flush() -> None:
        nonlocal access_units, targeted_access_units, injected_access_units
        nonlocal preserved_access_units, has_slice, current_is_keyframe, current_au
        if not current_au:
            return
        is_first_au = access_units == 0
        is_target = is_first_au or current_is_keyframe
        access_units += 1
        if is_target:
            targeted_access_units += 1
        injected, already_present = _write_access_unit(
            writer,
            data,
            current_au,
            sei_nal=sei_nal,
            requested_types=requested_types,
            inject_into_au=is_target,
        )
        if injected:
            injected_access_units += 1
        elif is_target and already_present:
            preserved_access_units += 1
        current_au = []
        has_slice = False
        current_is_keyframe = False

    for nal in _iter_nal_refs(data):
        is_slice = nal.nal_type in _VCL_RANGE
        if is_slice and nal.first_slice_in_pic and has_slice:
            _flush()
        if not is_slice and has_slice and nal.nal_type in _PREFIX_BOUNDARY_TYPES:
            _flush()

        current_au.append(nal)
        if is_slice:
            has_slice = True
            if nal.nal_type in _IRAP_TYPES:
                current_is_keyframe = True

    _flush()
    return StaticHdrSeiInjectionResult(
        access_units=access_units,
        targeted_access_units=targeted_access_units,
        injected_access_units=injected_access_units,
        preserved_access_units=preserved_access_units,
    )


def _write_access_unit(
    writer,
    data: bytes | mmap.mmap,
    nal_refs: list[_NalRef],
    *,
    sei_nal: bytes,
    requested_types: frozenset[int],
    inject_into_au: bool,
) -> tuple[bool, bool]:
    present_types = _collect_prefix_sei_payload_types(data, nal_refs)
    missing_types = [ptype for ptype in requested_types if ptype not in present_types]
    should_inject = inject_into_au and bool(missing_types)
    first_slice_index = next(
        (idx for idx, nal in enumerate(nal_refs) if nal.nal_type in _VCL_RANGE),
        None,
    )

    if should_inject and first_slice_index is None:
        writer.write(_START_CODE_4)
        writer.write(sei_nal)

    for index, nal in enumerate(nal_refs):
        if should_inject and first_slice_index is not None and index == first_slice_index:
            writer.write(_START_CODE_4)
            writer.write(sei_nal)
        writer.write(_START_CODE_4)
        writer.write(data[nal.payload_offset:nal.end_offset])

    already_present = requested_types.issubset(present_types)
    return should_inject, already_present


def _collect_prefix_sei_payload_types(
    data: bytes | mmap.mmap,
    nal_refs: list[_NalRef],
) -> set[int]:
    payload_types: set[int] = set()
    for nal in nal_refs:
        if nal.nal_type != _PREFIX_SEI_NAL_TYPE:
            continue
        payload_types.update(
            _parse_prefix_sei_payload_types(data[nal.payload_offset:nal.end_offset])
        )
    return payload_types


def _iter_nal_refs(data: bytes | mmap.mmap):
    length = len(data)
    start = _find_start_code(data, 0)
    while start is not None:
        start_offset, start_code_len = start
        payload_offset = start_offset + start_code_len
        next_start = _find_start_code(data, payload_offset)
        end_offset = next_start[0] if next_start is not None else length
        if end_offset - payload_offset >= 2:
            header_byte_0 = data[payload_offset]
            nal_type = (header_byte_0 >> 1) & 0x3F
            first_slice = False
            if nal_type in _VCL_RANGE and (end_offset - payload_offset) >= 3:
                first_slice = bool(data[payload_offset + 2] & 0x80)
            yield _NalRef(
                payload_offset=payload_offset,
                end_offset=end_offset,
                nal_type=nal_type,
                first_slice_in_pic=first_slice,
            )
        start = next_start


def _find_start_code(data: bytes | mmap.mmap, offset: int) -> tuple[int, int] | None:
    pos = data.find(_START_CODE_3, offset)
    while pos != -1:
        if pos > 0 and data[pos - 1] == 0x00:
            return pos - 1, 4
        return pos, 3
    return None


def _build_static_hdr_sei_nal(
    *,
    master_display: str,
    max_cll: str,
) -> tuple[bytes, frozenset[int]]:
    messages: list[tuple[int, bytes]] = []

    mastering_payload = _build_mastering_display_payload(master_display)
    if mastering_payload is not None:
        messages.append((_MASTERING_DISPLAY_PAYLOAD_TYPE, mastering_payload))

    cll_payload = _build_content_light_level_payload(max_cll)
    if cll_payload is not None:
        messages.append((_CONTENT_LIGHT_LEVEL_PAYLOAD_TYPE, cll_payload))

    if not messages:
        raise ValueError("Aucune metadonnee HDR statique a injecter.")

    rbsp = bytearray()
    requested_types: list[int] = []
    for payload_type, payload in messages:
        requested_types.append(payload_type)
        _append_payload_type_or_size(rbsp, payload_type)
        _append_payload_type_or_size(rbsp, len(payload))
        rbsp.extend(payload)
    rbsp.append(0x80)

    nal_payload = _escape_rbsp(bytes(rbsp))
    nal_header = bytes([_PREFIX_SEI_NAL_TYPE << 1, 0x01])
    return nal_header + nal_payload, frozenset(requested_types)


def _build_mastering_display_payload(master_display: str) -> bytes | None:
    raw = (master_display or "").strip()
    if not raw:
        return None
    match = _MASTER_DISPLAY_RE.match(raw)
    if match is None:
        raise ValueError(f"Format master_display invalide: {master_display}")
    values = [int(group) for group in match.groups()]
    return struct.pack(
        ">HHHHHHHHII",
        values[0], values[1],
        values[2], values[3],
        values[4], values[5],
        values[6], values[7],
        values[8], values[9],
    )


def _build_content_light_level_payload(max_cll: str) -> bytes | None:
    raw = (max_cll or "").strip()
    if not raw:
        return None
    match = _MAX_CLL_RE.match(raw)
    if match is None:
        raise ValueError(f"Format max_cll invalide: {max_cll}")
    max_content, max_average = (int(group) for group in match.groups())
    return struct.pack(">HH", max_content, max_average)


def _append_payload_type_or_size(buf: bytearray, value: int) -> None:
    remaining = int(value)
    while remaining >= 0xFF:
        buf.append(0xFF)
        remaining -= 0xFF
    buf.append(remaining)


def _escape_rbsp(rbsp: bytes) -> bytes:
    out = bytearray()
    zero_run = 0
    for byte in rbsp:
        if zero_run >= 2 and byte <= 0x03:
            out.append(0x03)
            zero_run = 0
        out.append(byte)
        zero_run = zero_run + 1 if byte == 0x00 else 0
    return bytes(out)


def _parse_prefix_sei_payload_types(nal_payload: bytes) -> set[int]:
    if len(nal_payload) <= 2:
        return set()
    rbsp = _unescape_rbsp(nal_payload[2:])
    payload_types: set[int] = set()
    offset = 0
    while offset < len(rbsp):
        if offset == len(rbsp) - 1 and rbsp[offset] == 0x80:
            break
        payload_type = 0
        while offset < len(rbsp) and rbsp[offset] == 0xFF:
            payload_type += 0xFF
            offset += 1
        if offset >= len(rbsp):
            break
        payload_type += rbsp[offset]
        offset += 1

        payload_size = 0
        while offset < len(rbsp) and rbsp[offset] == 0xFF:
            payload_size += 0xFF
            offset += 1
        if offset >= len(rbsp):
            break
        payload_size += rbsp[offset]
        offset += 1

        if offset + payload_size > len(rbsp):
            break
        payload_types.add(payload_type)
        offset += payload_size
    return payload_types


def _unescape_rbsp(ebsp: bytes) -> bytes:
    out = bytearray()
    zero_run = 0
    index = 0
    while index < len(ebsp):
        byte = ebsp[index]
        if zero_run >= 2 and byte == 0x03:
            zero_run = 0
            index += 1
            continue
        out.append(byte)
        zero_run = zero_run + 1 if byte == 0x00 else 0
        index += 1
    return bytes(out)


__all__ = [
    "StaticHdrSeiInjectionResult",
    "inject_static_hdr_sei",
    "inject_static_hdr_sei_file",
]
