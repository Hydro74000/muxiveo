from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "patch_hevc_trail_n.py"
    spec = importlib.util.spec_from_file_location("patch_hevc_trail_n", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_patch_annexb_trail_n_to_trail_r_flips_only_type_zero_nals():
    module = _load_module()
    source = (
        b"\x00\x00\x00\x01" + bytes([0x00, 0x01, 0xAA, 0xBB]) +
        b"\x00\x00\x01" + bytes([0x00, 0x01, 0xCC]) +
        b"\x00\x00\x01" + bytes([0x28, 0x01, 0xDD])
    )

    patched, stats = module.patch_annexb_trail_n_to_trail_r(source)

    headers: list[int] = []
    i = 0
    while i < len(patched):
        if patched[i:i + 4] == b"\x00\x00\x00\x01":
            headers.append(i + 4)
            i += 4
            continue
        if patched[i:i + 3] == b"\x00\x00\x01":
            headers.append(i + 3)
            i += 3
            continue
        i += 1

    assert [((patched[idx] >> 1) & 0x3F) for idx in headers] == [1, 1, 20]
    assert stats.trail_n_to_r == 2
    assert stats.total_nals == 3


def test_count_annexb_trail_types_counts_trail_n_and_trail_r(tmp_path):
    module = _load_module()
    data = (
        b"\x00\x00\x00\x01" + bytes([0x00, 0x01, 0xAA]) +  # TRAIL_N
        b"\x00\x00\x01" + bytes([0x02, 0x01, 0xBB]) +      # TRAIL_R
        b"\x00\x00\x01" + bytes([0x28, 0x01, 0xCC])        # IDR
    )
    sample = tmp_path / "sample.hevc"
    sample.write_bytes(data)

    counts = module.count_annexb_trail_types(sample)

    assert counts.trail_n == 1
    assert counts.trail_r == 1
    assert counts.total_nals == 3
