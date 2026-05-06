"""Audio fingerprint sync workflow for explicit remux-panel alignment."""

from __future__ import annotations

import json
import math
import subprocess
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.subprocess_utils import subprocess_text_kwargs, subprocess_windows_no_window_kwargs


class AudioSyncError(RuntimeError):
    """Raised when enhanced audio synchronization cannot compute a reliable delta."""


@dataclass(frozen=True)
class AudioSyncTrack:
    source_path: Path
    stream_index: int


@dataclass(frozen=True)
class AudioProbe:
    channels: int
    channel_layout: str
    duration_s: float | None = None


@dataclass(frozen=True)
class AudioSyncResult:
    offset_ms: int
    confidence: float
    lag_ms: int


class AudioSyncWorkflow:
    """
    Explicit audio-content synchronization.

    It extracts a low-rate mono signature from LFE/surround-ish channels, then
    correlates target against a chosen reference. The returned offset follows
    the existing TrackEntry.time_shift_ms convention: positive delays target,
    negative advances target.
    """

    def __init__(
        self,
        *,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        log_cb: Callable[[str, str], None] | None = None,
        sample_rate: int = 8000,
        analysis_seconds: int = 900,
        max_offset_seconds: int = 120,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._ffprobe = ffprobe_bin
        self._log = log_cb or (lambda _level, _message: None)
        self._sample_rate = int(sample_rate)
        self._analysis_seconds = int(analysis_seconds)
        self._max_offset_seconds = int(max_offset_seconds)

    def detect_offset(self, reference: AudioSyncTrack, target: AudioSyncTrack) -> AudioSyncResult:
        ref_probe = self._probe(reference)
        target_probe = self._probe(target)
        self._ensure_compatible(ref_probe, "reference")
        self._ensure_compatible(target_probe, "target")

        ref_signature = self._signature(reference, ref_probe)
        target_signature = self._signature(target, target_probe)
        if len(ref_signature) < 20 or len(target_signature) < 20:
            raise AudioSyncError("signature audio trop courte")

        lag_steps, confidence = self._best_lag(ref_signature, target_signature)
        if confidence < 0.35:
            raise AudioSyncError(f"corrélation audio insuffisante ({confidence:.2f})")

        hop_ms = 100
        lag_ms = int(round(lag_steps * hop_ms))
        return AudioSyncResult(
            offset_ms=-lag_ms,
            confidence=confidence,
            lag_ms=lag_ms,
        )

    def _probe(self, track: AudioSyncTrack) -> AudioProbe:
        cmd = [
            self._ffprobe,
            "-v", "error",
            "-select_streams", f"a:{self._audio_stream_ordinal(track)}",
            "-show_entries", "stream=channels,channel_layout,duration",
            "-of", "json",
            str(track.source_path),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=30,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0:
            raise AudioSyncError((result.stderr or "").strip() or "ffprobe audio échoué")
        try:
            payload = json.loads(result.stdout or "{}")
            stream = (payload.get("streams") or [])[0]
        except Exception as exc:
            raise AudioSyncError("réponse ffprobe audio invalide") from exc
        channels = int(stream.get("channels") or 0)
        duration_raw = stream.get("duration")
        try:
            duration_s = float(duration_raw) if duration_raw not in (None, "") else None
        except (TypeError, ValueError):
            duration_s = None
        return AudioProbe(
            channels=channels,
            channel_layout=str(stream.get("channel_layout") or ""),
            duration_s=duration_s,
        )

    def _audio_stream_ordinal(self, track: AudioSyncTrack) -> int:
        cmd = [
            self._ffprobe,
            "-v", "error",
            "-show_entries", "stream=index,codec_type",
            "-of", "json",
            str(track.source_path),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=30,
            **subprocess_text_kwargs(),
        )
        if result.returncode != 0:
            raise AudioSyncError((result.stderr or "").strip() or "ffprobe streams échoué")
        payload = json.loads(result.stdout or "{}")
        audio_ord = 0
        for stream in payload.get("streams") or []:
            if stream.get("codec_type") != "audio":
                continue
            if int(stream.get("index", -1)) == int(track.stream_index):
                return audio_ord
            audio_ord += 1
        raise AudioSyncError(f"piste audio introuvable: stream={track.stream_index}")

    @staticmethod
    def _ensure_compatible(probe: AudioProbe, label: str) -> None:
        layout = probe.channel_layout.lower()
        if probe.channels >= 6 or "5.1" in layout or "7.1" in layout:
            return
        raise AudioSyncError(f"piste {label} non compatible: moins de 5.1")

    def _signature(self, track: AudioSyncTrack, probe: AudioProbe) -> list[float]:
        channel_indices = self._focus_channel_indices(probe.channels)
        pan_terms = "+".join(
            f"{1.0 / len(channel_indices):.6f}*c{idx}"
            for idx in channel_indices
        )
        audio_filter = f"pan=mono|c0={pan_terms},highpass=f=25"
        cmd = [
            self._ffmpeg,
            "-hide_banner", "-loglevel", "error",
            "-i", str(track.source_path),
            "-map", f"0:{int(track.stream_index)}",
            "-vn", "-sn", "-dn",
            "-t", str(self._analysis_seconds),
            "-af", audio_filter,
            "-ar", str(self._sample_rate),
            "-ac", "1",
            "-f", "s16le",
            "pipe:1",
        ]
        self._log("INFO", "$ " + " ".join(str(part) for part in cmd))
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=max(120, self._analysis_seconds * 3),
            **subprocess_windows_no_window_kwargs(),
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip() if isinstance(result.stderr, bytes) else str(result.stderr or "")
            raise AudioSyncError(stderr or "extraction signature audio échouée")
        samples = array("h")
        samples.frombytes(result.stdout)
        if not samples:
            raise AudioSyncError("signature audio vide")
        return self._envelope(samples)

    @staticmethod
    def _focus_channel_indices(channels: int) -> list[int]:
        if channels >= 8:
            return [3, 4, 5, 6, 7]
        return [3, 4, 5]

    def _envelope(self, samples: array) -> list[float]:
        window = max(1, self._sample_rate // 10)
        values: list[float] = []
        previous = 0.0
        for start in range(0, len(samples) - window + 1, window):
            chunk = samples[start:start + window]
            energy = math.sqrt(sum(float(s) * float(s) for s in chunk) / len(chunk))
            novelty = max(0.0, energy - previous)
            values.append(novelty)
            previous = energy
        return self._normalize(values)

    @staticmethod
    def _normalize(values: list[float]) -> list[float]:
        if not values:
            return []
        mean = sum(values) / len(values)
        centered = [value - mean for value in values]
        variance = sum(value * value for value in centered) / len(centered)
        scale = math.sqrt(variance) or 1.0
        return [value / scale for value in centered]

    def _best_lag(self, reference: list[float], target: list[float]) -> tuple[int, float]:
        max_lag = min(self._max_offset_seconds * 10, len(reference) - 5, len(target) - 5)
        if max_lag <= 0:
            raise AudioSyncError("fenêtre de corrélation audio insuffisante")

        min_overlap = max(20, int(min(len(reference), len(target)) * 0.5))
        best_lag = 0
        best_score = -1.0
        for lag in range(-max_lag, max_lag + 1):
            if lag >= 0:
                ref_slice = reference[:len(reference) - lag]
                target_slice = target[lag:lag + len(ref_slice)]
            else:
                target_slice = target[:len(target) + lag]
                ref_slice = reference[-lag:-lag + len(target_slice)]
            if len(ref_slice) < min_overlap or len(target_slice) < min_overlap:
                continue
            denom = math.sqrt(sum(v * v for v in ref_slice) * sum(v * v for v in target_slice))
            if denom <= 0:
                continue
            score = sum(a * b for a, b in zip(ref_slice, target_slice)) / denom
            if score > best_score:
                best_score = score
                best_lag = lag

        if best_score < -0.5:
            raise AudioSyncError("corrélation audio impossible")
        return best_lag, float(best_score)


__all__ = [
    "AudioProbe",
    "AudioSyncError",
    "AudioSyncResult",
    "AudioSyncTrack",
    "AudioSyncWorkflow",
]
