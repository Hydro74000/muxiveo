"""Codec label normalization shared by renderers."""

from __future__ import annotations


CODEC_LABELS: dict[str, str] = {
    "hevc": "HEVC",
    "h265": "HEVC",
    "h264": "AVC",
    "avc": "AVC",
    "av1": "AV1",
    "mpeg2video": "MPEG Video",
    "mpeg4": "MPEG-4 Visual",
    "vp9": "VP9",
    "vp8": "VP8",
    "truehd": "TrueHD",
    "eac3": "E-AC-3",
    "ac3": "AC-3",
    "aac": "AAC",
    "flac": "FLAC",
    "dts": "DTS",
    "opus": "Opus",
    "subrip": "SubRip",
    "ass": "ASS",
    "hdmv_pgs_subtitle": "PGS",
}


def codec_label(codec_name: str) -> str:
    return CODEC_LABELS.get(codec_name.lower(), codec_name.upper())
