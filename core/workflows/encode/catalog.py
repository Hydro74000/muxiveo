"""Shared codec catalogue for encode workflow, hardware detection and UI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

SOFTWARE_VIDEO_CODECS: list[tuple[str, str]] = [
    ("libx265", "x265 — HEVC (logiciel)"),
    ("libx264", "x264 — H.264 (logiciel)"),
    ("libsvtav1", "SVT-AV1 (logiciel)"),
]

HARDWARE_VIDEO_CODECS: list[tuple[str, str]] = [
    ("hevc_nvenc", "NVENC — HEVC (NVIDIA)"),
    ("hevc_amf", "AMF — HEVC (AMD-WIN)"),
    ("hevc_vaapi", "VAAPI — HEVC (AMD)"),
    ("hevc_qsv", "QSV — HEVC (Intel)"),
    ("h264_nvenc", "NVENC — H.264 (NVIDIA)"),
    ("h264_amf", "AMF — H.264 (AMD-WIN)"),
    ("h264_vaapi", "VAAPI — H.264 (AMD)"),
    ("h264_qsv", "QSV — H.264 (Intel)"),
    ("av1_nvenc", "NVENC — AV1 (NVIDIA RTX 40+)"),
    ("av1_amf", "AMF — AV1 (AMD RX 7000+)"),
    ("av1_vaapi", "VAAPI — AV1 (AMD/Intel)"),
    ("av1_qsv", "QSV — AV1 (Intel Arc/12e gen+)"),
    ("nvencc_hevc", "NVEncC — HEVC (NVIDIA, rigaya)"),
    ("nvencc_h264", "NVEncC — H.264 (NVIDIA, rigaya)"),
    ("nvencc_av1", "NVEncC — AV1 (NVIDIA RTX 40+, rigaya)"),
]

AUDIO_CODECS: list[tuple[str, str]] = [
    ("copy", "Copie (sans réencodage)"),
    ("aac", "AAC"),
    ("ac3", "AC-3 (Dolby Digital)"),
    ("eac3", "EAC-3 (Dolby Digital+)"),
    ("flac", "FLAC (sans perte)"),
]

X265_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow", "placebo",
]
X264_PRESETS = X265_PRESETS
SVTAV1_PRESETS = [str(i) for i in range(13)]
NVENC_PRESETS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7", "slow", "medium", "fast", "hp", "hq"]
# The experimental logical preset "safe" stays implemented in code for traceability
# but is no longer exposed in the normal NVENC workflow/UI.
HEVC_NVENC_PRESETS = [*NVENC_PRESETS]
VAAPI_PRESETS = [str(i) for i in range(8)]
QSV_PRESETS = ["veryslow", "slower", "slow", "medium", "fast", "faster", "veryfast"]
AMF_PRESETS = ["quality", "balanced", "speed"]
NVENCC_PRESETS = ["default", "performance", "quality",
                  "P1", "P2", "P3", "P4", "P5", "P6", "P7"]

TONEMAP_ALGORITHMS = ["hable", "mobius", "reinhard", "gamma", "linear", "clip"]

NVENC_VIDEO_CODECS: frozenset[str] = frozenset({"hevc_nvenc", "h264_nvenc", "av1_nvenc"})
AMF_VIDEO_CODECS: frozenset[str] = frozenset({"hevc_amf", "h264_amf", "av1_amf"})
QSV_VIDEO_CODECS: frozenset[str] = frozenset({"hevc_qsv", "h264_qsv", "av1_qsv"})
VAAPI_VIDEO_CODECS: frozenset[str] = frozenset({"hevc_vaapi", "h264_vaapi", "av1_vaapi"})
NVENCC_VIDEO_CODECS: frozenset[str] = frozenset({"nvencc_hevc", "nvencc_h264", "nvencc_av1"})
H264_VIDEO_CODECS: frozenset[str] = frozenset({
    "libx264", "h264_nvenc", "h264_amf", "h264_qsv", "h264_vaapi", "nvencc_h264",
})
CQ_CAPABLE_VIDEO_CODECS: frozenset[str] = (
    NVENC_VIDEO_CODECS | AMF_VIDEO_CODECS | QSV_VIDEO_CODECS | VAAPI_VIDEO_CODECS | NVENCC_VIDEO_CODECS
)
VIDEO_ENCODER_BADGES: dict[str, str] = {
    "libx265": "x265",
    "libx264": "x264",
    "libsvtav1": "SVT-AV1",
    "hevc_nvenc": "NVENC",
    "h264_nvenc": "NVENC",
    "av1_nvenc": "NVENC",
    "hevc_amf": "AMF",
    "h264_amf": "AMF",
    "av1_amf": "AMF",
    "hevc_qsv": "QSV",
    "h264_qsv": "QSV",
    "av1_qsv": "QSV",
    "hevc_vaapi": "VAAPI",
    "h264_vaapi": "VAAPI",
    "av1_vaapi": "VAAPI",
    "nvencc_hevc": "NVEncC",
    "nvencc_h264": "NVEncC",
    "nvencc_av1": "NVEncC",
}
VIDEO_HDR_BADGE_ORDER: tuple[str, ...] = ("HDR", "HLG", "DV", "10+", "SDR")


class VideoCodecFamily(str, Enum):
    SOFTWARE = "software"
    NVENC = "nvenc"
    AMF = "amf"
    QSV = "qsv"
    VAAPI = "vaapi"
    NVENCC = "nvencc"
    OTHER = "other"


class StaticHdrMetadataMode(str, Enum):
    NONE = "none"
    NATIVE = "native"
    X265_PARAMS = "x265_params"
    SVTAV1_PARAMS = "svtav1_params"
    FRAME_SIDE_DATA = "frame_side_data"
    VAAPI_SEI = "vaapi_sei"
    BITSTREAM_PATCH = "bitstream_patch"


@dataclass(frozen=True)
class VideoCodecHdrCapabilities:
    """Compatibilité HDR d'un codec vidéo cible.

    Source unique consultée par le workflow (construction des commandes,
    validation) et par l'UI (activation des options HDR).
    """
    hdr: bool = False
    """Le flux de sortie peut être HDR (VUI PQ/HLG, case HDR10 statique)."""
    static_mode: StaticHdrMetadataMode = StaticHdrMetadataMode.NONE
    """Voie d'écriture des métadonnées statiques MDCV/CLL."""
    manual_static: bool = False
    """master-display / max-cll éditables manuellement."""
    dovi: bool = False
    hdr10plus: bool = False

    @property
    def dynamic(self) -> bool:
        return self.dovi or self.hdr10plus


