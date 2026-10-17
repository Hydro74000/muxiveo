from __future__ import annotations

import mmap
from dataclasses import dataclass
from pathlib import Path

from core.h26x_sei import (
    remove_emulation_prevention_bytes as _remove_emulation_prevention_bytes,
    iter_sei_messages as _iter_sei_messages,
    build_sei_rbsp as _build_sei_rbsp,
)


_SEI_PREFIX_NAL_TYPES = {39, 40}
_SEI_PIC_TIMING_PAYLOAD_TYPE = 1


@dataclass(frozen=True)
class SeiStripStats:
    total_nals: int
    sei_nals_seen: int
    pic_timing_messages_removed: int
    sei_nals_dropped: int
    sei_nals_rewritten: int


def _find_start_code(mm: mmap.mmap, offset: int, size: int) -> tuple[int, int] | None:
    pos = mm.find(b"\x00\x00\x01", offset)
    while pos != -1:
        if pos > 0 and mm[pos - 1] == 0x00:
            return pos - 1, 4
        return pos, 3
    return None


def strip_pic_timing_from_sei_nal(nal_payload: bytes) -> tuple[bytes | None, int]:
    if len(nal_payload) <= 2:
        return nal_payload, 0
    nal_header = nal_payload[:2]
    rbsp = _remove_emulation_prevention_bytes(nal_payload[2:])
    messages = _iter_sei_messages(rbsp)
    if not messages:
        return nal_payload, 0
    filtered = [(payload_type, payload) for payload_type, payload in messages if payload_type != _SEI_PIC_TIMING_PAYLOAD_TYPE]
    removed = len(messages) - len(filtered)
    if removed == 0:
        return nal_payload, 0
    if not filtered:
        return None, removed
    rebuilt = nal_header + _build_sei_rbsp(filtered)
    return rebuilt, removed


def strip_pic_timing_from_annexb_file(source: Path, dest: Path) -> SeiStripStats:
    total_nals = 0
    sei_nals_seen = 0
    pic_timing_messages_removed = 0
    sei_nals_dropped = 0
    sei_nals_rewritten = 0

    with source.open("rb") as src_fh, dest.open("wb") as dst_fh:
        with mmap.mmap(src_fh.fileno(), length=0, access=mmap.ACCESS_READ) as mm:
            size = len(mm)
            pos = 0
            write_from = 0
            while True:
                current = _find_start_code(mm, pos, size)
                if current is None:
                    break
                current_pos, current_sc_len = current
                header_idx = current_pos + current_sc_len
                if header_idx >= size:
                    break
                next_start = _find_start_code(mm, header_idx, size)
                next_pos = next_start[0] if next_start is not None else size
                nal_payload = bytes(mm[header_idx:next_pos])
                if not nal_payload:
                    break

                nal_type = (nal_payload[0] >> 1) & 0x3F
                total_nals += 1
                if nal_type in _SEI_PREFIX_NAL_TYPES:
                    sei_nals_seen += 1
                    rewritten, removed = strip_pic_timing_from_sei_nal(nal_payload)
                    if removed > 0:
                        if write_from < current_pos:
                            dst_fh.write(mm[write_from:current_pos])
                        pic_timing_messages_removed += removed
                        if rewritten is None:
                            sei_nals_dropped += 1
                        else:
                            dst_fh.write(mm[current_pos:header_idx])
                            dst_fh.write(rewritten)
                            sei_nals_rewritten += 1
                        write_from = next_pos

                pos = next_pos

            if write_from < size:
                dst_fh.write(mm[write_from:size])

    return SeiStripStats(
        total_nals=total_nals,
        sei_nals_seen=sei_nals_seen,
        pic_timing_messages_removed=pic_timing_messages_removed,
        sei_nals_dropped=sei_nals_dropped,
        sei_nals_rewritten=sei_nals_rewritten,
    )
