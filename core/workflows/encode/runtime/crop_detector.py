"""
Detection and alignment of black bars (crop) across video codecs and workflows.

Supports:
1. Fast Dolby Vision Level 5 (Active Area) RPU probing when available.
2. FFmpeg multi-sample cropdetect filter as robust fallback for SDR/HDR10/DV.
3. Codec-aware geometry alignment:
   - Multiple of 32 for NVEncC + Dolby Vision (CTU hardware alignment).
   - Multiple of 2 (even dimensions/offsets) for standard codecs (libx265, svt-av1, etc.).
"""

from __future__ import annotations

import collections
import re
import subprocess
from pathlib import Path

from core.dovi_profile_detector import DoviProfileDetector
from core.subprocess_utils import subprocess_text_kwargs


_CROP_RE = re.compile(r"\bcrop=(\d+):(\d+):(\d+):(\d+)")


def detect_black_bars_ffmpeg(
    source_path: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    duration_s: float | None = None,
    dimensions: tuple[int, int] | None = None,
    limit: int = 64,
) -> tuple[int, int, int, int]:
    """Détecte les bandes noires via le filtre cropdetect de ffmpeg sur plusieurs échantillons.

    Retourne:
        tuple[int, int, int, int]: (top, bottom, left, right) en pixels.
    """
    if dimensions is None:
        src_w, src_h = 0, 0
    else:
        src_w, src_h = dimensions

    if duration_s and duration_s > 180:
        sample_times = [
            duration_s * 0.20,
            duration_s * 0.40,
            duration_s * 0.60,
            duration_s * 0.80,
        ]
    elif duration_s and duration_s > 30:
        sample_times = [duration_s * 0.35, duration_s * 0.65]
    else:
        sample_times = [600.0, 1800.0, 3600.0]

    detected_crops: list[tuple[int, int, int, int]] = []

    for ts in sample_times:
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-nostdin",
            "-ss",
            f"{ts:.1f}",
            "-i",
            str(source_path),
            "-vframes",
            "5",
            "-vf",
            f"cropdetect=limit={limit}:round=2:reset=0",
            "-an",
            "-f",
            "null",
            "-",
        ]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=15,
                **subprocess_text_kwargs(),
            )
            output = (res.stderr or "") + (res.stdout or "")
            matches = list(_CROP_RE.finditer(output))
            if not matches:
                continue
            # Prendre le dernier crop stable de l'échantillon
            m = matches[-1]
            w, h, x, y = map(int, m.groups())
            # Filtrer les détections anormales (logos isolés ou écrans noirs complets < 50% de l'image)
            if src_w > 0 and src_h > 0:
                if w < (src_w * 0.5) or h < (src_h * 0.5):
                    continue
                top = max(0, y)
                bottom = max(0, src_h - h - y)
                left = max(0, x)
                right = max(0, src_w - w - x)
                detected_crops.append((top, bottom, left, right))
            else:
                detected_crops.append((y, 0, x, 0))
        except (subprocess.TimeoutExpired, OSError):
            continue

    if not detected_crops:
        return (0, 0, 0, 0)

    # Prendre le mode (valeur la plus fréquente) ou la médiane
    counts = collections.Counter(detected_crops)
    most_common, count = counts.most_common(1)[0]
    return most_common


def detect_video_crop(
    source_path: Path,
    *,
    dimensions: tuple[int, int],
    codec: str = "libx265",
    copy_dv: bool = False,
    is_dovi: bool = False,
    duration_s: float | None = None,
    ffmpeg_bin: str = "ffmpeg",
    dovi_tool_bin: str = "dovi_tool",
) -> tuple[int, int, int, int]:
    """Détecte les bandes noires et renvoie le crop (top, bottom, left, right) aligné pour le codec.

    1. Si Dolby Vision présent : probe RPU Level 5 en premier (mastering exact en ~1s).
    2. Fallback ffmpeg cropdetect multi-points avec seuil adapté (64 pour HDR 10-bit / master dither).
    3. Alignement sur multiple de 32 (si NVEncC + DV) ou multiple de 2 (standard).
    """
    raw_crop: tuple[int, int, int, int] | None = None

    # Toujours tenter le probing L5 si la source peut être Dolby Vision
    try:
        detector = DoviProfileDetector(dovi_tool_bin=dovi_tool_bin)
        l5 = detector.probe_l5_offsets(source_path)
        if l5 and any(v > 0 for v in l5):
            raw_crop = (l5[0], l5[1], l5[2], l5[3])  # (top, bottom, left, right)
    except Exception:
        raw_crop = None

    if raw_crop is None or (raw_crop[0] == 0 and raw_crop[1] == 0 and raw_crop[2] == 0 and raw_crop[3] == 0):
        # Fallback ffmpeg cropdetect
        raw_crop = detect_black_bars_ffmpeg(
            source_path,
            ffmpeg_bin=ffmpeg_bin,
            duration_s=duration_s,
            dimensions=dimensions,
            limit=64,
        )

    return align_crop_for_codec(
        raw_crop,
        dimensions=dimensions,
        codec=codec,
        copy_dv=copy_dv,
    )


def align_crop_for_codec(
    raw_crop: tuple[int, int, int, int],  # (top, bottom, left, right)
    dimensions: tuple[int, int],
    codec: str,
    copy_dv: bool = False,
) -> tuple[int, int, int, int]:
    """Aligne un recadrage brut selon le codec et l'éventuelle contrainte Dolby Vision."""
    src_w, src_h = dimensions
    if src_w <= 0 or src_h <= 0:
        return (0, 0, 0, 0)

    top, bottom, left, right = raw_crop
    if top == 0 and bottom == 0 and left == 0 and right == 0:
        return (0, 0, 0, 0)

    is_nvencc_dv = (codec == "nvencc_hevc" and copy_dv)
    mult = 32 if is_nvencc_dv else 2

    act_w = src_w - (left + right)
    act_h = src_h - (top + bottom)
    if act_w <= 0 or act_h <= 0:
        return (0, 0, 0, 0)

    aligned_w = (act_w // mult) * mult
    aligned_h = (act_h // mult) * mult
    diff_w = act_w - aligned_w
    diff_h = act_h - aligned_h

    final_left = left + diff_w // 2
    final_right = right + (diff_w - diff_w // 2)
    final_top = top + diff_h // 2
    final_bottom = bottom + (diff_h - diff_h // 2)

    # Pour tous les codecs, s'assurer que les offsets individuels sont pairs pour YUV420p
    if not is_nvencc_dv:
        if final_left % 2 != 0:
            final_left += 1
        if final_right % 2 != 0:
            final_right += 1
        if final_top % 2 != 0:
            final_top += 1
        if final_bottom % 2 != 0:
            final_bottom += 1
    else:
        # Pour NVEncC + DV, la hauteur résultante DOIT être exactement un multiple de 32.
        # Si un offset est impair, ajuster la répartition sans changer la somme totale:
        if final_top % 2 != 0 and final_bottom > 0:
            final_top += 1
            final_bottom -= 1
        elif final_bottom % 2 != 0 and final_top > 0:
            final_bottom += 1
            final_top -= 1

        if final_left % 2 != 0 and final_right > 0:
            final_left += 1
            final_right -= 1
        elif final_right % 2 != 0 and final_left > 0:
            final_right += 1
            final_left -= 1

    return (final_top, final_bottom, final_left, final_right)
