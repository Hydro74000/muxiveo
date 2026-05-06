from __future__ import annotations

from pathlib import Path

from core.workflows.encode.runtime.hevc_sei_normalizer import (
    _build_sei_rbsp,
    _iter_sei_messages,
    _remove_emulation_prevention_bytes,
    strip_pic_timing_from_annexb_file,
    strip_pic_timing_from_sei_nal,
)


def _sei_prefix_nal(*messages: tuple[int, bytes]) -> bytes:
    # nal_type 39 (PREFIX_SEI), nuh_layer_id 0, temporal_id_plus1 1
    return bytes([39 << 1, 0x01]) + _build_sei_rbsp(list(messages))


def _annexb(nals: list[bytes]) -> bytes:
    out = bytearray()
    for nal in nals:
        out.extend(b"\x00\x00\x01")
        out.extend(nal)
    return bytes(out)


def _nal_types_from_annexb(data: bytes) -> list[int]:
    types: list[int] = []
    i = 0
    while i < len(data):
        if data[i:i + 3] == b"\x00\x00\x01":
            hdr = i + 3
            if hdr < len(data):
                types.append((data[hdr] >> 1) & 0x3F)
            i = hdr + 1
            continue
        i += 1
    return types


def test_strip_pic_timing_from_sei_nal_removes_only_payload_type_1():
    payload_type_1 = (1, b"\x11\x22\x33")
    payload_type_4 = (4, b"\xB5\x00\x3C\x00\x01\x04")
    nal = _sei_prefix_nal(payload_type_1, payload_type_4)

    rewritten, removed = strip_pic_timing_from_sei_nal(nal)

    assert rewritten is not None
    assert removed == 1
    messages = _iter_sei_messages(_remove_emulation_prevention_bytes(rewritten[2:]))
    assert [payload_type for payload_type, _ in messages] == [4]
    assert messages[0][1] == payload_type_4[1]


def test_strip_pic_timing_from_sei_nal_drops_nal_if_it_only_contains_pic_timing():
    nal = _sei_prefix_nal((1, b"\x11\x22\x33"))

    rewritten, removed = strip_pic_timing_from_sei_nal(nal)

    assert rewritten is None
    assert removed == 1


def test_strip_pic_timing_from_annexb_file_reduces_double_prefix_pattern(tmp_path: Path):
    aud = bytes([35 << 1, 0x01])
    pic_timing = _sei_prefix_nal((1, b"\x11\x22\x33"))
    hdr10plus = _sei_prefix_nal((4, b"\xB5\x00\x3C\x00\x01\x04"))
    trail_n = bytes([0 << 1, 0x01, 0x80])
    unspec62 = bytes([62 << 1, 0x01, 0x80])
    source = tmp_path / "source.hevc"
    dest = tmp_path / "dest.hevc"
    source.write_bytes(_annexb([aud, pic_timing, hdr10plus, trail_n, unspec62]))

    stats = strip_pic_timing_from_annexb_file(source, dest)
    patched = dest.read_bytes()

    assert stats.pic_timing_messages_removed == 1
    assert stats.sei_nals_dropped == 1
    assert _nal_types_from_annexb(patched) == [35, 39, 0, 62]
