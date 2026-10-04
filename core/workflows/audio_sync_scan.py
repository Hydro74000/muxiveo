"""Analyse acoustique multi-fenêtres et localisation conservatrice des ruptures."""
from __future__ import annotations

import json
import subprocess
import re
import time
from typing import TYPE_CHECKING, Any

from core.subprocess_utils import subprocess_text_kwargs, subprocess_windows_no_window_kwargs
from core.workflows.audio_sync import AudioSyncError, AudioSyncTrack
from core.workflows.sync_calibration import SyncCalibration, SyncSegment

if TYPE_CHECKING:
    from numpy import ndarray

# Marge de pré-roll avant la fenêtre ; le découpage exact précède les filtres
# de cadence pour conserver les coordonnées temporelles de la source.
_SEEK_PREROLL_S = 5.0

# Noms des canaux d'ambiance des dispositions que FFmpeg expose via ffprobe.
# Les 7.1 « wide » placent aussi des canaux avant après c3 : le nombre de
# canaux seul ne permet donc pas de choisir les surrounds.
_SURROUND_BY_LAYOUT = {
    "5.1": ("BL", "BR"),
    "5.1(side)": ("SL", "SR"),
    "6.1": ("SL", "SR", "BC"),
    "6.1(back)": ("BL", "BR", "BC"),
    "7.1": ("BL", "BR", "SL", "SR"),
    "7.1(wide)": ("BL", "BR"),
    "7.1(wide-side)": ("SL", "SR"),
    "5.1.2": ("SL", "SR"),
    "5.1.2(back)": ("BL", "BR"),
    "5.1.4": ("SL", "SR"),
    "7.1.2": ("BL", "BR", "SL", "SR"),
    "7.1.4": ("BL", "BR", "SL", "SR"),
}


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

    def _run(self, command: list[str], *, timeout: float, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
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

    def samples(self, track, start, duration, cadence_filter: str | None = None,
                downmix: str | None = None) -> ndarray:
        import numpy as np
        af = ",".join(filter_part for filter_part in (downmix, cadence_filter, "highpass=f=300", "lowpass=f=3000")
                      if filter_part)
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

    def _check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise AudioSyncError("Analyse annulée.")

    def channel_layout(self, track) -> tuple[int, str]:
        """Nombre de canaux et disposition de la piste analysée."""
        spec = str(track.stream_index).removeprefix("0:")
        result = self._run([self.ffprobe, "-v", "error", "-select_streams", spec,
                            "-show_entries", "stream=channels,channel_layout", "-of", "json", str(track.source_path)],
                           timeout=30, **subprocess_text_kwargs())
        if result.returncode:
            raise AudioSyncError("Impossible de lire la disposition audio ; revue manuelle de la synchro nécessaire.")
        try:
            stream = (json.loads(result.stdout or "{}").get("streams") or [])[0]
            return int(stream.get("channels") or 0), str(stream.get("channel_layout") or "")
        except (IndexError, TypeError, ValueError) as exc:
            raise AudioSyncError("Disposition audio indisponible ; revue manuelle de la synchro nécessaire.") from exc

    @staticmethod
    def dialogue_free_downmixes(ref_layout: tuple[int, str], donor_layout: tuple[int, str]) -> tuple[str, str] | None:
        """Mixages sans canal central : un doublage n'y modifie que peu le signal.

        5.1/7.1 : surrounds seuls (musique et effets communs aux versions).
        Stéréo : différence L−R, qui annule les dialogues centrés.
        """
        if min(ref_layout[0], donor_layout[0]) >= 6:
            reference_channels = _SURROUND_BY_LAYOUT.get(ref_layout[1])
            donor_channels = _SURROUND_BY_LAYOUT.get(donor_layout[1])
            if reference_channels is None or donor_channels is None:
                raise AudioSyncError("Disposition multicanal non reconnue ; revue manuelle de la synchro nécessaire.")
            return ("pan=mono|c0=" + "+".join(reference_channels),
                    "pan=mono|c0=" + "+".join(donor_channels))
        if min(ref_layout[0], donor_layout[0]) >= 2:
            return ("pan=mono|c0=c0-c1", "pan=mono|c0=c0-c1")
        return None

    def _dialogue_free_downmixes(self, reference, donor) -> tuple[str, str] | None:
        return self.dialogue_free_downmixes(self.channel_layout(reference), self.channel_layout(donor))

    def envelope(self, track, cadence_filter=None, downmix: str | None = None) -> ndarray:
        """Décode une fois la piste en enveloppe 1 kHz (4 Mo par 1000 s).

        Le sous-échantillonnage suit le redressement, après le mixage mono
        (`downmix` : filtre pan remplaçant le mixage de tous les canaux).
        Aucun seek : toutes les positions restent dans la chronologie source.
        """
        import numpy as np
        filters = [downmix or "aformat=channel_layouts=mono"]
        if cadence_filter:
            filters.append(cadence_filter)
        filters += ["highpass=f=300", "lowpass=f=3000", "aeval=abs(val(0))", "aresample=1000"]
        result = self._run([
            self.ffmpeg, "-nostdin", "-v", "error", "-i", str(track.source_path),
            "-map", f"0:{track.stream_index}", "-af", ",".join(filters),
            "-ac", "1", "-ar", "1000", "-f", "f32le", "pipe:1",
        ], timeout=max(120, self.duration(track) * 2), **subprocess_windows_no_window_kwargs())
        if result.returncode:
            raise AudioSyncError(result.stderr.decode("utf-8", errors="replace"))
        return np.frombuffer(result.stdout, dtype="<f4")

    def pitch_samples(self, track, start, duration, cadence_filter: str | None = None) -> ndarray:
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
            p_donor = max(0.0, (p_ref - offset_ms / 1000.0) * speed_factor)

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
        downmixes: tuple[str, str] | None = None,
    ):
        p_donor = start * speed_factor
        if downmixes is None:
            reference_samples = self.samples(reference, start, duration)
            donor_samples = self.samples(donor, p_donor, duration, cadence_filter=cadence_filter)
        else:
            reference_samples = self.samples(reference, start, duration, downmix=downmixes[0])
            donor_samples = self.samples(donor, p_donor, duration, cadence_filter=cadence_filter,
                                         downmix=downmixes[1])
        return self.correlate(
            reference_samples, donor_samples,
            self.max_offset_s * 1000,
        )

    def scan(self, reference: AudioSyncTrack, donor: AudioSyncTrack, *, detect_cuts=False,
             drift_threshold_ms=25, cadence_mismatch=None, cadence_audio_method="auto",
             log=lambda message: None, _downmixes: tuple[str, str] | None = None):
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
        samples: list[dict[str, Any]] = []

        method_proven_by_fallback = False
        measure_kwargs = {"downmixes": _downmixes} if _downmixes else {}

        def _measure_around(position: float, cadence_filter: str | None):
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
                        cadence_filter=cadence_filter,
                        speed_factor=speed_factor,
                        **measure_kwargs,
                    )
                    return p, off, conf
                except AudioSyncError:
                    continue
            return None

        for position in positions:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise AudioSyncError("Analyse annulée.")
            measured = _measure_around(position, filter_str)
            if (
                measured is None and is_auto_method and not samples and pitch_analysis is None
                and filter_str is not None and cadence_mismatch is not None
            ):
                # Mode auto : la mesure initiale suppose atempo (hauteur préservée).
                # Un donneur accéléré sans correction de hauteur ne corrèle qu'avec
                # asetrate : retenter avant d'abandonner.
                alternative = "asetrate" if active_cadence_method == "atempo" else "atempo"
                alt_filter = build_cadence_audio_filter(cadence_mismatch, alternative)
                measured = _measure_around(position, alt_filter)
                if measured is not None:
                    active_cadence_method, filter_str = alternative, alt_filter
                    method_proven_by_fallback = True
                    log(f"Cadence : corrélation obtenue avec la méthode '{alternative}' (repli automatique).")
            if measured is None:
                if _downmixes is None:
                    self._check_cancelled()
                    downmixes = self._dialogue_free_downmixes(reference, donor)
                    if downmixes is not None:
                        log("Corrélation mono insuffisante ; nouvelle analyse hors dialogues (surrounds ou L−R)…")
                        return self.scan(reference, donor, detect_cuts=detect_cuts,
                                         drift_threshold_ms=drift_threshold_ms,
                                         cadence_mismatch=cadence_mismatch,
                                         cadence_audio_method=cadence_audio_method,
                                         log=log, _downmixes=downmixes)
                raise AudioSyncError("Corrélation acoustique insuffisante sur la fenêtre d'analyse.")
            p, offset, confidence = measured
            samples.append({"start_ms": float(p * 1000), "shift_ms": offset, "confidence": confidence})
            log(f"{p:.1f} s : {offset:+.1f} ms ({confidence:.2f})")

            # Analyse acoustique de hauteur tonale (sélection auto asetrate vs atempo) au 1er point mesuré
            if is_auto_method and pitch_analysis is None and cadence_mismatch and getattr(cadence_mismatch, "cadence_type", None) not in (None, "none"):
                pitch_analysis = self._analyze_cadence_pitch(reference, donor, p, offset, cadence_mismatch)
                if pitch_analysis is not None and method_proven_by_fallback and pitch_analysis.selected_method != active_cadence_method:
                    # La corrélation mesurée prime sur l'heuristique spectrale/F0.
                    log(f"Analyse acoustique de cadence : '{pitch_analysis.selected_method}' suggérée mais seule "
                        f"'{active_cadence_method}' corrèle — conservée.")
                elif pitch_analysis is not None:
                    active_cadence_method = pitch_analysis.selected_method
                    filter_str = build_cadence_audio_filter(cadence_mismatch, active_cadence_method)
                    log(f"Analyse acoustique de cadence : méthode '{pitch_analysis.selected_method}' sélectionnée — {pitch_analysis.details}")

        spread = max(s["shift_ms"] for s in samples) - min(s["shift_ms"] for s in samples)
        if spread <= drift_threshold_ms and not detect_cuts:
            return SyncCalibration(
                (SyncSegment(0, float(np.median([s["shift_ms"] for s in samples]))),),
                min(s["confidence"] for s in samples),
                tuple(samples),
                cadence_mismatch=cadence_mismatch,
                cadence_audio_method=active_cadence_method,
                cadence_pitch_analysis=pitch_analysis,
            )

        from core.workflows.cadence import detect_cadence_from_acoustic_samples
        acoustic_mismatch = detect_cadence_from_acoustic_samples(samples) if spread > drift_threshold_ms else None
        if acoustic_mismatch is not None:
            log(f"Différence de cadence détectée acoustiquement : {acoustic_mismatch.description}")
            if is_auto_method and pitch_analysis is None:
                p0 = samples[0]["start_ms"] / 1000.0
                off0 = samples[0]["shift_ms"]
                pitch_analysis = self._analyze_cadence_pitch(reference, donor, p0, off0, acoustic_mismatch)
                if pitch_analysis is not None:
                    active_cadence_method = pitch_analysis.selected_method
                    log(f"Analyse acoustique de cadence : méthode '{pitch_analysis.selected_method}' sélectionnée — {pitch_analysis.details}")
            if not detect_cuts:
                return SyncCalibration(
                    (SyncSegment(0, float(samples[0]["shift_ms"])),),
                    min(s["confidence"] for s in samples),
                    tuple(samples),
                    cadence_mismatch=acoustic_mismatch,
                    cadence_audio_method=active_cadence_method,
                    cadence_pitch_analysis=pitch_analysis,
                )
            cadence_mismatch = acoustic_mismatch
            speed_factor = acoustic_mismatch.speed_factor
            filter_str = build_cadence_audio_filter(cadence_mismatch, active_cadence_method)

        if not detect_cuts:
            raise AudioSyncError("Dérive détectée ; utiliser --detect-cuts ou une calibration manuelle.")
        from core.workflows.audio_sync_segments import scan_envelopes
        log("Analyse continue des pistes et validation des jonctions…")
        max_offset_ms = round(self.max_offset_s * 1000)
        check_cancelled = self._check_cancelled

        def scan(reference_envelope: ndarray, donor_envelope: ndarray):
            return scan_envelopes(
                reference_envelope, donor_envelope,
                max_offset_ms=max_offset_ms, tolerance_ms=drift_threshold_ms,
                speed_factor=speed_factor, check_cancelled=check_cancelled, log=log,
            )
        if _downmixes is not None:
            segments, confidence, anchors = scan(
                self.envelope(reference, downmix=_downmixes[0]),
                self.envelope(donor, filter_str, downmix=_downmixes[1]))
        else:
            try:
                segments, confidence, anchors = scan(
                    self.envelope(reference), self.envelope(donor, filter_str))
            except AudioSyncError as exc:
                self._check_cancelled()
                # Pistes de langues différentes : les dialogues masquent la musique
                # et les effets communs. Seconde passe sans le canal central.
                downmixes = self._dialogue_free_downmixes(reference, donor)
                if downmixes is None:
                    raise
                log(f"{exc} Nouvelle analyse hors dialogues (surrounds ou L−R)…")
                segments, confidence, anchors = scan(
                    self.envelope(reference, downmix=downmixes[0]),
                    self.envelope(donor, filter_str, downmix=downmixes[1]))
        return SyncCalibration(
            segments, confidence, anchors,
            cadence_mismatch=cadence_mismatch,
            cadence_audio_method=active_cadence_method,
            cadence_pitch_analysis=pitch_analysis,
        )
