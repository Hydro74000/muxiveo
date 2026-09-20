"""Contrat temporel commun : temps donneur (ms) → temps référence (ms)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class SyncSegment:
    start_ms: float
    shift_ms: float


@dataclass(frozen=True)
class SyncCalibration:
    segments: tuple[SyncSegment, ...]
    confidence: float = 1.0
    samples: tuple[dict, ...] = ()

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
        return {"version": 1, "kind": "sync-calibration", "timebase": "donor-ms",
                "segments": [asdict(s) for s in self.segments],
                "confidence": self.confidence, "samples": list(self.samples)}

    @classmethod
    def from_dict(cls, payload):
        if (not isinstance(payload, dict) or payload.get("version") != 1
                or payload.get("kind") != "sync-calibration"
                or payload.get("timebase") != "donor-ms"):
            raise ValueError("Format de calibration non pris en charge.")
        try:
            return cls(tuple(SyncSegment(float(s["start_ms"]), float(s["shift_ms"]))
                             for s in payload["segments"]),
                       float(payload.get("confidence", 1)), tuple(payload.get("samples", ())))
        except (KeyError, TypeError) as exc:
            raise ValueError("Calibration invalide.") from exc

    def intervals(self, start_ms: float, end_ms: float):
        """Découpe aux jonctions et retire les parties écrasées par un saut négatif."""
        previous_end = 0.0
        for index, segment in enumerate(self.segments):
            stop = self.segments[index + 1].start_ms if index + 1 < len(self.segments) else math.inf
            effective_start = max(segment.start_ms, previous_end - segment.shift_ms)
            left, right = max(start_ms, effective_start), min(end_ms, stop)
            if right > left:
                a, b = max(0, left + segment.shift_ms), max(0, right + segment.shift_ms)
                if b > a:
                    yield a, b
            previous_end = max(previous_end, stop + segment.shift_ms)