@dataclass(frozen=True)
class VideoCodecSpec:
    codec_id: str
    label: str
    family: VideoCodecFamily
    presets: tuple[str, ...]
    encoder_badge: str
    is_h264: bool = False
    supports_force_8bit: bool = False
    supports_10bit: bool = False

    @property
    def is_hardware(self) -> bool:
        return self.family is not VideoCodecFamily.SOFTWARE

    @property
    def hdr(self) -> "VideoCodecHdrCapabilities":
        return hdr_capabilities(self.codec_id)

    @property
    def supports_dynamic_hdr(self) -> bool:
        return self.hdr.dynamic

    @property
    def supports_dovi(self) -> bool:
        return self.hdr.dovi

    @property
    def supports_hdr10plus(self) -> bool:
        return self.hdr.hdr10plus


@dataclass(frozen=True)
class AudioCodecSpec:
    codec_id: str
    label: str
    passthrough: bool = False
    lossless: bool = False
    supports_bitrate: bool = True
    supports_truehd_core_bsf: bool = False


VIDEO_CODEC_SPECS: dict[str, VideoCodecSpec] = {
    "libx265": VideoCodecSpec(
        codec_id="libx265",
        label="x265 — HEVC (logiciel)",
        family=VideoCodecFamily.SOFTWARE,
        presets=tuple(X265_PRESETS),
        encoder_badge="x265",
        supports_10bit=True,
    ),
    "libx264": VideoCodecSpec(
        codec_id="libx264",
        label="x264 — H.264 (logiciel)",
        family=VideoCodecFamily.SOFTWARE,
        presets=tuple(X264_PRESETS),
        encoder_badge="x264",
        is_h264=True,
        supports_force_8bit=True,
        supports_10bit=True,
    ),
    "libsvtav1": VideoCodecSpec(
        codec_id="libsvtav1",
        label="SVT-AV1 (logiciel)",
        family=VideoCodecFamily.SOFTWARE,
        presets=tuple(SVTAV1_PRESETS),
        encoder_badge="SVT-AV1",
        supports_10bit=True,
    ),
    "hevc_nvenc": VideoCodecSpec(
        codec_id="hevc_nvenc",
        label="NVENC — HEVC (NVIDIA)",
        family=VideoCodecFamily.NVENC,
        presets=tuple(HEVC_NVENC_PRESETS),
        encoder_badge="NVENC",
        supports_10bit=True,
    ),
    "hevc_amf": VideoCodecSpec(
        codec_id="hevc_amf",
        label="AMF — HEVC (AMD-WIN)",
        family=VideoCodecFamily.AMF,
        presets=tuple(AMF_PRESETS),
        encoder_badge="AMF",
        supports_10bit=True,
    ),
    "hevc_vaapi": VideoCodecSpec(
        codec_id="hevc_vaapi",
        label="VAAPI — HEVC (AMD)",
        family=VideoCodecFamily.VAAPI,
        presets=tuple(VAAPI_PRESETS),
        encoder_badge="VAAPI",
        supports_10bit=True,
    ),
    "hevc_qsv": VideoCodecSpec(
        codec_id="hevc_qsv",
        label="QSV — HEVC (Intel)",
        family=VideoCodecFamily.QSV,
        presets=tuple(QSV_PRESETS),
        encoder_badge="QSV",
        supports_10bit=True,
    ),
    "h264_nvenc": VideoCodecSpec(
        codec_id="h264_nvenc",
        label="NVENC — H.264 (NVIDIA)",
        family=VideoCodecFamily.NVENC,
        presets=tuple(NVENC_PRESETS),
        encoder_badge="NVENC",
        is_h264=True,
        supports_force_8bit=True,
    ),
    "h264_amf": VideoCodecSpec(
        codec_id="h264_amf",
        label="AMF — H.264 (AMD-WIN)",
        family=VideoCodecFamily.AMF,
        presets=tuple(AMF_PRESETS),
        encoder_badge="AMF",
        is_h264=True,
        supports_force_8bit=True,
    ),
    "h264_vaapi": VideoCodecSpec(
        codec_id="h264_vaapi",
        label="VAAPI — H.264 (AMD)",
        family=VideoCodecFamily.VAAPI,
        presets=tuple(VAAPI_PRESETS),
        encoder_badge="VAAPI",
        is_h264=True,
        supports_force_8bit=True,
    ),
    "h264_qsv": VideoCodecSpec(
        codec_id="h264_qsv",
        label="QSV — H.264 (Intel)",
        family=VideoCodecFamily.QSV,
        presets=tuple(QSV_PRESETS),
        encoder_badge="QSV",
        is_h264=True,
        supports_force_8bit=True,
    ),
    "av1_nvenc": VideoCodecSpec(
        codec_id="av1_nvenc",
        label="NVENC — AV1 (NVIDIA RTX 40+)",
        family=VideoCodecFamily.NVENC,
        presets=tuple(NVENC_PRESETS),
        encoder_badge="NVENC",
        supports_10bit=True,
    ),
    "av1_amf": VideoCodecSpec(
        codec_id="av1_amf",
        label="AMF — AV1 (AMD RX 7000+)",
        family=VideoCodecFamily.AMF,
        presets=tuple(AMF_PRESETS),
        encoder_badge="AMF",
        supports_10bit=True,
    ),
    "av1_vaapi": VideoCodecSpec(
        codec_id="av1_vaapi",
        label="VAAPI — AV1 (AMD/Intel)",
        family=VideoCodecFamily.VAAPI,
        presets=tuple(VAAPI_PRESETS),
        encoder_badge="VAAPI",
        supports_10bit=True,
    ),
    "av1_qsv": VideoCodecSpec(
        codec_id="av1_qsv",
        label="QSV — AV1 (Intel Arc/12e gen+)",
        family=VideoCodecFamily.QSV,
        presets=tuple(QSV_PRESETS),
        encoder_badge="QSV",
        supports_10bit=True,
    ),
    "nvencc_hevc": VideoCodecSpec(
        codec_id="nvencc_hevc",
        label="NVEncC — HEVC (NVIDIA, rigaya)",
        family=VideoCodecFamily.NVENCC,
        presets=tuple(NVENCC_PRESETS),
        encoder_badge="NVEncC",
        supports_10bit=True,
    ),
    "nvencc_h264": VideoCodecSpec(
        codec_id="nvencc_h264",
        label="NVEncC — H.264 (NVIDIA, rigaya)",
        family=VideoCodecFamily.NVENCC,
        presets=tuple(NVENCC_PRESETS),
        encoder_badge="NVEncC",
        is_h264=True,
        supports_force_8bit=True,
        supports_10bit=True,
    ),
    "nvencc_av1": VideoCodecSpec(
        codec_id="nvencc_av1",
        label="NVEncC — AV1 (NVIDIA RTX 40+, rigaya)",
        family=VideoCodecFamily.NVENCC,
        presets=tuple(NVENCC_PRESETS),
        encoder_badge="NVEncC",
        supports_10bit=True,
    ),
}

