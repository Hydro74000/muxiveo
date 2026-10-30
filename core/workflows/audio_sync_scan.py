"""Analyse acoustique multi-fenêtres et localisation conservatrice des ruptures."""
from __future__ import annotations

from dataclasses import replace
import json
import subprocess
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from core.subprocess_utils import subprocess_text_kwargs, subprocess_windows_no_window_kwargs
from core.workflows.audio_sync import AudioSyncError, AudioSyncReviewRequired, AudioSyncTrack, format_sync_ranges
from core.workflows.cadence import active_cadence
from core.workflows.sync_calibration import SyncCalibration, SyncSegment

if TYPE_CHECKING:
    from numpy import ndarray

# Marge de pré-roll avant la fenêtre ; le découpage exact précède les filtres
# de cadence pour conserver les coordonnées temporelles de la source.
_SEEK_PREROLL_S = 5.0

# Écart propre à la méthode de sortie (atempo) face à la mesure exacte : mesuré aux positions
# ponctuelles les plus sûres ; au-delà de cet étalement, les mesures ne concordent pas.
_OUTPUT_BIAS_POSITIONS = 4
_OUTPUT_BIAS_SPREAD_MS = 20.0

# Vérification rapide : confiance minimale de chacune des mesures ponctuelles
# pour se passer de l'analyse continue quand le décalage est constant.
_QUICK_CHECK_MIN_CONFIDENCE = 0.9

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


def quick_check_enabled(options: Any, config: Any) -> bool:
    """Vérification rapide : option explicite (CLI), sinon réglage `[sync] quick_audio_check`."""
    value = getattr(options, "quick_sync_check", None)
    if value is None:
        value = getattr(config, "sync_quick_audio_check", False)
    return bool(value)


def coherent_sync_accepted(options: Any) -> bool:
    """Synchro cohérente en partie non vérifiée acceptée, sauf `--reject-coherent-sync` (CLI)."""
    return not bool(getattr(options, "reject_coherent_sync", False))


def _unverified_total_ms(ranges) -> float:
    return float(sum(stop - start for start, stop in ranges))


