"""Contrat temporel commun : temps donneur (ms) -> temps référence (ms)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math
from typing import Any

from core.workflows.cadence import CadenceMismatch, CadenceType

# Décalage constant mesuré jusqu'auquel (inclus) l'interface n'applique pas la synchro :
# un réencodage audio pour quelques millisecondes n'apporte rien.
NEGLIGIBLE_SYNC_MS = 5
# Tolérance des bornes d'une section éditée (arrondis des instants saisis).
_EDIT_TOLERANCE_MS = 1e-6


@dataclass(frozen=True)
class SyncSegment:
    start_ms: float
    shift_ms: float


@dataclass(frozen=True)
class SyncCalibration:
    segments: tuple[SyncSegment, ...]
    confidence: float = 1.0
    samples: tuple[dict, ...] = ()
    cadence_mismatch: CadenceMismatch | None = None
    cadence_audio_method: str = "atempo"
    cadence_pitch_analysis: Any | None = None
    # Validée par les seules mesures ponctuelles (analyse continue ignorée) ; non sérialisé.
    quick_check: bool = False

    def __post_init__(self):
        if not self.segments or self.segments[0].start_ms != 0:
            raise ValueError("La calibration doit commencer à 0 ms.")
        previous = -1.0
        for segment in self.segments:
            if (not math.isfinite(segment.start_ms) or not math.isfinite(segment.shift_ms)
                    or segment.start_ms <= previous):
                raise ValueError("Segments invalides : temps finis et strictement croissants requis.")
            previous = segment.start_ms
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("Confiance invalide.")

    @classmethod
    def linear(cls, offset_ms: float):
        return cls((SyncSegment(0, float(offset_ms)),))

    def to_dict(self):
        data = {
            "version": 1,
            "kind": "sync-calibration",
            "timebase": "donor-ms",
            "segments": [asdict(s) for s in self.segments],
            "confidence": self.confidence,
            "samples": list(self.samples),
        }
        if self.cadence_mismatch is not None:
            data["cadence_mismatch"] = self.cadence_mismatch.to_dict()
        if self.cadence_audio_method:
            data["cadence_audio_method"] = self.cadence_audio_method
        if self.cadence_pitch_analysis is not None:
            if hasattr(self.cadence_pitch_analysis, "to_dict"):
                data["cadence_pitch_analysis"] = self.cadence_pitch_analysis.to_dict()
            elif isinstance(self.cadence_pitch_analysis, dict):
                data["cadence_pitch_analysis"] = self.cadence_pitch_analysis
        return data

    @classmethod
    def from_dict(cls, payload):
        if not isinstance(payload, dict) or "segments" not in payload:
            raise ValueError("Format de calibration non pris en charge.")
        if payload.get("version") not in (None, 1) or payload.get("kind") not in (None, "sync-calibration") or payload.get("timebase") not in (None, "donor-ms"):
            raise ValueError("Format de calibration non pris en charge.")
        try:
            cadence_payload = payload.get("cadence_mismatch")
            cadence_mismatch = CadenceMismatch.from_dict(cadence_payload) if cadence_payload else None
            cadence_method = str(payload.get("cadence_audio_method", "atempo"))
            pitch_payload = payload.get("cadence_pitch_analysis")
            pitch_analysis = None
            if pitch_payload:
                try:
                    from core.workflows.cadence_pitch import CadencePitchAnalysis
                    pitch_analysis = CadencePitchAnalysis.from_dict(pitch_payload)
                except ImportError:
                    pitch_analysis = pitch_payload
            return cls(
                tuple(SyncSegment(float(s["start_ms"]), float(s["shift_ms"]))
                      for s in payload["segments"]),
                float(payload.get("confidence", 1)),
                tuple(payload.get("samples", ())),
                cadence_mismatch=cadence_mismatch,
                cadence_audio_method=cadence_method,
                cadence_pitch_analysis=pitch_analysis,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Calibration invalide.") from exc

    @property
    def cuts_count(self) -> int:
        return max(0, len(self.segments) - 1)

    def _stretch_ratio(self) -> float:
        return self.cadence_mismatch.time_stretch_ratio if self.cadence_mismatch else 1.0

    def output_ranges(self) -> list[tuple[float, float, int]]:
        """Plages de sortie (ms) effectivement occupées par chaque segment : (début, fin, index).

        Même règle que `physical_sync.audio_filter` : rien avant 0 ; après un saut négatif, le
        début du segment suivant est retiré ; après un saut positif, l'intervalle reste en silence.
        """
        ratio = self._stretch_ratio()
        ranges: list[tuple[float, float, int]] = []
        previous_end = 0.0
        for index, segment in enumerate(self.segments):
            start = segment.start_ms * ratio + segment.shift_ms
            stop = (self.segments[index + 1].start_ms * ratio + segment.shift_ms
                    if index + 1 < len(self.segments) else math.inf)
            effective = max(start, previous_end)
            if stop > effective:
                ranges.append((effective, stop, index))
            previous_end = max(previous_end, stop)
        return ranges

    def segment_index_at(self, output_ms: float) -> int:
        """Segment joué à `output_ms` ; dans un silence, celui qui le suit."""
        ranges = self.output_ranges()
        for start, stop, index in ranges:
            if start <= output_ms < stop:
                return index
        following = [(start, index) for start, _stop, index in ranges if start > output_ms]
        return min(following)[1] if following else (ranges[-1][2] if ranges else 0)

    def output_start_ms(self, index: int) -> float:
        """Instant de sortie où commence le segment `index` (jonction affichée)."""
        for start, _stop, segment_index in self.output_ranges():
            if segment_index == index:
                return start
        segment = self.segments[index]
        return segment.start_ms * self._stretch_ratio() + segment.shift_ms

    def donor_time_ms(self, output_ms: float) -> float:
        """Instant de la piste cible placé à `output_ms` dans la sortie (borné à son segment)."""
        index = self.segment_index_at(output_ms)
        segment = self.segments[index]
        donor = (output_ms - segment.shift_ms) / self._stretch_ratio()
        upper = self.segments[index + 1].start_ms if index + 1 < len(self.segments) else math.inf
        return max(segment.start_ms, min(upper, donor))

    def with_edit(self, output_start_ms: float, output_end_ms: float, *, silence: bool) -> SyncCalibration:
        """Ajoute une coupe (sortie de la section supprimée) ou un silence de la longueur de la section.

        La section est exprimée sur l'axe de sortie (référence). Les segments suivants sont
        décalés d'autant, comme lors d'un réglage du décalage : les sauts relatifs sont conservés.
        Une coupe commençant dans un silence en retire la partie couverte puis l'audio qui suit.
        """
        start_out, end_out = float(output_start_ms), float(output_end_ms)
        length = end_out - start_out
        if not math.isfinite(length) or length <= 0:
            raise ValueError("La fin de la section doit suivre son début.")
        delta = length if silence else -length
        ranges = self.output_ranges()
        current = next(((a, b, i) for a, b, i in ranges if a <= start_out < b), None)
        if current is None:
            # Silence (début de piste ou saut positif) : la plage suivante commence à `gap_end`.
            gap_end, range_end, index = min((r for r in ranges if r[0] > start_out), default=ranges[-1])
            if silence or end_out <= gap_end + _EDIT_TOLERANCE_MS:
                return self._shifted_from(index, delta)
            if end_out > range_end + _EDIT_TOLERANCE_MS:
                raise ValueError("La coupe chevauche une jonction existante.")
            return self._cut_content(index, self.segments[index].start_ms, start_out, end_out,
                                     resume_after=self._previous_end(ranges, index))
        _range_start, range_end, index = current
        segment = self.segments[index]
        donor = max(segment.start_ms, round((start_out - segment.shift_ms) / self._stretch_ratio(), 3))
        if silence:
            if donor <= segment.start_ms:
                return self._shifted_from(index, delta)
            return self._split_at(index, donor, delta)
        if end_out > range_end + _EDIT_TOLERANCE_MS:
            raise ValueError("La coupe chevauche une jonction existante.")
        resume_after = start_out if donor > segment.start_ms else self._previous_end(ranges, index)
        return self._cut_content(index, donor, start_out, end_out, resume_after=resume_after)

    @staticmethod
    def _previous_end(ranges: list[tuple[float, float, int]], index: int) -> float:
        """Fin de la sortie jouée avant le segment `index`."""
        return max((stop for _start, stop, i in ranges if i < index), default=0.0)

    def _shifted_from(self, index: int, delta: float) -> SyncCalibration:
        """Saut de la jonction `index` modifié (silence élargi ou réduit), suite décalée d'autant."""
        segments = [
            segment if i < index else SyncSegment(segment.start_ms, segment.shift_ms + delta)
            for i, segment in enumerate(self.segments)
        ]
        return replace(self, segments=_merged(segments), quick_check=False)

    def _split_at(self, index: int, donor: float, delta: float) -> SyncCalibration:
        """Nouvelle jonction dans le segment `index` à l'instant cible `donor`, suite décalée de `delta`."""
        segments = [
            *self.segments[:index + 1],
            SyncSegment(donor, self.segments[index].shift_ms + delta),
            *(SyncSegment(s.start_ms, s.shift_ms + delta) for s in self.segments[index + 1:]),
        ]
        return replace(self, segments=_merged(segments), quick_check=False)

    def _cut_content(self, index: int, donor: float, start_out: float, end_out: float, *,
                     resume_after: float) -> SyncCalibration:
        """Retire l'audio cible à partir de `donor` jusqu'à `end_out` et le reprend à `start_out`.

        Le morceau retiré devient un segment entièrement recouvert (sa sortie finit à `resume_after`,
        fin de ce qui le précède), comme `audio_filter` le traite déjà ; le silence éventuel
        entre `resume_after` et `start_out` est conservé.
        """
        ratio = self._stretch_ratio()
        segment = self.segments[index]
        removed = end_out - (donor * ratio + segment.shift_ms)
        resume = round(donor + removed / ratio, 3)
        length = end_out - start_out
        next_start = self.segments[index + 1].start_ms if index + 1 < len(self.segments) else math.inf
        # Coupe jusqu'à la jonction suivante : ce segment reprend lui-même la suite.
        resumed = [SyncSegment(resume, segment.shift_ms - length)] if resume < next_start - _EDIT_TOLERANCE_MS else []
        segments = [
            *self.segments[:index],
            *([segment] if donor > segment.start_ms else []),
            SyncSegment(donor, resume_after - donor * ratio - removed),
            *resumed,
            *(SyncSegment(s.start_ms, s.shift_ms - length) for s in self.segments[index + 1:]),
        ]
        return replace(self, segments=_merged(segments), quick_check=False)

    def without_junction(self, index: int) -> SyncCalibration:
        """Retire la jonction `index` (≥ 1) ; les segments suivants perdent son saut."""
        if not 1 <= index < len(self.segments):
            raise ValueError("Jonction inexistante.")
        jump = self.segments[index].shift_ms - self.segments[index - 1].shift_ms
        segments = [
            *self.segments[:index],
            *(SyncSegment(segment.start_ms, segment.shift_ms - jump) for segment in self.segments[index + 1:]),
        ]
        return replace(self, segments=tuple(segments), quick_check=False)

    @staticmethod
    def format_timestamp(ms: float) -> str:
        total_sec = max(0.0, float(ms) / 1000.0)
        h = int(total_sec // 3600)
        m = int((total_sec % 3600) // 60)
        s = total_sec % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        if self.cadence_mismatch is not None and self.cadence_mismatch.cadence_type != CadenceType.NONE:
            pitch_desc = ""
            if self.cadence_pitch_analysis is not None:
                details = getattr(self.cadence_pitch_analysis, "details", None)
                if isinstance(self.cadence_pitch_analysis, dict):
                    details = self.cadence_pitch_analysis.get("details")
                if details:
                    pitch_desc = f" — {details}"
            lines.append(f"Cadence : {self.cadence_mismatch.description} ({self.cadence_audio_method}){pitch_desc}")
        prev_shift = 0.0
        for i, segment in enumerate(self.segments):
            ts = self.format_timestamp(segment.start_ms)
            if i == 0:
                lines.append(f"Segment 1 : départ à {ts} -> décalage {segment.shift_ms:+.1f} ms")
            else:
                delta = segment.shift_ms - prev_shift
                lines.append(
                    f"Segment {i + 1} : coupure à {ts} -> décalage {segment.shift_ms:+.1f} ms (saut de {delta:+.1f} ms)"
                )
            prev_shift = segment.shift_ms
        return lines

    def intervals(self, start_ms: float, end_ms: float):
        """Découpe aux jonctions et retire les parties écrasées par un saut négatif."""
        ratio = self.cadence_mismatch.time_stretch_ratio if self.cadence_mismatch else 1.0
        scaled_start = start_ms * ratio
        scaled_end = end_ms * ratio
        previous_end = 0.0
        for index, segment in enumerate(self.segments):
            seg_start = segment.start_ms * ratio
            seg_stop = (self.segments[index + 1].start_ms * ratio) if index + 1 < len(self.segments) else math.inf
            effective_start = max(seg_start, previous_end - segment.shift_ms)
            left, right = max(scaled_start, effective_start), min(scaled_end, seg_stop)
            if right > left:
                a, b = max(0, left + segment.shift_ms), max(0, right + segment.shift_ms)
                if b > a:
                    yield a, b
            previous_end = max(previous_end, seg_stop + segment.shift_ms)


def _merged(segments: list[SyncSegment]) -> tuple[SyncSegment, ...]:
    """Jonctions sans saut retirées : deux segments consécutifs de même décalage n'en font qu'un."""
    result: list[SyncSegment] = []
    for segment in segments:
        if result and abs(segment.shift_ms - result[-1].shift_ms) < 1e-9:
            continue
        result.append(segment)
    return tuple(result)


def format_calibration_summary(calibration_or_dict: SyncCalibration | dict | None) -> list[str]:
    if calibration_or_dict is None:
        return []
    if isinstance(calibration_or_dict, SyncCalibration):
        return calibration_or_dict.summary_lines()
    if isinstance(calibration_or_dict, dict):
        try:
            return SyncCalibration.from_dict(calibration_or_dict).summary_lines()
        except Exception:
            return []
    return []



def effective_calibration(calibration_or_dict: SyncCalibration | dict | None) -> SyncCalibration | None:
    """Retourne la calibration si elle n'est pas réductible à un décalage constant (coupures ou cadence)."""
    calibration = calibration_or_dict
    if isinstance(calibration, dict):
        try:
            calibration = SyncCalibration.from_dict(calibration)
        except ValueError:
            return None
    if not isinstance(calibration, SyncCalibration):
        return None
    has_cadence = bool(
        calibration.cadence_mismatch is not None
        and calibration.cadence_mismatch.cadence_type != CadenceType.NONE
    )
    return calibration if len(calibration.segments) > 1 or has_cadence else None
