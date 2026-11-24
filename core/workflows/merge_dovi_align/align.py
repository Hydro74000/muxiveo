"""Correspondance image par image Film 1 → Film 2 (plan § 3.1, § 3.4).

Principe : une erreur de position qui ne déplace une image qu'à l'intérieur d'une suite d'images de Film 2
aux métadonnées identiques est sans effet ; seuls les **points de changement** de Film 2 sont donc vérifiés,
en position (coupe franche de Film 1, ou confirmation par l'image) et en contenu (relation de luminance
étalonnée sur des paires confirmées par l'image, sinon confirmation par l'image). Toute rupture, les bords
et un échantillon de contrôle sont confirmés par l'image. La carte n'est rendue que si tout est vérifié.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .features import MIN_STD, Band, active_band, band_features, best_vertical_shift
from .probe import Signatures
from .process import ProcessHooks
from .signals import Film2Metadata

#: Corrélation de vignettes consécutives sous laquelle Film 1 change de plan (coupe franche).
CUT_CORRELATION = 0.5
#: Confirmation par l'image : corrélation minimale au décalage retenu et marge face aux décalages voisins.
CONFIRM_CORRELATION = 0.80
CONFIRM_MARGIN = 0.02
#: Part minimale d'images informatives confirmées dans une fenêtre.
CONFIRM_SHARE = 0.9
#: Bande de recherche autour des décalages candidats (images).
BAND = 48
BLOCK_SECONDS = 300
SAMPLE_SECONDS = 120
#: Au-delà de cette part de Film 2 à décoder par fenêtres : passe complète de Film 2.
WINDOW_BUDGET = 0.25
#: Paires confirmées nécessaires à l'étalonnage de la relation de luminance.
MIN_CALIBRATION = 20
#: Pénalité d'un changement de décalage entre deux plans (en coupes concordantes).
CHANGE_PENALTY = 4.0
NONE_PENALTY = 0.5
#: Marge (coupes concordantes) sous laquelle un segment est ambigu.
AMBIGUITY_MARGIN = 2.0
#: Corrélation minimale des cadrages (bande commune) pour comparer les deux films.
GEOMETRY_CORRELATION = 0.6


class AlignmentError(RuntimeError):
    """Correspondance non établie : motif lisible et plages de Film 1 concernées."""

    def __init__(self, message: str, ranges: tuple[tuple[int, int], ...] = ()) -> None:
        super().__init__(message)
        self.ranges = ranges


@dataclass(frozen=True)
class Segment:
    """Images ``[film1_start, film1_start + length)`` de Film 1 ↔ ``[film2_start, …)`` de Film 2."""

    film1_start: int
    length: int
    film2_start: int

    @property
    def offset(self) -> int:
        return self.film2_start - self.film1_start

    @property
    def film1_end(self) -> int:
        return self.film1_start + self.length


@dataclass(frozen=True)
class Uncovered:
    """Images de Film 1 sans correspondance dans Film 2 (D1)."""

    start: int
    length: int
    #: Toutes noires dans Film 1.
    black: bool
    #: Image de Film 2 dont les métadonnées seraient recopiées (voisine du même côté), et si elle est noire.
    source: int
    source_black: bool


@dataclass(frozen=True)
class AlignmentMap:
    film1_frames: int
    film2_frames: int
    segments: tuple[Segment, ...]
    removed: tuple[tuple[int, int], ...]
    uncovered: tuple[Uncovered, ...]
    #: Images de Film 1 qui doivent porter une coupe (``scene_refresh``) après édition.
    expected_cuts: np.ndarray
    stats: dict = field(default_factory=dict)

    @property
    def identity(self) -> bool:
        return (self.film1_frames == self.film2_frames and len(self.segments) == 1
                and self.segments[0] == Segment(0, self.film1_frames, 0) and not self.uncovered)


# --- Film 2 : fenêtres décodées ou passe complète ----------------------------------------------------


class Film2Frames:
    """Accès aux signatures de Film 2 : par fenêtres (décodage à la demande) ou depuis une passe complète."""

    def __init__(self, frames: int, *, window: Callable[[int, int], Signatures] | None = None,
                 full: Signatures | None = None) -> None:
        self.frames = frames
        self._window = window
        self.full = full
        self.decoded = 0

    @property
    def windowed(self) -> bool:
        return self.full is None and self._window is not None

    def get(self, start: int, count: int) -> Signatures:
        start = max(0, start)
        stop = min(self.frames, start + count)
        if stop <= start:
            raise AlignmentError("Fenêtre de Film 2 hors du film.")
        if self.full is not None:
            return self.full.slice(start, stop)
        if self._window is None:
            raise AlignmentError("Film 2 non décodable.")
        self.decoded += stop - start
        return self._window(start, stop - start)


def take(signatures: Signatures, indices: np.ndarray) -> Signatures:
    """Sous-ensemble arbitraire d'images (indices dans ``signatures``)."""
    idx = np.asarray(indices, dtype=np.int64)
    return Signatures(signatures.width, signatures.height, signatures.ranks[idx], signatures.top[idx],
                      signatures.bottom[idx], signatures.left[idx], signatures.right[idx],
                      signatures.vignettes[idx], signatures.mean[idx], signatures.std[idx], signatures.peak[idx],
                      signatures.decoder, signatures.pts[idx] if signatures.pts.size else signatures.pts)