VIDEO_CODEC_FAMILY_MAP: dict[str, VideoCodecFamily] = {
    codec_id: spec.family for codec_id, spec in VIDEO_CODEC_SPECS.items()
}

AUDIO_CODEC_SPECS: dict[str, AudioCodecSpec] = {
    "copy": AudioCodecSpec(
        codec_id="copy",
        label="Copie (sans réencodage)",
        passthrough=True,
        supports_bitrate=False,
        supports_truehd_core_bsf=True,
    ),
    "aac": AudioCodecSpec(codec_id="aac", label="AAC"),
    "ac3": AudioCodecSpec(codec_id="ac3", label="AC-3 (Dolby Digital)"),
    "eac3": AudioCodecSpec(codec_id="eac3", label="EAC-3 (Dolby Digital+)"),
    "flac": AudioCodecSpec(
        codec_id="flac",
        label="FLAC (sans perte)",
        lossless=True,
        supports_bitrate=False,
    ),
}

_HDR = VideoCodecHdrCapabilities
_SDR_ONLY = VideoCodecHdrCapabilities()
_MODE = StaticHdrMetadataMode

# Table de compatibilité HDR par codec cible. Un codec absent est traité
# comme SDR uniquement : aucune option HDR ne lui est transmise.
#   - copy   : conserve la source telle quelle (normalisation DoVi possible).
#   - H.264  : SDR uniquement (pas de signalisation HDR10/DoVi/HDR10+).
#   - DoVi   : libx265 / NVEncC HEVC (RPU synchronisé) ; FFmpeg NVENC/AMF/
#              QSV/VAAPI cassent la synchro RPU/DPB.
VIDEO_CODEC_HDR_CAPABILITIES: dict[str, VideoCodecHdrCapabilities] = {
    "copy": _HDR(hdr=True, dovi=True, hdr10plus=True),
    "libx265": _HDR(hdr=True, static_mode=_MODE.X265_PARAMS, manual_static=True, dovi=True, hdr10plus=True),
    "libx264": _SDR_ONLY,
    "libsvtav1": _HDR(hdr=True, static_mode=_MODE.SVTAV1_PARAMS, manual_static=True),
    "hevc_nvenc": _HDR(hdr=True, static_mode=_MODE.BITSTREAM_PATCH, manual_static=True, hdr10plus=True),
    "hevc_amf": _HDR(hdr=True, static_mode=_MODE.FRAME_SIDE_DATA, hdr10plus=True),
    "hevc_vaapi": _HDR(hdr=True, static_mode=_MODE.VAAPI_SEI, hdr10plus=True),
    "hevc_qsv": _HDR(hdr=True, static_mode=_MODE.FRAME_SIDE_DATA, hdr10plus=True),
    "h264_nvenc": _SDR_ONLY,
    "h264_amf": _SDR_ONLY,
    "h264_vaapi": _SDR_ONLY,
    "h264_qsv": _SDR_ONLY,
    "av1_nvenc": _HDR(hdr=True),
    "av1_amf": _HDR(hdr=True),
    "av1_vaapi": _HDR(hdr=True),
    "av1_qsv": _HDR(hdr=True),
    "nvencc_hevc": _HDR(hdr=True, static_mode=_MODE.NATIVE, manual_static=True, dovi=True, hdr10plus=True),
    "nvencc_h264": _SDR_ONLY,
    "nvencc_av1": _HDR(hdr=True, static_mode=_MODE.NATIVE, manual_static=True, hdr10plus=True),
}


