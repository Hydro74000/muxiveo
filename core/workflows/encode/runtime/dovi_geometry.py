"""Géométrie Dolby Vision d'un réencodage, sans rééchantillonner l'image de base.

Les offsets de recadrage sont exprimés en pixels source. Le RPU est réaligné
par scène (L5) ; les autres métadonnées et l'ordre des images sont conservés.

NVENC (FFmpeg ``hevc_nvenc`` et NVEncC) code l'image par blocs de 32 lignes :
une source 3840 × 2160 devient 3840 × 2176 dans le flux HEVC, quelles que
soient les options. Les décodeurs Dolby Vision (téléviseurs, box Android)
rejettent une image codée plus grande que le canevas UHD, que les lignes
ajoutées soient masquées (fenêtre de conformité) ou visibles (padding) ; une
fenêtre de conformité sous ce canevas est acceptée (essais Spider-Verse,
octobre 2026). Un plein cadre est donc rogné au multiple de 32 inférieur.
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
from core.matroska.reader import strict_demuxer_reads_tracks
from core.pipeline_command import PipelineCommand
from core.runner import TaskCancelledError
from core.subprocess_utils import subprocess_text_kwargs

from core.workflows.encode.models import EncodeError, VideoCropSettings, VideoEncodeSettings, VideoResizeSettings
from core.workflows.encode.domain.codecs import resolve_resize_dimensions
from core.workflows.encode.runtime.nvencc_routing import nvencc_crop_offsets_from_extra_params
from core.workflows.encode.catalog import is_nvenc_hevc
from core.workflows.encode.runtime.crop_detector import align_crop_for_codec

#: Bloc de codage NVENC HEVC : hauteur et largeur codées arrondies à ce multiple.
NVENC_CODED_BLOCK = 32
#: Canevas des niveaux Dolby Vision UHD (6 à 9) : image codée maximale acceptée.
DOVI_UHD_CANVAS = (3840, 2160)


@dataclass(frozen=True)
class NvencDoviGeometryResult:
    video: VideoEncodeSettings
    dovi_rpu_prm: str | None = None  # External RPU edits must not be zeroed by crop=true.
    needs_rpu_alignment: bool = False
    crop_offsets: tuple[int, int, int, int] | None = None  # (left, top, right, bottom)
    #: Plein cadre rogné : l'image codée par NVENC dépasserait le canevas UHD.
    full_frame_crop: bool = False
    #: Dimensions codées par NVENC sans recadrage (multiples de 32).
    coded_dimensions: tuple[int, int] | None = None


def absolute_dovi_crop(video: VideoEncodeSettings, dimensions: tuple[int, int]) -> VideoCropSettings:
    crop = video.crop
    extra = nvencc_crop_offsets_from_extra_params(video.extra_params) if video.codec == "nvencc_hevc" else None
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


def nvenc_coded_dimensions(dimensions: tuple[int, int]) -> tuple[int, int]:
    """Dimensions codées par NVENC HEVC (arrondi au bloc de 32 supérieur)."""
    block = NVENC_CODED_BLOCK
    width, height = dimensions
    return (-(-width // block) * block, -(-height // block) * block)


def align_nvenc_dovi_geometry(
    video: VideoEncodeSettings,
    dimensions: tuple[int, int],
    l5_offsets: tuple[int, int, int, int] | None = None,
) -> NvencDoviGeometryResult:
    """Géométrie Dolby Vision NVENC (``hevc_nvenc``, NVEncC) : bandes et plein cadre.

    Bandes noires (L5) ou recadrage utilisateur : recadrage aligné sur 32.
    Plein cadre : inchangé si l'image codée tient dans le canevas UHD (fenêtre
    de conformité acceptée), sinon rognage symétrique au multiple de 32 inférieur.
    """
    src_w, src_h = dimensions
    if (
        src_w <= 0
        or src_h <= 0
        or not getattr(video, "copy_dv", False)
        or not is_nvenc_hevc(video.codec)
    ):
        return NvencDoviGeometryResult(video=video)

    if nvencc_dovi_resize_changes_scale(video, dimensions):
        return NvencDoviGeometryResult(video=replace(video, copy_dv=False, inject_hdr_meta=True))
    # A no-op resize must not turn into scaling after alignment changes the canvas.
    video = replace(video, resize=VideoResizeSettings())
    if src_w % 2 or src_h % 2:
        raise EncodeError("Dolby Vision NVENC : dimensions source paires requises pour l'alignement YUV420.")
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

    # 3. DÉCISION : BANDES / RECADRAGE UTILISATEUR vs PLEIN CADRE
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
        return NvencDoviGeometryResult(
            video=replace(video, crop=new_crop),
            needs_rpu_alignment=True,
            crop_offsets=(final_left, final_top, final_right, final_bottom),
        )

    # PLEIN CADRE : seul le débordement de l'image codée hors du canevas UHD est rogné.
    coded = nvenc_coded_dimensions(dimensions)
    over_w = src_w % NVENC_CODED_BLOCK if coded[0] > DOVI_UHD_CANVAS[0] else 0
    over_h = src_h % NVENC_CODED_BLOCK if coded[1] > DOVI_UHD_CANVAS[1] else 0
    if not (over_w or over_h):
        return NvencDoviGeometryResult(video=video)
    left, top = (over_w // 4) * 2, (over_h // 4) * 2
    offsets = (left, top, over_w - left, over_h - top)
    new_crop = VideoCropSettings(enabled=True, left=offsets[0], top=offsets[1], right=offsets[2], bottom=offsets[3])
    return NvencDoviGeometryResult(
        video=replace(video, crop=new_crop),
        needs_rpu_alignment=True,
        crop_offsets=offsets,
        full_frame_crop=True,
        coded_dimensions=coded,
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
    cleanup_paths: list[Path], mode: str | None = None,
) -> Path:
    """Follow the standard metadata workflow: container -> Annex B -> RPU.

    dovi_tool accepts MKV for extraction only (first video on older versions).
    Other containers and explicitly selected streams go through FFmpeg first.
    ``mode`` : mode RPU de dovi_tool (``"3"`` : P5 converti en P8.1 à l'extraction,
    identique octet pour octet à ``-m 3 convert`` puis extraction).
    """
    mode_args = ["-m", str(mode)] if mode else []
    output_rpu.unlink(missing_ok=True)

    def piped() -> None:
        # Flux Annex B par FFmpeg, en pipe (aucun fichier HEVC intermédiaire).
        decode = [ffmpeg_bin, "-nostdin", "-loglevel", "error"]
        append_ffmpeg_input_args(decode, source)
        decode.extend(["-map", f"0:{stream_index}", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", "-"])
        output_rpu.unlink(missing_ok=True)
        run_cmd(PipelineCommand([dovi_tool_bin, *mode_args, "extract-rpu", "-", "-o", str(output_rpu)], [decode]))

    suffix = source.suffix.lower()
    if suffix == ".mkv" and stream_index == 0 and not strict_demuxer_reads_tracks(source):
        # Lecteur Matroska de dovi_tool (matroska-demuxer) : seul le premier
        # SeekHead est suivi. Second SeekHead non chaîné (RFC 9559 §6.3 non
        # respectée : anciens Muxiveo, autres outils) → « can't find Element: Tracks ».
        piped()
    elif suffix in {".mkv", ".hevc", ".h265", ".265", ".x265"} and stream_index == 0:
        try:
            run_cmd([dovi_tool_bin, *mode_args, "extract-rpu", "-i", str(source), "-o", str(output_rpu)])
        except TaskCancelledError:
            raise
        except Exception:
            if suffix != ".mkv":
                raise
            piped()  # autre limite du lecteur Matroska de dovi_tool
    else:
        meta_input = work_dir / "source_meta.hevc"
        cleanup_paths.append(meta_input)
        cmd = [ffmpeg_bin, "-nostdin", "-y"]
        append_ffmpeg_input_args(cmd, source)
        cmd.extend(["-map", f"0:{stream_index}", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                    "-f", "hevc", str(meta_input)])
        run_cmd(cmd)
        run_cmd([dovi_tool_bin, *mode_args, "extract-rpu", "-i", str(meta_input), "-o", str(output_rpu)])
        meta_input.unlink(missing_ok=True)
    if output_rpu.is_file() and output_rpu.stat().st_size == 0:
        raise EncodeError(f"Dolby Vision : l'extraction du RPU depuis '{source.name}' a produit un fichier vide.")
    return output_rpu


def align_dovi_rpu_geometry(
    *, dovi_tool_bin: str, rpu_input: Path, output_rpu: Path,
    crop_offsets: tuple[int, int, int, int] = (0, 0, 0, 0),
    run_cmd: Callable[[list[str]], object] | None = None,
) -> Path:
    """Translate each existing L5 preset, retaining its exact frame ranges.

    Cropping removes borders (saturating at the picture edge). Never use
    active_area.crop/all to flatten scene-dependent offsets. Mode 0 preserves
    mapping, trims and the original RPU profile.
    """
    def run(cmd: list[str]) -> object:
        if run_cmd is not None:
            return run_cmd(cmd)
        # Commandes dovi_tool construites ci-dessous en argv, sans shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        return subprocess.run(cmd, check=True, capture_output=True, **subprocess_text_kwargs())  # nosec B603

    if rpu_input.is_file() and rpu_input.stat().st_size == 0:
        raise EncodeError(f"Dolby Vision : fichier RPU vide ({rpu_input.name}).")

    if crop_offsets == (0, 0, 0, 0):
        if rpu_input != output_rpu and rpu_input.is_file():
            shutil.copyfile(rpu_input, output_rpu)
        return output_rpu

    # Un JSON par RPU produit : pistes vidéo préparées en parallèle dans le même dossier.
    json_path = dovi_geometry_edit_json(output_rpu)
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
            preset[edge] = max(0, int(preset.get(edge, 0)) - crop_offsets[index])
    # The export also contains crop=true. Remove it so missing ranges/levels
    # are not implicitly rewritten to zero by the editor.
    edit_cfg = {"mode": 0, "active_area": {"presets": presets, "edits": edits}}
    json_path.write_text(json.dumps(edit_cfg), encoding="utf-8")
    run([dovi_tool_bin, "editor", "-i", str(rpu_input), "-j", str(json_path), "-o", str(output_rpu)])
    if output_rpu.is_file() and output_rpu.stat().st_size == 0:
        raise EncodeError("Dolby Vision : le RPU réaligné produit est vide.")
    return output_rpu


def dovi_geometry_edit_json(output_rpu: Path) -> Path:
    """Configuration ``dovi_tool editor`` écrite à côté du RPU réaligné."""
    return output_rpu.with_name(f"{output_rpu.stem}.l5.json")


def resolve_ffmpeg_dovi_geometry(
    video: VideoEncodeSettings,
    dimensions: tuple[int, int],
    l5_offsets: tuple[int, int, int, int] | None = None,
) -> VideoEncodeSettings:
    """Géométrie Dolby Vision d'un réencodage FFmpeg (RPU réinjecté par dovi_tool).

    ``hevc_nvenc`` suit la règle NVENC (bandes alignées sur 32, plein cadre
    ramené dans le canevas UHD) ; les autres encodeurs gardent le recadrage
    utilisateur. ``dovi_rpu_crop`` porte le recadrage absolu que le RPU doit suivre.
    """
    if not video.copy_dv or video.codec == "copy" or min(dimensions) <= 0:
        return video
    if is_nvenc_hevc(video.codec):
        geometry = align_nvenc_dovi_geometry(video, dimensions, l5_offsets=l5_offsets)
        offsets = geometry.crop_offsets if geometry.needs_rpu_alignment else None
        return replace(geometry.video, dovi_rpu_crop=offsets)
    crop = absolute_dovi_crop(video, dimensions)
    offsets = (crop.left, crop.top, crop.right, crop.bottom) if crop.is_active() else None
    return replace(video, dovi_rpu_crop=offsets)


def crop_dovi_rpu(
    *, video: VideoEncodeSettings, rpu_bin: Path, dovi_tool_bin: str,
    run_cmd: Callable[[list[str]], object], log: Callable[[str], None] | None = None,
) -> Path:
    """RPU réaligné sur ``video.dovi_rpu_crop`` (L5 par scène) ; inchangé sans recadrage."""
    offsets = video.dovi_rpu_crop
    if not offsets or not any(offsets) or not rpu_bin.is_file():
        return rpu_bin
    if log is not None:
        log(
            f"Dolby Vision : recadrage {offsets} (gauche, haut, droite, bas). "
            "Réalignement des offsets L5 par scène."
        )
    return align_dovi_rpu_geometry(
        dovi_tool_bin=dovi_tool_bin, rpu_input=rpu_bin,
        output_rpu=rpu_bin.with_name(f"{rpu_bin.stem}.crop.bin"),
        crop_offsets=offsets, run_cmd=run_cmd,
    )