# --- Coupes et alignement grossier -------------------------------------------------------------------


def detect_cuts(signatures: Signatures, band: Band | None = None) -> np.ndarray:
    """Coupes franches : vignette (normalisée sur la zone active) peu corrélée à la précédente, l'une
    des deux au moins informative. L'image 0 est toujours un début de plan."""
    features, informative = band_features(signatures, band or active_band(signatures))
    if len(signatures) < 2:
        return np.zeros(1, dtype=np.int64)
    corr = np.sum(features[1:] * features[:-1], axis=1)
    hard = (corr < CUT_CORRELATION) & (informative[1:] | informative[:-1])
    return np.concatenate([[0], np.flatnonzero(hard) + 1]).astype(np.int64)


def coarse_offsets(cuts1: np.ndarray, n1: int, cuts2: np.ndarray, n2: int, *, block: int, top: int = 3,
                   min_votes: int = 3) -> list[np.ndarray]:
    """Décalages candidats ``j - i`` par bloc de Film 1 : corrélation (FFT) du train de coupes du bloc avec
    celui de Film 2 ; votes exacts, à ± 1 image en appoint ; ``top`` meilleurs pics distincts d'au moins
    ``min_votes`` coupes exactes."""
    valid2 = cuts2[(cuts2 >= 0) & (cuts2 < n2)]
    exact2 = np.zeros(n2, dtype=np.float64)
    exact2[valid2] = 1.0
    near2 = exact2.copy()
    near2[np.clip(valid2 - 1, 0, n2 - 1)] = 1.0
    near2[np.clip(valid2 + 1, 0, n2 - 1)] = 1.0
    out: list[np.ndarray] = []
    for b0 in range(0, n1, block):
        b1 = min(n1, b0 + block)
        local = cuts1[(cuts1 >= b0) & (cuts1 < b1) & (cuts1 > 0)] - b0
        if local.size < min_votes:
            out.append(np.zeros(0, dtype=np.int64))
            continue
        length = b1 - b0
        train1 = np.zeros(length, dtype=np.float64)
        train1[local] = 1.0
        size = 1 << int(np.ceil(np.log2(length + n2)))
        spectrum1 = np.conj(np.fft.rfft(train1, size))
        # Décalages s du train local dans Film 2 : -(length-1) … n2-1 (lecture circulaire de la FFT).
        shifts = np.concatenate([np.arange(-(length - 1), 0), np.arange(n2)])

        def correlate(train2: np.ndarray) -> np.ndarray:
            # corr[s] = Σ train1[i] · train2[i + s] ; décalage j - i = s - b0.
            full = np.fft.irfft(np.fft.rfft(train2, size) * spectrum1, size)
            return np.rint(np.concatenate([full[size - (length - 1):], full[:n2]])).astype(np.int64)

        exact, near = correlate(exact2), correlate(near2)
        ranking = exact * 4 + near
        picks: list[int] = []
        for index in np.argsort(-ranking, kind="stable"):
            if exact[index] < min_votes or len(picks) >= top:
                break
            shift = int(shifts[index])
            if all(abs(shift - p) > 2 for p in picks):
                picks.append(shift)
        out.append(np.asarray([p - b0 for p in picks], dtype=np.int64))
    return out


