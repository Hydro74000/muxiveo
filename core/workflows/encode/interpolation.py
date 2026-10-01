"""
core/workflows/encode/interpolation.py — Interpolation d'images RIFE (muxiveo-rife).

Le binaire ``muxiveo-rife`` (``native/muxiveo-rife``) lit et écrit du y4m :
la vidéo passe par un pipeline de trois processus reliés par des pipes ::

    ffmpeg (décodage + filtres logiciels) | muxiveo-rife | encodeur

Public:
    INTERPOLATION_MODELS, INTERPOLATION_FACTORS
    PipelineCommand          — commande finale précédée d'étages amont
    InterpolationSource      — couleur / décalage de départ de la source
    probe_interpolation_source(...)
    build_decode_stage(...), build_rife_stage(...)
    expand_rpu_file(...), expand_hdr10plus_json(...)
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from core.bluray import append_ffmpeg_input_args
from core.pipeline_command import PipelineCommand, command_stages

# Préréglage qualité → modèle RIFE embarqué (voir native/muxiveo-rife/models.json).
INTERPOLATION_MODELS: dict[str, str] = {
    "fast": "rife-v4.22-lite",
    "balanced": "rife-v4.26",
    "max": "rife-v4.25-heavy",
}
INTERPOLATION_DEFAULT_QUALITY = "balanced"
INTERPOLATION_FACTORS: tuple[int, ...] = (2, 3, 4)

# Intervalle des lignes ``progress`` de muxiveo-rife (le log suit l'encodeur).
_RIFE_PROGRESS_INTERVAL_S = 30

_RIFE_MATRICES = {"bt709", "bt2020nc", "bt2020", "bt601", "smpte170m", "bt470bg", "smpte240m", "fcc"}
_RIFE_CHROMA_LOCATIONS = {"left", "center", "topleft"}

_RPU_START_CODE = b"\x00\x00\x00\x01"


@dataclass(frozen=True)
class InterpolationSource:
    """Propriétés de la source utiles à l'étage RIFE et au réhorodatage."""

    matrix: str = "bt709"
    color_range: str = "limited"
    chroma_location: str = "left"
    # Écart (s) entre le départ du flux vidéo et celui du conteneur : le y4m
    # ne transporte pas de timestamps, l'encodeur le réapplique (-itsoffset).
    start_offset_s: float = 0.0
    is_vfr: bool = False
    # Cadence nominale imposée au décodage quand la source est VFR (y4m = CFR).
    cfr_rate: str = ""


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def interpolation_source_from_probe(
    stream: dict[str, object],
    *,
    format_start_time: object = None,
    tonemap_to_sdr: bool = False,
) -> InterpolationSource:
    """Construit les propriétés d'interpolation depuis un flux ``ffprobe -show_streams``."""
    if tonemap_to_sdr:
        # Le tone mapping (avant RIFE) produit du BT.709 limité.
        matrix = "bt709"
        color_range = "limited"
    else:
        matrix = str(stream.get("color_space") or "").strip().lower()
        if matrix not in _RIFE_MATRICES:
            transfer = str(stream.get("color_transfer") or "").strip().lower()
            height = int(_float_or_none(stream.get("height")) or 0)
            if transfer in {"smpte2084", "arib-std-b67"}:
                matrix = "bt2020nc"
            else:
                matrix = "bt709" if height > 576 else "bt601"
        color_range = "full" if str(stream.get("color_range") or "").strip().lower() in {"pc", "jpeg"} else "limited"

    chroma = str(stream.get("chroma_location") or "").strip().lower()
    if chroma not in _RIFE_CHROMA_LOCATIONS:
        chroma = "left"

    start_offset = 0.0
    stream_start = _float_or_none(stream.get("start_time"))
    container_start = _float_or_none(format_start_time)
    if stream_start is not None:
        start_offset = max(0.0, stream_start - (container_start or 0.0))

    r_rate = str(stream.get("r_frame_rate") or "")
    avg_rate = str(stream.get("avg_frame_rate") or "")
    is_vfr = bool(r_rate and avg_rate and r_rate != avg_rate and _rates_differ(r_rate, avg_rate))

    return InterpolationSource(
        matrix=matrix,
        color_range=color_range,
        chroma_location=chroma,
        start_offset_s=round(start_offset, 6),
        is_vfr=is_vfr,
        cfr_rate=nominal_cfr_rate(r_rate, avg_rate) if is_vfr else "",
    )


