"""NVEncC Dolby Vision geometry without resampling the base layer.

Crop/pad offsets are expressed in source pixels. Full RPU editing translates
Level 5 per scene and preserves the remaining metadata and frame ordering.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from core.bluray import append_ffmpeg_input_args
from core.subprocess_utils import subprocess_text_kwargs

from core.workflows.encode.models import EncodeError, VideoCropSettings, VideoEncodeSettings, VideoResizeSettings
from core.workflows.encode.domain.codecs import resolve_resize_dimensions
from core.workflows.encode.runtime.nvencc_routing import nvencc_crop_offsets_from_extra_params
from core.workflows.encode.runtime.crop_detector import align_crop_for_codec


@dataclass(frozen=True)
class NvenccDoviGeometryResult:
    video: VideoEncodeSettings
    vpp_pad: tuple[int, int, int, int] | None = None  # (left, top, right, bottom)
    dovi_rpu_prm: str | None = None  # External RPU edits must not be zeroed by crop=true.
    needs_rpu_alignment: bool = False
    pad_offsets: tuple[int, int, int, int] | None = None  # (left, top, right, bottom)
    crop_offsets: tuple[int, int, int, int] | None = None  # (left, top, right, bottom)


def absolute_dovi_crop(video: VideoEncodeSettings, dimensions: tuple[int, int]) -> VideoCropSettings:
    crop = video.crop
    extra = nvencc_crop_offsets_from_extra_params(video.extra_params)
    if extra is not None and any(extra):
        return VideoCropSettings(enabled=True, left=extra[0], top=extra[1], right=extra[2], bottom=extra[3])
    if not crop.is_active() or crop.auto:
        return VideoCropSettings()
    if crop.unit != "percent":
        return replace(crop)
    width, height = dimensions
    return VideoCropSettings(
        enabled=True, left=width * max(0, crop.left) // 100,
        right=width * max(0, crop.right) // 100,
        top=height * max(0, crop.top) // 100, bottom=height * max(0, crop.bottom) // 100,
    )


def nvencc_dovi_resize_changes_scale(video: VideoEncodeSettings, dimensions: tuple[int, int]) -> bool:
    """Application policy: resampling and Dolby Vision copy are mutually exclusive."""
    resize = video.resize
    if not resize.is_active():
        return False
    if resize.mode == "percent":
        pct = max(1, int(resize.percent or 100))
        return min(pct, 100) != 100 if not resize.allow_upscale else pct != 100
    if min(dimensions) <= 0:
        return True
    crop = absolute_dovi_crop(video, dimensions)
    cropped = (dimensions[0] - crop.left - crop.right, dimensions[1] - crop.top - crop.bottom)
    # Dimensions effectives (ratio conservé, pas d'agrandissement) : un preset
    # égal à l'image recadrée ne rééchantillonne pas.
    return resolve_resize_dimensions(cropped[0], cropped[1], resize) != cropped


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

    if nvencc_dovi_resize_changes_scale(video, dimensions):
        return NvenccDoviGeometryResult(video=replace(video, copy_dv=False, inject_hdr_meta=True))
    # A no-op resize must not turn into scaling after alignment changes the canvas.
    video = replace(video, resize=VideoResizeSettings())
    if src_w % 2 or src_h % 2:
        raise EncodeError("Dolby Vision NVEncC : dimensions source paires requises pour l'alignement YUV420.")
    crop = absolute_dovi_crop(video, dimensions)
    u_left, u_top, u_right, u_bottom = crop.left, crop.top, crop.right, crop.bottom
    has_user_crop = crop.is_active()

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
            raise EncodeError("Dolby Vision : le recadrage dépasse les dimensions source.")

        final_top, final_bottom, final_left, final_right = align_crop_for_codec(
            (b_top, b_bottom, b_left, b_right), dimensions, video.codec, copy_dv=True,
        )
        if not any((final_top, final_bottom, final_left, final_right)):
            raise EncodeError("Dolby Vision : recadrage trop important pour la résolution source.")

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
            needs_rpu_alignment=True,
            crop_offsets=(final_left, final_top, final_right, final_bottom),
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
    pad_left = (pad_w // 4) * 2
    pad_right = pad_w - pad_left
    pad_top = (pad_h // 4) * 2
    pad_bottom = pad_h - pad_top
    vpp_pad = (pad_left, pad_top, pad_right, pad_bottom)

    cleared_crop = VideoCropSettings(enabled=False)
    return NvenccDoviGeometryResult(
        video=replace(video, crop=cleared_crop),
        vpp_pad=vpp_pad,
        dovi_rpu_prm=None,
        needs_rpu_alignment=True,
        pad_offsets=vpp_pad,
    )


def _probe_rpu_frame_count(
    dovi_tool_bin: str,
    rpu_path: Path,
    run_cmd: Callable[[list[str]], object] | None = None,
) -> int | None:
    cmd = [dovi_tool_bin, "info", "-i", str(rpu_path), "--summary"]
    text = ""
    if run_cmd is not None:
        try:
            out = run_cmd(cmd)
            text = str(out or "")
        except Exception:
            text = ""
    if not text:
        try:
            # Outil local configuré et arguments séparés, sans interprétation shell.
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            res = subprocess.run(cmd, check=True, capture_output=True, **subprocess_text_kwargs())  # nosec B603
            text = (res.stdout or "") + (res.stderr or "")
        except Exception:
            text = ""
    m = re.search(r"Frames\s*:\s*(\d+)", text)
    if m:
        return int(m.group(1))
    return None


def extract_dovi_rpu(
    *, source: Path, stream_index: int, ffmpeg_bin: str, dovi_tool_bin: str,
    output_rpu: Path, work_dir: Path, run_cmd: Callable[[list[str]], object],
    cleanup_paths: list[Path],
) -> Path:
    """Follow the standard metadata workflow: container -> Annex B -> RPU.

    dovi_tool accepts MKV for extraction only (first video on older versions).
    Other containers and explicitly selected streams go through FFmpeg first.
    """
    meta_input = source
    if source.suffix.lower() not in {".mkv", ".hevc", ".h265", ".265", ".x265"} or stream_index != 0:
        meta_input = work_dir / "source_meta.hevc"
        cleanup_paths.append(meta_input)
        cmd = [ffmpeg_bin, "-nostdin", "-y"]
        append_ffmpeg_input_args(cmd, source)
        cmd.extend(["-map", f"0:{stream_index}", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                    "-f", "hevc", str(meta_input)])
        run_cmd(cmd)
    output_rpu.unlink(missing_ok=True)
    run_cmd([dovi_tool_bin, "extract-rpu", "-i", str(meta_input), "-o", str(output_rpu)])
    if meta_input != source:
        meta_input.unlink(missing_ok=True)
    if output_rpu.is_file() and output_rpu.stat().st_size == 0:
        raise EncodeError(f"Dolby Vision : l'extraction du RPU depuis '{source.name}' a produit un fichier vide.")
    return output_rpu


def align_dovi_rpu_geometry(
    *, dovi_tool_bin: str, rpu_input: Path, output_rpu: Path,
    pad_offsets: tuple[int, int, int, int] = (0, 0, 0, 0),
    crop_offsets: tuple[int, int, int, int] = (0, 0, 0, 0),
    work_dir: Path, run_cmd: Callable[[list[str]], object] | None = None,
) -> Path:
    """Translate each existing L5 preset, retaining its exact frame ranges.

    Padding adds borders; cropping removes them (saturating at the picture
    edge). Never use active_area.crop/all to flatten scene-dependent offsets.
    Mode 0 preserves mapping, trims and the original RPU profile.
    """
    def run(cmd: list[str]) -> object:
        if run_cmd is not None:
            return run_cmd(cmd)
        # Commandes dovi_tool construites ci-dessous en argv, sans shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        return subprocess.run(cmd, check=True, capture_output=True, **subprocess_text_kwargs())  # nosec B603

    if rpu_input.is_file() and rpu_input.stat().st_size == 0:
        raise EncodeError(f"Dolby Vision : fichier RPU vide ({rpu_input.name}).")

    if crop_offsets == (0, 0, 0, 0) and pad_offsets == (0, 0, 0, 0):
        if rpu_input != output_rpu and rpu_input.is_file():
            shutil.copyfile(rpu_input, output_rpu)
        return output_rpu

    json_path = work_dir / "dovi_geometry_edit.json"
    json_path.unlink(missing_ok=True)
    output_rpu.unlink(missing_ok=True)

    run([dovi_tool_bin, "export", "-i", str(rpu_input), "-d", f"level5={json_path}"])
    if not json_path.is_file():
        raise EncodeError("Dolby Vision : l'export des métadonnées L5 a échoué.")

    raw_data = json.loads(json_path.read_text(encoding="utf-8"))
    active_area = raw_data.get("active_area") if isinstance(raw_data.get("active_area"), dict) else raw_data
    presets = active_area.get("presets") if isinstance(active_area, dict) else None
    edits = active_area.get("edits") if isinstance(active_area, dict) else None

    if not presets or not edits:
        frame_count = _probe_rpu_frame_count(dovi_tool_bin, rpu_input, run_cmd=run)
        if frame_count and frame_count > 0:
            if not presets:
                # Aucune zone active L5 dans le RPU source (cadre plein par défaut)
                presets = [{"id": 0, "left": 0, "top": 0, "right": 0, "bottom": 0}]
                edits = {f"0-{frame_count - 1}": 0}
            elif isinstance(presets, list) and len(presets) == 1 and not edits:
                # 1 preset sans table d'édits explicite : s'applique à tout le métrage
                preset_id = int(presets[0].get("id", 0))
                edits = {f"0-{frame_count - 1}": preset_id}

    if not presets or not edits:
        raise EncodeError(f"Dolby Vision : les métadonnées L5 ne permettent pas un réalignement sûr ({raw_data}).")

    for preset in presets:
        for index, edge in enumerate(("left", "top", "right", "bottom")):
            preset[edge] = max(0, int(preset.get(edge, 0)) - crop_offsets[index]) + pad_offsets[index]
    # The export also contains crop=true. Remove it so missing ranges/levels
    # are not implicitly rewritten to zero by the editor.
    edit_cfg = {"mode": 0, "active_area": {"presets": presets, "edits": edits}}
    json_path.write_text(json.dumps(edit_cfg), encoding="utf-8")
    run([dovi_tool_bin, "editor", "-i", str(rpu_input), "-j", str(json_path), "-o", str(output_rpu)])
    if output_rpu.is_file() and output_rpu.stat().st_size == 0:
        raise EncodeError("Dolby Vision : le RPU réaligné produit est vide.")
    return output_rpu