_NONE = -(10 ** 9)


def _emission(a: int, b: int, film1_cut: np.ndarray, cut2: np.ndarray, prefix: np.ndarray,
              candidates: np.ndarray, n2: int) -> np.ndarray:
    """Score du plan ``[a, b)`` de Film 1 à chaque décalage : +1 si son début (coupe de Film 1) tombe sur une
    coupe de Film 2, -1 par coupe de Film 2 à l'intérieur ; -1e9 si le plan sort entièrement de Film 2."""
    ja, jb = a + candidates, b + candidates
    valid = (jb > 0) & (ja < n2)
    start = np.zeros(candidates.size)
    if a > 0 and film1_cut[a]:
        inside_range = (ja >= 0) & (ja < n2)
        start[inside_range] = cut2[ja[inside_range]].astype(float)
    lo = np.clip(ja + 1, 0, n2)
    hi = np.clip(jb, 0, n2)
    inside = np.where(hi > lo, prefix[hi] - prefix[lo], 0)
    return np.where(valid, start - inside, -1e9)


def shot_offsets(bounds: np.ndarray, film1_cut: np.ndarray, cuts2: np.ndarray, n2: int,
                 candidates: np.ndarray) -> np.ndarray:
    """Décalage de chaque plan de Film 1 par programmation dynamique ; ``_NONE`` = sans correspondance.

    États : décalages candidats et « sans correspondance » (``-NONE_PENALTY`` par plan) ; un changement
    d'état coûte ``CHANGE_PENALTY``. Mémoire : un booléen par (plan, état).
    """
    cut2 = np.zeros(n2 + 1, dtype=bool)
    cut2[cuts2[(cuts2 >= 0) & (cuts2 < n2)]] = True
    prefix = np.concatenate([[0], np.cumsum(cut2[:n2])])
    shots = bounds.size - 1
    states = candidates.size + 1          # dernier état : sans correspondance
    switched = np.zeros((shots, states), dtype=bool)
    source = np.zeros(shots, dtype=np.int64)
    score = np.zeros(states)
    for k in range(shots):
        emission = np.append(_emission(int(bounds[k]), int(bounds[k + 1]), film1_cut, cut2, prefix,
                                       candidates, n2), -NONE_PENALTY)
        if k == 0:
            score = emission
            continue
        best = int(np.argmax(score))
        switch = float(score[best]) - CHANGE_PENALTY
        switched[k] = switch > score
        source[k] = best
        score = np.maximum(score, switch) + emission
    offsets = np.full(shots, _NONE, dtype=np.int64)
    state = int(np.argmax(score))
    for k in range(shots - 1, -1, -1):
        offsets[k] = _NONE if state == candidates.size else int(candidates[state])
        if k > 0 and switched[k, state]:
            state = int(source[k])
    return offsets


def run_margins(bounds: np.ndarray, offsets: np.ndarray, film1_cut: np.ndarray, cuts2: np.ndarray, n2: int,
                candidates: np.ndarray) -> list[float]:
    """Pour chaque suite de plans de même décalage : écart de score avec le meilleur autre décalage pour
    cette suite seule (petite marge = segment ambigu)."""
    cut2 = np.zeros(n2 + 1, dtype=bool)
    cut2[cuts2[(cuts2 >= 0) & (cuts2 < n2)]] = True
    prefix = np.concatenate([[0], np.cumsum(cut2[:n2])])
    margins: list[float] = []
    k = 0
    while k < offsets.size:
        end = k
        while end + 1 < offsets.size and offsets[end + 1] == offsets[k]:
            end += 1
        if offsets[k] != _NONE:
            total = np.zeros(candidates.size)
            for shot in range(k, end + 1):
                total += _emission(int(bounds[shot]), int(bounds[shot + 1]), film1_cut, cut2, prefix,
                                   candidates, n2)
            chosen = int(np.flatnonzero(candidates == offsets[k])[0])
            others = np.delete(total, chosen)
            margins.append(float(total[chosen] - (others.max() if others.size else -1e9)))
        else:
            margins.append(float("inf"))
        k = end + 1
    return margins


