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
from collections.abc import Callable, Collection, Mapping, Sequence
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
from core.workflows.encode.hdr_policy import default_static_hdr_checked, source_is_hdr
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
    "static_hdr_analysis_disabled_by_codec",
    "static_hdr_manual",
    "static_hdr_master_display_manual",
    "static_hdr_max_cll_manual",
    "hdr_disabled_by_codec",
    "dynamic_hdr_disabled_by_codec",
    "tonemap_enabled_by_codec",
)

#: Champs qu'un preset d'encodage applique (comme « Charger » du panneau Encodage).
PRESET_FIELDS: tuple[str, ...] = (
    "codec", "quality_mode", "rate_control", "crf", "cq", "bitrate_kbps", "target_size_mb", "preset",
    "extra_params", "bit_depth", "resize", "crop", "filters", "interpolation", "restoration",
    "bake_dovi_fel", "fel_device", "fel_precision", "fel_hdr10plus", "generate_hdr10plus", "tonemap_algorithm",
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
        # MediaInfo configuré, arguments séparés, sans shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        result = subprocess.run(  # nosec B603
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
            except (ArithmeticError, TypeError, ValueError):  # side data incomplète : ignorée
                pass
        elif sd.get("side_data_type") == "Content light level metadata":
            try:
                max_cll = f"{int(sd.get('max_content', 0))},{int(sd.get('max_average', 0))}"
            except (TypeError, ValueError):  # valeurs absentes ou illisibles : ignorées
                pass
    return master_display, max_cll


def hdr_meta_from_ffprobe_frames(ffprobe_bin: str, source_path: Path, stream_index: int | None = None
                                 ) -> tuple[str, str]:
    """(master_display, max_cll) lus dans le SEI de la première image (``ffprobe -show_frames``)."""
    cmd = [ffprobe_bin, "-v", "error", "-select_streams", str(stream_index) if stream_index is not None else "v:0",
           "-show_frames", "-read_intervals", "%+#1", "-print_format", "json"]
    cmd.extend(ffprobe_input_args(source_path))
    try:
        # ffprobe configuré, arguments séparés et chemin média protégé, sans shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        result = subprocess.run(cmd, capture_output=True, check=False, timeout=20, **subprocess_text_kwargs())  # nosec B603
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


def static_hdr_editable(state: Mapping[str, object], codec: str) -> bool:
    """Le codec permet de saisir le HDR10 statique (Copy : seulement DV normalisé P8.1 avec HDR10 injecté) ;
    sinon les valeurs de la source sont imposées (et ne sont plus des saisies)."""
    return (backend_capabilities_for_codec(codec).supports_manual_static_hdr
            or state_allows_copy_static_hdr_edit(state, codec))


def effective_static_hdr_fields(state: Mapping[str, object], *, codec: str) -> tuple[str, str]:
    """Master Display / MaxCLL effectifs : saisie si le codec l'autorise, sinon valeurs de la source."""
    current_md = str(state.get("master_display") or "").strip()
    current_cll = str(state.get("max_cll") or "").strip()
    default_md = str(state.get("default_master_display") or "").strip()
    default_cll = str(state.get("default_max_cll") or "").strip()
    if static_hdr_editable(state, codec):
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


#: Sous-profils Dolby Vision dont l'image de base n'est pas PQ (SDR, HLG) : aucun HDR10+ possible.
_NON_PQ_BASE_DOVI = frozenset({"p8_2", "p8_4"})


def hdr10plus_generation_available(ctx: SourceVideoContext, codec: str, *, tonemap: bool) -> bool:
    """La génération HDR10+ depuis le Dolby Vision s'applique : source DV à image de base PQ, codec qui porte le
    HDR10+ (Copy compris), sans tone mapping ; jamais Copy d'une source P5 (image de base IPT). Sous-profil
    inconnu : admis, contrôlé au lancement."""
    codec = str(codec or "").strip().lower()
    if tonemap or not source_has_dv(ctx.hdr_type) or not is_hdr10plus_codec(codec):
        return False
    profile = str(ctx.dovi_profile or "").strip().lower()
    return profile not in _NON_PQ_BASE_DOVI and not (codec == "copy" and profile == "p5")


def effective_hdr10plus_generation(state: Mapping[str, object], ctx: SourceVideoContext, *,
                                   codec: str | None = None, tonemap: bool | None = None) -> bool:
    """HDR10+ généré : intention ``generate_hdr10plus`` et génération applicable (règle unique : panneau, jobs,
    aperçu, lancement). L'intention reste mémorisée quand elle ne s'applique pas (codec, tone mapping)."""
    target = state_target_codec(state) if codec is None else codec
    mapped = bool(state.get("tonemap_to_sdr")) if tonemap is None else tonemap
    return bool(state.get("generate_hdr10plus")) and hdr10plus_generation_available(ctx, target, tonemap=mapped)


def restore_flags(state: Mapping[str, object]) -> tuple[bool, bool]:
    """Dolby Vision / HDR10+ retirés par un codec, à rétablir au retour sur un codec qui les porte."""
    flags = state.get("dynamic_hdr_disabled_by_codec")
    return (bool(flags[0]), bool(flags[1])) if isinstance(flags, (tuple, list)) and len(flags) == 2 else (False, False)


#: Champs HDR10 statiques, leur valeur de la source et leur marqueur de saisie explicite.
STATIC_HDR_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("master_display", "default_master_display", "static_hdr_master_display_manual"),
    ("max_cll", "default_max_cll", "static_hdr_max_cll_manual"),
)


