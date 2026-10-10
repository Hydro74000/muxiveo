"""Confirmation au lancement et état de reprise BL d'une piste."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

from core.fel.context import FelExecution
from core.fel.engine import FelCancelled, FelEngine, FelError, FelSource
from core.runner import TaskCancelledError
from core.workflows.encode.dovi_policy import sub_profile_from_value, wants_fel_bake
from core.workflows.encode.models import VideoEncodeSettings
from core.workflows.encode.runtime.dovi_geometry import extract_dovi_rpu
from core.workflows.encode.runtime.dovi_static_hdr import estimate_static_hdr_from_rpu


def requested(video: VideoEncodeSettings) -> bool:
    return wants_fel_bake(video.bake_dovi_fel, sub_profile_from_value(video.dovi_source_profile), video.codec)


def prepare_fel(
    video: VideoEncodeSettings, *, source: Path, work_dir: Path, index: int,
    ffmpeg: str, dovi: str, threads: int, run: Callable[[list[str]], object],
    capture: Callable[[list[str]], str], cancelled: Callable[[], bool], log: Callable[[str, str], None],
    engine_loader: Callable[[], FelEngine] | None = None,
) -> VideoEncodeSettings:
    if not requested(video):
        return video
    prefix = f"Piste vidéo #{index} — "
    try:
        engine = (engine_loader or FelEngine.installed)()
        rpu = work_dir / f"fel_original_{index}.bin"
        extract_dovi_rpu(source=source, stream_index=video.stream_index, ffmpeg_bin=ffmpeg,
                         dovi_tool_bin=dovi, output_rpu=rpu, work_dir=work_dir,
                         run_cmd=run, cleanup_paths=[], mode="0")
        kind = engine.classify(rpu, cancelled)
        if kind != "fel":
            log("INFO", prefix + f"Reconstruction FEL sans effet ({kind}).")
            return replace(video, bake_dovi_fel=False)
        context = FelExecution(FelSource(engine, source, video.stream_index, max(1, threads), video.input_frame_rate),
                               rpu, video.max_cll, video.static_hdr_light_level_source)
        result = replace(video, fel_context=context, source_color_transfer="smpte2084")
        if video.inject_hdr_meta and not video.tonemap_to_sdr:
            provenance = video.static_hdr_light_level_source or video.static_hdr_metadata_source
            if not video.max_cll or provenance in {"source", "rpu_estimate", "source_metadata"}:
                estimate = estimate_static_hdr_from_rpu(dovi, rpu, capture, prefer_l1=True)
                result = replace(result, max_cll=estimate.max_cll, static_hdr_light_level_source="fel_rpu_l1_estimate")
                log("WARN", prefix + f"MaxCLL/MaxFALL estimés depuis L1 : {estimate.max_cll or 'indisponibles'}.")
        if video.copy_hdr10plus:
            log("WARN", prefix + "HDR10+ conservé depuis la source : les images reconstruites peuvent nécessiter une nouvelle analyse.")
        log("INFO", prefix + "FEL confirmé : reconstruction active avant les filtres et l'encodage.")
        return result
    except FelCancelled as exc:
        raise TaskCancelledError() from exc
    except (FelError, OSError, RuntimeError) as exc:
        if cancelled() or isinstance(exc, TaskCancelledError):
            raise TaskCancelledError() from exc
        log("WARN", prefix + f"Reconstruction indisponible ({exc}) : repli BL.")
        return replace(video, bake_dovi_fel=False, fel_context=None)


def fallback_video(video: VideoEncodeSettings) -> VideoEncodeSettings:
    context = video.fel_context
    if context is None:
        return video
    return replace(video, fel_context=None, bake_dovi_fel=False,
                   max_cll=context.original_max_cll, static_hdr_light_level_source=context.original_light_source)
