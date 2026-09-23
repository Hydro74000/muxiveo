"""Extraction, corrélation et synchronisation temporelle des pistes de sous-titres."""
from __future__ import annotations

import math
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from core.subprocess_utils import subprocess_windows_no_window_kwargs
from core.workflows.subtitle_sync import parse_time, _TIMING
from core.workflows.sync_calibration import SyncCalibration, SyncSegment


class SubtitleSyncError(Exception):
    """Erreur survenue pendant l'analyse ou la synchronisation de sous-titres."""
    pass


@dataclass(frozen=True)
class SubtitleCue:
    start_ms: float
    end_ms: float
    text: str = ""

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.end_ms - self.start_ms)


class SubtitleSyncScanner:
    """Scanner de synchronisation de sous-titres (sous-titre vs sous-titre ou sous-titre vs audio)."""

    def __init__(
        self,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        *,
        cancel_event=None,
    ) -> None:
        self.ffmpeg = str(ffmpeg)
        self.ffprobe = str(ffprobe)
        self.cancel_event = cancel_event

    def _run(self, command: list[str], *, timeout: float = 60.0, **kwargs) -> subprocess.CompletedProcess:
        kwargs.pop("capture_output", None)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise SubtitleSyncError("Analyse annulée par l'utilisateur.")
                if time.monotonic() >= deadline:
                    raise SubtitleSyncError("Délai d'analyse dépassé.")
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    @staticmethod
    def extract_cues_from_text(text: str, suffix: str = ".srt") -> list[SubtitleCue]:
        suffix = suffix.lower()
        cues: list[SubtitleCue] = []

        if suffix in {".ass", ".ssa"}:
            events = False
            fields: list[str] = []
            for line in text.splitlines():
                if line.startswith("["):
                    events = (line.strip().lower() == "[events]")
                if events and line.lower().startswith("format:"):
                    fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
                elif events and line.lower().startswith("dialogue:"):
                    if "start" in fields and "end" in fields:
                        values = line.split(":", 1)[1].split(",", len(fields) - 1)
                        if len(values) == len(fields):
                            try:
                                a, b = fields.index("start"), fields.index("end")
                                s_ms = parse_time(values[a])
                                e_ms = parse_time(values[b])
                                dialogue = values[-1].strip() if len(values) > 0 else ""
                                # Nettoyer les tags ASS
                                dialogue = re.sub(r"\{[^\}]*\}", "", dialogue).strip()
                                if e_ms > s_ms:
                                    cues.append(SubtitleCue(s_ms, e_ms, dialogue))
                            except (ValueError, IndexError):
                                continue
            return cues

        # Format SRT / WebVTT standard
        blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
        for block in blocks:
            match = _TIMING.search(block)
            if match:
                try:
                    s_ms = parse_time(match["a"])
                    e_ms = parse_time(match["b"])
                    body = block[match.end():].strip()
                    # Retirer les tags HTML basiques (ex. <i>, </b>)
                    body = re.sub(r"<[^>]+>", "", body).strip()
                    if e_ms > s_ms:
                        cues.append(SubtitleCue(s_ms, e_ms, body))
                except (ValueError, IndexError):
                    continue
        return cues

    def extract_cues(self, source_path: Path, stream_index: int | None = None) -> list[SubtitleCue]:
        source_path = Path(source_path)
        if not source_path.exists():
            raise SubtitleSyncError(f"Fichier introuvable : {source_path}")

        suffix = source_path.suffix.lower()
        if suffix in {".srt", ".vtt", ".ass", ".ssa"}:
            try:
                content = source_path.read_text(encoding="utf-8-sig", errors="replace")
                return self.extract_cues_from_text(content, suffix=suffix)
            except Exception as exc:
                raise SubtitleSyncError(f"Erreur de lecture du sous-titre {source_path.name} : {exc}") from exc

        # Extraction par FFmpeg depuis conteneur MKV / MP4
        if isinstance(stream_index, str) and (stream_index.startswith("0:") or ":" in stream_index):
            map_arg = stream_index
        elif stream_index is not None:
            map_arg = f"0:{stream_index}"
        else:
            map_arg = "0:s:0"
        cmd = [
            self.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(source_path),
            "-map",
            map_arg,
            "-f",
            "srt",
            "pipe:1",
        ]
        result = self._run(cmd, timeout=45.0, **subprocess_windows_no_window_kwargs())
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="replace") if isinstance(result.stderr, bytes) else str(result.stderr)
            raise SubtitleSyncError(f"Échec de l'extraction des sous-titres : {err}")

        text = result.stdout.decode("utf-8", errors="replace") if isinstance(result.stdout, bytes) else str(result.stdout)
        cues = self.extract_cues_from_text(text, suffix=".srt")
        if not cues:
            raise SubtitleSyncError(f"Aucune réplique de sous-titre trouvée dans {source_path.name}.")
        return cues

    @staticmethod
    def cues_to_activity(cues: list[SubtitleCue], total_ms: float, bin_ms: float = 20.0) -> np.ndarray:
        n_bins = max(1, int(math.ceil(total_ms / bin_ms)))
        arr = np.zeros(n_bins, dtype=np.float32)
        for c in cues:
            s_idx = max(0, min(n_bins - 1, int(round(c.start_ms / bin_ms))))
            e_idx = max(0, min(n_bins - 1, int(round(c.end_ms / bin_ms))))
            if e_idx > s_idx:
                arr[s_idx:e_idx] = 1.0
        return arr

    @staticmethod
    def correlate_cues(
        ref_cues: list[SubtitleCue],
        tgt_cues: list[SubtitleCue],
        *,
        max_offset_ms: float = 30000.0,
        bin_ms: float = 20.0,
    ) -> tuple[float, float]:
        """Corrélation temporelle entre deux listes de répliques.

        Retourne (offset_ms, confiance). L'offset est la valeur à ajouter
        au temps de la cible pour l'aligner avec la référence.
        """
        if not ref_cues or not tgt_cues:
            raise SubtitleSyncError("Sous-titres insuffisants pour la corrélation.")

        max_time_ref = max(c.end_ms for c in ref_cues)
        max_time_tgt = max(c.end_ms for c in tgt_cues)
        total_ms = max(max_time_ref, max_time_tgt) + max_offset_ms + 1000.0

        a = SubtitleSyncScanner.cues_to_activity(ref_cues, total_ms, bin_ms)
        b = SubtitleSyncScanner.cues_to_activity(tgt_cues, total_ms, bin_ms)

        # Retirer la moyenne pour centrer le signal
        a_norm = a - float(np.mean(a))
        b_norm = b - float(np.mean(b))
        n = len(a_norm)
        size = 1 << (2 * n - 1).bit_length()

        fa = np.fft.rfft(a_norm, size)
        fb = np.fft.rfft(b_norm, size)
        corr = np.fft.irfft(fa * np.conj(fb), size)

        limit_bins = max(1, int(round(max_offset_ms / bin_ms)))
        lags = np.arange(-limit_bins, limit_bins + 1)
        pa, pb = np.r_[0, np.cumsum(a_norm * a_norm)], np.r_[0, np.cumsum(b_norm * b_norm)]
        left_a, left_b = np.maximum(lags, 0), np.maximum(-lags, 0)
        count = n - np.abs(lags)
        denom = np.sqrt((pa[left_a + count] - pa[left_a]) * (pb[left_b + count] - pb[left_b]))
        scores = corr[lags % size] / np.maximum(denom, 1e-20)

        best_idx = int(np.argmax(scores))
        best_lag = lags[best_idx]
        confidence = float(scores[best_idx])
        confidence = max(0.0, min(1.0, confidence))

        # L'offset est le décalage à appliquer à la cible
        offset_ms = float(best_lag * bin_ms)
        return offset_ms, confidence

    @staticmethod
    def correlate_audio_and_cues(
        audio_samples: np.ndarray,
        tgt_cues: list[SubtitleCue],
        *,
        sample_rate: int = 16000,
        start_time_ms: float = 0.0,
        max_offset_ms: float = 30000.0,
        bin_ms: float = 20.0,
    ) -> tuple[float, float]:
        """Corrélation de l'énergie vocale audio avec l'activité des sous-titres cibles."""
        if len(audio_samples) < sample_rate * 2 or not tgt_cues:
            raise SubtitleSyncError("Données audio ou sous-titres insuffisantes.")

        # Calcul de l'enveloppe énergétique de l'audio par fenêtres de bin_ms
        samples_per_bin = max(1, int(round(sample_rate * bin_ms / 1000.0)))
        n_bins = len(audio_samples) // samples_per_bin
        if n_bins < 50:
            raise SubtitleSyncError("Extrait audio trop court.")

        audio_trimmed = audio_samples[:n_bins * samples_per_bin]
        energy = np.sqrt(np.mean(audio_trimmed.reshape(n_bins, samples_per_bin) ** 2, axis=1))

        # Cues de la cible ramenées au repère local de l'audio
        local_cues = [
            SubtitleCue(max(0.0, c.start_ms - start_time_ms), max(0.0, c.end_ms - start_time_ms), c.text)
            for c in tgt_cues
        ]
        duration_ms = n_bins * bin_ms
        sub_act = SubtitleSyncScanner.cues_to_activity(local_cues, duration_ms, bin_ms)

        n = min(len(energy), len(sub_act))
        a = energy[:n] - float(np.mean(energy[:n]))
        b = sub_act[:n] - float(np.mean(sub_act[:n]))

        size = 1 << (2 * n - 1).bit_length()
        fa = np.fft.rfft(a, size)
        fb = np.fft.rfft(b, size)
        corr = np.fft.irfft(fa * np.conj(fb), size)

        limit_bins = max(1, int(round(max_offset_ms / bin_ms)))
        limit_bins = min(limit_bins, n // 2)
        lags = np.arange(-limit_bins, limit_bins + 1)
        scores = corr[lags % size]

        best_idx = int(np.argmax(scores))
        best_lag = lags[best_idx]
        offset_ms = float(best_lag * bin_ms)

        denom = np.sqrt(np.sum(a * a) * np.sum(b * b))
        conf = float(scores[best_idx] / max(denom, 1e-12)) if denom > 0 else 0.0
        confidence = max(0.0, min(1.0, conf * 1.5))  # normalisation empirique pour audio/texte
        return offset_ms, confidence

    def scan(
        self,
        reference_source: Path,
        reference_stream_index: int | str = 0,
        target_source: Path = Path("."),
        target_stream_index: int | str = 0,
        *,
        is_ref_sub: bool = True,
        detect_cuts: bool = True,
        log: Callable[[str], None] | None = None,
    ) -> SyncCalibration:
        """Effectue une analyse complète et retourne un SyncCalibration prêt à l'emploi."""
        _log = log or (lambda _: None)
        _log(f"Extraction des sous-titres cibles ({target_source.name} #{target_stream_index})…")
        tgt_cues = self.extract_cues(target_source, target_stream_index)
        _log(f"{len(tgt_cues)} répliques extraites pour la cible.")

        if is_ref_sub:
            _log(f"Extraction des sous-titres de référence ({reference_source.name} #{reference_stream_index})…")
            ref_cues = self.extract_cues(reference_source, reference_stream_index)
            _log(f"{len(ref_cues)} répliques extraites pour la référence.")

            # Analyse globale initiale
            offset_ms, confidence = self.correlate_cues(ref_cues, tgt_cues)
            _log(f"Alignement global : {offset_ms:+.1f} ms (confiance {confidence:.0%})")

            if not detect_cuts:
                return SyncCalibration.linear(round(offset_ms))

            # Recherche de coupures multi-segments
            max_t = min(max(c.end_ms for c in ref_cues), max(c.end_ms for c in tgt_cues))
            window_ms = 180000.0  # 3 minutes
            step_ms = 120000.0    # pas de 2 minutes
            samples: list[dict] = []

            cur_start = 0.0
            while cur_start + window_ms <= max_t:
                cur_end = cur_start + window_ms
                sub_ref = [c for c in ref_cues if cur_start <= c.start_ms < cur_end]
                sub_tgt = [c for c in tgt_cues if cur_start - 30000.0 <= c.start_ms < cur_end + 30000.0]
                if len(sub_ref) >= 8 and len(sub_tgt) >= 8:
                    try:
                        off, conf = self.correlate_cues(sub_ref, sub_tgt, max_offset_ms=45000.0)
                        if conf >= 0.25:
                            samples.append({
                                "start_ms": cur_start,
                                "end_ms": cur_end,
                                "shift_ms": round(off, 1),
                                "confidence": conf,
                            })
                            _log(f"  Fenêtre {cur_start/1000:.0f}s-{cur_end/1000:.0f}s : {off:+.1f} ms ({conf:.0%})")
                    except Exception:
                        pass
                cur_start += step_ms

            if len(samples) < 2:
                return SyncCalibration.linear(round(offset_ms))

            spread = max(s["shift_ms"] for s in samples) - min(s["shift_ms"] for s in samples)
            if spread <= 40.0:
                median_offset = float(np.median([s["shift_ms"] for s in samples]))
                return SyncCalibration.linear(round(median_offset))

            # Construction des segments de coupures
            segments = [SyncSegment(0.0, float(samples[0]["shift_ms"]))]
            for prev_s, next_s in zip(samples, samples[1:]):
                diff = next_s["shift_ms"] - segments[-1].shift_ms
                if abs(diff) > 40.0:
                    # Trouver la position de coupure entre les répliques
                    cut_pos_ms = (prev_s["end_ms"] + next_s["start_ms"]) / 2.0
                    segments.append(SyncSegment(round(cut_pos_ms, 1), float(next_s["shift_ms"])))
                    _log(f"  ✂ Coupure détectée à {SyncCalibration.format_timestamp(cut_pos_ms)} -> saut de {diff:+.1f} ms")

            return SyncCalibration(tuple(segments), confidence)

        else:
            # Référence audio vs cible sous-titres
            _log(f"Extraction audio de référence ({reference_source.name} #{reference_stream_index})…")
            from core.workflows.audio_sync_scan import AudioSyncScanner
            audio_scanner = AudioSyncScanner(self.ffmpeg, self.ffprobe)
            from core.workflows.audio_sync import AudioSyncTrack
            ref_track = AudioSyncTrack(reference_source, reference_stream_index)
            # Analyser les premières minutes (ou fenêtres significatives)
            duration_s = min(300.0, audio_scanner.duration(ref_track))
            samples = audio_scanner.samples(ref_track, 0.0, duration_s)
            offset_ms, confidence = self.correlate_audio_and_cues(
                samples,
                tgt_cues,
                sample_rate=16000,
                start_time_ms=0.0,
                max_offset_ms=30000.0,
            )
            _log(f"Alignement audio/sous-titres : {offset_ms:+.1f} ms (confiance {confidence:.0%})")
            return SyncCalibration.linear(round(offset_ms))