def static_hdr_field_manual(state: Mapping[str, object], field_name: str) -> bool:
    """Valeur HDR10 saisie (marqueur explicite, ou différente de celle de la source) ; vide : jamais manuelle."""
    for name, default_key, marker in STATIC_HDR_FIELDS:
        if name == field_name:
            value = str(state.get(name) or "").strip()
            return bool(value) and (bool(state.get(marker)) or value != str(state.get(default_key) or "").strip())
    raise KeyError(field_name)


def static_hdr_values_manual(state: Mapping[str, object]) -> bool:
    """HDR10 statique demandé explicitement avec au moins une valeur : valeur saisie, ou case cochée par
    l'utilisateur (``static_hdr_manual``) avec une valeur non vide. Champs vides : jamais (aucun marquage HDR)."""
    if any(static_hdr_field_manual(state, name) for name, _default, _marker in STATIC_HDR_FIELDS):
        return True
    return bool(state.get("static_hdr_manual")) and any(
        str(state.get(name) or "").strip() for name, _default, _marker in STATIC_HDR_FIELDS)


def effective_static_hdr_injection(state: Mapping[str, object], ctx: SourceVideoContext, *, codec: str,
                                   tonemap: bool) -> bool:
    """HDR10 statique injecté : demandé, codec HDR, sans tone mapping. Source à HDR10 statique (HDR10, P5 aux
    champs vides complétés au lancement) ; source sans (SDR, HLG) : seulement une valeur manuelle, jamais un héritage."""
    return (bool(state.get("inject_hdr_meta")) and supports_hdr_output(codec) and not tonemap
            and (source_has_static_hdr10(ctx.hdr_type, ctx.transfer) or static_hdr_values_manual(state)))


