"""Codec domain helpers extracted from EncodeWorkflow."""

from __future__ import annotations

import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass

from core.workflows.encode.catalog import (
    RateControlSpec,
    StaticHdrMetadataMode,
    presets_for_codec,
    resolve_rate_control,
    static_hdr_metadata_mode,
    AMF_VIDEO_CODECS,
    NVENC_VIDEO_CODECS,
    NVENCC_VIDEO_CODECS,
    QSV_VIDEO_CODECS,
    VAAPI_VIDEO_CODECS,
    is_h264_video_codec,
    supports_10bit,
    supports_hdr_output,
    supports_manual_static_hdr_metadata,
)
from core.workflows.encode.models import (
    AudioTrackSettings,
    QualityMode,
    VideoEncodeSettings,
    VideoFilterSettings,
    VideoResizeSettings,
    normalize_audio_bitrate_kbps,
)

@dataclass(frozen=True)
class EncodeCodecDomainCallbacks:
    platform: str
    vaapi_device: str | None = None
    qsv_device: str | None = None
    amf_device: str | None = None
    nvenc_device: str | None = None
    depth_conversion_filters: frozenset[str] = frozenset()


def rate_control_values(video: VideoEncodeSettings) -> tuple[RateControlSpec | None, int]:
    """Mode de débit effectif et valeur de qualité (bornée à la plage du mode).

    Valeur : ``crf`` pour les modes logiciels, ``cq`` pour les modes matériels ;
    ancien format (sans ``rate_control``) : CRF matériel → ``crf``, CQ logiciel → ``cq``.
    """
    spec = resolve_rate_control(video.codec, video.rate_control, video.quality_mode)
    if spec is None:
        return None, int(video.crf)
    if spec.family == "crf":
        legacy_cq = not video.rate_control and video.quality_mode == QualityMode.CQ
        value = video.cq if legacy_cq else video.crf
    else:
        legacy_crf = not video.rate_control and video.quality_mode == QualityMode.CRF
        value = video.crf if legacy_crf else video.cq
    low, high = spec.quality_range
    return spec, max(low, min(high, int(value)))


_TWO_PASS_VIDEO_CODECS = frozenset({"libx264", "libx265", "libsvtav1"})


def uses_two_pass_video(video: VideoEncodeSettings) -> bool:
    """Taille cible en 2 passes : codecs logiciels uniquement.

    Les encodeurs matériels ignorent ``-pass`` : une passe VBR plafonnée au débit
    calculé (``_hw_rate_control_args``), sans passe d'analyse inutile.
    """
    return video.quality_mode == QualityMode.SIZE and video.codec in _TWO_PASS_VIDEO_CODECS


def supports_output_10bit(video: VideoEncodeSettings) -> bool:
    """Capacité catalogue, avec confirmation GPU obligatoire pour NVEncC H.264."""
    if video.codec == "nvencc_h264":
        return video.encoder_supports_10bit is True
    return supports_10bit(video.codec)


def source_bit_depth_from_stream(stream: dict) -> int:
    """Profondeur ffprobe ; 0 quand aucun format exploitable n'est déclaré."""
    raw = stream.get("bits_per_raw_sample")
    try:
        depth = int(raw or 0)
    except (TypeError, ValueError):
        depth = 0
    if depth in {8, 10, 12, 16}:
        return depth
    fmt = str(stream.get("pix_fmt") or "").lower()
    if re.search(r"(?:p|gray|gbrp)(10|12|16)(?:le|be|msble)?$", fmt):
        return int(re.search(r"(10|12|16)(?:le|be|msble)?$", fmt).group(1))
    if fmt in {"p010le", "p010be"}:
        return 10
    if fmt in {"p016le", "p016be"}:
        return 16
    if fmt in {"nv12", "nv21", "yuv420p", "yuv422p", "yuv444p", "yuvj420p", "yuvj422p", "yuvj444p", "gbrp", "gray", "rgb24", "rgba", "bgra"}:
        return 8
    return 0


def resolve_output_bit_depth(video: VideoEncodeSettings, source_bit_depth: int | None = None) -> int:
    """Profondeur cible unique ; Copy conserve celle de la source sans conversion."""
    source = video.source_bit_depth if source_bit_depth is None else source_bit_depth
    if video.codec == "copy":
        return int(source or 0)
    if video.bit_depth in {"8", "10"}:
        return int(video.bit_depth)
    if output_hdr_transfer(video):
        return 10
    # Source > 8 bits (10, 12, 16) : 10 bits, la plus proche que les encodeurs proposent.
    if source > 8 and supports_output_10bit(video):
        return 10
    return 8


def bit_depth_error(video: VideoEncodeSettings) -> str:
    """Refuse une demande incompatible, avant de lancer l'encodeur."""
    if video.codec == "copy":
        return ""
    if video.bit_depth not in {"auto", "8", "10"}:
        return "profondeur invalide : choisissez Auto, 8 ou 10 bits."
    target = resolve_output_bit_depth(video)
    if target == 8 and output_hdr_transfer(video):
        return "une sortie HDR exige une profondeur de 10 bits."
    if target == 10 and not supports_output_10bit(video):
        return f"le codec '{video.codec}' ne supporte pas la sortie 10 bits sur cet encodeur."
    return ""


def _incompatible_pixel_format(video: VideoEncodeSettings, value: str, callbacks: EncodeCodecDomainCallbacks) -> bool:
    """Formats incompatibles avec HDR/capacité ou avec l'upload matériel imposé."""
    if video.codec in VAAPI_VIDEO_CODECS:
        return value != "vaapi"
    if video.codec in AMF_VIDEO_CODECS and callbacks.platform == "win32" and callbacks.amf_device is not None:
        return value != "d3d11"
    depth = source_bit_depth_from_stream({"pix_fmt": value})
    return bool(depth and (
        (bool(output_hdr_transfer(video)) and depth != 10)
        or (depth > 8 and not supports_output_10bit(video))
    ))


def filtered_bit_depth(video: VideoEncodeSettings) -> int:
    """Profondeur après les conversions couleur du workflow, avant encodage."""
    if video.tonemap_to_sdr:
        return 8
    if video.p5_to_hdr10:
        return 10
    return int(video.source_bit_depth or 0)


# Formats 4:2:0 de chaque profondeur cible (logiciel et matériel) : seuls
# formats acceptés tels quels par les profils Main / Main10 / High / High10.
_TARGET_420_FORMATS: dict[int, frozenset[str]] = {
    8: frozenset({"yuv420p", "yuvj420p", "nv12"}),
    10: frozenset({"yuv420p10le", "p010le"}),
}


def filtered_pix_fmt(video: VideoEncodeSettings) -> str:
    """Format des images après les conversions du workflow ("" si inconnu)."""
    if video.tonemap_to_sdr:
        return "yuv420p"
    if video.p5_to_hdr10:
        return "yuv420p10le"
    return str(video.source_pix_fmt or "").strip().lower()


def source_is_420(video: VideoEncodeSettings) -> bool:
    """Source 4:2:0 8/10 bits (ou format inconnu) : décodable et encodable telle quelle par le GPU."""
    fmt = str(video.source_pix_fmt or "").strip().lower()
    return not fmt or any(fmt in formats for formats in _TARGET_420_FORMATS.values())


def frames_match_target(video: VideoEncodeSettings, target: int) -> bool:
    """Images déjà au format 4:2:0 de la profondeur cible : aucune conversion à demander.

    Une profondeur égale ne suffit pas : une source 4:2:2 / 4:4:4 10 bits ferait
    refuser High10 par x264, échouer NVENC (p210) et passer x265 en Rext.
    """
    fmt = filtered_pix_fmt(video)
    if fmt:
        return fmt in _TARGET_420_FORMATS.get(target, frozenset())
    return filtered_bit_depth(video) == target


def force_h264_8bit(video: VideoEncodeSettings) -> bool:
    """Alias historique ; la profondeur canonique fait foi."""
    return video.bit_depth == "8" and is_h264_video_codec(video.codec)


def h264_8bit_pix_fmt_args(video: VideoEncodeSettings) -> list[str]:
    if not force_h264_8bit(video):
        return []
    if video.codec == "libx264":
        return ["-pix_fmt", "yuv420p"]
    return ["-pix_fmt", "nv12"]