def _segments_from_shots(bounds: np.ndarray, offsets: np.ndarray) -> list[tuple[int, int, int | None]]:
    """Suites de plans de même décalage : (début Film 1, fin Film 1, décalage ou None)."""
    out: list[tuple[int, int, int | None]] = []
    for k in range(offsets.size):
        value = None if offsets[k] == _NONE else int(offsets[k])
        a, b = int(bounds[k]), int(bounds[k + 1])
        if out and out[-1][2] == value:
            out[-1] = (out[-1][0], b, value)
        else:
            out.append((a, b, value))
    return out


# --- Vérification par l'image -------------------------------------------------------------------------


@dataclass
class _Context:
    film1: Signatures
    film2: Film2Frames
    band: Band
    dy: float
    hooks: ProcessHooks
    film1_features: np.ndarray = field(init=False)
    film1_informative: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self.film1_features, self.film1_informative = band_features(self.film1, self.band)

    def correlations(self, indices: np.ndarray, offset: int, spread: int = 3) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Corrélations (images × décalages offset-spread…offset+spread) et masques informatifs."""
        self.hooks.check()
        lo = int(indices.min()) + offset - spread
        hi = int(indices.max()) + offset + spread + 1
        lo_c, hi_c = max(0, lo), min(self.film2.frames, hi)
        if hi_c <= lo_c:
            raise AlignmentError("Fenêtre de Film 2 hors du film.")
        window = self.film2.get(lo_c, hi_c - lo_c)
        f2, inf2 = band_features(window, self.band.shifted(self.dy))
        shifts = np.arange(-spread, spread + 1)
        corr = np.full((indices.size, shifts.size), -1.0, dtype=np.float32)
        both = np.zeros((indices.size, shifts.size), dtype=bool)
        for column, shift in enumerate(shifts):
            j = indices + offset + shift - lo_c
            ok = (j >= 0) & (j < hi_c - lo_c)
            corr[ok, column] = np.sum(self.film1_features[indices[ok]] * f2[j[ok]], axis=1)
            both[ok, column] = self.film1_informative[indices[ok]] & inf2[j[ok]]
        return corr, both, shifts

    def confirm(self, indices: np.ndarray, offset: int) -> str:
        """« ok » (confirmé), « neutral » (aucune image informative des deux côtés) ou « fail »."""
        indices = np.asarray(indices, dtype=np.int64)
        indices = indices[(indices >= 0) & (indices < len(self.film1))]
        indices = indices[(indices + offset >= 0) & (indices + offset < self.film2.frames)]
        if indices.size == 0:
            return "fail"
        corr, both, shifts = self.correlations(indices, offset)
        center = int(np.flatnonzero(shifts == 0)[0])
        informative = both[:, center]
        if not np.any(informative):
            return "neutral"
        at = corr[informative, center]
        others = np.delete(corr[informative], center, axis=1).max(axis=1)
        good = (at >= CONFIRM_CORRELATION) & (at >= others - CONFIRM_MARGIN)
        return "ok" if float(np.mean(good)) >= CONFIRM_SHARE else "fail"


def _isotonic(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Régression isotone croissante (PAV) ; rend (x des blocs, valeurs des blocs)."""
    order = np.argsort(x, kind="stable")
    xs, ys = x[order].astype(np.float64), y[order].astype(np.float64)
    values: list[float] = []
    weights: list[float] = []
    lefts: list[float] = []
    rights: list[float] = []
    for xv, yv in zip(xs, ys):
        values.append(yv)
        weights.append(1.0)
        lefts.append(xv)
        rights.append(xv)
        while len(values) > 1 and values[-2] > values[-1]:
            w = weights[-2] + weights[-1]
            v = (values[-2] * weights[-2] + values[-1] * weights[-1]) / w
            values[-2:] = [v]
            weights[-2:] = [w]
            lefts[-2:] = [lefts[-2]]
            rights[-2:] = [rights[-1]]
    centers = (np.asarray(lefts) + np.asarray(rights)) / 2.0
    return centers, np.asarray(values)


# --- Ruptures ------------------------------------------------------------------------------------------