def apply_codec_hdr_policy(state: dict[str, object], *, source_hdr: HDRType, codec: str,
                           source_transfer: str = "") -> None:
    """Politique HDR d'un codec sur un état de piste (règle du panneau Encodage au changement de codec).

    - copy        : HDR source recopié tel quel (cases calquées sur la source).
    - codec SDR   : aucune option HDR (H.264…) ; les options décochées pour ce codec sont mémorisées
      (``hdr_disabled_by_codec``, ``dynamic_hdr_disabled_by_codec``) puis rétablies au retour sur un
      codec qui les porte (HDR10, Dolby Vision, HDR10+ indépendamment).
    - source sans HDR10 statique : pas d'injection HDR10 héritée d'une autre piste ; une demande explicite
      (valeurs MDCV / MaxCLL saisies) est gardée.
    """
    if codec == "copy":
        state["inject_hdr_meta"] = source_has_static_hdr10(source_hdr, source_transfer)
        state["copy_dv"] = source_has_dv(source_hdr)
        state["copy_hdr10plus"] = source_has_hdr10plus(source_hdr)
        state["tonemap_to_sdr"] = False
        state["hdr_disabled_by_codec"] = False
        state["dynamic_hdr_disabled_by_codec"] = (False, False)
        if not state_allows_copy_static_hdr_edit(state, codec):
            # Cases et valeurs imposées par la source : plus aucune saisie explicite.
            state.update({marker: False for _name, _default, marker in STATIC_HDR_FIELDS}, static_hdr_manual=False)
        return
    has_dv, has_hdr10plus = source_has_dv(source_hdr), source_has_hdr10plus(source_hdr)
    dv_ok, hdr10plus_ok = has_dv and is_dovi_codec(codec), has_hdr10plus and is_hdr10plus_codec(codec)
    restore_dv, restore_hdr10plus = restore_flags(state)
    restore_dv = restore_dv or (has_dv and not dv_ok and bool(state.get("copy_dv")))
    restore_hdr10plus = restore_hdr10plus or (has_hdr10plus and not hdr10plus_ok and bool(state.get("copy_hdr10plus")))
    if dv_ok and restore_dv:
        state["copy_dv"], restore_dv = True, False
    if hdr10plus_ok and restore_hdr10plus:
        state["copy_hdr10plus"], restore_hdr10plus = True, False
    state["dynamic_hdr_disabled_by_codec"] = (restore_dv, restore_hdr10plus)
    if not supports_hdr_output(codec):
        if state.get("inject_hdr_meta"):
            state["hdr_disabled_by_codec"] = True
        state["inject_hdr_meta"] = False
        state["copy_dv"] = False
        state["copy_hdr10plus"] = False
        state["static_hdr_metadata_analysis_request"] = ""
        return
    if state.get("hdr_disabled_by_codec"):
        state["inject_hdr_meta"], state["hdr_disabled_by_codec"] = True, False
    if not source_has_static_hdr10(source_hdr, source_transfer) and not static_hdr_values_manual(state):
        state["inject_hdr_meta"] = False


# ---------------------------------------------------------------------------
# États de piste
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncoderEnvironment:
    """Ce que la machine détectée impose : profondeur 10 bits, préréglage par défaut du codec."""

    supports_10bit: Callable[[str], bool] = supports_10bit
    default_preset: Callable[[str], str] = field(default=lambda codec: default_preset_for_codec(codec))


def resolved_tonemap(codec: str, *, source_hdr: bool, choice: bool | None) -> tuple[bool, bool]:
    """Tone mapping effectif et origine automatique (``tonemap_enabled_by_codec``), règle du panneau Encodage.

    Copy : aucun (imposé ; le choix reste mémorisé). Choix explicite (clic, ``tonemap.enabled``) : ce choix,
    vrai comme faux. Sinon défaut du codec : source HDR vers un codec sans HDR → coché d'office.
    """
    codec = str(codec or "").strip().lower()
    if codec == "copy":
        return False, False
    if choice is not None:
        return bool(choice), False
    auto = source_hdr and not backend_capabilities_for_codec(codec).supports_hdr
    return auto, auto