def hdr_capabilities(codec: str | None) -> VideoCodecHdrCapabilities:
    """Capacités HDR du codec cible (SDR uniquement si inconnu)."""
    return VIDEO_CODEC_HDR_CAPABILITIES.get(str(codec or "").strip().lower(), _SDR_ONLY)


def _codecs_where(predicate) -> frozenset[str]:
    return frozenset(codec for codec, caps in VIDEO_CODEC_HDR_CAPABILITIES.items() if predicate(caps))


# Vues dérivées de la table (compatibilité des imports existants).
HDR_VIDEO_CODECS: frozenset[str] = _codecs_where(lambda caps: caps.hdr)
DOVI_VIDEO_CODECS: frozenset[str] = _codecs_where(lambda caps: caps.dovi)
HDR10PLUS_VIDEO_CODECS: frozenset[str] = _codecs_where(lambda caps: caps.hdr10plus)
DYNAMIC_HDR_VIDEO_CODECS: frozenset[str] = DOVI_VIDEO_CODECS | HDR10PLUS_VIDEO_CODECS
MANUAL_STATIC_HDR_METADATA_CODECS: frozenset[str] = _codecs_where(lambda caps: caps.manual_static)
STATIC_HDR_METADATA_MODE_BY_CODEC: dict[str, StaticHdrMetadataMode] = {
    codec: caps.static_mode
    for codec, caps in VIDEO_CODEC_HDR_CAPABILITIES.items()
    if caps.static_mode is not StaticHdrMetadataMode.NONE
}


