"""Shared postprocess helpers formerly borrowed from the remux workflow facade."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from core.workflows.common.chapters import (
    probe_media_duration_seconds,
    write_ffmetadata_chapters,
)
from core.workflows.remux_mapping import resolve_mapped_tracks
from core.workflows.remux_models import RemuxConfig
from core.workflows.remux_sync import decide_strict_interleave_with_prescan


class RemuxPostprocessService:
    """Small shared contract used by encode without depending on RemuxWorkflow."""

    def __init__(self, *, ffprobe_bin: str) -> None:
        self._ffprobe_bin = ffprobe_bin

    def set_ffprobe_bin(self, ffprobe_bin: str) -> None:
        self._ffprobe_bin = ffprobe_bin

    def decide_strict_interleave_with_prescan(
        self,
        config: RemuxConfig,
        *,
        log_cb: Callable[[str, str], None],
    ) -> bool:
        return decide_strict_interleave_with_prescan(
            config,
            resolve_mapped_tracks=resolve_mapped_tracks,
            log_cb=log_cb,
        )

    def probe_duration_seconds(self, source: Path) -> float | None:
        return probe_media_duration_seconds(self._ffprobe_bin, source)

    @staticmethod
    def write_ffmetadata_chapters(
        entries: list,
        out_dir: Path,
        duration_s: float | None,
    ) -> Path:
        return write_ffmetadata_chapters(entries, out_dir, duration_s)


__all__ = ["RemuxPostprocessService"]