def tonemap_choice_of(state: Mapping[str, object], *, source_hdr: bool) -> bool | None:
    """Choix explicite du tone mapping (``tonemap_choice``) ; ``None`` : défaut du codec.

    Ancien état (clé absente) : déduit avec le codec **de cet état** (à figer avant tout changement de codec) —
    coché sans origine automatique → vrai ; décoché là où le défaut cocherait → faux ; sinon aucun choix.
    """
    if "tonemap_choice" in state:
        value = state.get("tonemap_choice")
        return value if isinstance(value, bool) else None
    codec = state_target_codec(state)
    if codec == "copy":
        return None
    if state.get("tonemap_to_sdr"):
        return None if state.get("tonemap_enabled_by_codec") else True
    return False if resolved_tonemap(codec, source_hdr=source_hdr, choice=None)[0] else None


def apply_tonemap_policy(state: dict[str, object], ctx: SourceVideoContext) -> None:
    """Tone mapping de l'état d'après son choix explicite, le codec et la source (:func:`resolved_tonemap`)."""
    source_hdr = source_is_hdr(ctx.hdr_type, ctx.transfer)
    choice = tonemap_choice_of(state, source_hdr=source_hdr)
    state["tonemap_choice"] = choice
    state["tonemap_to_sdr"], state["tonemap_enabled_by_codec"] = resolved_tonemap(
        state_target_codec(state), source_hdr=source_hdr, choice=choice)


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
    apply_tonemap_policy(normalized, ctx)
    copy_dv, copy_hdr10plus = effective_dynamic_hdr_flags(normalized, source_hdr=ctx.hdr_type,
                                                          target_codec=target_codec)
    normalized["copy_dv"] = copy_dv
    # Intention de copie HDR10+ gardée sous la génération qui la remplace : exclusion appliquée aux réglages
    # (``video_settings_from_state``), copie rétablie quand la génération est retirée (piste, preset, propagation).
    normalized["copy_hdr10plus"] = copy_hdr10plus
    normalized["generate_hdr10plus"] = bool(normalized.get("generate_hdr10plus"))
    md, cll = effective_static_hdr_fields(normalized, codec=target_codec)
    editable = static_hdr_editable(normalized, target_codec)
    for (name, _default, marker), value in zip(STATIC_HDR_FIELDS, (md, cll)):
        # Valeur remplacée (champ vidé complété) ou imposée par un codec sans édition (même à texte égal, règle
        # du panneau) : plus une saisie. ``static_hdr_manual`` (case cochée) reste, sauf en Copy.
        if not editable or str(normalized.get(name) or "").strip() != value:
            normalized[marker] = False
        normalized[name] = value
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
        "generate_hdr10plus": False,
        "dovi_profile": "0",
        "tonemap_to_sdr": False,
        "tonemap_choice": None,
        "tonemap_algorithm": "hable",
    }
    return normalize_video_state(state, ctx, env)


#: Options HDR dont « Appliquer à toutes » tient une intention commune (``propagate_state``, ``hdr_common``).
HDR_COMMON_OPTIONS: tuple[str, ...] = ("inject_hdr_meta", "copy_dv", "copy_hdr10plus")
#: Génération HDR10+ : intention commune distincte (aucune mémoire de copie), voir ``propagate_state``.
HDR10PLUS_GENERATION_OPTION = "generate_hdr10plus"


def hdr_option_in_source(option: str, ctx: SourceVideoContext) -> bool:
    """La source porte ce que l'option recopie (HDR10 statique, Dolby Vision, HDR10+) ou d'où elle génère (HDR10+
    généré depuis le Dolby Vision)."""
    if option in ("copy_dv", HDR10PLUS_GENERATION_OPTION):
        return source_has_dv(ctx.hdr_type)
    if option == "copy_hdr10plus":
        return source_has_hdr10plus(ctx.hdr_type)
    return source_has_static_hdr10(ctx.hdr_type, ctx.transfer)


def hdr_option_carried(option: str, codec: str) -> bool:
    """Le codec porte l'option (HDR en sortie, Dolby Vision, HDR10+ copié ou généré)."""
    if option == "copy_dv":
        return is_dovi_codec(codec)
    if option in ("copy_hdr10plus", HDR10PLUS_GENERATION_OPTION):
        return is_hdr10plus_codec(codec)
    return supports_hdr_output(codec)


