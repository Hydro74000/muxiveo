"""Comparaison HDR de deux pistes (deux sorties d'un même film, source et encodage, autre édition).

Alignement : débuts de plans de même nature (RPU, HDR10+, mesure) appariés par l'écart le plus
fréquent, sinon corrélation des courbes de moyenne en échelle PQ. Écarts : champs de métadonnées
puis courbes de pic et de moyenne, image A[i] face à B[i − décalage].
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from core.hdr_inspection.inspector import InspectionReport
from core.hdr_inspection.measure import ImageMeasurement
from core.hdr_inspection.pq import nits_to_pq, pq12_to_nits
from core.i18n import translate_text

#: Décalage maximal recherché (images) : dix minutes à 24 i/s.
MAX_OFFSET = 14400
#: Provenance des courbes : libellés (traduits à l'affichage).
SOURCE_LABELS = {"measure": "mesure", "hdr10plus": "HDR10+", "dovi": "L1"}
#: Tolérance d'appariement des plans (images) : détecteurs de coupes différents.
_SCENE_TOLERANCE = 1


@dataclass(frozen=True)
class Side:
    """Une piste comparée : rapport d'inspection et mesure éventuelle."""

    report: InspectionReport
    measurement: ImageMeasurement | None = None

    @property
    def fps(self) -> float:
        return self.report.snapshot.fps or 24000 / 1001


@dataclass(frozen=True)
class Curve:
    """Courbe par image (nits) et sa provenance (``measure``, ``hdr10plus``, ``dovi``)."""

    source: str
    values: np.ndarray


@dataclass(frozen=True)
class FieldDiff:
    label: str
    a: str
    b: str

    @property
    def same(self) -> bool:
        return self.a == self.b


@dataclass(frozen=True)
class CurveDelta:
    """Écart de deux courbes alignées (``kind`` : ``peak`` ou ``average``)."""

    kind: str
    source_a: str
    source_b: str
    frames: int
    #: Médiane de B / A (images de plus de 1 nit des deux côtés).
    median_ratio: float
    #: Corrélation des valeurs PQ.
    correlation: float
    #: Écart PQ absolu (codes 10 bits) : médiane et 99e centile.
    median_delta: float
    p99_delta: float
    #: Moments d'écart maximal (secondes dans A), moyenne glissante sur une seconde.
    worst: tuple[float, ...]


@dataclass(frozen=True, eq=False)
class Comparison:
    #: Image ``i`` de A ↔ image ``i − offset`` de B.
    offset: int
    #: Base de l'alignement : ``dovi``, ``hdr10plus``, ``measure``, ``curve`` ou ``none``.
    alignment: str
    #: Part des plans de B retrouvés dans A après décalage (alignement par plans), sinon corrélation.
    alignment_score: float
    fields: tuple[FieldDiff, ...]
    curves: tuple[CurveDelta, ...]
    peak_a: Curve | None
    peak_b: Curve | None
    average_a: Curve | None
    average_b: Curve | None


# ── Courbes ─────────────────────────────────────────────────────────────────
def curves(side: Side) -> dict[str, dict[str, np.ndarray]]:
    """Courbes disponibles par source : ``{"measure": {"peak": …, "average": …}, …}`` (nits)."""
    found: dict[str, dict[str, np.ndarray]] = {}
    measurement = side.measurement
    if measurement is not None and measurement.frames:
        found["measure"] = {"peak": measurement.peaks().astype(np.float64),
                            "average": measurement.average.astype(np.float64)}
    hdr10plus = side.report.hdr10plus
    if hdr10plus is not None and hdr10plus.frames:
        found["hdr10plus"] = {"peak": hdr10plus.max_scl.astype(np.float64),
                              "average": hdr10plus.average_rgb.astype(np.float64)}
    dovi = side.report.dovi
    if dovi is not None and dovi.frames:
        found["dovi"] = {"peak": pq12_to_nits(dovi.l1_max).astype(np.float64),
                         "average": pq12_to_nits(dovi.l1_avg).astype(np.float64)}
    return found


