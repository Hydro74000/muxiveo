"""Shared backend contracts for the encode workflow."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.runner import TaskSignals
from core.workflows.encode.models import EncodeConfig, QualityMode, VideoEncodeSettings

if TYPE_CHECKING:
    from pathlib import Path

    from core.workflows.encode.workflow import EncodeWorkflow


@dataclass(frozen=True)
class ProgressEvent:
    """Normalized progress payload consumed by the UI."""

    raw_line: str
    percent: float | None = None
    frame: int | None = None
    fps: float | None = None
    eta_seconds: float | None = None
    elapsed_seconds: float | None = None
    stage_label: str | None = None
    should_log: bool = False


@dataclass(frozen=True)
class BackendCapabilities:
    """Backend capabilities shared by workflow and UI."""

    backend_id: str
    quality_modes: tuple[QualityMode, ...]
    supports_dynamic_hdr: bool
    supports_manual_static_hdr: bool
    supports_tonemap: bool
    supports_multi_video: bool
    supports_main_filters: bool
    extra_params_backend: str
    progress_kind: str

    def supports_quality_mode(self, mode: QualityMode) -> bool:
        return mode in self.quality_modes


@dataclass(frozen=True)
class BackendContext:
    """Small execution context shared with backend implementations."""

    workflow: "EncodeWorkflow"
    plan: object | None = None


class EncodeBackend(ABC):
    """Internal backend contract used by the encode workflow."""

    backend_id: str

    @abstractmethod
    def capabilities(
        self,
        codec: str,
        config_ctx: BackendContext | None = None,
    ) -> BackendCapabilities:
        raise NotImplementedError

    @abstractmethod
    def validate(
        self,
        config: EncodeConfig,
        *,
        plan: object | None,
        ctx: BackendContext,
    ) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def build_preview(
        self,
        config: EncodeConfig,
        *,
        ctx: BackendContext,
    ) -> list[list[str]]:
        raise NotImplementedError

    @abstractmethod
    def build_single_preview(
        self,
        config: EncodeConfig,
        *,
        ctx: BackendContext,
    ) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def run(
        self,
        config: EncodeConfig,
        cleanup_paths: list["Path"],
        *,
        ctx: BackendContext,
        prep_signals: TaskSignals | None = None,
    ) -> TaskSignals:
        raise NotImplementedError

    @abstractmethod
    def normalize_extra_params(self, video: VideoEncodeSettings) -> str:
        raise NotImplementedError

    @abstractmethod
    def parse_progress(self, line: str) -> ProgressEvent | None:
        raise NotImplementedError
