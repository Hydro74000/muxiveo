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
    rate_controls_for_codec,
    supports_dovi,
    supports_dynamic_hdr,
    supports_hdr10plus,
    supports_hdr_output,
    supports_manual_static_hdr_metadata,
)
from core.workflows.encode.domain.codecs import (
    ExtraParamsReport,
    ffmpeg_extra_params_report,
    ffmpeg_workflow_option_values,
)
from core.workflows.encode.models import EncodeConfig, QualityMode, VideoEncodeSettings
from core.workflows.encode.planning.plan_models import EncodePlan


class FfmpegEncodeBackend(EncodeBackend):
    backend_id = "ffmpeg"

    def capabilities(
        self,
        codec: str,
        config_ctx: BackendContext | None = None,
    ) -> BackendCapabilities:
        _ = config_ctx
        controls = rate_controls_for_codec(codec)
        modes = tuple(dict.fromkeys(QualityMode(spec.family) for spec in controls)) or (QualityMode.CRF,)
        return BackendCapabilities(
            backend_id=self.backend_id,
            quality_modes=modes,
            rate_controls=controls,
            supports_dynamic_hdr=supports_dynamic_hdr(codec),
            supports_dovi=supports_dovi(codec),
            supports_hdr10plus=supports_hdr10plus(codec),
            supports_manual_static_hdr=supports_manual_static_hdr_metadata(codec),
            supports_hdr=supports_hdr_output(codec),
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
        _ = (plan, ctx)
        errors: list[str] = []
        videos = config.video_tracks or ([config.video] if config.video else [])
        for idx, video in enumerate(videos, start=1):
            if getattr(video, "copy_dv", False) and not supports_dovi(video.codec):
                if video.codec == "hevc_nvenc":
                    errors.append(
                        f"Piste vidéo #{idx} — Le codec FFmpeg 'hevc_nvenc' ne gère pas nativement "
                        "les métadonnées dynamiques Dolby Vision (incompatibilité DPB / risque d'écran noir "
                        "ou de rejet sur téléviseur). Suggestion : utilisez l'encodeur matériel dédié 'NVEncC (rigaya)' "
                        "(codec 'nvencc_hevc') qui intègre libdovi nativement, ou passez la vidéo en mode 'copy' (passthrough)."
                    )
                else:
                    errors.append(
                        f"Piste vidéo #{idx} — L'encodeur '{video.codec}' ne supporte pas l'injection Dolby Vision. "
                        "Suggestion : utilisez 'nvencc_hevc' (NVEncC avec libdovi), 'libx265' (logiciel) ou 'copy' (passthrough)."
                    )
        return errors

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
            plan=cast(EncodePlan | None, ctx.plan),
        )

    def normalize_extra_params(self, video: VideoEncodeSettings) -> str:
        return str(video.extra_params or "")

    def extra_params_report(self, video: VideoEncodeSettings) -> ExtraParamsReport:
        return ffmpeg_extra_params_report(video)

    def workflow_option_values(self, video: VideoEncodeSettings) -> dict[str, str]:
        return ffmpeg_workflow_option_values(video)

    def parse_progress(self, line: str) -> ProgressEvent | None:
        return parse_ffmpeg_progress(line)