def presets_for_codec(codec: str) -> list[str]:
    spec = video_codec_spec(codec)
    if spec is not None:
        return list(spec.presets)
    return X265_PRESETS


def is_h264_video_codec(codec: str) -> bool:
    spec = video_codec_spec(codec)
    return bool(spec is not None and spec.is_h264)


def supports_dynamic_hdr(codec: str) -> bool:
    return hdr_capabilities(codec).dynamic


def supports_dovi(codec: str) -> bool:
    return hdr_capabilities(codec).dovi


def supports_hdr10plus(codec: str) -> bool:
    return hdr_capabilities(codec).hdr10plus


def encoder_badge(codec: str) -> str:
    spec = video_codec_spec(codec)
    if spec is not None:
        return spec.encoder_badge
    normalized = str(codec or "").strip().lower()
    return VIDEO_ENCODER_BADGES.get(normalized, normalized.upper())


def video_codec_spec(codec: str) -> VideoCodecSpec | None:
    normalized = str(codec or "").strip().lower()
    return VIDEO_CODEC_SPECS.get(normalized)


def audio_codec_spec(codec: str) -> AudioCodecSpec | None:
    normalized = str(codec or "").strip().lower()
    return AUDIO_CODEC_SPECS.get(normalized)


def video_codec_family(codec: str) -> VideoCodecFamily:
    spec = video_codec_spec(codec)
    if spec is None:
        return VideoCodecFamily.OTHER
    return spec.family


