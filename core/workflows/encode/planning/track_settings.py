"""Réglages vidéo d'une piste : défauts selon la source, politique HDR par codec, propagation, preset.

Source unique de la résolution des réglages d'encodage d'une piste vidéo, partagée par le
panneau Encodage et la couche de service (CLI, futur daemon). Un « état » est le dictionnaire
de réglages d'une piste tel que le panneau le mémorise (``codec``, ``crf``, ``copy_dv``…) ;
:func:`video_settings_from_state` le convertit en :class:`VideoEncodeSettings`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.bluray import ffprobe_input_args
from core.inspector import FileInfo, HDRType, VideoTrack
from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.encode.backends import backend_capabilities_for_codec
from core.workflows.encode.catalog import default_preset_for_codec, supports_10bit, supports_hdr_output
from core.workflows.encode.domain.codecs import source_bit_depth_from_stream
from core.workflows.encode.dovi_policy import sub_profile_from_track
from core.workflows.encode.hdr_policy import default_static_hdr_checked
from core.workflows.encode.models import (
    EncodePreset,
    FrameInterpolationSettings,
    QualityMode,
    RestoreSettings,
    VideoCropSettings,
    VideoEncodeSettings,
    VideoFilterSettings,
    VideoResizeSettings,
)

#: Champs HDR propres à chaque source : jamais recopiés d'une piste à l'autre.
PER_SOURCE_HDR_KEYS: tuple[str, ...] = (
    "master_display",
    "max_cll",
    "default_master_display",
    "default_max_cll",
    "static_hdr_metadata_source",
    "static_hdr_metadata_confidence",
    "static_hdr_metadata_analysis_mode",
    "static_hdr_metadata_analysis_request",
    "hdr_disabled_by_codec",
    "dynamic_hdr_disabled_by_codec",
    "tonemap_enabled_by_codec",
)

#: Champs qu'un preset d'encodage applique (comme « Charger » du panneau Encodage).
PRESET_FIELDS: tuple[str, ...] = (
    "codec", "quality_mode", "rate_control", "crf", "cq", "bitrate_kbps", "target_size_mb", "preset",
    "extra_params", "bit_depth", "resize", "crop", "filters", "interpolation", "restoration",
    "bake_dovi_fel", "fel_device", "fel_precision", "fel_hdr10plus", "tonemap_algorithm",
)

#: Champs propres à la source, jamais repris d'un preset (HDR statique, copie DV / HDR10+, tone mapping).
PRESET_PROTECTED_FIELDS: tuple[str, ...] = (
    "inject_hdr_meta", "master_display", "max_cll", "tonemap_to_sdr", "copy_dv", "copy_hdr10plus",
    "dovi_profile", "static_hdr_metadata_source", "static_hdr_metadata_confidence",
    "static_hdr_metadata_analysis_mode", "static_hdr_metadata_analysis_request",
)


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceVideoContext:
    """Ce que la source impose aux réglages d'une piste vidéo."""

    hdr_type: HDRType = HDRType.NONE
    transfer: str = ""
    bit_depth: int = 0
    pix_fmt: str = ""
    codec: str = ""
    dovi_profile: str = ""
    master_display: str = ""
    max_cll: str = ""

    @classmethod
    def from_file_info(cls, info: FileInfo, mkv_tid: int | None = None, *, master_display: str = "",
                       max_cll: str = "") -> SourceVideoContext:
        video = video_track_for(info, mkv_tid)
        return cls(
            hdr_type=video.hdr_type if video is not None else info.hdr_type,
            transfer=str(getattr(video, "color_transfer", "") or "").strip().lower() if video is not None else "",
            bit_depth=video_source_bit_depth(video),
            pix_fmt=video_source_pix_fmt(video),
            codec=str(getattr(video, "codec", "") or ""),
            dovi_profile=source_dovi_profile(video),
            master_display=master_display,
            max_cll=max_cll,
        )