def force_10bit_active(video: VideoEncodeSettings) -> bool:
    """Alias historique : cible 10 bits résolue et encodeur compatible."""
    return video.codec != "copy" and resolve_output_bit_depth(video) == 10 and supports_output_10bit(video)


def has_video_transform(video: VideoEncodeSettings) -> bool:
    return bool(getattr(video, "has_video_transform", lambda: False)())


def has_cpu_video_filter(video: VideoEncodeSettings) -> bool:
    """True when FFmpeg must keep frames in system memory for filtering."""
    if video.codec == "copy":
        return False
    return bool(
        getattr(video, "p5_to_hdr10", False)
        or
        video.resize.is_active()
        or video.crop.is_active()
        or video.filters.is_active()
        or video.tonemap_to_sdr
    )


def depth_args(video: VideoEncodeSettings, target: int | None = None, *, hw_frames: bool = False) -> list[str]:
    """Format/profil cible ; aucune conversion logicielle d'images matérielles."""
    if video.codec == "copy":
        return []
    target = resolve_output_bit_depth(video) if target is None else target
    codec = video.codec
    args: list[str] = []
    software = codec in {"libx264", "libx265", "libsvtav1"}
    if not hw_frames and codec not in VAAPI_VIDEO_CODECS and not frames_match_target(video, target):
        fmt = ("yuv420p10le" if target == 10 else "yuv420p") if software else ("p010le" if target == 10 else "nv12")
        args.extend(["-pix_fmt", fmt])
    if target == 10:
        if codec == "libx264":
            args.extend(["-profile:v", "high10"])
        elif codec.startswith("hevc_"):
            args.extend(["-profile:v", "main10"])
    return args


def ten_bit_args(video: VideoEncodeSettings) -> list[str]:
    """Alias historique, conservé pour les intégrations externes."""
    return depth_args(video) if force_10bit_active(video) else []


def vaapi_compression_args(video: VideoEncodeSettings) -> list[str]:
    """``-compression_level`` du preset VAAPI ; preset « Aucun » → défaut du pilote.

    Une valeur hors de la liste du codec (ex. preset par défaut d'un autre
    encodeur) ferait échouer ffmpeg : elle n'est pas transmise.
    """
    level = str(video.preset or "").strip()
    return ["-compression_level", level] if level and level in presets_for_codec(video.codec) else []