def is_hardware_video_codec(codec: str) -> bool:
    spec = video_codec_spec(codec)
    return bool(spec is not None and spec.is_hardware)


def supports_hdr_output(codec: str) -> bool:
    """Vrai si le codec cible peut porter du HDR (``copy`` conserve la source)."""
    return hdr_capabilities(codec).hdr


def supports_force_8bit(codec: str) -> bool:
    spec = video_codec_spec(codec)
    return bool(spec is not None and spec.supports_force_8bit)


def supports_10bit(codec: str) -> bool:
    spec = video_codec_spec(codec)
    return bool(spec is not None and spec.supports_10bit)


def static_hdr_metadata_mode(codec: str) -> StaticHdrMetadataMode:
    return hdr_capabilities(codec).static_mode


def needs_static_hdr_bitstream_patch_codec(codec: str) -> bool:
    return static_hdr_metadata_mode(codec) is StaticHdrMetadataMode.BITSTREAM_PATCH


def supports_manual_static_hdr_metadata(codec: str) -> bool:
    return hdr_capabilities(codec).manual_static


__all__ = [
    "VideoCodecFamily",
    "StaticHdrMetadataMode",
    "VideoCodecSpec",
    "VideoCodecHdrCapabilities",
    "VIDEO_CODEC_HDR_CAPABILITIES",
    "HDR_VIDEO_CODECS",
    "hdr_capabilities",
    "AudioCodecSpec",
    "SOFTWARE_VIDEO_CODECS",
    "HARDWARE_VIDEO_CODECS",
    "AUDIO_CODECS",
    "X265_PRESETS",
    "X264_PRESETS",
    "SVTAV1_PRESETS",
    "NVENC_PRESETS",
    "HEVC_NVENC_PRESETS",
    "VAAPI_PRESETS",
    "QSV_PRESETS",
    "AMF_PRESETS",
    "NVENCC_PRESETS",
    "TONEMAP_ALGORITHMS",
    "NVENC_VIDEO_CODECS",
    "AMF_VIDEO_CODECS",
    "QSV_VIDEO_CODECS",
    "VAAPI_VIDEO_CODECS",
    "NVENCC_VIDEO_CODECS",
    "H264_VIDEO_CODECS",
    "CQ_CAPABLE_VIDEO_CODECS",
    "DOVI_VIDEO_CODECS",
    "HDR10PLUS_VIDEO_CODECS",
    "DYNAMIC_HDR_VIDEO_CODECS",
    "VIDEO_ENCODER_BADGES",
    "VIDEO_HDR_BADGE_ORDER",
    "VIDEO_CODEC_SPECS",
    "VIDEO_CODEC_FAMILY_MAP",
    "AUDIO_CODEC_SPECS",
    "STATIC_HDR_METADATA_MODE_BY_CODEC",
    "MANUAL_STATIC_HDR_METADATA_CODECS",
    "presets_for_codec",
    "is_h264_video_codec",
    "supports_hdr_output",
    "supports_dynamic_hdr",
    "supports_dovi",
    "supports_hdr10plus",
    "encoder_badge",
    "video_codec_spec",
    "audio_codec_spec",
    "video_codec_family",
    "is_hardware_video_codec",
    "supports_force_8bit",
    "supports_10bit",
    "static_hdr_metadata_mode",
    "needs_static_hdr_bitstream_patch_codec",
    "supports_manual_static_hdr_metadata",
]