class AudioSyncScanner:
    def __init__(self, ffmpeg="ffmpeg", ffprobe="ffprobe", *, window_s=60, max_offset_s=30, cancel_event=None,
                 quick_check=False, accept_coherent=False):
        """`quick_check` : décalage constant et mesures sûres → pas d'analyse continue
        (les écarts qui se compensent entre deux mesures ne sont alors pas vus).

        `accept_coherent` : une synchro cohérente en partie non vérifiée est
        acceptée avec un avertissement ; sinon `AudioSyncReviewRequired`.
        """
        self.ffmpeg, self.ffprobe = str(ffmpeg), str(ffprobe)
        self.window_s, self.max_offset_s = window_s, max_offset_s
        self.cancel_event = cancel_event
        self.quick_check = bool(quick_check)
        self.accept_coherent = bool(accept_coherent)
        # Sondes FFprobe mémorisées par fichier ou piste (les deux pistes sont décodées en parallèle).
        self._probes: dict[tuple[str, ...], Any] = {}
        self._probes_lock = threading.Lock()

    def _run(self, command: list[str], *, timeout: float, abort: threading.Event | None = None,
             **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        """Interrompt aussi les extractions FFmpeg lorsque l'utilisateur annule ou que `abort` est levé."""
        kwargs.pop("capture_output", None)
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)  # nosec B603
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise AudioSyncError("Analyse annulée.")
                if abort is not None and abort.is_set():
                    raise AudioSyncError("Analyse interrompue.")
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

    def _probed(self, key: tuple[str, ...], probe):
        """Résultat de `probe()` mémorisé sous `key` ; un échec n'est pas mémorisé."""
        with self._probes_lock:
            if key in self._probes:
                return self._probes[key]
        value = probe()
        with self._probes_lock:
            self._probes[key] = value
        return value

    def duration(self, track):
        return self._probed(("duration", str(track.source_path)), lambda: self._probe_duration(track))

    def _probe_duration(self, track) -> float:
        result = self._run([self.ffprobe, "-v", "error", "-show_entries",
                                 "format=duration", "-of", "json", str(track.source_path)],
                                capture_output=True, timeout=30, **subprocess_text_kwargs())
        if result.returncode:
            raise AudioSyncError(result.stderr)
        duration = float(json.loads(result.stdout)["format"]["duration"])
        if duration <= 0:
            raise AudioSyncError("Durée audio invalide.")
        return duration

    def _stream(self, track) -> dict[str, Any]:
        """Codec, canaux, disposition et fréquence de la piste analysée."""
        return self._probed(("stream", str(track.source_path), str(track.stream_index)),
                            lambda: self._probe_stream(track))

    def _probe_stream(self, track) -> dict[str, Any]:
        result = self._run([self.ffprobe, "-v", "error", "-select_streams", str(track.stream_index).removeprefix("0:"),
                            "-show_entries", "stream=codec_name,channels,channel_layout,sample_rate",
                            "-of", "json", str(track.source_path)],
                           timeout=30, **subprocess_text_kwargs())
        if result.returncode:
            raise AudioSyncError("Impossible de lire la disposition audio ; revue manuelle de la synchro nécessaire.")
        try:
            stream = (json.loads(result.stdout or "{}").get("streams") or [])[0]
        except (IndexError, TypeError, ValueError, AttributeError) as exc:
            raise AudioSyncError("Disposition audio indisponible ; revue manuelle de la synchro nécessaire.") from exc
        if not isinstance(stream, dict):
            raise AudioSyncError("Disposition audio indisponible ; revue manuelle de la synchro nécessaire.")
        return stream

    def _decoder_args(self, track) -> list[str]:
        """DTS 5.1 au plus à 48 kHz au plus : cœur seul (`-core_only`). L'extension sans perte
        (DTS-HD MA) ne change pas l'enveloppe analysée et triple le temps de décodage. Au-delà
        (7.1, 6.1 ES, 96 kHz), le cœur n'a ni les mêmes canaux ni la même fréquence que la
        disposition sondée : décodage complet."""
        try:
            stream = self._stream(track)
            core_only = (stream.get("codec_name") == "dts" and 0 < int(stream.get("channels") or 0) <= 6
                         and 0 < int(stream.get("sample_rate") or 0) <= 48000)
        except AudioSyncError:
            self._check_cancelled()
            return []
        except (TypeError, ValueError):
            return []
        return ["-core_only", "1"] if core_only else []

    def samples(self, track, start, duration, cadence_filter: str | None = None,
                downmix: str | None = None) -> ndarray:
        import numpy as np
        af = ",".join(filter_part for filter_part in (downmix, cadence_filter, "highpass=f=300", "lowpass=f=3000")
                      if filter_part)
        seek_in, trim_filters = _seek_args(start)
        af = ",".join([*trim_filters, af])
        result = self._run([
            self.ffmpeg, "-v", "error", *seek_in, *self._decoder_args(track), "-i", str(track.source_path),
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
        stream = self._stream(track)
        try:
            return int(stream.get("channels") or 0), str(stream.get("channel_layout") or "")
        except (TypeError, ValueError) as exc:
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

    @staticmethod
    def _envelope_chain(cadence_filter, downmix: str | None) -> list[str]:
        filters = [downmix or "aformat=channel_layouts=mono"]
        if cadence_filter:
            filters.append(cadence_filter)
        return [*filters, "highpass=f=300", "lowpass=f=3000", "aeval=abs(val(0))", "aresample=1000"]

    def envelope(self, track, cadence_filter=None, downmix: str | None = None, *,
                 abort: threading.Event | None = None) -> ndarray:
        """Décode une fois la piste en enveloppe 1 kHz (4 Mo par 1000 s).

        Le sous-échantillonnage suit le redressement, après le mixage mono
        (`downmix` : filtre pan remplaçant le mixage de tous les canaux).
        Aucun seek : toutes les positions restent dans la chronologie source.
        """
        import numpy as np
        result = self._run([
            self.ffmpeg, "-nostdin", "-v", "error", *self._decoder_args(track), "-i", str(track.source_path),
            "-map", f"0:{track.stream_index}", "-af", ",".join(self._envelope_chain(cadence_filter, downmix)),
            "-ac", "1", "-ar", "1000", "-f", "f32le", "pipe:1",
        ], timeout=max(120, self.duration(track) * 2), abort=abort, **subprocess_windows_no_window_kwargs())
        if result.returncode:
            raise AudioSyncError(result.stderr.decode("utf-8", errors="replace"))
        return np.frombuffer(result.stdout, dtype="<f4")

    def envelope_mixes(self, track, cadence_filter, downmixes: tuple[str | None, ...], *,
                       abort: threading.Event | None = None) -> list[ndarray]:
        """Enveloppes de plusieurs mixages (`envelope`) en une seule lecture du fichier.

        La lecture du conteneur entier (vidéo comprise) domine le temps d'analyse :
        une passe par mixage le relirait. Valeurs identiques à `envelope`, voie par voie.
        """
        import numpy as np
        count = len(downmixes)
        graph = [f"[0:{track.stream_index}]asplit={count}" + "".join(f"[s{i}]" for i in range(count))]
        graph += [f"[s{i}]" + ",".join([*self._envelope_chain(cadence_filter, downmix),
                                        "aformat=sample_fmts=flt:channel_layouts=mono"]) + f"[e{i}]"
                  for i, downmix in enumerate(downmixes)]
        graph.append("".join(f"[e{i}]" for i in range(count)) + f"amerge=inputs={count}[out]")
        result = self._run([
            self.ffmpeg, "-nostdin", "-v", "error", *self._decoder_args(track), "-i", str(track.source_path),
            "-filter_complex", ";".join(graph), "-map", "[out]", "-f", "f32le", "pipe:1",
        ], timeout=max(120, self.duration(track) * 2), abort=abort, **subprocess_windows_no_window_kwargs())
        if result.returncode:
            raise AudioSyncError(result.stderr.decode("utf-8", errors="replace"))
        values = np.frombuffer(result.stdout, dtype="<f4")
        values = values[:len(values) // count * count].reshape(-1, count)
        return [np.ascontiguousarray(values[:, i]) for i in range(count)]

    def _envelopes(self, reference, donor, cadence_filter=None,
                   downmixes: tuple[str, str] | None = None) -> tuple[ndarray, ndarray]:
        return self._envelope_pairs(reference, donor, cadence_filter, (downmixes,))[0]

    def _envelope_pairs(self, reference, donor, cadence_filter,
                        mixes: tuple[tuple[str, str] | None, ...]) -> list[tuple[ndarray, ndarray]]:
        """Décode les deux pistes en parallèle (deux FFmpeg simultanés), chacune une seule fois
        pour tous les `mixes` (paires référence/donneur, None = mixage mono complet).

        Le premier échec interrompt l'autre décodage ; c'est lui qui est remonté.
        """
        from concurrent.futures import ThreadPoolExecutor
        abort = threading.Event()
        failures: list[BaseException] = []

        def decode(track, cadence, downmixes: tuple[str | None, ...]):
            try:
                if len(downmixes) == 1:
                    return [self.envelope(track, cadence, downmix=downmixes[0], abort=abort)]
                return self.envelope_mixes(track, cadence, downmixes, abort=abort)
            except BaseException as exc:
                failures.append(exc)
                abort.set()
                raise

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="audio-envelope") as pool:
            reference_future = pool.submit(decode, reference, None, tuple(None if mix is None else mix[0] for mix in mixes))
            donor_future = pool.submit(decode, donor, cadence_filter,
                                       tuple(None if mix is None else mix[1] for mix in mixes))
        if failures:
            raise failures[0]
        return list(zip(reference_future.result(), donor_future.result()))

    def pitch_samples(self, track, start, duration, cadence_filter: str | None = None) -> ndarray:
        """Extrait les échantillons audio sans filtre passe-bande agressif pour l'analyse F0 et spectrale."""
        import numpy as np
        af = "lowpass=f=4000"
        if cadence_filter:
            af = f"{cadence_filter},{af}"
        seek_in, trim_filters = _seek_args(start)
        af = ",".join([*trim_filters, af])
        result = self._run([
            self.ffmpeg, "-v", "error", *seek_in, *self._decoder_args(track), "-i", str(track.source_path),
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
        donor_start: float | None = None,
    ):
        """Exécute l'analyse acoustique de hauteur tonale (F0 et spectre de Welch).

        `donor_start` : instant de la cible à analyser, si `offset_ms` n'a pas été mesuré sur la
        chronologie convertie (sinon déduit de `p_ref`, `offset_ms` et de la cadence).
        """
        try:
            from core.workflows.cadence import CadenceAudioMethod, build_cadence_audio_filter
            from core.workflows.cadence_pitch import analyze_cadence_pitch

            speed_factor = getattr(cadence_mismatch, "speed_factor", 1.0)
            p_donor = (donor_start if donor_start is not None
                       else max(0.0, (p_ref - offset_ms / 1000.0) * speed_factor))

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
            # Annulation : jamais confondue avec une analyse sans conclusion (atempo décidé à tort).
            self._check_cancelled()
            return None

    def _with_output_method_bias(self, calibration: SyncCalibration, reference: AudioSyncTrack,
                                 donor: AudioSyncTrack, samples, window: float, measure_method: str,
                                 measure_kwargs: dict[str, Any], log,
                                 samples_filter: str | None = None) -> SyncCalibration:
        """Synchro mesurée en `measure_method`, sortie dans une autre méthode : l'écart propre à
        celle-ci (atempo : avance de 6 à 22 ms selon cadence et débit, fenêtres WSOLA), mesuré aux
        positions les plus sûres, est ajouté à toute la synchro. Moins de 3 mesures
        concordantes : synchro de la mesure conservée (écart inférieur au seuil de perception).

        `samples_filter` : filtre des mesures ponctuelles ; égal au filtre exact, elles sont reprises
        telles quelles (même position, fenêtre et options) au lieu d'être refaites."""
        import numpy as np
        from core.workflows.cadence import build_cadence_audio_filter

        mismatch = calibration.active_cadence
        # Méthode « auto » non tranchée : filtre atempo à l'application.
        output_method = calibration.cadence_audio_method if calibration.cadence_audio_method != "auto" else "atempo"
        if mismatch is None or output_method == measure_method:
            return calibration
        exact_filter = build_cadence_audio_filter(mismatch, measure_method)
        output_filter = build_cadence_audio_filter(mismatch, output_method)
        differences: list[float] = []
        exact_measured = 0
        reuse = samples_filter == exact_filter
        for sample in sorted(samples, key=lambda item: -item["confidence"])[:_OUTPUT_BIAS_POSITIONS]:
            self._check_cancelled()
            position = sample["start_ms"] / 1000.0
            try:
                if reuse:
                    exact = sample["shift_ms"]
                else:
                    exact, _ = self.measure(reference, donor, position, window, cadence_filter=exact_filter,
                                            speed_factor=mismatch.speed_factor, **measure_kwargs)
                exact_measured += 1
                applied, _ = self.measure(reference, donor, position, window, cadence_filter=output_filter,
                                          speed_factor=mismatch.speed_factor, **measure_kwargs)
            except AudioSyncError:
                continue
            differences.append(applied - exact)
        if not differences and exact_measured and calibration.cadence_method_auto:
            # Mode auto : la méthode de sortie ne corrèle nulle part, la mesure oui ; la corrélation
            # prime sur l'heuristique de hauteur.
            log(f"Cadence : seule la méthode '{measure_method}' corrèle (repli automatique), retenue.")
            return replace(calibration, cadence_audio_method=measure_method)
        if len(differences) < 3 or max(differences) - min(differences) > _OUTPUT_BIAS_SPREAD_MS:
            log(f"Écart propre à la méthode '{output_method}' non mesurable : synchro de la mesure "
                f"'{measure_method}' conservée (écart attendu inférieur à 25 ms).")
            return calibration
        bias = float(np.median(differences))
        log(f"Méthode '{output_method}' : écart de {bias:+.1f} ms face à la mesure '{measure_method}', compensé.")
        return replace(
            calibration,
            segments=tuple(SyncSegment(segment.start_ms, segment.shift_ms + bias) for segment in calibration.segments),
            samples=tuple({**item, "shift_ms": item["shift_ms"] + bias} for item in calibration.samples),
        )

    def resolve_cadence_method(self, reference: AudioSyncTrack, donor: AudioSyncTrack,
                               calibration: SyncCalibration, log=lambda message: None) -> SyncCalibration:
        """Méthode « auto » d'une conversion de cadence réglée sans mesure, tranchée par la hauteur.

        Point analysé : milieu du plus long morceau joué de la calibration. Le décalage y est
        d'abord mesuré (réglage manuel encore approximatif, studio ouvert à 0 ms) ; sans
        corrélation, celui de la calibration. Sans conclusion, « atempo » (hauteur de la cible
        conservée), comme le filtre d'une méthode « auto ».
        """
        from core.workflows.cadence import build_cadence_audio_filter

        mismatch = calibration.active_cadence
        if mismatch is None or not calibration.cadence_method_undecided:
            return calibration
        duration_s = self.duration(reference)
        end_ms = duration_s * 1000.0
        played = [(start, min(stop, end_ms), index) for start, stop, index in calibration.output_ranges()
                  if min(stop, end_ms) > start]
        analysis = None
        if played:
            start, stop, index = max(played, key=lambda item: item[1] - item[0])
            middle_s = (start + stop) / 2000.0
            window = min(self.window_s, duration_s / 3)
            window_start = max(0.0, min(middle_s - window / 2, duration_s - window))
            try:
                offset_ms, _confidence = self.measure(
                    reference, donor, window_start, window,
                    cadence_filter=build_cadence_audio_filter(mismatch, "asetrate"),
                    speed_factor=mismatch.speed_factor,
                )
            except AudioSyncError:
                self._check_cancelled()
                offset_ms = calibration.segments[index].shift_ms
            analysis = self._analyze_cadence_pitch(
                reference, donor, window_start + window / 2 - 2.0, offset_ms, mismatch,
            )
        if analysis is None:
            log("Analyse de hauteur impossible : méthode 'atempo' (hauteur de la cible conservée).")
            return replace(calibration, cadence_audio_method="atempo", cadence_method_auto=True)
        log(f"Analyse acoustique de cadence : méthode '{analysis.selected_method}' sélectionnée — {analysis.details}")
        return replace(calibration, cadence_audio_method=analysis.selected_method, cadence_pitch_analysis=analysis,
                       cadence_method_auto=True)

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
             log=lambda message: None, warn=None, _downmixes: tuple[str, str] | None = None):
        """`warn` : avertissements (synchro cohérente acceptée) ; défaut : `log`."""
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

        # Mesure : rééchantillonnage exact (asetrate) ; l'étirement d'atempo (WSOLA) dégrade les
        # corrélations. La méthode de sortie reste celle demandée ; son écart propre est compensé
        # à la fin (`_with_output_method_bias`).
        measure_method = "asetrate"
        filter_str = None
        speed_factor = 1.0
        if active_cadence(cadence_mismatch) is not None:
            filter_str = build_cadence_audio_filter(cadence_mismatch, measure_method)
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
            if (measured is None and (is_auto_method or active_cadence_method == "atempo")
                    and measure_method == "asetrate" and not samples
                    and filter_str is not None and cadence_mismatch is not None):
                # Aucune corrélation en asetrate : retenter avec atempo avant d'abandonner. En mode
                # auto, seule cette méthode corrèle : elle devient celle de sortie.
                alternative = "atempo" if measure_method == "asetrate" else "asetrate"
                alt_filter = build_cadence_audio_filter(cadence_mismatch, alternative)
                measured = _measure_around(position, alt_filter)
                if measured is not None:
                    measure_method, filter_str = alternative, alt_filter
                    if is_auto_method:
                        active_cadence_method = alternative
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
                                         log=log, warn=warn, _downmixes=downmixes)
                raise AudioSyncError("Corrélation acoustique insuffisante sur la fenêtre d'analyse.")
            p, offset, confidence = measured
            samples.append({"start_ms": float(p * 1000), "shift_ms": offset, "confidence": confidence})
            log(f"{p:.1f} s : {offset:+.1f} ms ({confidence:.2f})")

            # Analyse acoustique de hauteur tonale (sélection auto asetrate vs atempo) au 1er point mesuré
            if is_auto_method and pitch_analysis is None and active_cadence(cadence_mismatch) is not None:
                # Milieu de la fenêtre mesurée : le décalage y vaut, la cible n'y est pas bornée à 0.
                pitch_analysis = self._analyze_cadence_pitch(
                    reference, donor, p + window / 2 - 2.0, offset, cadence_mismatch)
                if pitch_analysis is not None and method_proven_by_fallback and pitch_analysis.selected_method != active_cadence_method:
                    # La corrélation mesurée prime sur l'heuristique spectrale/F0.
                    log(f"Analyse acoustique de cadence : '{pitch_analysis.selected_method}' suggérée mais seule "
                        f"'{active_cadence_method}' corrèle — conservée.")
                elif pitch_analysis is not None:
                    active_cadence_method = pitch_analysis.selected_method
                    log(f"Analyse acoustique de cadence : méthode '{pitch_analysis.selected_method}' sélectionnée — {pitch_analysis.details}")

        # Filtre des mesures ponctuelles (celui de l'analyse continue peut changer ensuite).
        samples_filter = filter_str
        spread = max(s["shift_ms"] for s in samples) - min(s["shift_ms"] for s in samples)
        weakest = min(s["confidence"] for s in samples)
        quick = detect_cuts and self.quick_check and spread <= drift_threshold_ms
        if quick and weakest < _QUICK_CHECK_MIN_CONFIDENCE:
            log(f"Vérification rapide écartée : confiance minimale {weakest:.2f} < {_QUICK_CHECK_MIN_CONFIDENCE:.2f}.")
            quick = False
        if spread <= drift_threshold_ms and (not detect_cuts or quick):
            if quick:
                log(f"Vérification rapide : {len(samples)} mesures concordantes (écart {spread:.1f} ms), "
                    "analyse continue ignorée.")
            return self._with_output_method_bias(SyncCalibration(
                (SyncSegment(0, float(np.median([s["shift_ms"] for s in samples]))),),
                weakest,
                tuple(samples),
                cadence_mismatch=cadence_mismatch,
                cadence_audio_method=active_cadence_method,
                cadence_pitch_analysis=pitch_analysis,
                quick_check=quick,
                cadence_method_auto=is_auto_method,
            ), reference, donor, samples, window, measure_method, measure_kwargs, log, samples_filter)

        from core.workflows.cadence import detect_cadence_from_acoustic_samples
        acoustic_mismatch = detect_cadence_from_acoustic_samples(samples) if spread > drift_threshold_ms else None
        if acoustic_mismatch is not None:
            log(f"Différence de cadence détectée acoustiquement : {acoustic_mismatch.description}")
            if is_auto_method and pitch_analysis is None:
                p0 = samples[0]["start_ms"] / 1000.0 + window / 2 - 2.0
                off0 = samples[0]["shift_ms"]
                # Décalage mesuré sans conversion (dérive moyenne de la fenêtre, valable en son
                # milieu) : la cible y est lue au même instant moins ce décalage, sans étirement.
                pitch_analysis = self._analyze_cadence_pitch(
                    reference, donor, p0, off0, acoustic_mismatch, donor_start=max(0.0, p0 - off0 / 1000.0))
                if pitch_analysis is not None:
                    active_cadence_method = pitch_analysis.selected_method
                    log(f"Analyse acoustique de cadence : méthode '{pitch_analysis.selected_method}' sélectionnée — {pitch_analysis.details}")
            if not detect_cuts:
                return self._with_output_method_bias(SyncCalibration(
                    (SyncSegment(0, float(samples[0]["shift_ms"])),),
                    min(s["confidence"] for s in samples),
                    tuple(samples),
                    cadence_mismatch=acoustic_mismatch,
                    cadence_audio_method=active_cadence_method,
                    cadence_pitch_analysis=pitch_analysis,
                    cadence_method_auto=is_auto_method,
                ), reference, donor, samples, window, measure_method, measure_kwargs, log, samples_filter)
            cadence_mismatch = acoustic_mismatch
            speed_factor = acoustic_mismatch.speed_factor
            filter_str = build_cadence_audio_filter(cadence_mismatch, measure_method)

        if not detect_cuts:
            raise AudioSyncError("Dérive détectée ; utiliser --detect-cuts ou une calibration manuelle.")
        from core.workflows.audio_sync_segments import scan_envelopes
        log("Analyse continue des pistes et validation des jonctions…")
        max_offset_ms = round(self.max_offset_s * 1000)
        check_cancelled = self._check_cancelled

        def scan(envelopes: tuple[ndarray, ndarray]):
            review: list[tuple[float, float]] = []
            result = scan_envelopes(
                *envelopes,
                max_offset_ms=max_offset_ms, tolerance_ms=drift_threshold_ms,
                speed_factor=speed_factor, check_cancelled=check_cancelled, log=log, review=review,
            )
            return result, tuple(review)
        if _downmixes is not None:
            (segments, confidence, anchors), unverified = scan(
                self._envelopes(reference, donor, filter_str, _downmixes))
        else:
            # Pistes de langues différentes : les dialogues masquent la musique
            # et les effets communs. Seconde passe sans le canal central, décodée
            # avec la première : une seule lecture de chaque fichier.
            layout_error: Exception | None = None
            try:
                downmixes = self._dialogue_free_downmixes(reference, donor)
            except (AudioSyncError, OSError) as exc:
                self._check_cancelled()
                downmixes, layout_error = None, exc
            if downmixes is None:
                mono, dialogue_free = self._envelopes(reference, donor, filter_str), None
            else:
                mono, dialogue_free = self._envelope_pairs(reference, donor, filter_str, (None, downmixes))
            try:
                (segments, confidence, anchors), unverified = scan(mono)
            except AudioSyncError as exc:
                self._check_cancelled()
                if layout_error is not None:
                    raise layout_error from exc
                if dialogue_free is None:
                    raise
                log(f"{exc} Nouvelle analyse hors dialogues (surrounds ou L−R)…")
                (segments, confidence, anchors), unverified = scan(dialogue_free)
            else:
                if unverified and layout_error is not None:
                    # Disposition non reconnue (6.0…) : la proposition mono reste à valider.
                    log(f"{layout_error} Analyse mono conservée.")
                if unverified and dialogue_free is not None:
                    # Zones mono sans correspondance : la passe hors dialogues peut les vérifier.
                    log("Zones sans correspondance en mono ; nouvelle analyse hors dialogues (surrounds ou L−R)…")
                    try:
                        alternative = scan(dialogue_free)
                    except AudioSyncError as exc:
                        self._check_cancelled()
                        log(f"{exc} Analyse mono conservée.")
                    else:
                        if _unverified_total_ms(alternative[1]) < _unverified_total_ms(unverified):
                            (segments, confidence, anchors), unverified = alternative
        calibration = self._with_output_method_bias(SyncCalibration(
            segments, confidence, anchors,
            cadence_mismatch=cadence_mismatch,
            cadence_audio_method=active_cadence_method,
            cadence_pitch_analysis=pitch_analysis,
            cadence_method_auto=is_auto_method,
        ), reference, donor, samples, window, measure_method, measure_kwargs, log, samples_filter)
        if unverified:
            proposal = AudioSyncReviewRequired(
                f"Synchro cohérente à valider : {len(unverified)} zone(s) sans correspondance, "
                "même décalage de part et d'autre.",
                calibration, unverified,
            )
            zones = format_sync_ranges(proposal.output_ranges_ms())
            if not self.accept_coherent:
                raise AudioSyncReviewRequired(f"{proposal} Zones à relire : {zones}.", calibration, unverified)
            (warn or log)(f"Synchro cohérente acceptée malgré {len(unverified)} zone(s) sans correspondance "
                          f"(même décalage de part et d'autre) : {zones}.")
        return calibration
