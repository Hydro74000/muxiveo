"""Native H.264 elementary helpers (stdlib-only)."""

from __future__ import annotations

from ...io.bitreader import BitReader


def _remove_h264_emulation_prevention(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        if i + 2 < len(data) and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out.extend((0, 0))
            i += 3
            continue
        out.append(data[i])
        i += 1
    return bytes(out)


def extract_h264_sps_from_avcc(avcc: bytes) -> bytes:
    if len(avcc) <= 7 or avcc[0] != 1:
        return b""
    pos = 5
    if pos >= len(avcc):
        return b""
    num_sps = avcc[pos] & 0x1F
    pos += 1
    for _ in range(num_sps):
        if pos + 2 > len(avcc):
            return b""
        sps_len = int.from_bytes(avcc[pos : pos + 2], "big", signed=False)
        pos += 2
        if pos + sps_len > len(avcc):
            return b""
        sps = avcc[pos : pos + sps_len]
        if sps:
            return sps
        pos += sps_len
    return b""


def parse_h264_sps(sps: bytes) -> dict[str, int]:
    if not sps:
        return {}
    rbsp = _remove_h264_emulation_prevention(sps[1:] if len(sps) > 1 else b"")
    br = BitReader(rbsp)
    out: dict[str, int] = {}
    try:
        profile_idc = br.read_bits(8)
        _ = br.read_bits(8)  # constraint flags + reserved
        _ = br.read_bits(8)  # level_idc
        _ = br.read_ue()  # sps id

        chroma_format_idc = 1
        separate_colour_plane_flag = 0
        if profile_idc in {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}:
            chroma_format_idc = br.read_ue()
            if chroma_format_idc == 3:
                separate_colour_plane_flag = br.read_bit()
            _ = br.read_ue()
            _ = br.read_ue()
            _ = br.read_bit()
            scaling = br.read_bit()
            if scaling:
                max_lists = 8 if chroma_format_idc != 3 else 12
                for i in range(max_lists):
                    if br.read_bit():
                        size = 16 if i < 6 else 64
                        last_scale = 8
                        next_scale = 8
                        for _ in range(size):
                            if next_scale != 0:
                                delta = br.read_se()
                                next_scale = (last_scale + delta + 256) % 256
                            last_scale = next_scale if next_scale != 0 else last_scale

        _ = br.read_ue()  # log2_max_frame_num_minus4
        poc_type = br.read_ue()
        if poc_type == 0:
            _ = br.read_ue()
        elif poc_type == 1:
            _ = br.read_bit()
            _ = br.read_se()
            _ = br.read_se()
            cycle = br.read_ue()
            for _ in range(cycle):
                _ = br.read_se()

        out["ref_frames"] = br.read_ue()
        out["profile_idc"] = profile_idc
        out["chroma_format_idc"] = chroma_format_idc
        _ = br.read_bit()  # gaps flag
        pic_width_mbs_minus1 = br.read_ue()
        pic_height_map_units_minus1 = br.read_ue()
        frame_mbs_only_flag = br.read_bit()
        if not frame_mbs_only_flag:
            _ = br.read_bit()
        _ = br.read_bit()  # direct_8x8_inference_flag
        frame_cropping_flag = br.read_bit()
        crop_left = crop_right = crop_top = crop_bottom = 0
        if frame_cropping_flag:
            crop_left = br.read_ue()
            crop_right = br.read_ue()
            crop_top = br.read_ue()
            crop_bottom = br.read_ue()

        width = (pic_width_mbs_minus1 + 1) * 16
        height = (pic_height_map_units_minus1 + 1) * 16 * (2 - frame_mbs_only_flag)
        if chroma_format_idc == 0:
            sub_width_c = 1
            sub_height_c = 2 - frame_mbs_only_flag
        elif chroma_format_idc == 3:
            sub_width_c = 1
            sub_height_c = 2 - frame_mbs_only_flag if separate_colour_plane_flag == 0 else 1
        else:
            sub_width_c = 2
            sub_height_c = 2 if chroma_format_idc == 1 else (2 - frame_mbs_only_flag)
        crop_unit_x = sub_width_c
        crop_unit_y = sub_height_c
        visible_width = width - (crop_left + crop_right) * crop_unit_x
        visible_height = height - (crop_top + crop_bottom) * crop_unit_y
        out["stored_width"] = width
        out["stored_height"] = height
        out["visible_width"] = max(0, visible_width)
        out["visible_height"] = max(0, visible_height)
    except Exception:
        return out
    return out


def profile_name_from_idc(profile_idc: int) -> str:
    profile_map = {
        66: "Baseline",
        77: "Main",
        88: "Extended",
        100: "High",
        110: "High 10",
        122: "High 4:2:2",
        244: "High 4:4:4",
    }
    return profile_map.get(profile_idc, "")


def parse_avcc(avcc: bytes) -> dict[str, int | str]:
    if len(avcc) <= 5 or avcc[0] != 1:
        return {}
    profile_idc = int(avcc[1])
    level_idc = int(avcc[3])
    out: dict[str, int | str] = {
        "profile_idc": profile_idc,
        "profile": profile_name_from_idc(profile_idc),
        "level": level_idc,
    }
    sps = extract_h264_sps_from_avcc(avcc)
    if sps:
        sps_meta = parse_h264_sps(sps)
        for key in ("ref_frames", "stored_width", "stored_height", "visible_width", "visible_height", "chroma_format_idc"):
            if key in sps_meta:
                out[key] = int(sps_meta[key])
    return out