def ffmpeg_extra_args(video: VideoEncodeSettings) -> list[str]:
    """Tokens ffmpeg additionnels (libx264, NVENC/AMF/QSV/VAAPI).

    Le champ extra_params est passé tel quel à shlex.split — l'utilisateur saisit
    une suite de flags ffmpeg (ex: ``-spatial-aq 1 -temporal-aq 1 -rc-lookahead 32``
    ou ``-tune film -x264-params "aq-mode=3"``). libx265 et libsvtav1 consomment
    extra_params via leur propre syntaxe (``-x265-params`` / ``-svtav1-params``).
    """
    raw = (video.extra_params or "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return []


# Ancien nom, conservé pour compatibilité.
hw_extra_args = ffmpeg_extra_args


def _is_hevc_nvenc_safe_preset(video: VideoEncodeSettings) -> bool:
    return video.codec == "hevc_nvenc" and str(video.preset or "").strip().lower() == "safe"


# Preset transmis tel quel à l'encodeur (refus à l'ouverture s'il est inconnu).
_VERBATIM_PRESET_CODECS = frozenset({*NVENC_VIDEO_CODECS, "libx264", "libx265"})


def preset_problem(video: VideoEncodeSettings) -> tuple[str, str]:
    """(erreur, avertissement) d'un preset non pris en charge par le codec ("" si valide).

    Erreur : NVENC, x264, x265, SVT-AV1 (preset transmis tel quel, FFmpeg échouerait).
    Avertissement : AMF / QSV / VAAPI / NVEncC (preset inconnu ignoré, défaut de l'encodeur).
    """
    preset = str(video.preset or "").strip()
    codec = video.codec
    presets = presets_for_codec(codec)
    if codec == "copy" or not preset or not presets:
        return "", ""
    if codec == "libsvtav1":
        try:
            valid = -2 <= int(preset) <= 13
        except ValueError:
            valid = False
        message = f"preset « {preset} » non pris en charge par {codec} (-2 à 13)."
        return ("" if valid else message), ""
    effective = nvenc_effective_preset(video) if codec in NVENC_VIDEO_CODECS else preset
    accepted = {value.lower() for value in presets}
    if effective.lower() in accepted:
        return "", ""
    choices = ", ".join(value for value in presets if value)
    if codec in _VERBATIM_PRESET_CODECS:
        return f"preset « {preset} » non pris en charge par {codec} (au choix : {choices}).", ""
    return "", f"preset « {preset} » non pris en charge par {codec} : ignoré, défaut de l'encodeur."


def nvenc_effective_preset(video: VideoEncodeSettings) -> str:
    if _is_hevc_nvenc_safe_preset(video):
        # Backward-compat only: old saved profiles may still contain preset=safe.
        # The experimental workflow patch is disabled, but we still map the
        # dormant logical preset to a valid native NVENC preset.
        return "p5"
    return video.preset


def x265_params(video: VideoEncodeSettings) -> str:
    parts: list[str] = []
    if video.extra_params:
        parts.append(video.extra_params.strip(":"))
    if video.inject_hdr_meta and not video.tonemap_to_sdr:
        if video.master_display:
            parts.append(f"master-display={video.master_display}")
        if video.max_cll:
            parts.append(f"max-cll={video.max_cll}")
    return ":".join(part for part in parts if part)


_X265_MASTER_DISPLAY_RE = re.compile(
    r"G\((\d+),(\d+)\)B\((\d+),(\d+)\)R\((\d+),(\d+)\)WP\((\d+),(\d+)\)L\((\d+),(\d+)\)"
)


def svtav1_mastering_display(master_display: str) -> str:
    """master-display x265 (chromaticités ×50000, luminances ×10000) au format SVT-AV1 (décimal)."""
    match = _X265_MASTER_DISPLAY_RE.fullmatch(str(master_display or "").strip())
    if match is None:
        return ""
    xy = [int(value) / 50000 for value in match.groups()[:8]]
    lmax, lmin = int(match[9]) / 10000, int(match[10]) / 10000
    return (
        f"G({xy[0]:.5f},{xy[1]:.5f})B({xy[2]:.5f},{xy[3]:.5f})R({xy[4]:.5f},{xy[5]:.5f})"
        f"WP({xy[6]:.5f},{xy[7]:.5f})L({lmax:.4f},{lmin:.4f})"
    )


def svtav1_params(video: VideoEncodeSettings) -> str:
    """``-svtav1-params`` : paramètres libres + HDR10 statique (mastering-display, content-light)."""
    parts: list[str] = []
    if video.extra_params:
        parts.append(video.extra_params.strip(":"))
    if video.inject_hdr_meta and not video.tonemap_to_sdr:
        mastering = svtav1_mastering_display(video.master_display)
        if mastering:
            parts.append(f"mastering-display={mastering}")
        if video.max_cll:
            parts.append(f"content-light={video.max_cll.strip()}")
    return ":".join(part for part in parts if part)


_PQ_TRANSFERS = frozenset({"smpte2084", "smpte-st-2084", "pq"})
_HLG_TRANSFERS = frozenset({"arib-std-b67", "hlg"})
_UNKNOWN_TRANSFERS = frozenset({"", "unknown", "unspecified", "reserved"})
_VUI_TRANSFER = {"pq": "smpte2084", "hlg": "arib-std-b67"}


def transfer_kind(color_transfer: str | None) -> str:
    """Famille d'un color_transfer : "pq", "hlg", "sdr", ou "" si inconnu."""
    value = str(color_transfer or "").strip().lower()
    if value in _PQ_TRANSFERS:
        return "pq"
    if value in _HLG_TRANSFERS:
        return "hlg"
    if value in _UNKNOWN_TRANSFERS:
        return ""
    return "sdr"


def output_hdr_transfer(video: VideoEncodeSettings) -> str:
    """Transfert HDR de la sortie : "pq", "hlg", ou "" pour une sortie SDR.

    La sortie est HDR quand la source l'est (ou après conversion P5 → HDR10),
    sans tone-mapping, vers un codec capable de HDR. Une demande HDR explicite
    (statique ou dynamique) sur une source au transfert inconnu ou SDR (source
    PQ mal étiquetée) vaut PQ, comme avant. Exception : la copie Dolby Vision
    d'une source P8.2 (image de base SDR) ne rend pas la sortie HDR.
    """
    if video.codec == "copy" or video.tonemap_to_sdr or not supports_hdr_output(video.codec):
        return ""
    if bool(getattr(video, "p5_to_hdr10", False)):
        return "pq"
    kind = transfer_kind(getattr(video, "source_color_transfer", ""))
    if kind in {"pq", "hlg"}:
        return kind
    sdr_base_dovi = str(getattr(video, "dovi_source_profile", "") or "") == "p8_2"
    if video.inject_hdr_meta or video.copy_hdr10plus or (video.copy_dv and not sdr_base_dovi):
        return "pq"
    return ""


def hdr_setparams_filter(video: VideoEncodeSettings) -> str:
    """Marquage couleur HDR posé sur les images (ffmpeg ≥ 7 ignore ``-color_*`` en sortie)."""
    transfer = output_hdr_transfer(video)
    if not transfer:
        return ""
    return (
        "setparams=color_primaries=bt2020:"
        f"color_trc={_VUI_TRANSFER[transfer]}:colorspace=bt2020nc:range=tv"
    )


def requests_hdr_metadata(video: VideoEncodeSettings) -> bool:
    if video.tonemap_to_sdr or not supports_hdr_output(video.codec):
        return False
    return bool(video.inject_hdr_meta or video.copy_dv or video.copy_hdr10plus)


# Encodeurs ffmpeg qui posent le HDR10 statique depuis les side data des images
# décodées : ces données sont perdues par le pipe y4m de l'interpolation RIFE.
_SIDE_DATA_HDR_HEVC_ENCODERS = frozenset({"hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_vaapi"})
_SIDE_DATA_HDR_AV1_ENCODERS = frozenset({"av1_nvenc", "av1_qsv", "av1_amf", "av1_vaapi"})

# Retrait des métadonnées HDR10 statiques des images : l'encodeur n'écrit plus celles
# de la source, les valeurs voulues sont posées en SEI après encodage.
_STRIP_STATIC_HDR_SIDE_DATA = (
    "sidedata=mode=delete:type=MASTERING_DISPLAY_METADATA,"
    "sidedata=mode=delete:type=CONTENT_LIGHT_LEVEL"
)


def needs_static_hdr_sei_reinjection(video: VideoEncodeSettings) -> bool:
    """Vrai si les SEI HDR10 statiques d'une piste HEVC sont posés après encodage.

    Encodeurs ffmpeg qui ne les écrivent que depuis les side data des images :
    interpolation (le pipe y4m les perd) ou valeurs éditables (hevc_nvenc, dont
    l'encodeur ignorerait les valeurs saisies au profit de celles de la source).
    """
    return (
        video.codec in _SIDE_DATA_HDR_HEVC_ENCODERS
        and should_reinject_static_hdr_metadata(video)
        and (video.interpolates() or supports_manual_static_hdr_metadata(video.codec))
    )


def strips_source_static_hdr_side_data(video: VideoEncodeSettings) -> bool:
    """Vrai si les métadonnées HDR10 statiques de la source sont retirées des images.

    Valeurs écrites explicitement (paramètres x265 / SVT-AV1) ou SEI réinjectés :
    sinon l'encodeur ou le conteneur (éléments Colour) reprend celles de la source,
    et les valeurs saisies ne seraient visibles que dans le flux.
    Case HDR10 décochée sur une sortie HDR : ffmpeg recopierait celles de la
    source (images et conteneur), elles sont donc retirées.
    """
    if not should_reinject_static_hdr_metadata(video):
        return bool(output_hdr_transfer(video)) and not video.inject_hdr_meta
    explicit = static_hdr_metadata_mode(video.codec) in {
        StaticHdrMetadataMode.X265_PARAMS,
        StaticHdrMetadataMode.SVTAV1_PARAMS,
    }
    return explicit or needs_static_hdr_sei_reinjection(video)


def interpolated_static_hdr_lost(video: VideoEncodeSettings) -> bool:
    """Vrai si l'interpolation fait perdre un HDR10 statique impossible à réinjecter (AV1)."""
    return (
        video.interpolates()
        and video.codec in _SIDE_DATA_HDR_AV1_ENCODERS
        and bool(video.inject_hdr_meta)
        and requests_hdr_metadata(video)
    )


def should_reinject_static_hdr_metadata(video: VideoEncodeSettings) -> bool:
    """Vrai si le pipeline d'injection doit reposer des SEI HDR statiques.

    Ce chemin est utile dès qu'on passe par la pipeline de réinjection
    DoVi/HDR10+ : une conversion P5/P7→P8 ou certaines chaînes HEVC
    hardware peuvent perdre les SEI MDCV/CLL même si la source initiale
    les exposait correctement. L'injection est idempotente et ne duplique
    pas les SEI déjà présents.
    """
    # Case HDR10 statique décochée : aucune métadonnée statique, même si
    # des valeurs sont renseignées ou qu'un HDR dynamique est conservé.
    if not video.inject_hdr_meta or not requests_hdr_metadata(video):
        return False
    if video.codec == "copy":
        return bool(
            video.copy_dv
            and str(video.dovi_profile or "0").strip() == "2"
            and (video.master_display or video.max_cll)
        )
    return bool(video.master_display or video.max_cll)


# --- Paramètres avancés saisis à la main --------------------------------------
#
# Une option saisie est :
#   - retirée si elle est incompatible avec le workflow (mapping des pistes,
#     choix du codec, -vf quand le workflow pose déjà des filtres) ;
#   - appliquée avec un avertissement si elle remplace un réglage de l'onglet
#     Video (mode / valeur de qualité, preset, profil, pix_fmt) : ajoutée après
#     les options du workflow, sa dernière occurrence l'emporte ;
#   - appliquée sans message sinon (valeurs par défaut du workflow comprises,
#     ex. -async_depth).

# Codecs dont extra_params est une chaîne de paramètres de l'encodeur
# (``-x265-params`` / ``-svtav1-params``), pas des flags ffmpeg.
_PARAM_STRING_CODECS = frozenset({"libx265", "libsvtav1"})

# Toujours retirées : casseraient l'assemblage des pistes ou changeraient d'encodeur.
FFMPEG_WORKFLOW_OWNED_FLAGS: frozenset[str] = frozenset({"map", "c"})
# Retirées si le workflow pose déjà des filtres : ffmpeg ne garde que le dernier
# -vf, crop / upload matériel / suppression des side data seraient perdus.
_FFMPEG_FILTER_FLAGS = frozenset({"vf", "filter_complex", "lavfi"})
# Contrôle de débit : chaque mode qualité en pose au moins une.
_FFMPEG_RATE_CONTROL_FLAGS = frozenset({
    "rc", "rc_mode", "cq", "qp", "crf", "global_quality", "qp_i", "qp_p", "qp_b", "b", "q",
})
# Réglages de l'onglet Video : une saisie les remplace (avertissement).
_FFMPEG_UI_FLAGS = frozenset({"preset", "quality", "compression_level", "profile", "pix_fmt"})
_FFMPEG_FLAG_ALIASES = {"vcodec": "c", "codec": "c", "filter": "vf", "qscale": "q"}

_NUMERIC_TOKEN_RE = re.compile(r"-?\d+(?:[.,]\d+)?[kKmMgG]?")


@dataclass(frozen=True)
class ExtraParamsReport:
    """Tri des paramètres avancés saisis à la main."""

    kept: tuple[str, ...] = ()
    """Tokens ajoutés en fin de commande."""
    removed: tuple[str, ...] = ()
    """Tokens retirés (incompatibles avec le workflow)."""
    overriding: tuple[str, ...] = ()
    """Tokens conservés qui remplacent un réglage de l'onglet Video."""


def is_option_token(token: str) -> bool:
    """Vrai pour un flag (``-x`` / ``--x``), faux pour une valeur (y compris ``-1``)."""
    return token.startswith("-") and len(token) > 1 and not _NUMERIC_TOKEN_RE.fullmatch(token)


def option_items(tokens: list[str]) -> list[list[str]]:
    """Regroupe les tokens en options ``[flag]`` ou ``[flag, valeur]``."""
    items: list[list[str]] = []
    i = 0
    while i < len(tokens):
        if is_option_token(tokens[i]) and i + 1 < len(tokens) and not is_option_token(tokens[i + 1]):
            items.append(tokens[i:i + 2])
            i += 2
        else:
            items.append([tokens[i]])
            i += 1
    return items


def option_values(tokens: list[str], option_name: Callable[[str], str]) -> dict[str, str]:
    """Options d'une commande : ``{nom canonique: valeur}`` ("" pour un flag seul)."""
    values: dict[str, str] = {}
    for item in option_items(tokens):
        if is_option_token(item[0]):
            values[option_name(item[0])] = item[1] if len(item) > 1 else ""
    return values


def ffmpeg_option_name(token: str) -> str:
    """Nom canonique d'une option ffmpeg (``-b:v`` → ``b``, ``-vcodec`` → ``c``)."""
    name = token.lstrip("-").split(":", 1)[0]
    return _FFMPEG_FLAG_ALIASES.get(name, name)


def classify_user_args(
    user_args: list[str],
    base_args: list[str],
    *,
    option_name: Callable[[str], str],
    removed_names: frozenset[str] = frozenset(),
    removed_if_set: frozenset[str] = frozenset(),
    override_names: frozenset[str] = frozenset(),
    exclusive_groups: tuple[frozenset[str], ...] = (),
) -> ExtraParamsReport:
    """Trie les options saisies face aux options déjà posées par le workflow.

    - ``removed_names`` : toujours retirées ;
    - ``removed_if_set`` : retirées si le workflow pose la même option ;
    - ``override_names`` : conservées, signalées si le workflow pose la même
      option (ou un membre du même groupe exclusif).
    """
    base_names = {option_name(token) for token in base_args if is_option_token(token)}
    set_names = set(base_names)
    for group in exclusive_groups:
        if base_names & group:
            set_names |= group
    kept: list[str] = []
    removed: list[str] = []
    overriding: list[str] = []
    for item in option_items(user_args):
        name = option_name(item[0]) if is_option_token(item[0]) else None
        if name is not None and (name in removed_names or (name in removed_if_set and name in set_names)):
            removed.extend(item)
            continue
        kept.extend(item)
        if name is not None and name in set_names and name in override_names:
            overriding.extend(item)
    return ExtraParamsReport(tuple(kept), tuple(removed), tuple(overriding))


def classify_ffmpeg_extra_args(
    user_args: list[str],
    base_args: list[str],
    *,
    workflow_filters: bool,
) -> ExtraParamsReport:
    return classify_user_args(
        user_args,
        base_args,
        option_name=ffmpeg_option_name,
        removed_names=FFMPEG_WORKFLOW_OWNED_FLAGS | (_FFMPEG_FILTER_FLAGS if workflow_filters else frozenset()),
        override_names=_FFMPEG_RATE_CONTROL_FLAGS | _FFMPEG_UI_FLAGS,
        exclusive_groups=(_FFMPEG_RATE_CONTROL_FLAGS,),
    )


def ffmpeg_option_owned_by_workflow(name: str) -> bool:
    """Option ffmpeg toujours retirée des paramètres avancés (mapping, codec)."""
    return ffmpeg_option_name(name) in FFMPEG_WORKFLOW_OWNED_FLAGS


def _split_user_extras(video: VideoEncodeSettings, args: list[str]) -> tuple[list[str], list[str]]:
    """Sépare la fin de ``args`` (paramètres avancés bruts) du reste de la commande."""
    if video.codec in _PARAM_STRING_CODECS or video.codec == "copy":
        return args, []
    extras = ffmpeg_extra_args(video)
    if not extras or args[-len(extras):] != extras:
        return args, []
    return args[:-len(extras)], extras


def _classify_built_args(
    video: VideoEncodeSettings,
    args: list[str],
    callbacks: EncodeCodecDomainCallbacks,
) -> tuple[list[str], ExtraParamsReport]:
    base, extras = _split_user_extras(video, args)
    if not extras:
        return args, ExtraParamsReport()
    workflow_filters = bool(build_encoder_vf(video, callbacks=callbacks))
    kept: list[str] = []
    removed: list[str] = []
    for item in option_items(extras):
        name = ffmpeg_option_name(item[0])
        incompatible = name == "pix_fmt" and len(item) == 2 and _incompatible_pixel_format(video, item[1], callbacks)
        if incompatible:
            removed.extend(item)
        else:
            kept.extend(item)
    # Même sans conversion (donc sans -pix_fmt émis), une saisie remplace le
    # choix de profondeur de l'onglet Video : avertissement conservé.
    warning_base = [*base, "-pix_fmt", "yuv420p10le" if resolve_output_bit_depth(video) == 10 else "yuv420p"]
    report = classify_ffmpeg_extra_args(kept, warning_base, workflow_filters=workflow_filters)
    return base, ExtraParamsReport(report.kept, (*removed, *report.removed), report.overriding)


def _without_user_overrides(
    video: VideoEncodeSettings,
    args: list[str],
    callbacks: EncodeCodecDomainCallbacks,
) -> list[str]:
    base, report = _classify_built_args(video, args, callbacks)
    if base is args:
        return args
    return [*base, *report.kept]


def _raw_video_codec_args(video: VideoEncodeSettings, callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    if video.codec in _HW_FFMPEG_VIDEO_CODECS:
        return _hw_codec_args_raw(video, video.bitrate_kbps, callbacks=callbacks)
    spec, _quality = rate_control_values(video)
    if spec is None or spec.family == "crf":
        return _video_codec_args_crf_raw(video, callbacks=callbacks)
    return _video_codec_args_bitrate_raw(video, video.bitrate_kbps, callbacks=callbacks)


def ffmpeg_extra_params_report(
    video: VideoEncodeSettings,
    *,
    callbacks: EncodeCodecDomainCallbacks | None = None,
) -> ExtraParamsReport:
    """Tri des paramètres avancés ffmpeg (retirés, remplaçant l'onglet Video)."""
    if video.codec in NVENCC_VIDEO_CODECS:
        return ExtraParamsReport()
    callbacks = callbacks or EncodeCodecDomainCallbacks(platform=sys.platform)
    return _classify_built_args(video, _raw_video_codec_args(video, callbacks), callbacks)[1]


def ffmpeg_workflow_option_values(
    video: VideoEncodeSettings,
    *,
    callbacks: EncodeCodecDomainCallbacks | None = None,
) -> dict[str, str]:
    """Options posées par le workflow pour ces réglages (pré-remplissage de l'éditeur)."""
    if video.codec in NVENCC_VIDEO_CODECS or video.codec == "copy":
        return {}
    callbacks = callbacks or EncodeCodecDomainCallbacks(platform=sys.platform)
    bare = VideoEncodeSettings(**{**video.__dict__, "extra_params": ""})
    return option_values(_raw_video_codec_args(bare, callbacks), ffmpeg_option_name)


_ENCODER_PARAM_KEY_RE = re.compile(r"[A-Za-z0-9][\w.-]*")


def extra_params_syntax_error(codec: str, text: str) -> str:
    """Erreur de syntaxe des paramètres avancés pour ce codec ("" si valides)."""
    raw = (text or "").strip()
    codec = str(codec or "").strip().lower()
    if not raw or codec == "copy":
        return ""
    if codec in _PARAM_STRING_CODECS:
        name = "x265-params" if codec == "libx265" else "svtav1-params"
        for chunk in raw.strip(":").split(":"):
            chunk = chunk.strip()
            if not chunk:
                continue
            key = chunk.split("=", 1)[0]
            if any(char.isspace() for char in chunk) or not _ENCODER_PARAM_KEY_RE.fullmatch(key):
                return f"syntaxe {name} attendue (clé=valeur:clé=valeur), reçu « {chunk} »"
        return ""
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        return f"guillemets non fermés ({exc})"
    if tokens and not is_option_token(tokens[0]):
        return f"« {tokens[0]} » n'est précédé d'aucune option"
    for previous, token in zip(tokens, tokens[1:]):
        if not is_option_token(previous) and not is_option_token(token):
            return f"valeur « {token} » sans option"
    return ""


_HW_FFMPEG_VIDEO_CODECS = NVENC_VIDEO_CODECS | AMF_VIDEO_CODECS | QSV_VIDEO_CODECS | VAAPI_VIDEO_CODECS


def video_codec_args(video: VideoEncodeSettings, bitrate_kbps: int, *, callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    # NVEncC est un binaire externe (pas un encodeur ffmpeg) : la construction
    # de la commande passe par core/workflows/encode/runtime/nvencc.py. Côté
    # ffmpeg-only, on retourne une liste vide pour que le caller (workflow.py)
    # ait détecté NVEncC en amont et basculé sur le pipeline 3-process.
    if video.codec in NVENCC_VIDEO_CODECS:
        return []
    if video.codec in _HW_FFMPEG_VIDEO_CODECS:
        return _without_user_overrides(video, _hw_codec_args_raw(video, bitrate_kbps, callbacks=callbacks), callbacks)
    spec, _quality = rate_control_values(video)
    if spec is None or spec.family == "crf":
        return video_codec_args_crf(video, callbacks=callbacks)
    return video_codec_args_bitrate(video, bitrate_kbps, callbacks=callbacks)


def video_codec_args_crf(video: VideoEncodeSettings, *, callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    if video.codec in _HW_FFMPEG_VIDEO_CODECS:
        return video_codec_args(video, video.bitrate_kbps, callbacks=callbacks)
    return _without_user_overrides(video, _video_codec_args_crf_raw(video, callbacks=callbacks), callbacks)


def video_codec_args_bitrate(
    video: VideoEncodeSettings,
    bitrate_kbps: int,
    *,
    callbacks: EncodeCodecDomainCallbacks,
) -> list[str]:
    """Arguments pour un débit imposé (débit moyen, taille cible convertie en débit)."""
    if video.codec in _HW_FFMPEG_VIDEO_CODECS:
        return _without_user_overrides(video, _hw_codec_args_raw(video, bitrate_kbps, callbacks=callbacks), callbacks)
    return _without_user_overrides(
        video,
        _video_codec_args_bitrate_raw(video, bitrate_kbps, callbacks=callbacks),
        callbacks,
    )


def _hw_rate_control_args(video: VideoEncodeSettings, bitrate_kbps: int) -> list[str]:
    """Options de débit d'un encodeur matériel ffmpeg selon son mode (catalog.VIDEO_RATE_CONTROLS).

    Taille cible : une passe VBR plafonnée (``-pass`` est ignoré par ces encodeurs).
    """
    spec, quality = rate_control_values(video)
    rc_id = spec.rc_id if spec is not None else "vbr"
    q = str(quality)
    br = f"{int(bitrate_kbps)}k"
    peak = f"{int(bitrate_kbps * 1.5)}k"
    buffer = f"{int(bitrate_kbps) * 2}k"
    capped = ["-maxrate:v", peak, "-bufsize:v", buffer]
    codec = video.codec
    if codec in NVENC_VIDEO_CODECS:
        return {
            "vbr_cq": ["-rc:v", "vbr", "-b:v", "0", "-cq:v", q],
            "constqp": ["-rc:v", "constqp", "-qp", q],
            "vbr": ["-rc:v", "vbr", "-b:v", br],
            "cbr": ["-rc:v", "cbr", "-b:v", br],
        }.get(rc_id, ["-rc:v", "vbr", "-b:v", br, *capped, "-multipass", "fullres"])
    if codec in AMF_VIDEO_CODECS:
        if rc_id == "cqp":
            # hevc_amf n'expose pas -qp_b.
            return ["-rc", "cqp", "-qp_i", q, "-qp_p", q, *([] if codec == "hevc_amf" else ["-qp_b", q])]
        if rc_id == "qvbr":
            return ["-rc", "qvbr", "-qvbr_quality_level", q, "-b:v", br]
        if rc_id in {"vbr_peak", "hqvbr"}:
            return ["-rc", rc_id, "-b:v", br, "-maxrate:v", peak]
        if rc_id in {"vbr_latency", "cbr", "hqcbr"}:
            return ["-rc", rc_id, "-b:v", br]
        return ["-rc", "vbr_peak", "-b:v", br, *capped]
    if codec in QSV_VIDEO_CODECS:
        # Mode implicite : -global_quality seul = QP constant ; maxrate = débit → CBR.
        return {
            "cqp": ["-global_quality", q],
            "vbr": ["-b:v", br, "-maxrate:v", peak],
            "cbr": ["-b:v", br, "-maxrate:v", br],
        }.get(rc_id, ["-b:v", br, *capped])
    # VAAPI
    cqp_value = ["-global_quality", q] if codec == "av1_vaapi" else ["-qp", q]
    return {
        "cqp": ["-rc_mode", "CQP", *cqp_value],
        "icq": ["-rc_mode", "ICQ", "-global_quality", q],
        "qvbr": ["-rc_mode", "QVBR", "-b:v", br, "-global_quality", q],
        "vbr": ["-rc_mode", "VBR", "-b:v", br],
        "cbr": ["-rc_mode", "CBR", "-b:v", br],
        "avbr": ["-rc_mode", "AVBR", "-b:v", br],
    }.get(rc_id, ["-rc_mode", "VBR", "-b:v", br, *capped])


def _hw_codec_args_raw(
    video: VideoEncodeSettings,
    bitrate_kbps: int,
    *,
    callbacks: EncodeCodecDomainCallbacks,
) -> list[str]:
    """Codec, mode de débit, preset, périphérique, profondeur puis paramètres avancés."""
    codec = video.codec
    hw_frames = "-hwaccel_output_format" in hardware_input_args(video, callbacks=callbacks)
    hw_frames |= codec in AMF_VIDEO_CODECS and callbacks.platform == "win32" and callbacks.amf_device is not None
    depth = depth_args(video, hw_frames=hw_frames)
    rate = _hw_rate_control_args(video, bitrate_kbps)
    if codec in NVENC_VIDEO_CODECS:
        return [
            "-c:v", codec, *rate, "-preset:v", nvenc_effective_preset(video),
            *nvenc_device_args(callbacks),
            *depth,
            *ffmpeg_extra_args(video),
        ]
    if codec in AMF_VIDEO_CODECS:
        quality = ["-quality", video.preset] if video.preset in presets_for_codec(codec) else []
        return ["-c:v", codec, *rate, *quality, *depth, *ffmpeg_extra_args(video)]
    if codec in QSV_VIDEO_CODECS:
        preset = ["-preset", video.preset] if video.preset in presets_for_codec(codec) else []
        return ["-c:v", codec, *rate, "-async_depth", "4", *preset, *depth, *ffmpeg_extra_args(video)]
    return [
        "-c:v", codec, *rate,
        *vaapi_compression_args(video),
        "-async_depth", "4",
        *depth,
        *ffmpeg_extra_args(video),
    ]


def _video_codec_args_crf_raw(video: VideoEncodeSettings, *, callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    """Codecs logiciels en qualité constante (CRF)."""
    _ = callbacks
    crf = str(rate_control_values(video)[1])
    match video.codec:
        case "copy":
            return ["-c:v", "copy"]
        case "libx265":
            args = ["-c:v", "libx265", "-crf", crf, "-preset", video.preset]
            args.extend(depth_args(video))
            x265 = x265_params(video)
            if x265:
                args.extend(["-x265-params", x265])
            return args
        case "libx264":
            return [
                "-c:v", "libx264", "-crf", crf, "-preset", video.preset,
                *depth_args(video),
                *ffmpeg_extra_args(video),
            ]
        case "libsvtav1":
            args = ["-c:v", "libsvtav1", "-crf", crf, "-preset", video.preset]
            args.extend(depth_args(video))
            params = svtav1_params(video)
            if params:
                args.extend(["-svtav1-params", params])
            return args
        case _:
            return ["-c:v", video.codec, "-crf", crf]


def _video_codec_args_bitrate_raw(
    video: VideoEncodeSettings,
    bitrate_kbps: int,
    *,
    callbacks: EncodeCodecDomainCallbacks,
) -> list[str]:
    """Codecs logiciels à débit imposé (débit moyen, taille cible en 2 passes)."""
    _ = callbacks
    match video.codec:
        case "copy":
            return ["-c:v", "copy"]
        case "libx265":
            args = ["-c:v", "libx265", "-b:v", f"{bitrate_kbps}k", "-preset", video.preset]
            args.extend(depth_args(video))
            x265 = x265_params(video)
            if x265:
                args.extend(["-x265-params", x265])
            return args
        case "libx264":
            return [
                "-c:v", "libx264", "-b:v", f"{bitrate_kbps}k", "-preset", video.preset,
                *depth_args(video),
                *ffmpeg_extra_args(video),
            ]
        case "libsvtav1":
            args = ["-c:v", "libsvtav1", "-b:v", f"{bitrate_kbps}k", "-preset", video.preset]
            args.extend(depth_args(video))
            params = svtav1_params(video)
            if params:
                args.extend(["-svtav1-params", params])
            return args
        case _:
            return ["-c:v", video.codec, "-b:v", f"{bitrate_kbps}k"]


_RESIZE_PRESETS: dict[str, tuple[int, int, str]] = {
    "720p": (1280, 720, "720p"),
    "1080p": (1920, 1080, "1080p"),
    "1440p": (2560, 1440, "1440p"),
    "2160p": (3840, 2160, "2160p"),
}

_DEBLOCK_PRESETS: dict[str, tuple[str, float, float, float, float]] = {
    "ultralight": ("weak", 0.04, 0.03, 0.02, 0.02),
    "light": ("weak", 0.06, 0.04, 0.03, 0.03),
    "medium": ("strong", 0.08, 0.05, 0.04, 0.04),
    "strong": ("strong", 0.10, 0.06, 0.05, 0.05),
    "stronger": ("strong", 0.12, 0.07, 0.06, 0.05),
    "verystrong": ("strong", 0.14, 0.08, 0.07, 0.06),
}

_NLMEANS_PRESETS: dict[str, tuple[float, int, int]] = {
    "ultralight": (1.0, 3, 7),
    "light": (2.0, 5, 9),
    "medium": (3.0, 7, 11),
    "strong": (5.0, 7, 15),
}

# Périphérique Vulkan des filtres GPU (nlmeans_vulkan) ; aussi utilisé par libplacebo.
VULKAN_FILTER_DEVICE = "mre_vk"


def _vulkan_nlmeans_filter(video: VideoEncodeSettings, strength: float, patch: int, radius: int) -> str:
    """NLMeans sur le GPU, mêmes réglages que le CPU (sorties à ≈ 46-50 dB).

    10 bits envoyés en p010le (alignés en haut) : en yuv420p10le, nlmeans_vulkan
    lit des valeurs 64 fois plus petites et n'applique plus la force.
    ``t`` (parallélisme) choisi par le workflow selon la résolution
    (``vulkan_parallelism``) : la valeur par défaut de FFmpeg (8) épuise
    l'allocation dès la 4K ; la sortie ne dépend pas de ``t``.
    """
    ten_bit = bool(video.p5_to_hdr10) or not 0 < int(video.source_bit_depth or 0) <= 8
    upload, planar = ("p010le", "yuv420p10le") if ten_bit else ("yuv420p", "yuv420p")
    tail = f",format={planar}" if planar != upload else ""
    return (
        f"format={upload},hwupload,nlmeans_vulkan=s={strength}:p={patch}:r={radius}:"
        f"t={max(1, int(video.vulkan_parallelism or 1))},"
        f"hwdownload,format={upload}{tail}"
    )


def vulkan_filter_device_args(video: VideoEncodeSettings) -> list[str]:
    """Périphérique Vulkan de l'étage qui filtre (avant ``-i``) ; vide sans filtre Vulkan."""
    if video.codec == "copy" or not (video.nlmeans_vulkan and video.filters.nlmeans_enabled):
        return []
    index = str(video.vulkan_device or "").strip()
    device = f"vulkan={VULKAN_FILTER_DEVICE}" + (f":{index}" if index else "")
    return ["-init_hw_device", device, "-filter_hw_device", VULKAN_FILTER_DEVICE]


def vulkan_filters_compatible(video: VideoEncodeSettings, callbacks: EncodeCodecDomainCallbacks) -> bool:
    """Un seul ``-filter_hw_device`` par commande : VAAPI et AMF Windows (upload D3D11) gardent le leur."""
    if video.codec in VAAPI_VIDEO_CODECS:
        return False
    return not (video.codec in AMF_VIDEO_CODECS and callbacks.platform == "win32" and callbacks.amf_device is not None)


def nlmeans_settings(filters: VideoFilterSettings) -> tuple[str, float, int, int]:
    """Préréglage NLMeans, facteur de force du profil, patch et recherche (FFmpeg et NVEncC)."""
    preset = str(filters.nlmeans_strength or "light").strip().lower()
    if preset not in _NLMEANS_PRESETS:
        preset = "light"
    _strength, patch, radius = _NLMEANS_PRESETS[preset]
    profile = str(filters.nlmeans_profile or "").strip().lower()
    scale = 1.0
    if profile in {"grain", "animation"}:
        scale = 0.75
    elif profile in {"high motion", "highmotion", "sprite"}:
        radius = max(5, radius - 2)
    return preset, scale, patch, radius


_CHROMA_PRESETS: dict[str, tuple[int, int]] = {
    "ultralight": (12, 3),
    "light": (18, 5),
    "medium": (24, 5),
    "strong": (30, 7),
    "stronger": (36, 7),
    "verystrong": (42, 9),
}


def _sanitize_scale_algorithm(value: str) -> str:
    algo = str(value or "lanczos").strip().lower()
    return algo if algo in {"fast_bilinear", "bilinear", "bicubic", "neighbor", "area", "lanczos", "spline"} else "lanczos"


def _build_crop_filter(video: VideoEncodeSettings) -> str:
    crop = video.crop
    if not crop.is_active():
        return ""
    top = max(0, int(crop.top))
    bottom = max(0, int(crop.bottom))
    left = max(0, int(crop.left))
    right = max(0, int(crop.right))
    if crop.auto:
        # Autocrop detection is session-side; until detection fills numeric
        # offsets, command generation keeps the source untouched.
        return ""
    if crop.unit == "percent":
        return (
            "crop="
            f"iw*(100-{left}-{right})/100:"
            f"ih*(100-{top}-{bottom})/100:"
            f"iw*{left}/100:"
            f"ih*{top}/100"
        )
    return f"crop=iw-{left}-{right}:ih-{top}-{bottom}:{left}:{top}"


def resolve_resize_dimensions(src_w: int, src_h: int, resize: VideoResizeSettings) -> tuple[int, int]:
    """Dimensions de sortie calculées comme le filtre ``scale`` de ``_build_resize_filter``.

    ``src_w``/``src_h`` : image après recadrage. (0, 0) si la source est inconnue.
    """
    if src_w <= 0 or src_h <= 0:
        return (0, 0)
    if not resize.is_active():
        return (src_w, src_h)
    mode = str(resize.mode or "preset").strip().lower()
    if mode == "percent":
        pct = max(1, int(resize.percent or 100))
        if not bool(resize.allow_upscale):
            pct = min(pct, 100)
        return (int(src_w * pct / 100 / 2) * 2, int(src_h * pct / 100 / 2) * 2)
    if mode == "size":
        width = max(2, int(resize.width or 2))
        height = max(2, int(resize.height or 2))
    else:
        width, height, _label = _RESIZE_PRESETS.get(str(resize.preset or "720p"), _RESIZE_PRESETS["720p"])
    if not bool(resize.allow_upscale):
        width, height = min(width, src_w), min(height, src_h)
    if not resize.keep_aspect:
        return (width, height)
    # force_original_aspect_ratio=decrease + force_divisible_by=2 (libavfilter/scale_eval.c) :
    # chaque côté est ramené au multiple de 2 le plus proche du ratio source.
    fit_w = _rescale(height, src_w, src_h * 2) * 2
    fit_h = _rescale(width, src_h, src_w * 2) * 2
    return (min(fit_w, width) // 2 * 2, min(fit_h, height) // 2 * 2)


def _rescale(value: int, num: int, den: int) -> int:
    """``av_rescale`` : value × num / den arrondi au plus proche."""
    return (value * num + den // 2) // den


def _build_resize_filter(video: VideoEncodeSettings) -> str:
    resize = video.resize
    if not resize.is_active():
        return ""
    algo = _sanitize_scale_algorithm(resize.algorithm)
    mode = str(resize.mode or "preset").strip().lower()
    if mode == "percent":
        pct = max(1, int(resize.percent or 100))
        if not bool(resize.allow_upscale):
            pct = min(pct, 100)
        return f"scale=trunc(iw*{pct}/100/2)*2:trunc(ih*{pct}/100/2)*2:flags={algo}"
    if mode == "size":
        width = max(2, int(resize.width or 2))
        height = max(2, int(resize.height or 2))
    else:
        width, height, _label = _RESIZE_PRESETS.get(str(resize.preset or "720p"), _RESIZE_PRESETS["720p"])
    if not bool(resize.allow_upscale):
        # Virgule échappée : ffmpeg sépare les filtres sur ',' dans le filtergraph -vf.
        target_w = f"min({width}\\,iw)"
        target_h = f"min({height}\\,ih)"
    else:
        target_w = str(width)
        target_h = str(height)
    if resize.keep_aspect:
        return (
            f"scale={target_w}:{target_h}:"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2:flags={algo}"
        )
    return f"scale={target_w}:{target_h}:flags={algo}"


def _build_filters(video: VideoEncodeSettings) -> list[str]:
    filters = video.filters
    chain: list[str] = []
    if filters.yadif_enabled:
        mode = str(filters.yadif_mode or "send_frame").strip()
        parity = str(filters.yadif_parity or "auto").strip()
        deint = str(filters.yadif_deint or "all").strip()
        chain.append(f"yadif={mode}:{parity}:{deint}")
    crop = _build_crop_filter(video)
    if crop:
        chain.append(crop)
    if filters.deblock_enabled:
        kind, alpha, beta, gamma, delta = _DEBLOCK_PRESETS.get(
            str(filters.deblock_strength or "medium").strip().lower(),
            _DEBLOCK_PRESETS["medium"],
        )
        block = max(4, min(512, int(filters.deblock_block or 8)))
        chain.append(
            f"deblock=filter={kind}:block={block}:"
            f"alpha={alpha}:beta={beta}:gamma={gamma}:delta={delta}"
        )
    if filters.nlmeans_enabled:
        preset, scale, patch, radius = nlmeans_settings(filters)
        # ffmpeg nlmeans 's' a un minimum dur de 1.0 (ultralight*0.75=0.75 → erreur).
        strength = max(1.0, _NLMEANS_PRESETS[preset][0] * scale)
        if video.nlmeans_vulkan:
            chain.append(_vulkan_nlmeans_filter(video, strength, patch, radius))
        else:
            chain.append(f"nlmeans=s={strength}:p={patch}:r={radius}")
    if filters.chroma_smooth_enabled:
        thres, size = _CHROMA_PRESETS.get(
            str(filters.chroma_smooth_strength or "medium").strip().lower(),
            _CHROMA_PRESETS["medium"],
        )
        chain.append(
            f"chromanr=thres={thres}:sizew={size}:sizeh={size}:"
            "stepw=1:steph=1:distance=manhattan"
        )
    resize = _build_resize_filter(video)
    if resize:
        chain.append(resize)
    return chain


def build_encoder_vf(
    video: VideoEncodeSettings,
    *,
    callbacks: EncodeCodecDomainCallbacks,
    piped_frames: bool = False,
    hw_decoded: bool = True,
) -> str:
    """Chaîne ``-vf`` de l'encodeur.

    ``piped_frames`` : les trames arrivent déjà filtrées d'un pipe y4m
    (interpolation RIFE) — seul le suffixe format/upload matériel est produit.
    ``hw_decoded`` : faux si la vidéo provient d'une entrée sans ``-hwaccel``
    (entrée auxiliaire de décalage, autre source) — VAAPI exige alors un upload.
    """
    cpu_vf = "" if piped_frames else build_vf(video)
    metadata_filters = [
        # VUI posée sur les images (pipe y4m : posée par l'appelant avant l'upload).
        "" if piped_frames else hdr_setparams_filter(video),
        # Valeurs voulues écrites par l'encodeur ou réinjectées en SEI (un SEI déjà
        # présent n'est jamais remplacé), ou case HDR10 décochée : celles de la
        # source ne doivent pas suivre.
        _STRIP_STATIC_HDR_SIDE_DATA if strips_source_static_hdr_side_data(video) else "",
    ]
    vf = ",".join(part for part in (cpu_vf, *metadata_filters) if part)
    hw_frames = hw_decoded and "-hwaccel_output_format" in hardware_input_args(
        video, callbacks=callbacks, piped_frames=piped_frames,
    )
    if hw_frames:
        conversion = _hardware_depth_filter(video, callbacks)
        return ",".join(part for part in (vf, conversion) if part)
    target = resolve_output_bit_depth(video)
    if video.codec in VAAPI_VIDEO_CODECS or (
        video.codec in AMF_VIDEO_CODECS and callbacks.platform == "win32" and callbacks.amf_device is not None
    ):
        upload = f"format={'p010' if target == 10 else 'nv12'},hwupload"
        return f"{vf},{upload}" if vf else upload
    # Une entrée auxiliaire ou un pipe n'a pas les surfaces GPU prévues par
    # les options du codec : convertir ici plutôt que laisser l'encodeur deviner.
    if (piped_frames or not hw_decoded) and not frames_match_target(video, target):
        fmt = "yuv420p10le" if target == 10 else "yuv420p"
        vf = ",".join(part for part in (vf, f"format={fmt}") if part)
    return vf


# Source Dolby Vision P5 : couleurs IPT converties en HDR10 BT.2020/PQ plage
# limitée, sans compression des hautes lumières. Images logicielles : libplacebo
# crée son propre périphérique Vulkan, sans ``-filter_hw_device`` global qui
# entrerait en conflit avec celui d'un encodeur AMF / VAAPI / QSV.
P5_TO_HDR10_FILTER = (
    "libplacebo=apply_dolbyvision=true:"
    "color_primaries=bt2020:color_trc=smpte2084:"
    "colorspace=bt2020nc:range=tv:"
    "tonemapping=clip:peak_detect=false:gamut_mode=clip:"
    "format=yuv420p10le"
)


def build_vf(video: VideoEncodeSettings) -> str:
    if video.codec == "copy":
        return ""
    chain: list[str] = []
    if bool(getattr(video, "p5_to_hdr10", False)):
        chain.append(P5_TO_HDR10_FILTER)
    chain.extend(_build_filters(video))
    if not video.tonemap_to_sdr:
        return ",".join(chain)
    algo = video.tonemap_algorithm or "hable"
    chain.append(
        "zscale=transfer=linear:npl=100,"
        "format=gbrpf32le,"
        "zscale=primaries=bt709,"
        f"tonemap=tonemap={algo}:desat=0,"
        "zscale=transfer=bt709:matrix=bt709:range=tv,"
        "format=yuv420p,"
        # Les métadonnées HDR de frame traversent tonemap : l'encodeur les
        # réécrirait en SEI dans un flux SDR.
        "sidedata=mode=delete:type=MASTERING_DISPLAY_METADATA,"
        "sidedata=mode=delete:type=CONTENT_LIGHT_LEVEL,"
        "sidedata=mode=delete:type=DYNAMIC_HDR_PLUS"
    )
    return ",".join(chain)


def _hardware_depth_filter(video: VideoEncodeSettings, callbacks: EncodeCodecDomainCallbacks) -> str:
    """Convertit la profondeur sur GPU seulement si le filtre a été détecté."""
    if video.source_bit_depth == resolve_output_bit_depth(video):
        return ""
    # QSV : vpp_qsv / scale_qsv ``format=`` échouent (« Error creating frames_ctx
    # for output pad », UHD 630, FFmpeg 2026-10, enfants D3D11 et DXVA2) :
    # conversion logicielle (décodage CPU + -pix_fmt), seule voie vérifiée.
    name = (
        "scale_cuda" if video.codec in NVENC_VIDEO_CODECS else
        "scale_vaapi" if video.codec in VAAPI_VIDEO_CODECS else ""
    )
    if name not in callbacks.depth_conversion_filters or not video.source_bit_depth:
        return ""
    return f"{name}=format={'p010le' if resolve_output_bit_depth(video) == 10 else 'nv12'}"


def hardware_input_args(
    video: VideoEncodeSettings,
    *,
    callbacks: EncodeCodecDomainCallbacks,
    piped_frames: bool = False,
) -> list[str]:
    """Options d'entrée matérielles (périphérique encodeur, décodage HW).

    ``piped_frames`` : entrée y4m logicielle — périphérique encodeur seul, sans
    ``-hwaccel`` ni filtre P5 (portés par l'étage de décodage).
    """
    # Étage qui filtre : périphérique Vulkan des filtres GPU (jamais l'étage encodeur d'un pipe).
    args: list[str] = [] if piped_frames else vulkan_filter_device_args(video)
    tonemap = bool(video.tonemap_to_sdr)
    software_filtering = has_cpu_video_filter(video) or piped_frames or video.interpolates()
    # H.264 High10 est décodé en logiciel : FFmpeg peut se replier seul,
    # mais un scale_cuda ou un encodeur VAAPI recevrait alors des images CPU.
    software_filtering |= video.source_codec in {"h264", "avc"} and video.source_bit_depth > 8
    # 4:2:2 / 4:4:4 / 12 bits : rarement décodés par le GPU, jamais encodés en
    # Main10 / High10 tels quels ; conversion 4:2:0 logicielle (-pix_fmt).
    software_filtering |= not source_is_420(video)
    # Une saisie -pix_fmt est un format en mémoire système. Éviter une
    # conversion implicite impossible depuis les surfaces CUDA/QSV/D3D11.
    software_filtering |= any(
        ffmpeg_option_name(item[0]) == "pix_fmt" and len(item) == 2
        and not _incompatible_pixel_format(video, item[1], callbacks)
        and item[1] not in {"cuda", "qsv", "vaapi", "d3d11"}
        for item in option_items(ffmpeg_extra_args(video))
    )
    depth_conversion = video.source_bit_depth != resolve_output_bit_depth(video)
    software_filtering |= depth_conversion and not bool(_hardware_depth_filter(video, callbacks))

    if video.codec in VAAPI_VIDEO_CODECS:
        if callbacks.vaapi_device:
            args.extend(["-vaapi_device", callbacks.vaapi_device])
            if not software_filtering and not tonemap:
                args.extend(["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"])
        return args

    if video.codec in QSV_VIDEO_CODECS:
        if callbacks.qsv_device:
            args.extend(["-qsv_device", callbacks.qsv_device])
        if software_filtering or tonemap:
            return args
        args.extend(["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"])
        return args

    if video.codec in AMF_VIDEO_CODECS and callbacks.platform == "win32":
        if callbacks.amf_device:
            args.extend([
                "-init_hw_device", f"d3d11va=mre_amf:{callbacks.amf_device}",
                "-filter_hw_device", "mre_amf",
            ])
        if software_filtering or tonemap:
            return args
        if callbacks.amf_device:
            args.extend([
                "-hwaccel", "d3d11va",
                "-hwaccel_device", "mre_amf",
                "-hwaccel_output_format", "d3d11",
            ])
        else:
            args.extend(["-hwaccel", "d3d11va", "-hwaccel_output_format", "d3d11"])
        return args

    if software_filtering or tonemap:
        return args

    if video.codec in NVENC_VIDEO_CODECS:
        if callbacks.platform == "win32" and callbacks.nvenc_device:
            args.extend(["-hwaccel_device", callbacks.nvenc_device])
        args.extend(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])

    return args


def nvenc_device_args(callbacks: EncodeCodecDomainCallbacks) -> list[str]:
    if callbacks.platform != "win32" or not callbacks.nvenc_device:
        return []
    return ["-gpu", callbacks.nvenc_device]


def hdr_meta_args(video: VideoEncodeSettings) -> list[str]:
    if video.codec == "copy" or not supports_hdr_output(video.codec):
        return []
    # VUI tagging — placés en options output (après -c:v) pour qu'ffmpeg les
    # attache au flux encodé et non au décodeur d'entrée. Couvre tous les
    # encoders HEVC/AV1 (libx265, libsvtav1, libaom-av1, hevc_nvenc, av1_nvenc,
    # hevc_amf, av1_amf, hevc_qsv, av1_qsv, hevc_vaapi).
    # `bt2020nc` == `bt2020_ncl` (matrix non-constant luminance, requis HDR10/DV).
    # `-color_range tv` : HDR10/HDR10+/DoVi sont toujours limited range (16-235) ;
    # sans ce flag certains players/TV interprètent en full range → couleurs lavées.
    args = [
        "-color_primaries", "bt2020",
        "-color_trc",       _VUI_TRANSFER.get(output_hdr_transfer(video), "smpte2084"),
        "-colorspace",      "bt2020nc",
        "-color_range",     "tv",
    ]
    # SEI HDR explicites côté hevc_vaapi (default `hdr+a53_cc`, on force pour être
    # robuste si l'utilisateur passe des extra_params qui changeraient le défaut).
    if video.codec == "hevc_vaapi":
        args.extend(["-sei", "+hdr"])
    # IMPORTANT — Limitation ffmpeg (≤ 7.1) :
    # ffmpeg n'expose AUCUNE option globale `-master_display` / `-max_cll` côté
    # output. Les seules voies fiables pour obtenir les SEI MDCV/CLL :
    #   - libx265 : via `-x265-params master-display=...:max-cll=...`
    #     (déjà géré par x265_params() ci-dessus, branche écrite par la
    #     génération `video_codec_args`).
    #   - libsvtav1 : via `-svtav1-params mastering-display=...:content-light=...`
    #     (svtav1_params(), conversion vers le format décimal SVT-AV1).
    #   - hevc_vaapi : `-sei +hdr` (par défaut) sérialise les side_data HDR.
    #   - hevc_amf / hevc_qsv : support natif via side_data AVFrame déjà
    #     présents sur la source, mais pas de voie CLI fiable pour éditer
    #     manuellement master_display / max_cll.
    #   - hevc_nvenc : selon le build FFmpeg/NVENC, la voie native reste
    #     incomplète ; le workflow garde un fallback bitstream dédié en
    #     dernier recours.
    return args


def needs_hdr_vui(video: VideoEncodeSettings) -> bool:
    """Vrai si la sortie nécessite le tagging VUI bt2020 + PQ / HLG.

    DoVi P8 RPU et HDR10+ s'appuient sur une base layer correctement taggée
    (bt2020 / smpte2084 / bt2020nc) — sans ces VUI les TV appliquent un
    tone-mapping bt709 incorrect même quand le RPU est ré-injecté ensuite.
    Une sortie HDR sans métadonnées statiques reste aussi taggée.
    """
    return bool(output_hdr_transfer(video))


def audio_codec_args(out_idx: int, audio: AudioTrackSettings) -> list[str]:
    args: list[str] = []
    needs_downmix = needs_ac3_51_downmix(audio)
    bitrate_kbps = normalize_audio_bitrate_kbps(
        audio.codec,
        audio.bitrate_kbps,
        audio.input_channels,
        None,
        audio.input_channel_layout,
    )
    match audio.codec:
        case "copy":
            args.extend([f"-c:a:{out_idx}", "copy"])
            if audio.extract_truehd_core:
                args.extend([f"-bsf:a:{out_idx}", "truehd_core"])
        case "aac":
            args.extend([f"-c:a:{out_idx}", "aac", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
        case "ac3":
            args.extend([f"-c:a:{out_idx}", "ac3", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
        case "eac3":
            args.extend([f"-c:a:{out_idx}", "eac3", f"-b:a:{out_idx}", f"{bitrate_kbps}k"])
        case "flac":
            args.extend([f"-c:a:{out_idx}", "flac"])
        case _:
            args.extend([f"-c:a:{out_idx}", audio.codec])
    if needs_downmix:
        args.extend([f"-ac:a:{out_idx}", "6", f"-channel_layout:a:{out_idx}", "5.1"])
    return args


def needs_ac3_51_downmix(audio: AudioTrackSettings) -> bool:
    if audio.codec not in {"ac3", "eac3"}:
        return False
    if (audio.input_channels or 0) >= 8:
        return True
    layout = (audio.input_channel_layout or "").lower()
    return "7.1" in layout