def nominal_cfr_rate(r_rate: str, avg_rate: str) -> str:
    """Cadence CFR de normalisation d'une source VFR (``r_frame_rate`` si plausible)."""
    r = _rate_value(r_rate)
    avg = _rate_value(avg_rate)
    if r and (not avg or r <= 2 * avg):
        return r_rate
    return avg_rate if avg else ""


def _rate_value(rate: str) -> float | None:
    num, _, den = rate.partition("/")
    try:
        n = float(num)
        d = float(den) if den else 1.0
    except ValueError:
        return None
    return n / d if d else None


def _rates_differ(left: str, right: str, *, tolerance: float = 0.001) -> bool:
    """Écart relatif > 0,1 % : au-delà des arrondis CFR (MP4 « 2997/100 » contre 30000/1001)."""
    a = _rate_value(left)
    b = _rate_value(right)
    if not a or not b:
        return False
    return abs(a - b) / max(a, b) > tolerance


def build_decode_stage(
    ffmpeg_bin: str,
    source: Path | str,
    *,
    stream_index: int,
    vf: str = "",
    pre_input_args: Sequence[str] = (),
    thread_args: Sequence[str] = (),
) -> list[str]:
    """Étage 1 : décodage + filtres logiciels → y4m sur stdout."""
    cmd = [str(ffmpeg_bin), "-hide_banner", "-nostdin", "-loglevel", "error"]
    cmd.extend(pre_input_args)
    append_ffmpeg_input_args(cmd, source)
    cmd.extend(["-map", f"0:{int(stream_index)}"])
    if vf:
        cmd.extend(["-vf", vf])
    cmd.extend(thread_args)
    cmd.extend([
        "-fps_mode", "passthrough",
        "-an", "-sn", "-dn",
        "-f", "yuv4mpegpipe", "-strict", "-1", "-",
    ])
    return cmd


def build_rife_stage(
    rife_bin: str,
    *,
    factor: int,
    quality: str,
    source: InterpolationSource,
    scene_threshold: float = 10.0,
    gpu: int = -1,
) -> list[str]:
    """Étage 2 : muxiveo-rife (y4m stdin → y4m stdout)."""
    model = INTERPOLATION_MODELS.get(str(quality or ""), INTERPOLATION_MODELS[INTERPOLATION_DEFAULT_QUALITY])
    cmd = [
        str(rife_bin),
        "--factor", str(int(factor)),
        "--model", model,
        "--matrix", source.matrix,
        "--range", source.color_range,
        "--chroma-loc", source.chroma_location,
        "--scene-threshold", f"{max(0.0, float(scene_threshold)):g}",
        "--progress-interval", str(_RIFE_PROGRESS_INTERVAL_S),
    ]
    if int(gpu) >= 0:
        cmd.extend(["--gpu", str(int(gpu))])
    return cmd


# =============================================================================
# Métadonnées dynamiques par trame (DoVi RPU, HDR10+)
# =============================================================================

def expand_rpu_file(source: Path, dest: Path, factor: int) -> int:
    """Duplique chaque RPU Dolby Vision ``factor`` fois ; retourne le nombre de RPU écrits.

    ``dovi_tool extract-rpu`` écrit une suite de NAL UNSPEC62 préfixées par un
    start code 4 octets ; l'émulation de start code garantit qu'aucun préfixe
    n'apparaît dans une charge utile. La trame interpolée hérite ainsi des
    métadonnées de la trame source qui la précède.
    """
    data = source.read_bytes()
    if not data.startswith(_RPU_START_CODE):
        raise ValueError(f"RPU illisible (start code absent) : {source}")
    units = [unit for unit in data.split(_RPU_START_CODE) if unit]
    factor = max(1, int(factor))
    with dest.open("wb") as fh:
        for unit in units:
            for _ in range(factor):
                fh.write(_RPU_START_CODE)
                fh.write(unit)
    return len(units) * factor


def expand_hdr10plus_json(source: Path, dest: Path, factor: int) -> int:
    """Duplique chaque trame d'un JSON ``hdr10plus_tool extract`` ``factor`` fois.

    Les index de trame (séquence et scène) et le résumé des scènes sont
    renumérotés ; retourne le nombre de trames écrites.
    """
    payload = json.loads(source.read_text(encoding="utf-8"))
    factor = max(1, int(factor))
    scenes = payload.get("SceneInfo")
    if not isinstance(scenes, list):
        raise ValueError(f"JSON HDR10+ sans SceneInfo : {source}")

    expanded: list[dict[str, object]] = []
    for entry in scenes:
        if not isinstance(entry, dict):
            continue
        for copy_index in range(factor):
            frame = dict(entry)
            for key in ("SequenceFrameIndex", "SceneFrameIndex"):
                value = entry.get(key)
                if isinstance(value, int):
                    frame[key] = value * factor + copy_index
            expanded.append(frame)
    payload["SceneInfo"] = expanded

    summary = payload.get("SceneInfoSummary")
    if isinstance(summary, dict):
        for key in ("SceneFirstFrameIndex", "SceneFrameNumbers"):
            values = summary.get(key)
            if isinstance(values, list):
                summary[key] = [v * factor if isinstance(v, int) else v for v in values]

    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return len(expanded)