def video_track_for(info: FileInfo, mkv_tid: int | None) -> VideoTrack | None:
    """Piste vidéo réelle d'un ``mkv_tid`` (sinon la piste principale)."""
    if mkv_tid is not None:
        for video in info.video_tracks:
            if video.index == mkv_tid:
                return video
    return info.primary_video


def video_source_bit_depth(video: VideoTrack | None) -> int:
    if video is None:
        return 0
    try:
        bit_depth = int(getattr(video, "bit_depth", 0) or 0)
    except (TypeError, ValueError):
        bit_depth = 0
    return bit_depth if bit_depth in {8, 10, 12, 16} else source_bit_depth_from_stream(video.raw or {})


def video_source_pix_fmt(video: VideoTrack | None) -> str:
    """pix_fmt ffprobe de la piste ("" inconnu) : sous-échantillonnage de la source."""
    raw = getattr(video, "raw", None) or {}
    return str(raw.get("pix_fmt") or "").strip().lower() if isinstance(raw, dict) else ""


def source_dovi_profile(video: VideoTrack | None) -> str:
    """Sous-profil Dolby Vision inspecté (P8.2 SDR…) ; "" inconnu."""
    profile = sub_profile_from_track(getattr(video, "dovi_profile", None), getattr(video, "dovi_compat_id", None))
    return "" if profile.value == "unknown" else profile.value


def source_has_static_hdr10(source_hdr: HDRType | None, source_transfer: str = "") -> bool:
    """Source PQ portant (ou pouvant porter) des métadonnées HDR10 statiques (base HLG exclue)."""
    return default_static_hdr_checked(source_hdr, source_transfer)


def source_has_dv(source_hdr: HDRType) -> bool:
    return source_hdr in (HDRType.DOLBY_VISION, HDRType.DOLBY_VISION_HDR10PLUS)


def source_has_hdr10plus(source_hdr: HDRType) -> bool:
    return source_hdr in (HDRType.HDR10PLUS, HDRType.DOLBY_VISION_HDR10PLUS)


# ---------------------------------------------------------------------------
# HDR10 statique de la source
# ---------------------------------------------------------------------------

#: Chromaticités CIE 1931 (x, y) des primaires exposés par MediaInfo (G, B, R, point blanc D65).
MASTER_DISPLAY_PRIMARIES: dict[str, tuple[tuple[float, float], ...]] = {
    "bt.2020":    ((0.170, 0.797), (0.131, 0.046), (0.708, 0.292), (0.3127, 0.3290)),
    "display p3": ((0.265, 0.690), (0.150, 0.060), (0.680, 0.320), (0.3127, 0.3290)),
    "bt.709":     ((0.300, 0.600), (0.150, 0.060), (0.640, 0.330), (0.3127, 0.3290)),
    "p3-d65":     ((0.265, 0.690), (0.150, 0.060), (0.680, 0.320), (0.3127, 0.3290)),
}


def _master_display_string(g: tuple[float, float], b: tuple[float, float], r: tuple[float, float],
                           wp: tuple[float, float], lmax: float, lmin: float) -> str:
    def c(value: float) -> int:
        return int(round(value * 50000))

    def lum(value: float) -> int:
        return int(round(value * 10000))

    return (f"G({c(g[0])},{c(g[1])})B({c(b[0])},{c(b[1])})R({c(r[0])},{c(r[1])})"
            f"WP({c(wp[0])},{c(wp[1])})L({lum(lmax)},{lum(lmin)})")


