"""Static HDR10 metadata estimator for Dolby Vision P5 -> P8.1 conversions."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.subprocess_utils import subprocess_text_kwargs


@dataclass(frozen=True)
class StaticHdrEstimate:
    master_display: str
    max_cll: str
    confidence: str
    sample_count: int
    coverage: float
    warnings: tuple[str, ...] = field(default_factory=tuple)
    source: str = "estimated_p5_to_p8"


class StaticHdrEstimateService:
    """Estimate HDR10 static metadata from a converted P5 -> P8 bitstream."""

    TARGET_SAMPLES = 96
    EDGE_SKIP_RATIO = 0.10
    DEFAULT_MAX_CLL = "1000,400"
    SOURCE_LABEL = "estimated_p5_to_p8"
    MIN_ACTIVE_PEAK_NITS = 5.0
    MIN_ACTIVE_AVERAGE_NITS = 0.25

    _SIGNALSTATS_RE = re.compile(
        r"lavfi\.signalstats\.(YMAX|YAVG|YBITDEPTH)\s*=\s*([0-9]+(?:\.[0-9]+)?)"
    )

    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        dovi_tool_bin: str,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._dovi_tool = dovi_tool_bin
        self._cache: dict[tuple[object, ...], StaticHdrEstimate] = {}

    def estimate_p5_to_p8_static_hdr(
        self,
        source: Path,
        *,
        stream_index: int,
        duration_s: float | None,
        work_dir: Path | None,
        progress_cb: Callable[[str], None] | None = None,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> StaticHdrEstimate:
        cache_key = self._cache_key(source, stream_index, duration_s)
        if cache_key in self._cache:
            return self._cache[cache_key]

        base_dir = Path(work_dir) if work_dir is not None else Path(tempfile.gettempdir())
        base_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="static_hdr_p5_", dir=str(base_dir)))
        try:
            self._check_cancelled(cancel_cb)
            annexb = tmp_dir / "source_annexb.hevc"
            converted = tmp_dir / "source_p8.hevc"

            self._emit(progress_cb, "Analyse HDR10 estimée : extraction HEVC P5…")
            self._run_capture(
                [
                    self._ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(source),
                    "-map",
                    f"0:{int(stream_index)}",
                    "-c",
                    "copy",
                    "-bsf:v",
                    "hevc_mp4toannexb",
                    "-f",
                    "hevc",
                    str(annexb),
                ],
                cancel_cb=cancel_cb,
            )

            self._emit(progress_cb, "Analyse HDR10 estimée : conversion P5 → P8…")
            self._run_capture(
                [
                    self._dovi_tool,
                    "-m",
                    "3",
                    "convert",
                    "-i",
                    str(annexb),
                    "-o",
                    str(converted),
                ],
                cancel_cb=cancel_cb,
            )

            self._check_cancelled(cancel_cb)
            self._emit(progress_cb, "Analyse HDR10 estimée : échantillonnage luminance…")
            output = self._run_capture(
                self._analysis_cmd(converted, duration_s=duration_s),
                cancel_cb=cancel_cb,
            )
            estimate = self.estimate_from_signalstats_output(output)
            self._cache[cache_key] = estimate
            return estimate
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @classmethod
    def estimate_from_signalstats_output(cls, output: str) -> StaticHdrEstimate:
        pairs = cls._parse_signalstats(output)
        warnings: list[str] = []
        if not pairs:
            warnings.append(
                "Aucun échantillon signalstats exploitable — valeurs HDR10 conservatrices appliquées."
            )
            return StaticHdrEstimate(
                master_display=cls.master_display_for_peak(1000),
                max_cll=cls.DEFAULT_MAX_CLL,
                confidence="low",
                sample_count=0,
                coverage=0.0,
                warnings=tuple(warnings),
            )

        luminance_pairs = [
            (
                cls.pq_code_value_to_nits(ymax, bit_depth=bit_depth),
                cls.pq_code_value_to_nits(yavg, bit_depth=bit_depth),
            )
            for ymax, yavg, bit_depth in pairs
        ]
        active_pairs = cls._active_luminance_pairs(luminance_pairs)
        ignored_dark = len(luminance_pairs) - len(active_pairs)
        if active_pairs:
            luminance_pairs = active_pairs
            if ignored_dark:
                warnings.append(f"{ignored_dark} échantillon(s) quasi noir(s) ignoré(s).")
        else:
            warnings.append(
                "Aucun échantillon actif détecté — estimation basée sur les frames disponibles."
            )

        peaks = [ymax for ymax, _yavg in luminance_pairs]
        averages = [yavg for _ymax, yavg in luminance_pairs]
        peak_nits = max(peaks) if peaks else 0.0
        fall_nits = max(averages) if averages else 0.0
        max_content = cls.round_hdr_nits(peak_nits)
        max_average = min(max_content, cls.round_hdr_nits(fall_nits))
        sample_count = len(luminance_pairs)
        coverage = min(1.0, sample_count / cls.TARGET_SAMPLES)
        confidence = cls.confidence_for_sample_count(sample_count)
        if sample_count < 24:
            warnings.append("Échantillonnage faible : estimation HDR10 peu fiable.")

        return StaticHdrEstimate(
            master_display=cls.master_display_for_peak(max_content),
            max_cll=f"{max_content},{max_average}",
            confidence=confidence,
            sample_count=sample_count,
            coverage=coverage,
            warnings=tuple(warnings),
        )

    @classmethod
    def _parse_signalstats(cls, output: str) -> list[tuple[float, float, int | None]]:
        current: dict[str, float] = {}
        pairs: list[tuple[float, float, int | None]] = []
        saw_frame_marker = False

        def flush_current() -> None:
            if "YMAX" not in current or "YAVG" not in current:
                return
            bit_depth = current.get("YBITDEPTH")
            pairs.append((
                current["YMAX"],
                current["YAVG"],
                int(bit_depth) if bit_depth else None,
            ))

        for line in (output or "").splitlines():
            if line.startswith("frame:"):
                saw_frame_marker = True
                flush_current()
                current = {}
                continue
            match = cls._SIGNALSTATS_RE.search(line)
            if match is None:
                continue
            key, raw_value = match.groups()
            current[key] = float(raw_value)
            if not saw_frame_marker and "YMAX" in current and "YAVG" in current:
                flush_current()
                bit_depth = current.get("YBITDEPTH")
                current = {"YBITDEPTH": bit_depth} if bit_depth else {}
        flush_current()
        return pairs

    @classmethod
    def _active_luminance_pairs(cls, pairs: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [
            (peak, average)
            for peak, average in pairs
            if peak >= cls.MIN_ACTIVE_PEAK_NITS or average >= cls.MIN_ACTIVE_AVERAGE_NITS
        ]

    @staticmethod
    def pq_code_value_to_nits(value: float, *, bit_depth: int | None = None) -> float:
        """Convert a signalstats luma code value to approximate PQ nits."""
        try:
            raw = float(value)
        except (TypeError, ValueError):
            return 0.0
        if raw <= 0:
            return 0.0
        if bit_depth:
            scale = 2 ** max(0, int(bit_depth) - 8)
            black = 16.0 * scale
            white = 235.0 * scale
            normalized = (raw - black) / (white - black)
        elif raw <= 255:
            normalized = (raw - 16.0) / (235.0 - 16.0)
        elif raw <= 1023:
            normalized = (raw - 64.0) / (940.0 - 64.0)
        else:
            normalized = (raw - 4096.0) / (60160.0 - 4096.0)
        normalized = max(0.0, min(1.0, normalized))
        return StaticHdrEstimateService._pq_eotf(normalized)

    @staticmethod
    def _pq_eotf(normalized: float) -> float:
        if normalized <= 0:
            return 0.0
        m1 = 2610.0 / 16384.0
        m2 = 2523.0 / 32.0
        c1 = 3424.0 / 4096.0
        c2 = 2413.0 / 128.0
        c3 = 2392.0 / 128.0
        n = max(0.0, min(1.0, normalized))
        p = n ** (1.0 / m2)
        numerator = max(p - c1, 0.0)
        denominator = c2 - c3 * p
        if denominator <= 0:
            return 10000.0
        return 10000.0 * ((numerator / denominator) ** (1.0 / m1))

    @staticmethod
    def round_hdr_nits(value: float) -> int:
        bounded = max(1.0, min(10000.0, float(value or 0.0)))
        step = 50 if bounded <= 1000 else 100
        return max(step, int(math.ceil(bounded / step) * step))

    @staticmethod
    def confidence_for_sample_count(sample_count: int) -> str:
        if sample_count >= 72:
            return "high"
        if sample_count >= 24:
            return "medium"
        return "low"

    @staticmethod
    def master_display_for_peak(max_cll: int) -> str:
        peak = int(max_cll or 0)
        if peak <= 1000:
            master_peak = 1000
        elif peak <= 4000:
            master_peak = 4000
        else:
            master_peak = 10000
        return (
            "G(8500,39850)"
            "B(6550,2300)"
            "R(35400,14600)"
            "WP(15635,16450)"
            f"L({master_peak * 10000},1)"
        )

    def _analysis_cmd(self, converted: Path, *, duration_s: float | None) -> list[str]:
        interval = 60.0
        skip_start = 0.0
        skip_end = 0.0
        if duration_s and duration_s > 0:
            skip_start, skip_end = self._analysis_window(duration_s)
            usable = max(1.0, skip_end - skip_start)
            interval = max(1.0, usable / self.TARGET_SAMPLES)
        fps = 1.0 / interval
        filters: list[str] = []
        if skip_end > skip_start:
            filters.append(f"trim=start={skip_start:.3f}:end={skip_end:.3f}")
        filters.extend([
            f"fps=fps={fps:.8f}",
            "signalstats",
            "metadata=print:file=-",
        ])
        return [
            self._ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(converted),
            "-vf",
            ",".join(filters),
            "-an",
            "-f",
            "null",
            "-",
        ]

    @classmethod
    def _analysis_window(cls, duration_s: float) -> tuple[float, float]:
        duration = max(0.0, float(duration_s or 0.0))
        if duration <= 0:
            return 0.0, 0.0
        skip = duration * cls.EDGE_SKIP_RATIO
        start = skip
        end = duration - skip
        if end <= start:
            return 0.0, duration
        return start, end

    def _run_capture(
        self,
        cmd: list[str],
        *,
        cancel_cb: Callable[[], bool] | None = None,
    ) -> str:
        self._check_cancelled(cancel_cb)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **subprocess_text_kwargs(),
        )
        while True:
            self._check_cancelled(cancel_cb, proc=proc)
            try:
                stdout, stderr = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                continue
        output = f"{stdout or ''}\n{stderr or ''}"
        if proc.returncode != 0:
            raise RuntimeError(f"Commande échouée ({proc.returncode}) : {' '.join(cmd)}\n{output}")
        return output

    @staticmethod
    def _check_cancelled(
        cancel_cb: Callable[[], bool] | None,
        *,
        proc: subprocess.Popen | None = None,
    ) -> None:
        if cancel_cb is not None and cancel_cb():
            if proc is not None and proc.poll() is None:
                proc.terminate()
                deadline = time.monotonic() + 2.0
                while proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if proc.poll() is None:
                    proc.kill()
            raise RuntimeError("Analyse HDR10 estimée annulée.")

    @staticmethod
    def _emit(progress_cb: Callable[[str], None] | None, message: str) -> None:
        if progress_cb is not None:
            progress_cb(message)

    @staticmethod
    def _cache_key(source: Path, stream_index: int, duration_s: float | None) -> tuple[object, ...]:
        try:
            stat = source.stat()
            return (
                str(source),
                stat.st_size,
                stat.st_mtime_ns,
                int(stream_index),
                round(float(duration_s or 0.0), 3),
                "p5_to_p8",
            )
        except OSError:
            return (str(source), int(stream_index), round(float(duration_s or 0.0), 3), "p5_to_p8")


__all__ = ["StaticHdrEstimate", "StaticHdrEstimateService"]