def dovi_scene_cut_edit(scene_frames: Iterable[int], factor: int) -> dict[str, object]:
    """Édition ``dovi_tool editor`` retirant le drapeau de coupe des copies interpolées.

    Après duplication, la coupe de la trame source ``s`` est portée par les
    trames ``s*f … s*f+f-1`` ; seule la première doit rester une coupe.
    """
    cuts: dict[str, bool] = {}
    if factor > 1:
        for frame in sorted(set(int(f) for f in scene_frames)):
            first = frame * factor + 1
            cuts[f"{first}-{frame * factor + factor - 1}"] = False
    return {"scene_cuts": cuts} if cuts else {}


def expand_dynamic_hdr_metadata(
    *,
    factor: int,
    rpu_bin: Path | None = None,
    hdr10p_json: Path | None = None,
    dovi_tool_bin: str | None = None,
    run_cmd: Callable[[list[str]], object] | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Étend en place le RPU DoVi et le JSON HDR10+ à la cadence interpolée.

    Avec ``dovi_tool_bin`` et ``run_cmd``, les coupes de scène Dolby Vision
    restent sur la seule trame d'origine (sinon chaque coupe serait doublée).
    """
    if factor <= 1:
        return
    if rpu_bin is not None and rpu_bin.is_file():
        scenes_txt = rpu_bin.with_name(f"{rpu_bin.stem}.scenes.txt")
        scene_frames: list[int] = []
        if dovi_tool_bin and run_cmd is not None:
            try:
                run_cmd([dovi_tool_bin, "export", "-i", str(rpu_bin), "-d", f"scenes={scenes_txt}"])
                scene_frames = [int(line) for line in scenes_txt.read_text(encoding="utf-8").split() if line.isdigit()]
            except Exception as exc:  # noqa: BLE001 — nettoyage cosmétique, non bloquant
                if log:
                    log(f"Interpolation x{factor} : coupes Dolby Vision non relues ({exc}) ; drapeaux dupliqués conservés.")
            finally:
                scenes_txt.unlink(missing_ok=True)

        expanded = rpu_bin.with_name(f"{rpu_bin.stem}.x{factor}{rpu_bin.suffix}")
        count = expand_rpu_file(rpu_bin, expanded, factor)
        edit = dovi_scene_cut_edit(scene_frames, factor)
        if edit and dovi_tool_bin and run_cmd is not None:
            edit_json = rpu_bin.with_name(f"{rpu_bin.stem}.scenes.json")
            fixed = rpu_bin.with_name(f"{rpu_bin.stem}.x{factor}.scenes{rpu_bin.suffix}")
            edit_json.write_text(json.dumps(edit), encoding="utf-8")
            try:
                run_cmd([dovi_tool_bin, "editor", "-i", str(expanded), "-j", str(edit_json), "-o", str(fixed)])
                fixed.replace(expanded)
            finally:
                edit_json.unlink(missing_ok=True)
                fixed.unlink(missing_ok=True)
        expanded.replace(rpu_bin)
        if log:
            log(
                f"Interpolation x{factor} : RPU Dolby Vision étendu à {count} trames "
                f"({len(scene_frames)} coupe(s) de scène conservée(s))."
            )
    if hdr10p_json is not None and hdr10p_json.is_file():
        expanded = hdr10p_json.with_name(f"{hdr10p_json.stem}.x{factor}{hdr10p_json.suffix}")
        count = expand_hdr10plus_json(hdr10p_json, expanded, factor)
        expanded.replace(hdr10p_json)
        if log:
            log(f"Interpolation x{factor} : métadonnées HDR10+ étendues à {count} trames.")


__all__ = [
    "INTERPOLATION_DEFAULT_QUALITY",
    "INTERPOLATION_FACTORS",
    "INTERPOLATION_MODELS",
    "InterpolationSource",
    "PipelineCommand",
    "build_decode_stage",
    "build_rife_stage",
    "command_stages",
    "dovi_scene_cut_edit",
    "expand_dynamic_hdr_metadata",
    "expand_hdr10plus_json",
    "expand_rpu_file",
    "interpolation_source_from_probe",
]