def hdr_meta_from_mediainfo_json(data: Mapping[str, Any]) -> tuple[str, str]:
    """(master_display, max_cll) au format x265 depuis un JSON MediaInfo déjà chargé."""
    media = data.get("media") or {}
    mi_video = next(
        (t for t in (media.get("track") or []) if isinstance(t, dict) and t.get("@type") == "Video"),
        None,
    )
    if mi_video is None:
        return "", ""
    master_display = ""
    primaries_label = str(mi_video.get("MasteringDisplay_ColorPrimaries") or "").lower()
    primaries = MASTER_DISPLAY_PRIMARIES.get(primaries_label.strip())
    try:
        lmin = float(mi_video.get("MasteringDisplay_Luminance_Min") or 0)
        lmax = float(mi_video.get("MasteringDisplay_Luminance_Max") or 0)
    except (TypeError, ValueError):
        lmin = lmax = 0.0
    if primaries and lmax > 0:
        g, b, r, wp = primaries
        master_display = _master_display_string(g, b, r, wp, lmax, lmin)

    def first_int(value: object) -> int:
        match = re.search(r"\d+(?:[   _]\d{3})*", str(value or ""))
        if match is None:
            return 0
        return int(match.group(0).replace(" ", "").replace(" ", "").replace(" ", "").replace("_", ""))

    max_cll = ""
    try:
        max_content = first_int(mi_video.get("MaxCLL"))
        max_average = first_int(mi_video.get("MaxFALL"))
    except (TypeError, ValueError):
        max_content = max_average = 0
    if max_content > 0:
        max_cll = f"{max_content},{max_average}"
    return master_display, max_cll


