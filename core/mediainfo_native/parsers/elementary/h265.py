"""Native H.265/HEVC elementary helpers (stdlib-only)."""

from __future__ import annotations


def _profile_name(profile_idc: int) -> str:
    mapping = {
        1: "Main",
        2: "Main 10",
        3: "Main Still Picture",
        4: "Range Extensions",
    }
    return mapping.get(profile_idc, "")


def _level_string(level_idc: int) -> str:
    if level_idc <= 0:
        return ""
    level = level_idc / 30.0
    if abs(level - round(level)) < 1e-6:
        return str(int(round(level)))
    return f"{level:.1f}".rstrip("0").rstrip(".")


def parse_hvcc(hvcc: bytes) -> dict[str, int | str]:
    # ISO/IEC 14496-15 HEVCDecoderConfigurationRecord
    if len(hvcc) < 19 or hvcc[0] != 1:
        return {}
    profile_idc = hvcc[1] & 0x1F
    tier_flag = (hvcc[1] >> 5) & 0x01
    level_idc = int(hvcc[12])
    chroma_format = hvcc[16] & 0x03
    bit_depth_luma_minus8 = hvcc[17] & 0x07
    bit_depth = 8 + bit_depth_luma_minus8
    pixel_format = ""
    if chroma_format == 1:
        pixel_format = "yuv420p10le" if bit_depth >= 10 else "yuv420p"
    elif chroma_format == 2:
        pixel_format = "yuv422p10le" if bit_depth >= 10 else "yuv422p"
    elif chroma_format == 3:
        pixel_format = "yuv444p10le" if bit_depth >= 10 else "yuv444p"
    return {
        "profile_idc": profile_idc,
        "profile": _profile_name(profile_idc),
        "tier": "High" if tier_flag else "Main",
        "level_idc": level_idc,
        "level": _level_string(level_idc),
        "bit_depth": bit_depth,
        "chroma_format": chroma_format,
        "pixel_format": pixel_format,
    }