def _option_memory(state: Mapping[str, object], option: str) -> bool:
    if option == "inject_hdr_meta":
        return bool(state.get("hdr_disabled_by_codec"))
    return restore_flags(state)[0 if option == "copy_dv" else 1]


def propagate_state(template: Mapping[str, object], own: Mapping[str, object], ctx: SourceVideoContext,
                    env: EncoderEnvironment | None = None) -> dict[str, object]:
    """Réglages communs de ``template`` appliqués à une piste ; HDR propre à sa source conservé.

    HDR10 statique, Dolby Vision, HDR10+ : jamais recopiés de la piste modèle. Seule l'intention commune de
    « Appliquer à toutes » (``template["hdr_common"]``, tenue par le panneau, option par option) se propage, vers
    une source qui porte l'option (retirée puis mémorisée par un codec qui ne la porte pas). Sinon (source sans
    l'option, intention inconnue) : valeur et mémoire de la piste ; la normalisation applique ensuite la
    politique du codec. Tone mapping : choix commun (``hdr_common["tonemap_choice"]``) sinon choix de la piste,
    défaut automatique calculé pour sa propre source.
    """
    state = copy_state(template)
    for key in PER_SOURCE_HDR_KEYS:
        state[key] = own.get(key, "")
    state.pop("hdr_common", None)
    raw_common = template.get("hdr_common")
    common: Mapping[str, object] = raw_common if isinstance(raw_common, Mapping) else {}
    # Tone mapping : choix commun s'il existe, sinon choix (et défaut) propres à la piste.
    common_tonemap = common.get("tonemap_choice")
    state["tonemap_choice"] = (common_tonemap if isinstance(common_tonemap, bool)
                               else tonemap_choice_of(own, source_hdr=source_is_hdr(ctx.hdr_type, ctx.transfer)))
    codec = state_target_codec(state)
    if codec != "copy":
        memory: dict[str, bool] = {}
        for option in HDR_COMMON_OPTIONS:
            carried = hdr_option_carried(option, codec)
            intent = common.get(option)
            if isinstance(intent, bool) and hdr_option_in_source(option, ctx):
                state[option] = intent and carried
                memory[option] = intent and not carried
            else:
                state[option] = bool(own.get(option))
                memory[option] = _option_memory(own, option)
        state["hdr_disabled_by_codec"] = memory["inject_hdr_meta"]
        state["dynamic_hdr_disabled_by_codec"] = (memory["copy_dv"], memory["copy_hdr10plus"])
    # Génération HDR10+ : intention propre (jamais une mémoire de copie), conservée par piste ; l'intention commune
    # s'applique aux seules sources Dolby Vision (hors piste modèle Copy, qui ne propage pas).
    generate = common.get(HDR10PLUS_GENERATION_OPTION)
    state[HDR10PLUS_GENERATION_OPTION] = (generate if isinstance(generate, bool) and codec != "copy"
                                          and source_has_dv(ctx.hdr_type)
                                          else bool(own.get(HDR10PLUS_GENERATION_OPTION)))
    return normalize_video_state(state, ctx, env)