def _refine_junction(ctx: _Context, lo: int, hi: int, da: int, db: int) -> tuple[int, int]:
    """Jonction exacte entre deux segments de décalages ``da`` puis ``db`` dans ``[lo, hi)`` de Film 1.

    Rend (première image de Film 1 au décalage ``db``, nombre d'images de Film 1 sans correspondance avant
    elle). ``db > da`` : images de Film 2 retirées ; ``db < da`` : ``da - db`` images de Film 1 insérées.
    """
    indices = np.arange(lo, hi, dtype=np.int64)
    scores = {}
    for offset in (da, db):
        valid = indices[(indices + offset >= 0) & (indices + offset < ctx.film2.frames)]
        values = np.full(indices.size, -1.0)
        if valid.size:
            corr, both, shifts = ctx.correlations(valid, offset, spread=0)
            values[np.searchsorted(indices, valid)] = np.where(both[:, 0], corr[:, 0], 0.0)
        scores[offset] = values
    inserted = max(0, da - db)
    a, b = scores[da], scores[db]
    prefix_a = np.concatenate([[0.0], np.cumsum(a)])
    suffix_b = np.concatenate([np.cumsum(b[::-1])[::-1], [0.0]])
    best, best_t = -np.inf, lo
    for t in range(0, indices.size - inserted + 1):
        total = prefix_a[t] + suffix_b[t + inserted]
        if total > best:
            best, best_t = total, t
    return lo + best_t + inserted, inserted


# --- Point d'entrée ------------------------------------------------------------------------------------