def _scene_starts(side: Side) -> dict[str, np.ndarray]:
    found: dict[str, np.ndarray] = {}
    dovi, hdr10plus, measurement = side.report.dovi, side.report.hdr10plus, side.measurement
    if dovi is not None and dovi.scenes >= 2:
        found["dovi"] = dovi.scene_starts.astype(np.int64)
    if hdr10plus is not None and hdr10plus.scenes >= 2:
        found["hdr10plus"] = hdr10plus.scene_starts.astype(np.int64)
    if measurement is not None and measurement.scene_starts.size >= 2:
        found["measure"] = measurement.scene_starts.astype(np.int64)
    return found


_ORDER = ("measure", "hdr10plus", "dovi")


def pick_curves(a: Side, b: Side, kind: str) -> tuple[Curve | None, Curve | None]:
    """Même provenance des deux côtés si possible (mesure > HDR10+ > L1), sinon la meilleure de chacun."""
    curves_a, curves_b = curves(a), curves(b)
    common = next((source for source in _ORDER if source in curves_a and source in curves_b), None)
    if common is not None:
        return Curve(common, curves_a[common][kind]), Curve(common, curves_b[common][kind])
    first = next((Curve(s, curves_a[s][kind]) for s in _ORDER if s in curves_a), None)
    second = next((Curve(s, curves_b[s][kind]) for s in _ORDER if s in curves_b), None)
    return first, second


# ── Alignement ──────────────────────────────────────────────────────────────
def scene_offset(first: np.ndarray, second: np.ndarray, max_offset: int = MAX_OFFSET) -> tuple[int, float]:
    """Décalage ``d`` (``second + d`` ≈ ``first``) le plus fréquent entre débuts de plans, et la part appariée."""
    reference = np.unique(np.asarray(first[1:], dtype=np.int64))
    starts = np.unique(np.asarray(second[1:], dtype=np.int64))
    if reference.size < 2 or starts.size < 2:
        return 0, 0.0
    low = np.searchsorted(reference, starts - max_offset, side="left")
    high = np.searchsorted(reference, starts + max_offset, side="right")
    counts = high - low
    if not counts.sum():
        return 0, 0.0
    # Écarts de chaque début de B à tous les débuts de A situés à moins de ``max_offset`` images.
    owner = np.repeat(np.arange(starts.size), counts)
    position = np.arange(int(counts.sum())) - np.repeat(np.cumsum(counts) - counts, counts)
    deltas = reference[np.repeat(low, counts) + position] - starts[owner]
    histogram = np.bincount(deltas + max_offset, minlength=2 * max_offset + 1)
    # Tolérance d'une image : pic de la somme glissante, puis barycentre de l'histogramme dans cette fenêtre
    # (détecteurs décalés d'une image comme coupes fortuites voisines) ; à égalité, le plus proche de 0.
    window = np.arange(-_SCENE_TOLERANCE, _SCENE_TOLERANCE + 1)
    smoothed = np.convolve(histogram, np.ones(window.size, dtype=np.int64), mode="same")
    peaks = np.flatnonzero(smoothed == smoothed.max())
    around = np.clip(peaks[:, None] + window, 0, histogram.size - 1)
    weights = histogram[around].astype(np.float64)
    centres = np.round((weights * around).sum(axis=1) / np.maximum(weights.sum(axis=1), 1)).astype(np.int64)
    candidates = np.unique(centres) - max_offset
    offset = int(candidates[np.argmin(np.abs(candidates))])
    shifted = starts + offset
    index = np.clip(np.searchsorted(reference, shifted), 1, reference.size - 1)
    nearest = np.minimum(np.abs(reference[index] - shifted), np.abs(reference[index - 1] - shifted))
    return offset, float((nearest <= _SCENE_TOLERANCE).mean())