def preset_problem(
    settings: VideoEncodeSettings,
    ctx: SourceVideoContext,
    env: EncoderEnvironment | None = None,
    *,
    tonemap: bool = False,
    codecs: Collection[str] | None = None,
    rate_controls: Collection[str] | None = None,
    presets: Sequence[str] | None = None,
) -> str:
    """Raison pour laquelle un preset ne s'applique pas à cette piste (« Charger » du panneau) ; ``""`` sinon.

    Refus explicite, jamais de normalisation silencieuse. ``codecs`` / ``rate_controls`` / ``presets`` :
    ce que la machine propose pour ``settings.codec`` (détection) ; ``None`` : catalogue (codecs non vérifiés).
    """
    from core.i18n import translate_text
    from core.workflows.encode.catalog import presets_for_codec, rate_controls_for_codec
    from core.workflows.encode.domain.codecs import rate_control_values

    env = env or EncoderEnvironment()
    codec = settings.codec
    if settings.bit_depth not in {"auto", "8", "10"}:
        return translate_text("profondeur invalide")
    if settings.bit_depth == "10" and not env.supports_10bit(codec):
        return translate_text("sortie 10 bits indisponible pour {codec}", codec=codec)
    if (settings.bit_depth == "8" and supports_hdr_output(codec) and source_is_hdr(ctx.hdr_type, ctx.transfer)
            and not tonemap):
        return translate_text("une sortie HDR exige une profondeur de 10 bits")
    if codecs is not None and codec not in codecs:
        return translate_text("codec {codec} indisponible", codec=codec)
    rc_spec = rate_control_values(settings)[0]
    modes = ({spec.rc_id for spec in rate_controls_for_codec(codec)} if rate_controls is None
             else set(rate_controls))
    if rc_spec is None or rc_spec.rc_id not in modes:
        return translate_text("mode {mode} non supporté par {codec}",
                              mode=rc_spec.label if rc_spec is not None else settings.quality_mode.label(),
                              codec=codec)
    codec_presets = presets_for_codec(codec) if presets is None else list(presets)
    if codec_presets and settings.preset not in codec_presets:
        return translate_text("preset {preset} inconnu pour {codec}", preset=settings.preset, codec=codec)
    return ""


def apply_preset_to_state(state: Mapping[str, object], preset: EncodePreset, ctx: SourceVideoContext,
                          env: EncoderEnvironment | None = None) -> dict[str, object]:
    """Preset d'encodage appliqué à un état (comme « Charger » du panneau) : HDR de la source conservé.

    Tone mapping : choix explicite gardé, sinon défaut du nouveau codec (:func:`resolved_tonemap`).
    """
    settings = preset.to_video_settings()
    updated = copy_state(state)
    # Choix figé avec le codec de l'état avant que le preset ne le change (ancien état sans clé).
    updated["tonemap_choice"] = tonemap_choice_of(state, source_hdr=source_is_hdr(ctx.hdr_type, ctx.transfer))
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
    generate_hdr10plus = effective_hdr10plus_generation(state, ctx, codec=codec, tonemap=tonemap)
    copy_dv, copy_hdr10plus = copy_dv and not tonemap, copy_hdr10plus and not tonemap and not generate_hdr10plus
    inject_hdr_meta = effective_static_hdr_injection(state, ctx, codec=codec, tonemap=tonemap)
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
        # Copy : sous-profil gardé seulement pour la génération HDR10+ (refus P5 / base non PQ au lancement).
        dovi_source_profile=ctx.dovi_profile if codec != "copy" or generate_hdr10plus else "",
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
        static_hdr_light_level_source="manual" if static_hdr_field_manual(state, "max_cll") else "source",
        static_hdr_master_display_source="manual" if static_hdr_field_manual(state, "master_display") else "source",
        static_hdr_metadata_source=str(state.get("static_hdr_metadata_source") or ""),
        static_hdr_metadata_confidence=str(state.get("static_hdr_metadata_confidence") or ""),
        static_hdr_metadata_analysis_mode=str(state.get("static_hdr_metadata_analysis_mode") or ""),
        static_hdr_metadata_analysis_request=str(state.get("static_hdr_metadata_analysis_request") or ""),
        copy_dv=copy_dv,
        copy_hdr10plus=copy_hdr10plus,
        generate_hdr10plus=generate_hdr10plus,
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
            and not bool(getattr(video, "generate_hdr10plus", False))
            for video in video_tracks
        )
        and all(
            str(getattr(audio, "codec", "") or "").strip().lower() == "copy"
            for audio in getattr(config, "audio_tracks", []) or []
        )
    )
