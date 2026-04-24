"""Internal helpers extracted from encode.workflow to keep the façade smaller."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.workflows.encode.catalog import (
    AMF_VIDEO_CODECS,
    NVENC_VIDEO_CODECS,
    QSV_VIDEO_CODECS,
    VAAPI_VIDEO_CODECS,
)
from core.workflows.encode.models import QualityMode, VideoEncodeSettings
_UI_ENCODE_PROGRESS_PREFIX = "__MRE_PROGRESS__ "


def ui_encode_progress_message(*, label: str, event: str, line: str = "") -> str:
    payload = {
        "kind": "encode_ffmpeg",
        "label": str(label),
        "event": str(event),
        "line": str(line),
    }
    return _UI_ENCODE_PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class EncodeSyncTrack:
    track_type: str


@dataclass(frozen=True)
class EncodeSyncMappedTrack:
    source_file_index: int
    stream_index: int
    track: EncodeSyncTrack


@dataclass(frozen=True)
class EncodeOffsetInputSpec:
    map_key: tuple[Path, int, str]
    input_path: Path | str
    input_stream_index: int
    offset_ms: int


@dataclass(frozen=True)
class VideoTrackPrepSpec:
    order: int
    video: VideoEncodeSettings
    source: Path
    stream_index: int
    offset_ms: int


@dataclass(frozen=True)
class VideoTrackPrepTask:
    order: int
    resource_key: str
    estimated_ram_bytes: int
    run: Callable[[], tuple[dict[str, object], list[Path]]]


@dataclass(frozen=True)
class VideoPreparationResourcePolicy:
    """Politique explicite d'allocation des ressources encodeur."""

    vaapi_device: str | None = None
    ffmpeg_threads: int = 1

    def resource_key(self, video: VideoEncodeSettings) -> str:
        codec = str(video.codec or "").strip().lower()
        if codec in NVENC_VIDEO_CODECS:
            return "gpu:nvenc"
        if codec in VAAPI_VIDEO_CODECS:
            return f"gpu:vaapi:{self.vaapi_device or 'auto'}"
        if codec in QSV_VIDEO_CODECS:
            return "gpu:qsv"
        if codec in AMF_VIDEO_CODECS:
            return "gpu:amf"
        return "cpu"

    def estimated_ram_bytes(
        self,
        video: VideoEncodeSettings,
        *,
        source_size: int,
    ) -> int:
        mib = 1024 * 1024
        resource_key = self.resource_key(video)
        threads = max(1, int(self.ffmpeg_threads or 1))

        if resource_key == "cpu":
            base = 768 * mib
            per_thread = 96 * mib
        else:
            base = 384 * mib
            per_thread = 32 * mib

        if video.quality_mode == QualityMode.SIZE:
            base += 128 * mib
        if video.copy_dv or video.copy_hdr10plus:
            base += 256 * mib

        source_component = 0
        if source_size > 0:
            source_component = min(max(source_size // 32, 128 * mib), 1024 * mib)

        return base + source_component + (threads * per_thread)


class VideoTrackPreparationOrchestrator:
    """Orchestrateur de préparation des pistes vidéo avec contrôle de collisions."""

    def __init__(
        self,
        *,
        max_parallel: int,
        cancel_cb: Callable[[], None],
        on_worker_failure: Callable[[], None] | None = None,
        min_available_ram_bytes: int = 0,
        available_ram_cb: Callable[[], int] | None = None,
        on_ram_wait: Callable[[int, int, int], None] | None = None,
        ram_wait_timeout_s: float = 0.25,
    ) -> None:
        self._max_parallel = max(1, int(max_parallel))
        self._cancel_cb = cancel_cb
        self._on_worker_failure = on_worker_failure
        self._min_available_ram_bytes = max(0, int(min_available_ram_bytes))
        self._available_ram_cb = available_ram_cb
        self._on_ram_wait = on_ram_wait
        self._ram_wait_timeout_s = max(0.05, float(ram_wait_timeout_s))
        self._resource_semaphores: dict[str, threading.Semaphore] = {}
        self._resource_guard = threading.Lock()
        self._ram_guard = threading.Condition()
        self._reserved_ram_bytes = 0

    def _semaphore(self, resource_key: str) -> threading.Semaphore:
        with self._resource_guard:
            semaphore = self._resource_semaphores.get(resource_key)
            if semaphore is None:
                semaphore = threading.Semaphore(1)
                self._resource_semaphores[resource_key] = semaphore
            return semaphore

    def _claim_ram_budget(self, task: VideoTrackPrepTask) -> int:
        if (
            self._available_ram_cb is None
            or self._min_available_ram_bytes <= 0
            or task.estimated_ram_bytes <= 0
        ):
            return 0

        warned = False
        with self._ram_guard:
            while True:
                self._cancel_cb()
                available = max(0, int(self._available_ram_cb() or 0))
                if available <= 0:
                    return 0

                required = (
                    self._min_available_ram_bytes
                    + self._reserved_ram_bytes
                    + task.estimated_ram_bytes
                )
                if available >= required:
                    self._reserved_ram_bytes += task.estimated_ram_bytes
                    return task.estimated_ram_bytes

                if self._reserved_ram_bytes == 0 and available > self._min_available_ram_bytes:
                    claim = max(1, available - self._min_available_ram_bytes)
                    self._reserved_ram_bytes += claim
                    return claim

                if (not warned) and self._on_ram_wait is not None:
                    self._on_ram_wait(task.order, required, available)
                    warned = True

                self._ram_guard.wait(timeout=self._ram_wait_timeout_s)

    def _release_ram_budget(self, reserved_bytes: int) -> None:
        if reserved_bytes <= 0:
            return
        with self._ram_guard:
            self._reserved_ram_bytes = max(0, self._reserved_ram_bytes - int(reserved_bytes))
            self._ram_guard.notify_all()

    def _run_task(
        self,
        task: VideoTrackPrepTask,
    ) -> tuple[int, dict[str, object], list[Path]]:
        self._cancel_cb()
        semaphore = self._semaphore(task.resource_key)
        with semaphore:
            reserved_bytes = self._claim_ram_budget(task)
            try:
                self._cancel_cb()
                prepared_input, cleanup = task.run()
                return task.order, prepared_input, cleanup
            finally:
                self._release_ram_budget(reserved_bytes)

    def execute(
        self,
        tasks: list[VideoTrackPrepTask],
    ) -> list[tuple[int, dict[str, object], list[Path]]]:
        if not tasks:
            return []

        if self._max_parallel <= 1 or len(tasks) == 1:
            return [self._run_task(task) for task in tasks]

        results: list[tuple[int, dict[str, object], list[Path]]] = []
        worker_count = min(self._max_parallel, len(tasks))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(self._run_task, task) for task in tasks]
            for future in as_completed(futures):
                self._cancel_cb()
                try:
                    results.append(future.result())
                except Exception:
                    if self._on_worker_failure is not None:
                        self._on_worker_failure()
                    raise
        return results