def curve_offset(first: np.ndarray, second: np.ndarray, max_offset: int = MAX_OFFSET) -> tuple[int, float]:
    """Décalage maximisant la corrélation des courbes (échelle PQ), et cette corrélation."""
    a = np.nan_to_num(nits_to_pq(np.clip(first, 0, 10000)).astype(np.float64))
    b = np.nan_to_num(nits_to_pq(np.clip(second, 0, 10000)).astype(np.float64))
    if a.size < 48 or b.size < 48:
        return 0, 0.0
    a, b = a - a.mean(), b - b.mean()
    size = 1 << int(np.ceil(np.log2(a.size + b.size)))
    correlation = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    # correlation[d] = Σ a[i + d]·b[i] (indices négatifs en fin de tableau).
    lags = np.concatenate([np.arange(0, min(max_offset, a.size - 1) + 1),
                           np.arange(-min(max_offset, b.size - 1), 0)])
    values = np.concatenate([correlation[:lags[lags >= 0].size], correlation[size - (lags < 0).sum():]])
    # Normalisation par le recouvrement : un petit recouvrement ne doit pas l'emporter.
    overlap = np.minimum(a.size - np.maximum(lags, 0), b.size + np.minimum(lags, 0)).astype(np.float64)
    valid = overlap >= min(a.size, b.size) / 2
    if not valid.any():
        return 0, 0.0
    scores = np.where(valid, values / np.maximum(overlap, 1), -np.inf)
    offset = int(lags[int(np.argmax(scores))])
    return offset, _correlation(*_overlap(a, b, offset))


def _overlap(a: np.ndarray, b: np.ndarray, offset: int) -> tuple[np.ndarray, np.ndarray]:
    """Parties communes : ``a[i]`` face à ``b[i − offset]``."""
    start = max(0, offset)
    end = min(a.size, b.size + offset)
    if end <= start:
        return np.zeros(0), np.zeros(0)
    return a[start:end], b[start - offset:end - offset]


def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    valid = np.isfinite(a) & np.isfinite(b)
    if valid.sum() < 2 or np.std(a[valid]) == 0 or np.std(b[valid]) == 0:
        return 0.0
    return float(np.corrcoef(a[valid], b[valid])[0, 1])


def align(a: Side, b: Side) -> tuple[int, str, float]:
    """Décalage, base et score de l'alignement ; plans de même nature d'abord."""
    scenes_a, scenes_b = _scene_starts(a), _scene_starts(b)
    for source in ("dovi", "hdr10plus", "measure"):
        if source in scenes_a and source in scenes_b:
            offset, share = scene_offset(scenes_a[source], scenes_b[source])
            if share >= 0.3:
                return offset, source, share
    first, second = pick_curves(a, b, "average")
    if first is not None and second is not None:
        offset, correlation = curve_offset(first.values, second.values)
        if correlation > 0.5:
            return offset, "curve", correlation
    return 0, "none", 0.0


# ── Écarts ──────────────────────────────────────────────────────────────────
def _worst_moments(delta: np.ndarray, start: int, fps: float, count: int = 5) -> tuple[float, ...]:
    """Instants (s, dans A) des plus grands écarts moyens sur une seconde, espacés d'au moins dix secondes."""
    window = max(1, int(round(fps)))
    if delta.size < window:
        return ()
    smooth = np.convolve(np.nan_to_num(delta), np.ones(window) / window, mode="valid")
    order = np.argsort(-smooth)
    picked: list[int] = []
    gap = int(10 * fps)
    for index in order:
        if smooth[index] <= 0 or len(picked) >= count:
            break
        if all(abs(int(index) - other) >= gap for other in picked):
            picked.append(int(index))
    return tuple(sorted((start + index + window / 2) / fps for index in picked))


def curve_delta(kind: str, first: Curve, second: Curve, offset: int, fps: float) -> CurveDelta | None:
    a, b = _overlap(first.values, second.values, offset)
    if a.size == 0:
        return None
    pa = nits_to_pq(np.clip(a, 0, 10000)).astype(np.float64) * 1023
    pb = nits_to_pq(np.clip(b, 0, 10000)).astype(np.float64) * 1023
    delta = np.abs(pa - pb)
    finite = np.isfinite(delta)
    bright = finite & (a > 1) & (b > 1)
    ratio = float(np.median(b[bright] / a[bright])) if bright.any() else 1.0
    return CurveDelta(kind, first.source, second.source, int(a.size), ratio, _correlation(pa, pb),
                      float(np.median(delta[finite])) if finite.any() else 0.0,
                      float(np.percentile(delta[finite], 99)) if finite.any() else 0.0,
                      _worst_moments(np.where(finite, delta, 0.0), max(0, offset), fps))


def _value(value: object, unit: str = "") -> str:
    if value is None or value == "":
        return "—"
    return f"{value} {unit}".strip()


