"""FFmpeg backend implementation for the encode workflow."""

from __future__ import annotations

from typing import cast

from core.runner import TaskSignals
from core.workflows.encode.backends.models import (
    BackendCapabilities,
    BackendContext,
    EncodeBackend,
    ProgressEvent,
)
from core.workflows.encode.backends.progress import parse_ffmpeg_progress
from core.workflows.encode.catalog import (
    CQ_CAPABLE_VIDEO_CODECS,
    supports_dynamic_hdr,
    supports_manual_static_hdr_metadata,
)
from core.workflows.encode.models import EncodeConfig, QualityMode, VideoEncodeSettings


class FfmpegEncodeBackend(EncodeBackend):
    backend_id = "ffmpeg"

    def capabilities(
        self,
        codec: str,
        config_ctx: BackendContext | None = None,
    ) -> BackendCapabilities:
        _ = config_ctx
        modes: list[QualityMode] = [QualityMode.CRF]
        if codec in CQ_CAPABLE_VIDEO_CODECS:
            modes.append(QualityMode.CQ)
        modes.extend([QualityMode.BITRATE, QualityMode.SIZE])
        return BackendCapabilities(
            backend_id=self.backend_id,
            quality_modes=tuple(modes),
            supports_dynamic_hdr=supports_dynamic_hdr(codec),
            supports_manual_static_hdr=supports_manual_static_hdr_metadata(codec),
            supports_tonemap=True,
            supports_multi_video=True,
            supports_main_filters=True,
            extra_params_backend="ffmpeg",
            progress_kind="ffmpeg",
        )

    def validate(
        self,
        config: EncodeConfig,
        *,
        plan: object | None,
        ctx: BackendContext,
    ) -> list[str]:
        _ = (config, plan, ctx)
        return []

    def build_preview(
        self,
        config: EncodeConfig,
        *,
        ctx: BackendContext,
    ) -> list[list[str]]:
        preview = ctx.workflow._build_direct_output_commands(config)
        if preview and isinstance(preview[0], str):
            return [list(cast(list[str], preview))]
        return [list(cmd) for cmd in cast(list[list[str]], preview)]

    def build_single_preview(
        self,
        config: EncodeConfig,
        *,
        ctx: BackendContext,
    ) -> list[str]:
        preview = self.build_preview(config, ctx=ctx)
        return list(preview[0]) if preview else []

    def run(
        self,
        config: EncodeConfig,
        cleanup_paths: list,
        *,
        ctx: BackendContext,
        prep_signals: TaskSignals | None = None,
    ) -> TaskSignals:
        return ctx.workflow._run_ffmpeg_direct_output(
            config,
            cleanup_paths,
            prep_signals=prep_signals,
            plan=cast(object, ctx.plan),
        )

    def normalize_extra_params(self, video: VideoEncodeSettings) -> str:
        return str(video.extra_params or "")

    def parse_progress(self, line: str) -> ProgressEvent | None:
        return parse_ffmpeg_progress(line)