def align(film1: Signatures, metadata: Film2Metadata, film2: Film2Frames, *, frame_rate: float,
          full_film2: Callable[[], Signatures] | None = None, force_full_film2: bool = False,
          hooks: ProcessHooks | None = None, log: Callable[[str], None] | None = None) -> AlignmentMap:
    """Carte Film 1 → Film 2 vérifiée, ou :class:`AlignmentError`."""
    hooks = hooks or ProcessHooks()
    say = log or (lambda _message: None)
    n1, n2 = len(film1), metadata.frames
    if n1 == 0 or n2 == 0:
        raise AlignmentError("Film vide.")
    fps = frame_rate if frame_rate > 0 else 24.0
    band1 = active_band(film1)
    cuts1 = detect_cuts(film1, band1)
    film1_cut = np.zeros(n1 + 1, dtype=bool)
    film1_cut[cuts1] = True

    def need_full(reason: str) -> None:
        if film2.full is not None:
            return
        if full_film2 is None:
            raise AlignmentError(f"Film 2 doit être lu entièrement ({reason}) mais n'est pas décodable.")
        say(f"Passe complète de Film 2 : {reason}.")
        film2.full = full_film2()
        if len(film2.full) != n2:
            raise AlignmentError(f"Film 2 : {len(film2.full)} images décodées pour {n2} images de métadonnées.")

    if force_full_film2:
        need_full("vérification image par image demandée")
    if not film2.windowed and film2.full is None:
        need_full("entrée non indexable")
    reference = metadata.reference_cuts
    if film2.full is not None:
        reference = detect_cuts(film2.full)
    elif not metadata.has_reference_cuts:
        need_full("aucune coupe exploitable dans les métadonnées")
        assert film2.full is not None
        reference = detect_cuts(film2.full)

    # Alignement grossier puis programmation dynamique sur les plans de Film 1.
    block = max(1000, int(BLOCK_SECONDS * fps))
    per_block = coarse_offsets(cuts1, n1, reference, n2, block=block)
    pool = {int(d) for candidates in per_block for d in candidates}
    if not pool:
        raise AlignmentError("Aucune correspondance de coupes entre les deux films : contenus différents ?")
    candidates = np.unique(np.concatenate([np.arange(d - BAND, d + BAND + 1) for d in sorted(pool)]))
    bounds = np.append(cuts1, n1)
    offsets = shot_offsets(bounds, film1_cut, reference, n2, candidates)
    runs = _segments_from_shots(bounds, offsets)
    if all(value is None for _a, _b, value in runs):
        raise AlignmentError("Aucun plan de Film 1 ne correspond à Film 2.")
    margins = run_margins(bounds, offsets, film1_cut, reference, n2, candidates)
    reference_set = np.zeros(n2 + 1, dtype=bool)
    reference_set[reference[(reference >= 0) & (reference < n2)]] = True

    # Géométrie : paires d'images à coupe concordante réparties sur le film.
    matched: list[tuple[int, int]] = []
    for a, b, d in runs:
        if d is None:
            continue
        for c in cuts1[(cuts1 >= a) & (cuts1 < b) & (cuts1 > 0)].tolist():
            if 0 <= c + d < n2 and reference_set[c + d]:
                matched.append((c, d))
    picks = matched[:: max(1, len(matched) // 12)][:12]
    if not picks:
        first = next((a, d) for a, _b, d in runs if d is not None)
        picks = [(first[0], int(first[1]))]
    probe_f1, probe_f2 = [], []
    for c, d in picks:
        indices = np.arange(max(0, c - 3), min(n1, c + 4))
        indices = indices[(indices + d >= 0) & (indices + d < n2)]
        if indices.size == 0:
            continue
        probe_f1.append(take(film1, indices))
        probe_f2.append(film2.get(int(indices[0] + d), int(indices.size)))
    if not probe_f2:
        raise AlignmentError("Aucune image de Film 2 comparable.")
    joined1, joined2 = _concat(probe_f1), _concat(probe_f2)
    band = band1.intersect(active_band(joined2))
    if band.empty:
        raise AlignmentError("Zones actives des deux films disjointes : cadrages incompatibles.")
    dy, score = best_vertical_shift(joined1, joined2, band)
    say(f"Cadrage : bande commune {band.top:+.3f}…{band.bottom:+.3f} (largeur), décalage vertical {dy:+.3f}, "
        f"corrélation {score:.2f}.")
    if score < GEOMETRY_CORRELATION:
        raise AlignmentError(f"Cadrages incompatibles (corrélation {score:.2f} sur la bande commune).")
    ctx = _Context(film1, film2, band, dy, hooks)

    # Ruptures affinées par l'image, segments bornés à Film 2.
    raw: list[list[int]] = [[a, b, d] for a, b, d in runs if d is not None]
    raw_margins = [m for (_a, _b, d), m in zip(runs, margins) if d is not None]
    for index in range(len(raw) - 1):
        hooks.check()
        first_seg, second_seg = raw[index], raw[index + 1]
        if second_seg[0] != first_seg[1]:
            continue  # plans sans correspondance entre les deux : bornes de plans conservées
        lo = max(first_seg[0], first_seg[1] - 240)
        hi = min(second_seg[1], second_seg[0] + 240)
        start, inserted = _refine_junction(ctx, lo, hi, first_seg[2], second_seg[2])
        first_seg[1] = start - inserted
        second_seg[0] = start
    built: list[Segment] = []
    ambiguous_segments: list[Segment] = []
    for (a, b, d), margin in zip(raw, raw_margins):
        a = max(a, -d, 0)
        b = min(b, n2 - d)
        if b > a:
            built.append(Segment(a, b - a, a + d))
            if margin < AMBIGUITY_MARGIN:
                ambiguous_segments.append(built[-1])
    if not built:
        raise AlignmentError("Aucune image de Film 1 ne correspond à Film 2.")

    # Fenêtres à confirmer par l'image : bords des segments, points hors coupe, segments ambigus,
    # échantillon de contrôle ; regroupées et bornées à leur segment.
    wanted: list[tuple[int, int, Segment]] = []
    for seg in built:
        wanted.append((seg.film1_start, seg.film1_start + 6, seg))
        wanted.append((seg.film1_end - 6, seg.film1_end, seg))
    for seg in ambiguous_segments:
        middle = seg.film1_start + seg.length // 2
        wanted.append((middle - 3, middle + 3, seg))
    on_cut_points: list[tuple[int, int]] = []
    for seg in built:
        lo, hi = seg.film2_start, seg.film2_start + seg.length
        points = metadata.change_points[(metadata.change_points >= lo) & (metadata.change_points < hi)]
        for j in points.tolist():
            i = j - seg.offset
            if film1_cut[i]:
                on_cut_points.append((i, seg.offset))
            else:
                wanted.append((i - 2, i + 3, seg))
    step = max(1, int(SAMPLE_SECONDS * fps))
    for t in range(0, n1, step):
        holder = next((item for item in built if item.film1_start <= t < item.film1_end), None)
        if holder is not None:
            k = int(np.searchsorted(cuts1, t))
            begin = int(cuts1[k]) if k < cuts1.size and cuts1[k] < holder.film1_end else t
            wanted.append((begin, begin + 6, holder))
    windows = _merge_windows(wanted)
    planned = sum(hi - lo + 6 for lo, hi, _d in windows)
    if film2.windowed and planned > WINDOW_BUDGET * n2:
        need_full(f"{planned} images de Film 2 à décoder par fenêtres (> {int(WINDOW_BUDGET * 100)} %)")
    confirmed = np.zeros(n1, dtype=bool)
    for lo, hi, d in windows:
        result = ctx.confirm(np.arange(lo, hi), d)
        if result == "fail":
            raise AlignmentError(f"Images {lo}–{hi - 1} de Film 1 : correspondance non confirmée par l'image "
                                 f"(décalage {d:+d}).", ((lo, hi),))
        if result == "ok":
            confirmed[lo:hi] = True

    # Contenu des plans à coupe concordante : relation de luminance étalonnée sur les paires confirmées.
    content_checked = _check_luminance(ctx, metadata, cuts1, n1, on_cut_points, confirmed, say)

    # Images sans correspondance, Film 2 retiré, coupes attendues.
    covered = np.zeros(n1, dtype=bool)
    for seg in built:
        covered[seg.film1_start:seg.film1_end] = True
    uncovered = _uncovered(ctx, covered, built)
    used = np.zeros(n2, dtype=bool)
    for seg in built:
        used[seg.film2_start:seg.film2_start + seg.length] = True
    removed = _runs(~used)
    expected = _expected_cuts(metadata, reference, built, uncovered, n1)
    stats = {
        "film1_cuts": int(cuts1.size), "reference_cuts": int(reference.size), "segments": len(built),
        "change_points": int(metadata.change_points.size), "points_on_cuts": len(on_cut_points),
        "image_windows": len(windows), "ambiguous_segments": len(ambiguous_segments),
        "content_checked": content_checked, "film2_decoded": film2.decoded,
        "film2_full": film2.full is not None, "band": band, "dy": dy, "geometry_score": score,
    }
    return AlignmentMap(n1, n2, tuple(built), removed, uncovered, expected, stats)


def _concat(parts: list[Signatures]) -> Signatures:
    first = parts[0]

    def join(name: str) -> np.ndarray:
        return np.concatenate([getattr(part, name) for part in parts])

    return Signatures(first.width, first.height, join("ranks"), join("top"), join("bottom"), join("left"),
                      join("right"), join("vignettes"), join("mean"), join("std"), join("peak"), first.decoder)


def _runs(mask: np.ndarray) -> tuple[tuple[int, int], ...]:
    """Plages ``(début, longueur)`` des valeurs vraies."""
    if not mask.size:
        return ()
    edges = np.flatnonzero(np.diff(np.concatenate([[0], mask.astype(np.int8), [0]])))
    return tuple((int(a), int(b - a)) for a, b in zip(edges[::2], edges[1::2]))


def _merge_windows(wanted: list[tuple[int, int, Segment]]) -> list[tuple[int, int, int]]:
    """Fenêtres ``(début, fin, décalage)`` bornées à leur segment, regroupées si elles se touchent."""
    clipped = sorted((max(lo, seg.film1_start), min(hi, seg.film1_end), seg.offset) for lo, hi, seg in wanted)
    merged: list[list[int]] = []
    for lo, hi, d in clipped:
        if hi <= lo:
            continue
        if merged and merged[-1][2] == d and lo <= merged[-1][1] and hi - merged[-1][0] <= 48:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi, d])
    return [(lo, hi, d) for lo, hi, d in merged]