def _fields(a: Side, b: Side) -> tuple[FieldDiff, ...]:
    rows: list[FieldDiff] = []

    def add(label: str, first: object, second: object, unit: str = "") -> None:
        rows.append(FieldDiff(translate_text(label), _value(first, unit), _value(second, unit)))

    def rate(fps: float | None) -> str | None:
        return translate_text("{fps} i/s", fps=f"{fps:.3f}") if fps else None

    sa, sb = a.report.snapshot, b.report.snapshot
    add("Formats", " + ".join(sa.formats), " + ".join(sb.formats))
    add("Définition", f"{sa.width}×{sa.height}" if sa.width else None, f"{sb.width}×{sb.height}" if sb.width else None)
    add("Cadence", rate(sa.fps), rate(sb.fps))
    add("Profondeur", sa.bit_depth, sb.bit_depth, "bits")
    add("Images", sa.frames, sb.frames)
    add("Écran de mastering", _mastering(a), _mastering(b))
    add("MaxCLL / MaxFALL", _levels(sa.max_cll, sa.max_fall), _levels(sb.max_cll, sb.max_fall))
    da, db = a.report.dovi, b.report.dovi
    if sa.dovi is not None or sb.dovi is not None:
        add("Dolby Vision", sa.dovi.label if sa.dovi else None, sb.dovi.label if sb.dovi else None)
    if da is not None or db is not None:
        add("Couche d'amélioration", da.el_type if da else None, db.el_type if db else None)
        add("Version CM", da.cm_version if da else None, db.cm_version if db else None)
        add("Plans (RPU)", da.scenes if da else None, db.scenes if db else None)
        add("Pic L1", round(da.l1_peak_nits()) if da else None, round(db.l1_peak_nits()) if db else None, "nits")
        add("L6 MaxCLL / MaxFALL", _l6(da), _l6(db))
    ha, hb = a.report.hdr10plus, b.report.hdr10plus
    if ha is not None or hb is not None:
        add("Profil HDR10+", ha.profile if ha else None, hb.profile if hb else None)
        add("Plans (HDR10+)", ha.scenes if ha else None, hb.scenes if hb else None)
        add("Pic MaxScl", round(ha.credible_peak_nits()) if ha else None,
            round(hb.credible_peak_nits()) if hb else None, "nits")
    ma, mb = a.measurement, b.measurement
    if ma is not None or mb is not None:
        add("MaxCLL / MaxFALL mesurés", _levels(ma.max_cll(), ma.max_fall()) if ma else None,
            _levels(mb.max_cll(), mb.max_fall()) if mb else None)
    return tuple(rows)


def _mastering(side: Side) -> str | None:
    mastering = side.report.snapshot.mastering
    if mastering is None:
        return None
    low = f"{mastering.min_nits:g}" if mastering.min_nits is not None else "?"
    high = f"{mastering.max_nits:g}" if mastering.max_nits is not None else "?"
    return f"{mastering.primaries} {low}–{high} nits"


def _levels(cll: int | None, fall: int | None) -> str | None:
    return None if cll is None and fall is None else f"{cll if cll is not None else '?'} / {fall if fall is not None else '?'}"


def _l6(dovi: object) -> str | None:
    sets = getattr(dovi, "l6", None)
    if not sets:
        return None
    first = max(sets, key=lambda item: item.frames)
    return f"{first.max_cll} / {first.max_fall}"


def compare(a: Side, b: Side, offset: int | None = None) -> Comparison:
    """Compare B à A ; ``offset`` imposé (images) ou recherché automatiquement."""
    if offset is None:
        offset, alignment, score = align(a, b)
    else:
        alignment, score = "manual", 0.0
    peak_a, peak_b = pick_curves(a, b, "peak")
    average_a, average_b = pick_curves(a, b, "average")
    deltas = []
    for kind, first, second in (("peak", peak_a, peak_b), ("average", average_a, average_b)):
        if first is not None and second is not None:
            delta = curve_delta(kind, first, second, offset, a.fps)
            if delta is not None:
                deltas.append(delta)
    return Comparison(offset, alignment, score, _fields(a, b), tuple(deltas), peak_a, peak_b, average_a, average_b)
