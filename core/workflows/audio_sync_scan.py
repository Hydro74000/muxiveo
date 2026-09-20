"""Analyse acoustique multi-fenêtres et localisation conservatrice des ruptures."""
from __future__ import annotations

import json
import subprocess
import re
import time

from core.subprocess_utils import subprocess_text_kwargs, subprocess_windows_no_window_kwargs
from core.workflows.audio_sync import AudioSyncError, AudioSyncTrack
from core.workflows.sync_calibration import SyncCalibration, SyncSegment


class AudioSyncScanner:
    def __init__(self, ffmpeg="ffmpeg", ffprobe="ffprobe", *, window_s=20, max_offset_s=10, cancel_event=None):
        self.ffmpeg, self.ffprobe = str(ffmpeg), str(ffprobe)
        self.window_s, self.max_offset_s = window_s, max_offset_s
        self.cancel_event = cancel_event

    def _run(self, command, *, timeout, **kwargs):
        """Interrompt aussi les extractions FFmpeg lorsque l'utilisateur annule."""
        kwargs.pop("capture_output", None)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise AudioSyncError("Analyse annulée.")
                if time.monotonic() >= deadline:
                    raise AudioSyncError("Délai d'analyse dépassé.")
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def duration(self, track):
        result = self._run([self.ffprobe, "-v", "error", "-show_entries",
                                 "format=duration", "-of", "json", str(track.source_path)],
                                capture_output=True, timeout=30, **subprocess_text_kwargs())
        if result.returncode:
            raise AudioSyncError(result.stderr)
        duration = float(json.loads(result.stdout)["format"]["duration"])
        if duration <= 0:
            raise AudioSyncError("Durée audio invalide.")
        return duration

    def samples(self, track, start, duration):
        import numpy as np
        result = self._run([
            self.ffmpeg, "-v", "error", "-ss", str(max(0, start)), "-i", str(track.source_path),
            "-map", f"0:{track.stream_index}" if isinstance(track.stream_index, int) else str(track.stream_index),
            "-t", str(duration), "-af", "highpass=f=300,lowpass=f=3000",
            "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"],
            capture_output=True, timeout=max(120, duration * 4), **subprocess_windows_no_window_kwargs())
        if result.returncode:
            raise AudioSyncError(result.stderr.decode("utf-8", errors="replace"))
        return np.frombuffer(result.stdout, dtype="<f4").astype(float)

    def black_transitions(self, track, start, duration):
        result = self._run([self.ffmpeg, "-nostdin", "-hide_banner", "-ss", str(start),
            "-i", str(track.source_path), "-t", str(duration), "-an", "-sn",
            "-vf", "blackdetect=d=0.08:pix_th=0.1", "-f", "null", "-"],
            timeout=max(120, duration * 4), **subprocess_text_kwargs())
        if result.returncode:
            return []  # Source audio seule : pas de candidat vidéo.
        return [start + (float(a) + float(b)) / 2 for a, b in
                re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", result.stderr)]

    @staticmethod
    def correlate(reference, donor, max_offset_ms=10000):
        """Corrélation FFT des enveloppes à 1 ms, normalisée par recouvrement."""
        import numpy as np
        def envelope(values):
            size = len(values) // 16
            if size < 100:
                raise AudioSyncError("Fenêtre audio insuffisante.")
            values = np.sqrt(np.mean(np.square(values[:size * 16].reshape(size, 16)), axis=1))
            return values - values.mean()
        a, b = envelope(reference), envelope(donor)
        n = min(len(a), len(b))
        a, b = a[:n], b[:n]
        size = 1 << (2 * n - 1).bit_length()
        corr = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
        limit = min(int(max_offset_ms), n // 3)
        lags = np.arange(-limit, limit + 1)
        pa, pb = np.r_[0, np.cumsum(a * a)], np.r_[0, np.cumsum(b * b)]
        left_a, left_b = np.maximum(lags, 0), np.maximum(-lags, 0)
        count = n - np.abs(lags)
        denominator = np.sqrt((pa[left_a + count] - pa[left_a]) * (pb[left_b + count] - pb[left_b]))
        scores = corr[lags % size] / np.maximum(denominator, 1e-20)
        best = int(np.argmax(scores))
        confidence = float(scores[best])
        outside = scores[np.abs(lags - lags[best]) > 40]
        margin = confidence - float(outside.max()) if outside.size else confidence
        if confidence < 0.35 or margin < 0.04 or best in {0, len(scores) - 1}:
            raise AudioSyncError("Corrélation insuffisante ou ambiguë ; calibration manuelle requise.")
        return float(lags[best]), min(1.0, confidence)

    def measure(self, reference, donor, start, duration):
        return self.correlate(self.samples(reference, start, duration),
                              self.samples(donor, start, duration), self.max_offset_s * 1000)

    def scan(self, reference: AudioSyncTrack, donor: AudioSyncTrack, *, detect_cuts=False,
             drift_threshold_ms=25, log=lambda message: None):
        import numpy as np
        if drift_threshold_ms <= 0:
            raise ValueError("Le seuil de dérive doit être positif.")
        duration = min(self.duration(reference), self.duration(donor))
        window = min(self.window_s, duration / 3)
        positions = np.linspace(0, max(0, duration - window), 6)
        samples = []
        for position in positions:
            offset, confidence = self.measure(reference, donor, float(position), window)
            samples.append({"start_ms": float(position * 1000), "shift_ms": offset, "confidence": confidence})
            log(f"{position:.1f} s : {offset:+.1f} ms ({confidence:.2f})")
        spread = max(s["shift_ms"] for s in samples) - min(s["shift_ms"] for s in samples)
        if spread <= drift_threshold_ms:
            return SyncCalibration((SyncSegment(0, float(np.median([s["shift_ms"] for s in samples]))),),
                                   min(s["confidence"] for s in samples), tuple(samples))
        if not detect_cuts:
            raise AudioSyncError("Dérive détectée ; utiliser --detect-cuts ou une calibration manuelle.")
        segments = [SyncSegment(0, samples[0]["shift_ms"])]
        for left, right in zip(samples, samples[1:]):
            if abs(right["shift_ms"] - segments[-1].shift_ms) <= drift_threshold_ms:
                continue
            low, high = left["start_ms"] / 1000, right["start_ms"] / 1000 + window
            # Seules des transitions silencieuses corroborées par les fenêtres
            # voisines sont acceptées ; jamais inventer une coupure au milieu.
            values = self.samples(donor, low, high - low)
            frame = 160
            energy = np.sqrt(np.mean(values[:len(values) // frame * frame].reshape(-1, frame) ** 2, axis=1))
            quiet = energy < 0.0032
            edges = np.diff(np.r_[False, quiet, False].astype(int))
            candidates = [(a, b) for a, b in zip(np.where(edges == 1)[0], np.where(edges == -1)[0]) if b - a >= 30]
            transitions = [low + (a + b) / 200 for a, b in candidates]
            for black in self.black_transitions(donor, low, high - low):
                if not any(abs(black - existing) < 0.3 for existing in transitions):
                    transitions.append(black)
            matches = []
            for cut in sorted(transitions):
                check_window = min(6, window)
                before = max(0, cut - check_window - self.max_offset_s)
                after = cut + self.max_offset_s
                if after + check_window > duration:
                    continue
                try:
                    prior, _ = self.measure(reference, donor, before, check_window)
                    following, _ = self.measure(reference, donor, after, check_window)
                except AudioSyncError:
                    continue
                if abs(prior - segments[-1].shift_ms) <= drift_threshold_ms and abs(following - right["shift_ms"]) <= drift_threshold_ms:
                    matches.append(cut)
            if len(matches) != 1:
                raise AudioSyncError("Rupture non localisée avec certitude ; calibration manuelle requise.")
            segments.append(SyncSegment(matches[0] * 1000, right["shift_ms"]))
        return SyncCalibration(tuple(segments), min(s["confidence"] for s in samples), tuple(samples))