def _check_luminance(ctx: _Context, metadata: Film2Metadata, cuts1: np.ndarray, n1: int,
                     on_cut_points: list[tuple[int, int]], confirmed: np.ndarray,
                     say: Callable[[str], None]) -> int:
    """Contenu des plans dont le début (point de changement) est sur une coupe : relation de luminance
    **étalonnée uniquement sur les plans confirmés par l'image**, jamais réajustée sur ceux qu'elle valide ;
    hors tolérance, sans information ou étalonnage impossible → confirmation par l'image."""
    if not on_cut_points:
        return 0
    bounds = np.append(cuts1, n1)

    def shot(i: int) -> tuple[int, int]:
        k = int(np.searchsorted(bounds, i, side="right")) - 1
        return int(bounds[k]), int(bounds[k + 1])

    def level1(i: int) -> float:
        a, b = shot(i)
        return float(np.mean(ctx.film1.mean[a:b]))

    def level2(i: int, d: int) -> float | None:
        if not metadata.luminance.size:
            return None
        a, b = shot(i)
        lo, hi = max(0, a + d), min(metadata.frames, b + d)
        return float(np.mean(metadata.luminance[lo:hi])) if hi > lo else None

    def informative(i: int) -> bool:
        a, b = shot(i)
        return float(np.mean(ctx.film1.std[a:b])) >= MIN_STD

    calibration = [(level2(i, d), level1(i)) for i, d in on_cut_points if confirmed[i] and informative(i)]
    pairs = [(x, y) for x, y in calibration if x is not None]
    pending = [(i, d) for i, d in on_cut_points if not confirmed[i]]
    checked = 0
    if ctx.film2.full is not None:
        say(f"Film 2 lu entièrement : {len(pending)} plans confirmés par l'image.")
    elif len(pairs) >= MIN_CALIBRATION:
        xs = np.asarray([x for x, _y in pairs])
        ys = np.asarray([y for _x, y in pairs])
        centers, values = _isotonic(xs, ys)
        residuals = ys - np.interp(xs, centers, values)
        mad = float(np.median(np.abs(residuals - np.median(residuals))))
        tolerance = max(3.0 * 1.4826 * mad, 12.0)
        remaining = []
        for i, d in pending:
            x = level2(i, d)
            if x is not None and informative(i) and abs(level1(i) - float(np.interp(x, centers, values))) <= tolerance:
                continue
            remaining.append((i, d))
        say(f"Relation de luminance étalonnée sur {len(pairs)} plans confirmés par l'image "
            f"(tolérance {tolerance:.0f} codes) ; {len(remaining)} plans hors relation confirmés par l'image.")
        pending = remaining
    else:
        say(f"Relation de luminance non étalonnée ({len(pairs)} plans confirmés) : {len(pending)} plans "
            "confirmés par l'image.")
    for i, d in pending:
        checked += 1
        if ctx.confirm(np.arange(i, i + 6), d) == "fail":
            raise AlignmentError(f"Plan à l'image {i} de Film 1 : contenu différent de Film 2.", ((i, i + 6),))
    return checked


