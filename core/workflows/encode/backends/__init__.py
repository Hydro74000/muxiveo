"""Backend selection and capabilities for the encode workflow."""

from __future__ import annotations

from core.workflows.encode.catalog import VideoCodecFamily, video_codec_family

from .ffmpeg_backend import FfmpegEncodeBackend
from .models import BackendCapabilities, BackendContext, EncodeBackend, ProgressEvent
from .nvencc_backend import NvenccEncodeBackend

_FFMPEG_BACKEND = FfmpegEncodeBackend()
_NVENCC_BACKEND = NvenccEncodeBackend()


def backend_id_for_codec(codec: str | None) -> str:
    family = video_codec_family(str(codec or "").strip().lower())
    if family is VideoCodecFamily.NVENCC:
        return "nvencc"
    return "ffmpeg"


def backend_for_codec(codec: str | None) -> EncodeBackend:
    if backend_id_for_codec(codec) == "nvencc":
        return _NVENCC_BACKEND
    return _FFMPEG_BACKEND


def backend_capabilities_for_codec(
    codec: str | None,
    *,
    config_ctx: BackendContext | None = None,
) -> BackendCapabilities:
    normalized = str(codec or "").strip().lower()
    return backend_for_codec(normalized).capabilities(normalized, config_ctx=config_ctx)


__all__ = [
    "BackendCapabilities",
    "BackendContext",
    "EncodeBackend",
    "FfmpegEncodeBackend",
    "NvenccEncodeBackend",
    "ProgressEvent",
    "backend_capabilities_for_codec",
    "backend_for_codec",
    "backend_id_for_codec",
]