def hdr_meta_from_mediainfo(mediainfo_bin: str, source_path: Path) -> tuple[str, str]:
    """(master_display, max_cll) d'après MediaInfo ; ('', '') si absent ou sans HDR10 statique."""
    try:
        result = subprocess.run(
            [mediainfo_bin, "--Output=JSON", str(source_path)],
            capture_output=True, check=False, timeout=60, **subprocess_text_kwargs(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return "", ""
    if result.returncode != 0:
        return "", ""
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return "", ""
    return hdr_meta_from_mediainfo_json(data)


def hdr_meta_from_ffprobe_raw(raw: Mapping[str, Any]) -> tuple[str, str]:
    """(master_display, max_cll) depuis les side data du flux ffprobe (rare)."""
    master_display = ""
    max_cll = ""

    def rat(v: object) -> float:
        s = str(v)
        if "/" in s:
            a, b = s.split("/", 1)
            return float(a) / float(b)
        return float(s)

    for sd in raw.get("side_data_list", []):
        if sd.get("side_data_type") == "Mastering display metadata":
            try:
                master_display = _master_display_string(
                    (rat(sd.get("green_x", 0)), rat(sd.get("green_y", 0))),
                    (rat(sd.get("blue_x", 0)), rat(sd.get("blue_y", 0))),
                    (rat(sd.get("red_x", 0)), rat(sd.get("red_y", 0))),
                    (rat(sd.get("white_point_x", 0)), rat(sd.get("white_point_y", 0))),
                    rat(sd.get("max_luminance", 0)), rat(sd.get("min_luminance", 0)),
                )
            except Exception:  # noqa: BLE001 - side data incomplète : ignorée
                pass
        elif sd.get("side_data_type") == "Content light level metadata":
            try:
                max_cll = f"{int(sd.get('max_content', 0))},{int(sd.get('max_average', 0))}"
            except Exception:  # noqa: BLE001
                pass
    return master_display, max_cll


def hdr_meta_from_ffprobe_frames(ffprobe_bin: str, source_path: Path, stream_index: int | None = None
                                 ) -> tuple[str, str]:
    """(master_display, max_cll) lus dans le SEI de la première image (``ffprobe -show_frames``)."""
    cmd = [ffprobe_bin, "-v", "error", "-select_streams", str(stream_index) if stream_index is not None else "v:0",
           "-show_frames", "-read_intervals", "%+#1", "-print_format", "json"]
    cmd.extend(ffprobe_input_args(source_path))
    try:
        result = subprocess.run(cmd, capture_output=True, check=False, timeout=20, **subprocess_text_kwargs())
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return "", ""
    if result.returncode != 0:
        return "", ""
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return "", ""
    frames = data.get("frames") or []
    if not frames:
        return "", ""

    def num(rat: object) -> int:
        try:
            return int(str(rat).split("/", 1)[0])
        except (ValueError, AttributeError):
            return 0

    master_display = ""
    max_cll = ""
    for sd in frames[0].get("side_data_list") or []:
        stype = sd.get("side_data_type") or ""
        if stype == "Mastering display metadata":
            gx, gy = num(sd.get("green_x")), num(sd.get("green_y"))
            bx, by = num(sd.get("blue_x")), num(sd.get("blue_y"))
            rx, ry = num(sd.get("red_x")), num(sd.get("red_y"))
            wx, wy = num(sd.get("white_point_x")), num(sd.get("white_point_y"))
            lmin, lmax = num(sd.get("min_luminance")), num(sd.get("max_luminance"))
            if lmax > 0 and (rx > 0 or gx > 0 or bx > 0):
                master_display = f"G({gx},{gy})B({bx},{by})R({rx},{ry})WP({wx},{wy})L({lmax},{lmin})"
        elif stype == "Content light level metadata":
            try:
                mc = int(sd.get("max_content") or 0)
                ma = int(sd.get("max_average") or 0)
            except (TypeError, ValueError):
                mc = ma = 0
            if mc > 0:
                max_cll = f"{mc},{ma}"
    return master_display, max_cll


def extract_hdr_meta_fields(
    raw: Mapping[str, Any],
    *,
    mediainfo_json: Mapping[str, Any] | None = None,
    from_mediainfo: Callable[[], tuple[str, str]] | None = None,
    from_frames: Callable[[], tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """HDR10 statique de la source : MediaInfo (JSON ou sonde), puis side data ffprobe, puis 1ʳᵉ image."""
    master_display, max_cll = "", ""
    if mediainfo_json is not None:
        master_display, max_cll = hdr_meta_from_mediainfo_json(mediainfo_json)
    elif from_mediainfo is not None:
        master_display, max_cll = from_mediainfo()
    if master_display and max_cll:
        return master_display, max_cll
    ff_md, ff_cll = hdr_meta_from_ffprobe_raw(raw)
    master_display = master_display or ff_md
    max_cll = max_cll or ff_cll
    if master_display and max_cll:
        return master_display, max_cll
    if from_frames is not None:
        ff2_md, ff2_cll = from_frames()
        master_display = master_display or ff2_md
        max_cll = max_cll or ff2_cll
    return master_display, max_cll


# ---------------------------------------------------------------------------
# Politique HDR par codec
# ---------------------------------------------------------------------------


def is_dovi_codec(codec: str) -> bool:
    return bool(getattr(backend_capabilities_for_codec(codec), "supports_dovi", False))


def is_hdr10plus_codec(codec: str) -> bool:
    return bool(getattr(backend_capabilities_for_codec(codec), "supports_hdr10plus", False))


def state_target_codec(state: Mapping[str, object] | None) -> str:
    if state is None:
        return "copy"
    return str(state.get("codec") or "copy").strip().lower()


def copy_state(state: Mapping[str, object]) -> dict[str, object]:
    copied = dict(state)
    by_codec = state.get("extra_params_by_codec")
    copied["extra_params_by_codec"] = dict(by_codec) if isinstance(by_codec, dict) else {}
    return copied


def state_allows_copy_static_hdr_edit(state: Mapping[str, object], codec: str) -> bool:
    return bool(
        str(codec or "").strip().lower() == "copy"
        and bool(state.get("inject_hdr_meta"))
        and bool(state.get("copy_dv"))
        and str(state.get("dovi_profile") or "0").strip() == "2"
    )


def effective_static_hdr_fields(state: Mapping[str, object], *, codec: str) -> tuple[str, str]:
    """Master Display / MaxCLL effectifs : saisie si le codec l'autorise, sinon valeurs de la source."""
    current_md = str(state.get("master_display") or "").strip()
    current_cll = str(state.get("max_cll") or "").strip()
    default_md = str(state.get("default_master_display") or "").strip()
    default_cll = str(state.get("default_max_cll") or "").strip()
    if (backend_capabilities_for_codec(codec).supports_manual_static_hdr
            or state_allows_copy_static_hdr_edit(state, codec)):
        return current_md or default_md, current_cll or default_cll
    return default_md, default_cll


def effective_dynamic_hdr_flags(state: Mapping[str, object], *, source_hdr: HDRType,
                                target_codec: str | None = None) -> tuple[bool, bool]:
    """Copie Dolby Vision / HDR10+ effective : demandée, permise par le codec et présente dans la source."""
    codec = str(target_codec or "").strip().lower() if target_codec is not None else state_target_codec(state)
    copy_dv = bool(state.get("copy_dv")) and is_dovi_codec(codec) and source_has_dv(source_hdr)
    copy_hdr10plus = (bool(state.get("copy_hdr10plus")) and is_hdr10plus_codec(codec)
                      and source_has_hdr10plus(source_hdr))
    return copy_dv, copy_hdr10plus


def apply_codec_hdr_policy(state: dict[str, object], *, source_hdr: HDRType, codec: str,
                           source_transfer: str = "") -> None:
    """Politique HDR d'un codec sur un état de piste.

    - copy        : HDR source recopié tel quel (cases calquées sur la source).
    - codec SDR   : aucune option HDR (H.264…).
    - source sans HDR10 statique : pas d'injection HDR10 héritée d'une autre piste.
    """
    if codec == "copy":
        state["inject_hdr_meta"] = source_has_static_hdr10(source_hdr, source_transfer)
        state["copy_dv"] = source_has_dv(source_hdr)
        state["copy_hdr10plus"] = source_has_hdr10plus(source_hdr)
        state["tonemap_to_sdr"] = False
        return
    if not supports_hdr_output(codec):
        state["inject_hdr_meta"] = False
        state["copy_dv"] = False
        state["copy_hdr10plus"] = False
        state["static_hdr_metadata_analysis_request"] = ""
        return
    if not source_has_static_hdr10(source_hdr, source_transfer):
        state["inject_hdr_meta"] = False


# ---------------------------------------------------------------------------
# États de piste
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncoderEnvironment:
    """Ce que la machine détectée impose : profondeur 10 bits, préréglage par défaut du codec."""

    supports_10bit: Callable[[str], bool] = supports_10bit
    default_preset: Callable[[str], str] = field(default=lambda codec: default_preset_for_codec(codec))


def normalize_video_state(state: Mapping[str, object], ctx: SourceVideoContext,
                          env: EncoderEnvironment | None = None) -> dict[str, object]:
    """Réaligne un état sur la source et le codec : profondeur, politique HDR, HDR effectif."""
    env = env or EncoderEnvironment()
    normalized = copy_state(state)
    target_codec = state_target_codec(normalized)
    depth = str(normalized.get("bit_depth") or "auto")
    normalized["bit_depth"] = ("auto" if depth == "10" and target_codec != "copy"
                               and not env.supports_10bit(target_codec) else depth)
    apply_codec_hdr_policy(normalized, source_hdr=ctx.hdr_type, codec=target_codec, source_transfer=ctx.transfer)
    copy_dv, copy_hdr10plus = effective_dynamic_hdr_flags(normalized, source_hdr=ctx.hdr_type,
                                                          target_codec=target_codec)
    normalized["copy_dv"] = copy_dv
    normalized["copy_hdr10plus"] = copy_hdr10plus
    md, cll = effective_static_hdr_fields(normalized, codec=target_codec)
    normalized["master_display"] = md
    normalized["max_cll"] = cll
    return normalized


def default_video_state(ctx: SourceVideoContext, env: EncoderEnvironment | None = None) -> dict[str, object]:
    """État initial d'une piste : copie, HDR de la source (HDR10 statique injecté si la source en porte)."""
    is_hdr_source = source_has_static_hdr10(ctx.hdr_type, ctx.transfer)
    state: dict[str, object] = {
        "codec": "copy",
        "quality_mode": QualityMode.CRF,
        "rate_control": "",
        "preset": "slow",
        "crf": 18,
        "cq": 26,
        "bitrate_kbps": "5000",
        "target_size_mb": "4000",
        "extra_params": "",
        "extra_params_by_codec": {},
        # Auto = profondeur source (10 bits si HDR) : jamais une valeur figée par piste.
        "bit_depth": "auto",
        "resize": VideoResizeSettings(),
        "crop": VideoCropSettings(),
        "filters": VideoFilterSettings(),
        "interpolation": FrameInterpolationSettings(),
        "restoration": RestoreSettings(),
        "inject_hdr_meta": is_hdr_source,
        "master_display": ctx.master_display,
        "max_cll": ctx.max_cll,
        # Valeurs de la source figées : mémoire pour un retour après décoche ou champs vidés.
        "default_master_display": ctx.master_display,
        "default_max_cll": ctx.max_cll,
        "static_hdr_metadata_source": "",
        "static_hdr_metadata_confidence": "",
        "static_hdr_metadata_analysis_mode": "",
        "static_hdr_metadata_analysis_request": "",
        "copy_dv": source_has_dv(ctx.hdr_type),
        "bake_dovi_fel": None,
        "fel_device": "auto",
        "copy_hdr10plus": source_has_hdr10plus(ctx.hdr_type),
        "dovi_profile": "0",
        "tonemap_to_sdr": False,
        "tonemap_algorithm": "hable",
    }
    return normalize_video_state(state, ctx, env)


def propagate_state(template: Mapping[str, object], own: Mapping[str, object], ctx: SourceVideoContext,
                    env: EncoderEnvironment | None = None) -> dict[str, object]:
    """Réglages communs de ``template`` appliqués à une piste ; HDR propre à sa source conservé."""
    state = copy_state(template)
    for key in PER_SOURCE_HDR_KEYS:
        state[key] = own.get(key, "")
    return normalize_video_state(state, ctx, env)


def apply_preset_to_state(state: Mapping[str, object], preset: EncodePreset, ctx: SourceVideoContext,
                          env: EncoderEnvironment | None = None) -> dict[str, object]:
    """Preset d'encodage appliqué à un état (comme « Charger » du panneau) : HDR de la source conservé."""
    settings = preset.to_video_settings()
    updated = copy_state(state)
    for key in PRESET_FIELDS:
        if key == "preset":
            updated[key] = preset.preset
        elif key == "quality_mode":
            updated[key] = settings.quality_mode
        else:
            updated[key] = getattr(settings, key)
    return normalize_video_state(updated, ctx, env)


def _state_int(state: Mapping[str, object], key: str, default: int) -> int:
    value = state.get(key, default)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def video_settings_from_state(
    state: Mapping[str, object],
    ctx: SourceVideoContext,
    *,
    stream_index: int,
    source_path: Path | None,
    track_entry_id: str | None,
    env: EncoderEnvironment | None = None,
) -> VideoEncodeSettings:
    """Réglages d'encodage d'une piste d'après son état (HDR effectif selon la source et le codec)."""
    env = env or EncoderEnvironment()
    mode = state.get("quality_mode") or QualityMode.CRF
    if not isinstance(mode, QualityMode):
        mode = QualityMode(str(mode))
    codec = str(state.get("codec") or "libx265")
    tonemap = bool(state.get("tonemap_to_sdr"))
    copy_dv, copy_hdr10plus = effective_dynamic_hdr_flags(state, source_hdr=ctx.hdr_type, target_codec=codec)
    copy_dv, copy_hdr10plus = copy_dv and not tonemap, copy_hdr10plus and not tonemap
    inject_hdr_meta = bool(state.get("inject_hdr_meta")) and supports_hdr_output(codec) and not tonemap
    master_display, max_cll = effective_static_hdr_fields(state, codec=codec) if inject_hdr_meta else ("", "")
    return VideoEncodeSettings(
        stream_index=int(stream_index),
        source_path=source_path,
        track_entry_id=track_entry_id,
        codec=codec,
        quality_mode=mode,
        rate_control=str(state.get("rate_control") or ""),
        crf=_state_int(state, "crf", 18),
        cq=_state_int(state, "cq", 26),
        bitrate_kbps=_state_int(state, "bitrate_kbps", 0),
        target_size_mb=_state_int(state, "target_size_mb", 0),
        preset=env.default_preset(codec) if state.get("preset") is None else str(state.get("preset")),
        extra_params=str(state.get("extra_params") or "").strip(),
        bit_depth=str(state.get("bit_depth") or "auto"),
        source_bit_depth=ctx.bit_depth,
        source_codec=ctx.codec,
        source_pix_fmt=ctx.pix_fmt,
        dovi_source_profile=ctx.dovi_profile if codec != "copy" else "",
        encoder_supports_10bit=env.supports_10bit(codec),
        resize=VideoResizeSettings.from_value(state.get("resize")),
        crop=VideoCropSettings.from_value(state.get("crop")),
        filters=VideoFilterSettings.from_value(state.get("filters")),
        interpolation=FrameInterpolationSettings.from_value(state.get("interpolation")),
        restoration=RestoreSettings.from_value(state.get("restoration")),
        inject_hdr_meta=inject_hdr_meta,
        master_display=master_display,
        max_cll=max_cll,
        bake_dovi_fel=bool(state["bake_dovi_fel"]) if isinstance(state.get("bake_dovi_fel"), bool) else None,
        fel_device=str(state.get("fel_device") or "auto"),
        fel_precision=str(state.get("fel_precision") or "exact"),
        fel_hdr10plus=str(state.get("fel_hdr10plus") or "recompute"),
        static_hdr_light_level_source=(
            "source" if str(state.get("max_cll") or "") == str(state.get("default_max_cll") or "") else "manual"),
        static_hdr_master_display_source=(
            "source" if str(state.get("master_display") or "") == str(state.get("default_master_display") or "")
            else "manual"),
        static_hdr_metadata_source=str(state.get("static_hdr_metadata_source") or ""),
        static_hdr_metadata_confidence=str(state.get("static_hdr_metadata_confidence") or ""),
        static_hdr_metadata_analysis_mode=str(state.get("static_hdr_metadata_analysis_mode") or ""),
        static_hdr_metadata_analysis_request=str(state.get("static_hdr_metadata_analysis_request") or ""),
        copy_dv=copy_dv,
        copy_hdr10plus=copy_hdr10plus,
        dovi_profile=str(state.get("dovi_profile") or "0"),
        source_color_transfer=ctx.transfer,
        tonemap_to_sdr=tonemap,
        tonemap_algorithm=str(state.get("tonemap_algorithm") or "hable"),
    )


# ---------------------------------------------------------------------------
# Routage
# ---------------------------------------------------------------------------


def routing_video_tracks(config: object) -> list[object]:
    tracks = list(getattr(config, "video_tracks", []) or [])
    if tracks:
        return tracks
    primary = getattr(config, "video", None)
    return [primary] if primary is not None else []


def requires_dovi_profile_normalization(video: object) -> bool:
    return bool(getattr(video, "copy_dv", False) and str(getattr(video, "dovi_profile", "0") or "0").strip() == "2")


def is_pure_copy(config: object) -> bool:
    """Vrai si tout est copié sans transformation : un remux suffit (règle de la barre d'exécution)."""
    video_tracks = routing_video_tracks(config)
    if not video_tracks:
        return False
    return (
        all(
            str(getattr(video, "codec", "") or "").strip().lower() == "copy"
            and not bool(getattr(video, "inject_hdr_meta", False))
            and not bool(getattr(video, "tonemap_to_sdr", False))
            and not bool(getattr(video, "has_video_transform", lambda: False)())
            and not requires_dovi_profile_normalization(video)
            for video in video_tracks
        )
        and all(
            str(getattr(audio, "codec", "") or "").strip().lower() == "copy"
            for audio in getattr(config, "audio_tracks", []) or []
        )
    )
