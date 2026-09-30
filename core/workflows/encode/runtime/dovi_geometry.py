"""Dolby Vision geometry alignment for NVEncC hardware CTU constraints.

Aligns video dimensions to multiples of 32 to prevent NVENC hardware
padding (which causes conformance window mismatches breaking Dolby
Vision playback on TVs / Android TV SoC hardware compositors).

Two operational modes:
1. Active area with black bars (or user crop):
   Adjust crop to the nearest multiple of 32 (rounding down active picture,
   cropping minimal extra border to remove black bar residue).
   Sets ``--dolby-vision-rpu-prm crop=true`` so libdovi zeroes out L5 active area.
2. Fullframe image on edges (no black bars, RPU padding at 0):
   Pad to the next multiple of 32 with ``--vpp-pad <left>,<top>,<right>,<bottom>``.
   Realigns RPU metadata via ``dovi_tool editor`` so Level 5 active area matches
   the added padding without cropping active content.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from core.workflows.encode.models import VideoCropSettings, VideoEncodeSettings
from core.workflows.encode.runtime.nvencc_routing import nvencc_crop_offsets_from_extra_params


@dataclass(frozen=True)
class NvenccDoviGeometryResult:
    video: VideoEncodeSettings
    vpp_pad: tuple[int, int, int, int] | None = None  # (left, top, right, bottom)
    dovi_rpu_prm: str | None = None  # "crop=true" when cropped
    needs_rpu_pad_alignment: bool = False
    pad_offsets: tuple[int, int, int, int] | None = None  # (left, top, right, bottom)


def align_nvencc_dovi_geometry(
    video: VideoEncodeSettings,
    dimensions: tuple[int, int],
    l5_offsets: tuple[int, int, int, int] | None = None,
) -> NvenccDoviGeometryResult:
    """Calcule et aligne la géométrie Dolby Vision sur un multiple de 32 pour NVEncC."""
    src_w, src_h = dimensions
    if (
        src_w <= 0
        or src_h <= 0
        or not getattr(video, "copy_dv", False)
        or video.codec != "nvencc_hevc"
    ):
        return NvenccDoviGeometryResult(video=video)

    # 1. Vérification du crop utilisateur (widget UI ou extra_params)
    crop_obj = getattr(video, "crop", None)
    has_user_crop = False
    u_left = u_top = u_right = u_bottom = 0
    if crop_obj is not None and getattr(crop_obj, "is_active", lambda: False)():
        if not getattr(crop_obj, "auto", False) and getattr(crop_obj, "unit", "") != "percent":
            u_left = max(0, int(getattr(crop_obj, "left", 0) or 0))
            u_top = max(0, int(getattr(crop_obj, "top", 0) or 0))
            u_right = max(0, int(getattr(crop_obj, "right", 0) or 0))
            u_bottom = max(0, int(getattr(crop_obj, "bottom", 0) or 0))
            if any(c != 0 for c in (u_left, u_top, u_right, u_bottom)):
                has_user_crop = True

    extra_crop = nvencc_crop_offsets_from_extra_params(getattr(video, "extra_params", ""))
    if extra_crop is not None and any(c != 0 for c in extra_crop):
        has_user_crop = True
        u_left, u_top, u_right, u_bottom = extra_crop

    # 2. Vérification des offsets L5 RPU : (top, bottom, left, right)
    rpu_top = rpu_bottom = rpu_left = rpu_right = 0
    has_rpu_padding = False
    if l5_offsets:
        rpu_top, rpu_bottom, rpu_left, rpu_right = l5_offsets
        if any(v > 0 for v in l5_offsets):
            has_rpu_padding = True

    # 3. DÉCISION : BRANCHE 1 (CROP) vs BRANCHE 2 (PAD)
    if has_user_crop or has_rpu_padding:
        b_left = u_left if has_user_crop else rpu_left
        b_top = u_top if has_user_crop else rpu_top
        b_right = u_right if has_user_crop else rpu_right
        b_bottom = u_bottom if has_user_crop else rpu_bottom

        act_w = src_w - (b_left + b_right)
        act_h = src_h - (b_top + b_bottom)
        if act_w <= 0 or act_h <= 0:
            return NvenccDoviGeometryResult(video=video)

        aligned_w = (act_w // 32) * 32
        aligned_h = (act_h // 32) * 32
        diff_w = act_w - aligned_w
        diff_h = act_h - aligned_h

        final_left = b_left + diff_w // 2
        final_right = b_right + (diff_w - diff_w // 2)
        final_top = b_top + diff_h // 2
        final_bottom = b_bottom + (diff_h - diff_h // 2)

        new_crop = VideoCropSettings(
            enabled=True,
            left=final_left,
            top=final_top,
            right=final_right,
            bottom=final_bottom,
        )
        return NvenccDoviGeometryResult(
            video=replace(video, crop=new_crop),
            vpp_pad=None,
            dovi_rpu_prm="crop=true",
            needs_rpu_pad_alignment=False,
            pad_offsets=None,
        )

    # BRANCHE 2 (PAD) : Image bord à bord sans bandes (padding RPU à 0)
    needs_w_pad = (src_w % 32 != 0)
    needs_h_pad = (src_h % 32 != 0)
    if not needs_w_pad and not needs_h_pad:
        # Résolution déjà multiple de 32 (ex. Lanterns 3840x1920)
        return NvenccDoviGeometryResult(video=video)

    target_w = ((src_w + 31) // 32) * 32
    target_h = ((src_h + 31) // 32) * 32
    pad_w = target_w - src_w
    pad_h = target_h - src_h
    pad_left = pad_w // 2
    pad_right = pad_w - pad_left
    pad_top = pad_h // 2
    pad_bottom = pad_h - pad_top
    vpp_pad = (pad_left, pad_top, pad_right, pad_bottom)

    cleared_crop = VideoCropSettings(enabled=False)
    return NvenccDoviGeometryResult(
        video=replace(video, crop=cleared_crop),
        vpp_pad=vpp_pad,
        dovi_rpu_prm=None,
        needs_rpu_pad_alignment=True,
        pad_offsets=vpp_pad,
    )


def align_dovi_rpu_padding(
    *,
    dovi_tool_bin: str,
    rpu_input: Path,
    output_rpu: Path,
    pad_offsets: tuple[int, int, int, int],  # (left, top, right, bottom)
    work_dir: Path,
) -> Path:
    """Édite le fichier RPU Dolby Vision pour réaligner l'aire active (Level 5) selon le padding."""
    edit_cfg = {
        "mode": 0,
        "active_area": {
            "presets": [
                {
                    "id": 0,
                    "left": pad_offsets[0],
                    "top": pad_offsets[1],
                    "right": pad_offsets[2],
                    "bottom": pad_offsets[3],
                }
            ],
            "edits": {
                "all": 0,
            },
        },
    }
    json_path = work_dir / "dovi_pad_edit.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(edit_cfg, f)

    subprocess.run(
        [
            dovi_tool_bin,
            "editor",
            "-i",
            str(rpu_input),
            "-j",
            str(json_path),
            "-o",
            str(output_rpu),
        ],
        check=True,
        capture_output=True,
    )
    return output_rpu
