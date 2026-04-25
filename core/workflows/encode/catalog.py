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
VAAPI_PRESETS = [str(i) for i in range(8)]
QSV_PRESETS = ["veryslow", "slower", "slow", "medium", "fast", "faster", "veryfast"]
AMF_PRESETS = ["quality", "balanced", "speed"]

TONEMAP_ALGORITHMS = ["hable", "mobius", "reinhard", "gamma", "linear", "clip"]

NVENC_VIDEO_CODECS: frozenset[str] = frozenset({"hevc_nvenc", "h264_nvenc", "av1_nvenc"})
AMF_VIDEO_CODECS: frozenset[str] = frozenset({"hevc_amf", "h264_amf", "av1_amf"})
QSV_VIDEO_CODECS: frozenset[str] = frozenset({"hevc_qsv", "h264_qsv", "av1_qsv"})
VAAPI_VIDEO_CODECS: frozenset[str] = frozenset({"hevc_vaapi", "h264_vaapi", "av1_vaapi"})
H264_VIDEO_CODECS: frozenset[str] = frozenset({"libx264", "h264_nvenc", "h264_amf", "h264_qsv", "h264_vaapi"})
DYNAMIC_HDR_VIDEO_CODECS: frozenset[str] = frozenset({"copy", "libx265", "hevc_nvenc", "hevc_amf", "hevc_qsv", "hevc_vaapi"})

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
}
VIDEO_HDR_BADGE_ORDER: tuple[str, ...] = ("HDR", "HLG", "DV", "10+", "SDR")


class VideoCodecFamily(str, Enum):
    SOFTWARE = "software"
    NVENC = "nvenc"
    AMF = "amf"
    QSV = "qsv"
    VAAPI = "vaapi"
    OTHER = "other"


@dataclass(frozen=True)
class VideoCodecSpec:
    codec_id: str
    label: str
    family: VideoCodecFamily
    presets: tuple[str, ...]
    encoder_badge: str
    supports_dynamic_hdr: bool = False
    is_h264: bool = False
    supports_force_8bit: bool = False

    @property
    def is_hardware(self) -> bool:
        return self.family is not VideoCodecFamily.SOFTWARE


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
        supports_dynamic_hdr=True,
    ),
    "libx264": VideoCodecSpec(
        codec_id="libx264",
        label="x264 — H.264 (logiciel)",
        family=VideoCodecFamily.SOFTWARE,
        presets=tuple(X264_PRESETS),
        encoder_badge="x264",
        is_h264=True,
        supports_force_8bit=True,
    ),
    "libsvtav1": VideoCodecSpec(
        codec_id="libsvtav1",
        label="SVT-AV1 (logiciel)",
        family=VideoCodecFamily.SOFTWARE,
        presets=tuple(SVTAV1_PRESETS),
        encoder_badge="SVT-AV1",
    ),
    "hevc_nvenc": VideoCodecSpec(
        codec_id="hevc_nvenc",
        label="NVENC — HEVC (NVIDIA)",
        family=VideoCodecFamily.NVENC,
        presets=tuple(NVENC_PRESETS),
        encoder_badge="NVENC",
        supports_dynamic_hdr=True,
    ),
    "hevc_amf": VideoCodecSpec(
        codec_id="hevc_amf",
        label="AMF — HEVC (AMD-WIN)",
        family=VideoCodecFamily.AMF,
        presets=tuple(AMF_PRESETS),
        encoder_badge="AMF",
        supports_dynamic_hdr=True,
    ),
    "hevc_vaapi": VideoCodecSpec(
        codec_id="hevc_vaapi",
        label="VAAPI — HEVC (AMD)",
        family=VideoCodecFamily.VAAPI,
        presets=tuple(VAAPI_PRESETS),
        encoder_badge="VAAPI",
        supports_dynamic_hdr=True,
    ),
    "hevc_qsv": VideoCodecSpec(
        codec_id="hevc_qsv",
        label="QSV — HEVC (Intel)",
        family=VideoCodecFamily.QSV,
        presets=tuple(QSV_PRESETS),
        encoder_badge="QSV",
        supports_dynamic_hdr=True,
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
    ),
    "av1_amf": VideoCodecSpec(
        codec_id="av1_amf",
        label="AMF — AV1 (AMD RX 7000+)",
        family=VideoCodecFamily.AMF,
        presets=tuple(AMF_PRESETS),
        encoder_badge="AMF",
    ),
    "av1_vaapi": VideoCodecSpec(
        codec_id="av1_vaapi",
        label="VAAPI — AV1 (AMD/Intel)",
        family=VideoCodecFamily.VAAPI,
        presets=tuple(VAAPI_PRESETS),
        encoder_badge="VAAPI",
    ),
    "av1_qsv": VideoCodecSpec(
        codec_id="av1_qsv",
        label="QSV — AV1 (Intel Arc/12e gen+)",
        family=VideoCodecFamily.QSV,
        presets=tuple(QSV_PRESETS),
        encoder_badge="QSV",
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


def presets_for_codec(codec: str) -> list[str]:
    spec = video_codec_spec(codec)
    if spec is not None:
        return list(spec.presets)
    return X265_PRESETS


def is_h264_video_codec(codec: str) -> bool:
    spec = video_codec_spec(codec)
    return bool(spec is not None and spec.is_h264)


def supports_dynamic_hdr(codec: str) -> bool:
    spec = video_codec_spec(codec)
    if spec is not None:
        return spec.supports_dynamic_hdr
    return str(codec or "").strip().lower() in DYNAMIC_HDR_VIDEO_CODECS


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


def supports_force_8bit(codec: str) -> bool:
    spec = video_codec_spec(codec)
    return bool(spec is not None and spec.supports_force_8bit)


__all__ = [
    "VideoCodecFamily",
    "VideoCodecSpec",
    "AudioCodecSpec",
    "SOFTWARE_VIDEO_CODECS",
    "HARDWARE_VIDEO_CODECS",
    "AUDIO_CODECS",
    "X265_PRESETS",
    "X264_PRESETS",
    "SVTAV1_PRESETS",
    "NVENC_PRESETS",
    "VAAPI_PRESETS",
    "QSV_PRESETS",
    "AMF_PRESETS",
    "TONEMAP_ALGORITHMS",
    "NVENC_VIDEO_CODECS",
    "AMF_VIDEO_CODECS",
    "QSV_VIDEO_CODECS",
    "VAAPI_VIDEO_CODECS",
    "H264_VIDEO_CODECS",
    "DYNAMIC_HDR_VIDEO_CODECS",
    "VIDEO_ENCODER_BADGES",
    "VIDEO_HDR_BADGE_ORDER",
    "VIDEO_CODEC_SPECS",
    "VIDEO_CODEC_FAMILY_MAP",
    "AUDIO_CODEC_SPECS",
    "presets_for_codec",
    "is_h264_video_codec",
    "supports_dynamic_hdr",
    "encoder_badge",
    "video_codec_spec",
    "audio_codec_spec",
    "video_codec_family",
    "is_hardware_video_codec",
    "supports_force_8bit",
]