def _uncovered(ctx: _Context, covered: np.ndarray, built: list[Segment]) -> tuple[Uncovered, ...]:
    out = []
    for start, length in _runs(~covered):
        black = bool(np.all(ctx.film1.top[start:start + length] < 0))
        before = next((s for s in reversed(built) if s.film1_end <= start), None)
        after = next((s for s in built if s.film1_start >= start + length), None)
        source = (before.film2_start + before.length - 1) if before is not None else (
            after.film2_start if after is not None else 0)
        window = ctx.film2.get(source, 1)
        out.append(Uncovered(start, length, black, source, bool(window.top[0] < 0)))
    return tuple(out)


def _expected_cuts(metadata: Film2Metadata, reference: np.ndarray, built: list[Segment],
                   uncovered: tuple[Uncovered, ...], n1: int) -> np.ndarray:
    """Coupes de Film 2 projetées sur Film 1, plus le début de chaque segment après une plage retirée
    ou sans correspondance, et l'image 0."""
    cuts = metadata.reference_cuts if metadata.reference_cuts.size else reference
    out = {0}
    previous_end_f2 = None
    for seg in built:
        lo, hi = seg.film2_start, seg.film2_start + seg.length
        inside = cuts[(cuts >= lo) & (cuts < hi)] - seg.offset
        out.update(int(i) for i in inside)
        if previous_end_f2 is not None and seg.film2_start != previous_end_f2:
            out.add(seg.film1_start)
        previous_end_f2 = hi
    for gap in uncovered:
        out.add(gap.start)
        if gap.start + gap.length < n1:
            out.add(gap.start + gap.length)
    return np.asarray(sorted(i for i in out if 0 <= i < n1), dtype=np.int64)
