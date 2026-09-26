"""Analyse acoustique multi-fenêtres et localisation conservatrice des ruptures."""
from __future__ import annotations

import json
import subprocess
import re
import time

from core.subprocess_utils import subprocess_text_kwargs, subprocess_windows_no_window_kwargs
from core.workflows.audio_sync import AudioSyncError, AudioSyncTrack
from core.workflows.sync_calibration import SyncCalibration, SyncSegment

# Marge de pré-roll avant la fenêtre ; le découpage exact précède les filtres
# de cadence pour conserver les coordonnées temporelles de la source.
_SEEK_PREROLL_S = 5.0


def _seek_args(start: float) -> tuple[list[str], list[str]]:
    """Retourne les options de seek et les filtres de découpage avant conversion de cadence."""
    start = max(0.0, float(start))
    pre = max(0.0, start - _SEEK_PREROLL_S)
    trim = start - pre
    return (["-ss", f"{pre:.6f}"] if pre > 0 else []), (
        [f"atrim=start={trim:.6f}", "asetpts=PTS-STARTPTS"] if trim > 0 else []
    )


class AudioSyncScanner:
    def __init__(self, ffmpeg="ffmpeg", ffprobe="ffprobe", *, window_s=60, max_offset_s=30, cancel_event=None):
        self.ffmpeg, self.ffprobe = str(ffmpeg), str(ffprobe)
        self.window_s, self.max_offset_s = window_s, max_offset_s
        self.cancel_event = cancel_event

    def _run(self, command, *, timeout, **kwargs):
        """Interrompt aussi les extractions FFmpeg lorsque l'utilisateur annule."""
        kwargs.pop("capture_output", None)
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)  # nosec B603
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise AudioSyncError("Analyse annulée.")
                if time.monotonic() >= deadline:
                    raise AudioSyncError("Délai d'analyse dépassé.")
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
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

    def samples(self, track, start, duration, cadence_filter: str | None = None):
        import numpy as np
        af = "highpass=f=300,lowpass=f=3000"
        if cadence_filter:
            af = f"{cadence_filter},{af}"
        seek_in, trim_filters = _seek_args(start)
        af = ",".join([*trim_filters, af])
        result = self._run([
            self.ffmpeg, "-v", "error", *seek_in, "-i", str(track.source_path),
            "-map", f"0:{track.stream_index}" if isinstance(track.stream_index, int) else str(track.stream_index),
            "-t", str(duration), "-af", af,
            "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"],
            capture_output=True, timeout=max(120, duration * 4), **subprocess_windows_no_window_kwargs())
        if result.returncode:
            raise AudioSyncError(result.stderr.decode("utf-8", errors="replace"))
        return np.frombuffer(result.stdout, dtype="<f4").astype(float)

    def pitch_samples(self, track, start, duration, cadence_filter: str | None = None):
        """Extrait les échantillons audio sans filtre passe-bande agressif pour l'analyse F0 et spectrale."""
        import numpy as np
        af = "lowpass=f=4000"
        if cadence_filter:
            af = f"{cadence_filter},{af}"
        seek_in, trim_filters = _seek_args(start)
        af = ",".join([*trim_filters, af])
        result = self._run([
            self.ffmpeg, "-v", "error", *seek_in, "-i", str(track.source_path),
            "-map", f"0:{track.stream_index}" if isinstance(track.stream_index, int) else str(track.stream_index),
            "-t", str(duration), "-af", af,
            "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"],
            capture_output=True, timeout=max(120, duration * 4), **subprocess_windows_no_window_kwargs())
        if result.returncode:
            return np.array([], dtype=float)
        return np.frombuffer(result.stdout, dtype="<f4").astype(float)

    def _analyze_cadence_pitch(
        self,
        reference: AudioSyncTrack,
        donor: AudioSyncTrack,
        p_ref: float,
        offset_ms: float,
        cadence_mismatch,
        duration: float = 4.0,
    ):
        """Exécute l'analyse acoustique de hauteur tonale (F0 et spectre de Welch)."""
        try:
            from core.workflows.cadence import CadenceAudioMethod, build_cadence_audio_filter
            from core.workflows.cadence_pitch import analyze_cadence_pitch

            speed_factor = getattr(cadence_mismatch, "speed_factor", 1.0)
            p_donor = max(0.0, (p_ref + offset_ms / 1000.0) * speed_factor)

            filter_ase = build_cadence_audio_filter(cadence_mismatch, CadenceAudioMethod.ASETRATE)
            filter_ate = build_cadence_audio_filter(cadence_mismatch, CadenceAudioMethod.ATEMPO)

            ref_samples = self.pitch_samples(reference, p_ref, duration)
            donor_ase_samples = self.pitch_samples(donor, p_donor, duration, cadence_filter=filter_ase)
            donor_ate_samples = self.pitch_samples(donor, p_donor, duration, cadence_filter=filter_ate)

            if len(ref_samples) < 16000 or len(donor_ase_samples) < 16000 or len(donor_ate_samples) < 16000:
                return None

            return analyze_cadence_pitch(
                ref_samples,
                donor_ase_samples,
                donor_ate_samples,
                sr=16000,
                mismatch=cadence_mismatch,
            )
        except Exception:
            return None

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

    def measure(
        self,
        reference,
        donor,
        start,
        duration,
        cadence_filter: str | None = None,
        speed_factor: float = 1.0,
    ):
        p_donor = start * speed_factor
        return self.correlate(
            self.samples(reference, start, duration),
            self.samples(donor, p_donor, duration, cadence_filter=cadence_filter),
            self.max_offset_s * 1000,
        )

    def scan(self, reference: AudioSyncTrack, donor: AudioSyncTrack, *, detect_cuts=False,
             drift_threshold_ms=25, cadence_mismatch=None, cadence_audio_method="auto",
             log=lambda message: None):
        import numpy as np
        from core.workflows.cadence import CadenceAudioMethod, build_cadence_audio_filter
        if drift_threshold_ms <= 0:
            raise ValueError("Le seuil de dérive doit être positif.")
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AudioSyncError("Analyse annulée.")

        is_auto_method = cadence_audio_method in ("auto", CadenceAudioMethod.AUTO)
        active_cadence_method = "atempo" if is_auto_method else (
            cadence_audio_method.value if isinstance(cadence_audio_method, CadenceAudioMethod) else str(cadence_audio_method)
        )
        pitch_analysis = None

        filter_str = None
        speed_factor = 1.0
        if cadence_mismatch and getattr(cadence_mismatch, "cadence_type", None) not in (None, "none"):
            filter_str = build_cadence_audio_filter(cadence_mismatch, active_cadence_method)
            speed_factor = getattr(cadence_mismatch, "speed_factor", 1.0)

        donor_duration_ref = self.duration(donor) / speed_factor if speed_factor > 0 else self.duration(donor)
        duration = min(self.duration(reference), donor_duration_ref)
        window = min(self.window_s, duration / 3)
        max_pos = max(0, duration - window - min(self.max_offset_s, duration * 0.05))
        positions = np.linspace(0, max_pos, 6)
        samples = []

        for position in positions:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise AudioSyncError("Analyse annulée.")
            measured = None
            for nudge in (0, -4.0, 4.0, -8.0, 8.0):
                p = position + nudge
                if p < 0 or p + window > duration:
                    continue
                try:
                    off, conf = self.measure(
                        reference,
                        donor,
                        float(p),
                        window,
                        cadence_filter=filter_str,
                        speed_factor=speed_factor,
                    )
                    measured = (p, off, conf)
                    break
                except AudioSyncError:
                    continue
            if measured is None:
                raise AudioSyncError("Corrélation acoustique insuffisante sur la fenêtre d'analyse.")
            p, offset, confidence = measured
            samples.append({"start_ms": float(p * 1000), "shift_ms": offset, "confidence": confidence})
            log(f"{p:.1f} s : {offset:+.1f} ms ({confidence:.2f})")

            # Analyse acoustique de hauteur tonale (sélection auto asetrate vs atempo) au 1er point mesuré
            if is_auto_method and pitch_analysis is None and cadence_mismatch and getattr(cadence_mismatch, "cadence_type", None) not in (None, "none"):
                pitch_analysis = self._analyze_cadence_pitch(reference, donor, p, offset, cadence_mismatch)
                if pitch_analysis is not None:
                    active_cadence_method = pitch_analysis.selected_method
                    filter_str = build_cadence_audio_filter(cadence_mismatch, active_cadence_method)
                    log(f"Analyse acoustique de cadence : méthode '{pitch_analysis.selected_method}' sélectionnée — {pitch_analysis.details}")

        spread = max(s["shift_ms"] for s in samples) - min(s["shift_ms"] for s in samples)
        if spread <= drift_threshold_ms:
            return SyncCalibration(
                (SyncSegment(0, float(np.median([s["shift_ms"] for s in samples]))),),
                min(s["confidence"] for s in samples),
                tuple(samples),
                cadence_mismatch=cadence_mismatch,
                cadence_audio_method=active_cadence_method,
                cadence_pitch_analysis=pitch_analysis,
            )

        from core.workflows.cadence import detect_cadence_from_acoustic_samples
        acoustic_mismatch = detect_cadence_from_acoustic_samples(samples)
        if acoustic_mismatch is not None:
            log(f"Différence de cadence détectée acoustiquement : {acoustic_mismatch.description}")
            if is_auto_method and pitch_analysis is None:
                p0 = samples[0]["start_ms"] / 1000.0
                off0 = samples[0]["shift_ms"]
                pitch_analysis = self._analyze_cadence_pitch(reference, donor, p0, off0, acoustic_mismatch)
                if pitch_analysis is not None:
                    active_cadence_method = pitch_analysis.selected_method
                    log(f"Analyse acoustique de cadence : méthode '{pitch_analysis.selected_method}' sélectionnée — {pitch_analysis.details}")
            return SyncCalibration(
                (SyncSegment(0, float(samples[0]["shift_ms"])),),
                min(s["confidence"] for s in samples),
                tuple(samples),
                cadence_mismatch=acoustic_mismatch,
                cadence_audio_method=active_cadence_method,
                cadence_pitch_analysis=pitch_analysis,
            )

        if not detect_cuts:
            raise AudioSyncError("Dérive détectée ; utiliser --detect-cuts ou une calibration manuelle.")
        segments = [SyncSegment(0, samples[0]["shift_ms"])]
        for left, right in zip(samples, samples[1:]):
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise AudioSyncError("Analyse annulée.")
            if abs(right["shift_ms"] - segments[-1].shift_ms) <= drift_threshold_ms:
                continue
            left_shift = segments[-1].shift_ms
            right_shift = right["shift_ms"]
            l = left["start_ms"] / 1000
            r = right["start_ms"] / 1000 + window
            check_window = min(6, window)
            for _ in range(10):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise AudioSyncError("Analyse annulée.")
                if r - l <= 8.0:
                    break
                mid = (l + r) / 2
                try:
                    off, _ = self.measure(
                        reference, donor, mid, check_window,
                        cadence_filter=filter_str, speed_factor=speed_factor,
                    )
                    if abs(off - left_shift) <= drift_threshold_ms:
                        l = mid
                    elif abs(off - right_shift) <= drift_threshold_ms:
                        r = mid
                    else:
                        l = max(l, mid - 10)
                        r = min(r, mid + 10)
                        break
                except AudioSyncError:
                    try:
                        off_early, _ = self.measure(
                            reference, donor, max(l, mid - 12), check_window,
                            cadence_filter=filter_str, speed_factor=speed_factor,
                        )
                        if abs(off_early - left_shift) <= drift_threshold_ms:
                            l = mid - 12
                    except AudioSyncError:
                        pass
                    try:
                        off_late, _ = self.measure(
                            reference, donor, min(r - check_window, mid + 12), check_window,
                            cadence_filter=filter_str, speed_factor=speed_factor,
                        )
                        if abs(off_late - right_shift) <= drift_threshold_ms:
                            r = mid + 12
                    except AudioSyncError:
                        pass
                    if r - l > 30:
                        l = max(l, mid - 15)
                        r = min(r, mid + 15)
                    break

            w_low = max(0, l - 5)
            w_high = min(duration, r + 5)
            values = self.samples(donor, w_low * speed_factor, w_high - w_low, cadence_filter=filter_str)
            frame = 160
            energy = np.sqrt(np.mean(values[:len(values) // frame * frame].reshape(-1, frame) ** 2, axis=1))
            quiet = energy < 0.0032
            edges = np.diff(np.r_[False, quiet, False].astype(int))
            candidates = [(a, b) for a, b in zip(np.where(edges == 1)[0], np.where(edges == -1)[0]) if b - a >= 30]
            transitions = [w_low + (a + b) / 200 for a, b in candidates]
            for black in self.black_transitions(donor, w_low * speed_factor, (w_high - w_low) * speed_factor):
                black_ref = black / speed_factor if speed_factor > 0 else black
                if not any(abs(black_ref - existing) < 0.3 for existing in transitions):
                    transitions.append(black_ref)

            best_cut = None
            for cut in sorted(transitions):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise AudioSyncError("Analyse annulée.")
                before = max(0, cut - check_window - self.max_offset_s)
                after = cut + self.max_offset_s
                if after + check_window > duration:
                    continue
                try:
                    prior, _ = self.measure(
                        reference, donor, before, check_window,
                        cadence_filter=filter_str, speed_factor=speed_factor,
                    )
                    following, _ = self.measure(
                        reference, donor, after, check_window,
                        cadence_filter=filter_str, speed_factor=speed_factor,
                    )
                except AudioSyncError:
                    continue
                if abs(prior - left_shift) <= drift_threshold_ms and abs(following - right_shift) <= drift_threshold_ms:
                    best_cut = cut
                    break

            if best_cut is None:
                best_cut = (l + r) / 2
            segments.append(SyncSegment(best_cut * 1000, right["shift_ms"]))

        return SyncCalibration(
            tuple(segments),
            min(s["confidence"] for s in samples),
            tuple(samples),
            cadence_mismatch=cadence_mismatch,
            cadence_audio_method=active_cadence_method,
            cadence_pitch_analysis=pitch_analysis,
        )
