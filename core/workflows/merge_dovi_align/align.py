"""Correspondance image par image Film 1 → Film 2 (plan § 3.1, § 3.4).

Principe : une erreur de position qui ne déplace une image qu'à l'intérieur d'une suite d'images de Film 2
aux métadonnées identiques est sans effet ; seuls les **points de changement** de Film 2 sont donc vérifiés,
un par un, en position (coupe franche commune, ou bascule confirmée par l'image) et en contenu (relation de
luminance étalonnée sur des plans confirmés par l'image, sinon l'image). Ruptures, bords, segments ambigus et
un échantillon de contrôle sont confirmés par l'image. Passe complète de Film 2 : écarts locaux recherchés
image par image en bande et tous les points confirmés par l'image. La carte n'est rendue que si tout est
vérifié.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .features import MIN_STD, Band, active_band, band_features, best_shift, cell_margins
from .probe import Signatures
from .process import ProcessHooks
from .signals import Film2Metadata

#: Corrélation de vignettes consécutives sous laquelle Film 1 change de plan (coupe franche).
CUT_CORRELATION = 0.5
#: Contenu confirmé : corrélation minimale de chaque paire d'images informatives.
CONFIRM_CORRELATION = 0.80
#: Avance minimale du décalage retenu sur ses voisins pour confirmer une position.
POSITION_LEAD = 1e-3
#: Images voisines quasi identiques (plan fixe) : une égalité de décalages y est sans effet.
STATIC_CORRELATION = 0.995
POSITION_RADIUS = 3
#: Bande de recherche autour des décalages candidats (images).
BAND = 48
BLOCK_SECONDS = 300
SAMPLE_SECONDS = 120
#: Au-delà de cette part de Film 2 lue par fenêtres : passe complète de Film 2.
WINDOW_BUDGET = 0.25
#: Plans confirmés par l'image nécessaires à l'étalonnage de la relation de luminance.
MIN_CALIBRATION = 20
#: Pénalité d'un changement de décalage entre deux plans (en coupes concordantes).
CHANGE_PENALTY = 4.0
NONE_PENALTY = 0.5
#: Marge entre la meilleure carte et la meilleure carte concurrente sous laquelle un segment est ambigu.
AMBIGUITY_MARGIN = 2.0
#: Insertion maximale (images de Film 1) traitée à la jonction de deux plans consécutifs.
MAX_INSERT = 200
#: Plafond des états de la programmation dynamique et pas des points de reprise (mémoire bornée).
MAX_STATES = 1024
CHECKPOINT = 64
GEOMETRY_CORRELATION = 0.6
GEOMETRY_AGREEMENT = 0.8
GEOMETRY_PAIRS = 10
#: Passe complète : bande du balayage image par image et longueur minimale d'un écart retenu.
FRAME_BAND = 24
FRAME_RUN = 3
_NONE = -(10 ** 9)


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

    @property
    def film2_end(self) -> int:
        return self.film2_start + self.length


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
    """Accès aux signatures de Film 2 : par fenêtres (décodage à la demande, budget borné) ou depuis une
    passe complète, chargée au besoin (entrée non indexable, budget dépassé, vérification demandée)."""

    def __init__(self, frames: int, *, window: Callable[[int, int], Signatures] | None = None,
                 full: Signatures | None = None, full_loader: Callable[[], Signatures] | None = None,
                 budget: float = WINDOW_BUDGET) -> None:
        self.frames = frames
        self._window = window
        self.full = full
        self._loader = full_loader
        self._budget = budget
        self.decoded = 0
        self.hooks = ProcessHooks()
        self.log: Callable[[str], None] = lambda _message: None
        self.full_reason = ""

    @property
    def windowed(self) -> bool:
        return self.full is None and self._window is not None

    def load_full(self, reason: str) -> Signatures:
        """Passe complète de Film 2 (une fois) ; :class:`AlignmentError` si impossible."""
        if self.full is None:
            self.hooks.check()
            if self._loader is None:
                raise AlignmentError(f"Film 2 doit être lu entièrement ({reason}) mais n'est pas décodable.")
            self.log(f"Passe complète de Film 2 : {reason}.")
            full = self._loader()
            if len(full) != self.frames:
                raise AlignmentError(f"Film 2 : {len(full)} images décodées pour {self.frames} images de "
                                     "métadonnées.")
            self.full, self.full_reason = full, reason
        return self.full

    def get(self, start: int, count: int) -> Signatures:
        self.hooks.check()
        start = max(0, start)
        stop = min(self.frames, start + count)
        if stop <= start:
            raise AlignmentError("Fenêtre de Film 2 hors du film.")
        if self.full is None and self._window is None:
            self.load_full("entrée non indexable")
        if self.full is None and self.decoded + (stop - start) > self._budget * self.frames:
            self.load_full(f"plus de {int(self._budget * 100)} % de Film 2 à lire par fenêtres")
        if self.full is not None:
            return self.full.slice(start, stop)
        assert self._window is not None
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
                   min_votes: int = 3, hooks: ProcessHooks | None = None) -> list[tuple[np.ndarray, np.ndarray]]:
    """Décalages candidats ``j - i`` par bloc de Film 1 et leurs votes exacts : corrélation (FFT) du train de
    coupes du bloc avec celui de Film 2 (votes exacts, à ± 1 image en appoint) ; ``top`` meilleurs pics
    distincts d'au moins ``min_votes`` coupes exactes."""
    hooks = hooks or ProcessHooks()
    valid2 = cuts2[(cuts2 >= 0) & (cuts2 < n2)]
    exact2 = np.zeros(n2, dtype=np.float64)
    exact2[valid2] = 1.0
    near2 = exact2.copy()
    near2[np.clip(valid2 - 1, 0, n2 - 1)] = 1.0
    near2[np.clip(valid2 + 1, 0, n2 - 1)] = 1.0
    out: list[tuple[np.ndarray, np.ndarray]] = []
    for b0 in range(0, n1, block):
        hooks.check()
        b1 = min(n1, b0 + block)
        local = cuts1[(cuts1 >= b0) & (cuts1 < b1) & (cuts1 > 0)] - b0
        if local.size < min_votes:
            out.append((np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)))
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
        eligible = np.flatnonzero(exact >= min_votes)
        ranking = eligible[np.argsort(-(exact[eligible] * 4 + near[eligible]), kind="stable")]
        picks: list[int] = []
        votes: list[int] = []
        for index in ranking:
            shift = int(shifts[index])
            if all(abs(shift - p) > 2 for p in picks):
                picks.append(shift)
                votes.append(int(exact[index]))
                if len(picks) >= top:
                    break
        out.append((np.asarray([p - b0 for p in picks], dtype=np.int64), np.asarray(votes, dtype=np.int64)))
    return out


def candidate_states(per_block: list[tuple[np.ndarray, np.ndarray]]) -> np.ndarray:
    """États de la programmation dynamique : bande ± ``BAND`` autour des décalages candidats, les plus votés
    d'abord, plafonnés à ``MAX_STATES`` (mémoire bornée)."""
    votes: dict[int, int] = {}
    for offsets, counts in per_block:
        for offset, count in zip(offsets.tolist(), counts.tolist()):
            votes[offset] = votes.get(offset, 0) + count
    states: set[int] = set()
    for offset in sorted(votes, key=lambda value: (-votes[value], value)):
        band = set(range(offset - BAND, offset + BAND + 1))
        if len(states | band) > MAX_STATES:
            break
        states |= band
    return np.asarray(sorted(states), dtype=np.int64)


def _prefix_argmax(values: np.ndarray) -> np.ndarray:
    """Indice du maximum (premier atteint) de chaque préfixe."""
    best = np.maximum.accumulate(values)
    index = np.where(values == best, np.arange(values.size), 0)
    return np.maximum.accumulate(index)


class _ShotModel:
    """Programmation dynamique sur les plans de Film 1, monotone dans Film 2.

    États : décalages candidats et « sans correspondance ». Un plan au décalage d marque +1 si son début
    (coupe de Film 1) tombe sur une coupe de Film 2 et -1 par coupe de Film 2 à l'intérieur ; « sans
    correspondance » coûte ``NONE_PENALTY`` ; un changement d'état ``CHANGE_PENALTY``. Une bascule vers d'
    n'est admise que depuis d ≤ d' + ``MAX_INSERT`` (Film 2 jamais relu à rebours au-delà d'une insertion
    traitée à la jonction). Remontée par points de reprise : mémoire ``plans / CHECKPOINT × états``.
    """

    def __init__(self, bounds: np.ndarray, film1_cut: np.ndarray, cuts2: np.ndarray, n2: int,
                 candidates: np.ndarray, hooks: ProcessHooks) -> None:
        self.bounds, self.film1_cut, self.n2, self.candidates, self.hooks = bounds, film1_cut, n2, candidates, hooks
        self.cut2 = np.zeros(n2 + 1, dtype=bool)
        self.cut2[cuts2[(cuts2 >= 0) & (cuts2 < n2)]] = True
        self.prefix = np.concatenate([[0], np.cumsum(self.cut2[:n2])])
        self.limit = np.searchsorted(candidates, candidates + MAX_INSERT, side="right") - 1
        self.states = candidates.size + 1
        self.none = candidates.size

    @property
    def shots(self) -> int:
        return self.bounds.size - 1

    def emission(self, k: int, forbid: tuple[int, int, int] | None) -> np.ndarray:
        a, b = int(self.bounds[k]), int(self.bounds[k + 1])
        ja, jb = a + self.candidates, b + self.candidates
        valid = (jb > 0) & (ja < self.n2)
        start = np.zeros(self.candidates.size)
        if a > 0 and self.film1_cut[a]:
            inside_range = (ja >= 0) & (ja < self.n2)
            start[inside_range] = self.cut2[ja[inside_range]].astype(float)
        lo = np.clip(ja + 1, 0, self.n2)
        hi = np.clip(jb, 0, self.n2)
        inside = np.where(hi > lo, self.prefix[hi] - self.prefix[lo], 0)
        values = np.append(np.where(valid, start - inside, -1e9), -NONE_PENALTY)
        if forbid is not None and forbid[0] <= k < forbid[1]:
            values[forbid[2]] = -1e12
        return values

    def _step(self, score: np.ndarray, emission: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Score après un plan et état précédent de chaque état."""
        mapped = score[:self.none]
        prefix_best = np.maximum.accumulate(mapped)
        prefix_arg = _prefix_argmax(mapped)
        allowed, allowed_arg = prefix_best[self.limit], prefix_arg[self.limit]
        from_none = score[self.none] > allowed
        best = np.append(np.where(from_none, score[self.none], allowed), np.max(score))
        arg = np.append(np.where(from_none, self.none, allowed_arg), int(np.argmax(score)))
        switch = best - CHANGE_PENALTY
        stay = switch <= score
        source = np.where(stay, np.arange(self.states), arg)
        return np.where(stay, score, switch) + emission, source

    def solve(self, first: int = 0, last: int | None = None, *, entry: int | None = None,
              exit: int | None = None, forbid: tuple[int, int, int] | None = None) -> tuple[np.ndarray, float]:
        """Meilleur chemin sur les plans ``[first, last)`` (états), et son score. ``entry`` : état imposé au
        plan précédant ``first`` ; ``exit`` : état imposé au dernier plan ; ``forbid`` : (plans début, fin,
        état) interdit."""
        last = self.shots if last is None else last
        checkpoints: list[np.ndarray] = []
        score = np.zeros(0)
        for k in range(first, last):
            if (k - first) % 256 == 0:
                self.hooks.check()
            emission = self.emission(k, forbid)
            if k == first:
                if entry is None:
                    score = emission
                else:
                    start = np.full(self.states, -1e12)
                    start[entry] = 0.0
                    score, _source = self._step(start, emission)
            else:
                score, _source = self._step(score, emission)
            if (k - first) % CHECKPOINT == CHECKPOINT - 1:
                checkpoints.append(score.copy())
        if exit is not None:
            state = exit
        else:
            state = int(np.argmax(score))
        total = float(score[state])
        path = np.zeros(last - first, dtype=np.int64)
        # Remontée bloc par bloc : recalcul des états précédents depuis le point de reprise du bloc.
        block_end = last
        while block_end > first:
            block_start = first + ((block_end - 1 - first) // CHECKPOINT) * CHECKPOINT
            self.hooks.check()
            sources: list[np.ndarray] = []
            if block_start == first:
                if entry is None:
                    score = self.emission(first, forbid)
                    sources.append(np.arange(self.states))
                else:
                    start = np.full(self.states, -1e12)
                    start[entry] = 0.0
                    score, source = self._step(start, self.emission(first, forbid))
                    sources.append(source)
                begin = first + 1
            else:
                score = checkpoints[(block_start - first) // CHECKPOINT - 1].copy()
                begin = block_start
            for k in range(begin, block_end):
                score, source = self._step(score, self.emission(k, forbid))
                sources.append(source)
            for k in range(block_end - 1, block_start - 1, -1):
                path[k - first] = state
                state = int(sources[k - block_start][state])
            block_end = block_start
        return path, total

    def offsets(self, path: np.ndarray) -> np.ndarray:
        return np.asarray([_NONE if s == self.none else int(self.candidates[s]) for s in path], dtype=np.int64)


def _segments_from_shots(bounds: np.ndarray, offsets: np.ndarray) -> list[tuple[int, int, int | None, int, int]]:
    """Suites de plans de même décalage : (début Film 1, fin Film 1, décalage ou None, premier plan, fin plans)."""
    out: list[tuple[int, int, int | None, int, int]] = []
    for k in range(offsets.size):
        value = None if offsets[k] == _NONE else int(offsets[k])
        a, b = int(bounds[k]), int(bounds[k + 1])
        if out and out[-1][2] == value:
            out[-1] = (out[-1][0], b, value, out[-1][3], k + 1)
        else:
            out.append((a, b, value, k, k + 1))
    return out


# --- Vérification par l'image -------------------------------------------------------------------------


@dataclass
class _Context:
    film1: Signatures
    film2: Film2Frames
    band: Band
    dx: float
    dy: float
    hooks: ProcessHooks
    film1_features: np.ndarray = field(init=False)
    film1_informative: np.ndarray = field(init=False)
    film1_static: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self.film1_features, self.film1_informative = band_features(self.film1, self.band)
        static = np.zeros(len(self.film1), dtype=bool)
        if len(self.film1) > 1:
            static[1:] = np.sum(self.film1_features[1:] * self.film1_features[:-1], axis=1) >= STATIC_CORRELATION
        self.film1_static = static

    def pair_scores(self, indices: np.ndarray, offset: int, spread: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Corrélations (images × décalages offset-spread…offset+spread) ; masques « deux informatives » et
        « une seule informative » (désaccord)."""
        self.hooks.check()
        lo = int(indices.min()) + offset - spread
        hi = int(indices.max()) + offset + spread + 1
        lo_c, hi_c = max(0, lo), min(self.film2.frames, hi)
        if hi_c <= lo_c:
            raise AlignmentError("Fenêtre de Film 2 hors du film.")
        window = self.film2.get(lo_c, hi_c - lo_c)
        f2, inf2 = band_features(window, self.band.shifted(self.dy, self.dx))
        width = 2 * spread + 1
        corr = np.full((indices.size, width), -1.0, dtype=np.float32)
        both = np.zeros((indices.size, width), dtype=bool)
        one = np.zeros((indices.size, width), dtype=bool)
        for column, shift in enumerate(range(-spread, spread + 1)):
            j = indices + offset + shift - lo_c
            ok = (j >= 0) & (j < hi_c - lo_c)
            corr[ok, column] = np.sum(self.film1_features[indices[ok]] * f2[j[ok]], axis=1)
            first = self.film1_informative[indices[ok]]
            second = inf2[j[ok]]
            both[ok, column] = first & second
            one[ok, column] = first ^ second
        return corr, both, one

    def confirm_content(self, indices: np.ndarray, offset: int) -> str:
        """« ok » : chaque paire informative corrélée ; « neutral » : aucune image informative d'aucun côté ;
        « fail » : désaccord (une seule image informative, ou corrélation insuffisante)."""
        indices = self.inside(indices, offset)
        if indices.size == 0:
            return "fail"
        corr, both, one = self.pair_scores(indices, offset, 0)
        if np.any(one[:, 0]):
            return "fail"
        if not np.any(both[:, 0]):
            return "neutral"
        return "ok" if bool(np.all(corr[both[:, 0], 0] >= CONFIRM_CORRELATION)) else "fail"

    def confirm_position(self, i: int, offset: int) -> bool:
        """Point de changement à l'image ``i`` de Film 1 (image ``j = i + offset`` de Film 2).

        Contenu : chaque paire d'images informatives autour du point corrélée (aucune image informative d'un
        seul côté). Position : la bascule ``i - 1 | i`` de Film 1 tombe sur la bascule ``j - 1 | j`` de Film 2
        — l'image ``i - 1`` s'apparie strictement mieux à ``j - 1`` qu'aux images ``j, j + 1…`` qui suivent la
        bascule, l'image ``i`` mieux à ``j`` qu'aux images ``j - 1, j - 2…`` qui la précèdent. Égalité admise
        seulement si ``i - 1`` et ``i`` sont identiques dans Film 1 (aucun changement visible : position sans
        effet). Aucune image informative des deux côtés (noir, uniforme) : position sans effet."""
        indices = self.inside(np.arange(i - POSITION_RADIUS, i + POSITION_RADIUS), offset)
        if indices.size == 0:
            return False
        corr, both, one = self.pair_scores(indices, offset, POSITION_RADIUS)
        center = POSITION_RADIUS
        if np.any(one[:, center]):
            return False
        mask = both[:, center]
        if not np.any(mask):
            return True
        if not bool(np.all(corr[mask, center] >= CONFIRM_CORRELATION)):
            return False
        still = 0 < i < len(self.film1) and bool(self.film1_static[i])
        for frame, crossing in ((i - 1, slice(center + 1, None)), (i, slice(0, center))):
            row = np.flatnonzero(indices == frame)
            if row.size == 0 or not both[row[0], center]:
                continue
            values = np.where(both[row[0]], corr[row[0]], -np.inf)
            rivals = values[crossing]
            if rivals.size and values[center] - rivals.max() <= POSITION_LEAD and not still:
                return False
        return True

    def inside(self, indices: np.ndarray, offset: int) -> np.ndarray:
        indices = np.asarray(indices, dtype=np.int64)
        indices = indices[(indices >= 0) & (indices < len(self.film1))]
        return indices[(indices + offset >= 0) & (indices + offset < self.film2.frames)]


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


# --- Ruptures et passe image par image ------------------------------------------------------------------


def _refine_junction(ctx: _Context, lo: int, hi: int, da: int, db: int) -> tuple[int, int]:
    """Jonction exacte entre deux segments de décalages ``da`` puis ``db`` dans ``[lo, hi)`` de Film 1.

    Rend (première image de Film 1 au décalage ``db``, nombre d'images de Film 1 sans correspondance avant
    elle). ``db > da`` : images de Film 2 retirées ; ``db < da`` : ``da - db`` images de Film 1 insérées.
    """
    inserted = max(0, da - db)
    if hi - lo < inserted + 2:
        raise AlignmentError(f"Jonction {lo}–{hi} : insertion de {inserted} images plus grande que la fenêtre.",
                             ((lo, hi),))
    indices = np.arange(lo, hi, dtype=np.int64)
    scores = {}
    for offset in (da, db):
        values = np.full(indices.size, -1.0)
        valid = ctx.inside(indices, offset)
        if valid.size:
            corr, both, one = ctx.pair_scores(valid, offset, 0)
            values[np.searchsorted(indices, valid)] = np.where(both[:, 0], corr[:, 0],
                                                               np.where(one[:, 0], -1.0, 0.0))
        scores[offset] = values
    prefix_a = np.concatenate([[0.0], np.cumsum(scores[da])])
    suffix_b = np.concatenate([np.cumsum(scores[db][::-1])[::-1], [0.0]])
    positions = np.arange(0, indices.size - inserted + 1)
    totals = prefix_a[positions] + suffix_b[positions + inserted]
    best = int(positions[int(np.argmax(totals))])
    return lo + best + inserted, inserted


def _frame_scan(ctx: _Context, segment: Segment) -> list[tuple[int, int, int]]:
    """Passe complète : écarts locaux à l'intérieur d'un segment (au moins ``FRAME_RUN`` images informatives
    consécutives nettement mieux appariées à un même autre décalage, ± ``FRAME_BAND``) ; (début, fin,
    décalage absolu)."""
    indices = np.arange(segment.film1_start, segment.film1_end, dtype=np.int64)
    out: list[tuple[int, int, int]] = []
    for start in range(0, indices.size, 4096):
        chunk = indices[start:start + 4096]
        corr, both, _one = ctx.pair_scores(chunk, segment.offset, FRAME_BAND)
        masked = np.where(both, corr, -np.inf)
        best = np.argmax(masked, axis=1) - FRAME_BAND
        lead = masked.max(axis=1) - masked[:, FRAME_BAND]
        strong = both[:, FRAME_BAND] & (best != 0) & (lead > 0.02) & ~ctx.film1_static[chunk]
        k = 0
        while k < chunk.size:
            if not strong[k]:
                k += 1
                continue
            end = k
            while end + 1 < chunk.size and strong[end + 1] and best[end + 1] == best[k]:
                end += 1
            if end - k + 1 >= FRAME_RUN:
                out.append((int(chunk[k]), int(chunk[end]) + 1, segment.offset + int(best[k])))
            k = end + 1
    return out


# --- Point d'entrée ------------------------------------------------------------------------------------


def align(film1: Signatures, metadata: Film2Metadata, film2: Film2Frames, *, frame_rate: float,
          force_full_film2: bool = False, hooks: ProcessHooks | None = None,
          log: Callable[[str], None] | None = None) -> AlignmentMap:
    """Carte Film 1 → Film 2 vérifiée, ou :class:`AlignmentError`."""
    hooks = hooks or ProcessHooks()
    hooks.check()
    say = log or (lambda _message: None)
    film2.hooks, film2.log = hooks, say
    n1, n2 = len(film1), metadata.frames
    if n1 == 0 or n2 == 0:
        raise AlignmentError("Film vide.")
    fps = frame_rate if frame_rate > 0 else 24.0
    band1 = active_band(film1)
    cuts1 = detect_cuts(film1, band1)
    hooks.check()
    film1_cut = np.zeros(n1 + 1, dtype=bool)
    film1_cut[cuts1] = True
    if force_full_film2:
        film2.load_full("vérification image par image demandée")
    if not film2.windowed and film2.full is None:
        film2.load_full("entrée non indexable")
    if film2.full is None and not metadata.has_reference_cuts:
        film2.load_full("aucune coupe exploitable dans les métadonnées")
    reference = detect_cuts(film2.full) if film2.full is not None else metadata.reference_cuts

    # Alignement grossier puis programmation dynamique monotone sur les plans de Film 1.
    block = max(1000, int(BLOCK_SECONDS * fps))
    candidates = candidate_states(coarse_offsets(cuts1, n1, reference, n2, block=block, hooks=hooks))
    if not candidates.size:
        raise AlignmentError("Aucune correspondance de coupes entre les deux films : contenus différents ?")
    bounds = np.append(cuts1, n1)
    model = _ShotModel(bounds, film1_cut, reference, n2, candidates, hooks)
    path, best_score = model.solve()
    runs = _segments_from_shots(bounds, model.offsets(path))
    if all(value is None for _a, _b, value, _k0, _k1 in runs):
        raise AlignmentError("Aucun plan de Film 1 ne correspond à Film 2.")

    ctx = _geometry(film1, film2, runs, cuts1, reference, n1, n2, band1, hooks, say)

    # Segments : suites sans image informative rattachées à un voisin, jonctions de plans consécutifs
    # affinées par l'image, bornés à Film 2, monotones.
    raw = _absorb_blank([(a, b, d) for a, b, d, _k0, _k1 in runs], ctx.film1_informative)
    _refine_all(ctx, raw)
    built = _bounded(raw, n2)
    if film2.full is not None:
        built = _frame_refine(ctx, built, n2, say)
    _check_order(built)

    # Marge entre chemins concurrents : la meilleure carte qui évite le décalage d'une suite de plans
    # (transitions comprises, sur la suite et ses voisins) ; écart faible → segment ambigu.
    ambiguous: list[Segment] = []
    for _a, _b, d, k0, k1 in runs:
        if d is None:
            continue
        hooks.check()
        state = int(np.flatnonzero(candidates == d)[0])
        lo_k, hi_k = max(0, k0 - 4), min(model.shots, k1 + 4)
        entry = int(path[lo_k - 1]) if lo_k > 0 else None
        exit_state = int(path[hi_k - 1])
        _best, local = model.solve(lo_k, hi_k, entry=entry, exit=exit_state)
        _alt, alternative = model.solve(lo_k, hi_k, entry=entry, exit=exit_state, forbid=(k0, k1, state))
        if local - alternative < AMBIGUITY_MARGIN:
            ambiguous.extend(seg for seg in built if seg.offset == d and seg.film1_start < bounds[k1]
                             and seg.film1_end > bounds[k0])

    exhaustive = film2.full is not None
    content: list[tuple[int, int, int]] = []        # fenêtres de contenu (début, fin, décalage)
    positions: list[tuple[int, int]] = []           # points dont la position est confirmée par l'image
    on_cut: list[tuple[int, int]] = []              # points sur une coupe commune (contenu à établir)
    for seg in built:
        content.append((seg.film1_start, min(seg.film1_end, seg.film1_start + 6), seg.offset))
        content.append((max(seg.film1_start, seg.film1_end - 6), seg.film1_end, seg.offset))
        points = metadata.change_points[(metadata.change_points >= seg.film2_start)
                                        & (metadata.change_points < seg.film2_end)]
        individual = exhaustive or seg in ambiguous
        for j in points.tolist():
            i = j - seg.offset
            if i == seg.film1_start:
                continue  # bord du segment : jonction affinée par l'image, fenêtre de contenu ci-dessus
            if film1_cut[i] and reference_has(reference, j) and not individual:
                on_cut.append((i, seg.offset))
            else:
                positions.append((i, seg.offset))
    step = max(1, int(SAMPLE_SECONDS * fps))
    for t in range(0, n1, step):
        holder = next((item for item in built if item.film1_start <= t < item.film1_end), None)
        if holder is not None:
            k = int(np.searchsorted(cuts1, t))
            begin = int(cuts1[k]) if k < cuts1.size and cuts1[k] < holder.film1_end else t
            content.append((begin, min(holder.film1_end, begin + 6), holder.offset))

    confirmed = np.zeros(n1, dtype=bool)
    for lo, hi, d in _merge(content):
        result = ctx.confirm_content(np.arange(lo, hi), d)
        if result == "fail":
            raise AlignmentError(f"Images {lo}–{hi - 1} de Film 1 : contenu différent de Film 2 (décalage {d:+d}).",
                                 ((lo, hi),))
        if result == "ok":
            confirmed[lo:hi] = True
    for i, d in positions:
        if not ctx.confirm_position(i, d):
            raise AlignmentError(f"Image {i} de Film 1 : point de changement de Film 2 non confirmé par l'image "
                                 f"(décalage {d:+d}).", ((i - POSITION_RADIUS, i + POSITION_RADIUS),))
        confirmed[max(0, i - POSITION_RADIUS):i + POSITION_RADIUS] = True
    content_checked = _check_luminance(ctx, metadata, cuts1, n1, on_cut, confirmed, say)

    covered = np.zeros(n1, dtype=bool)
    for seg in built:
        covered[seg.film1_start:seg.film1_end] = True
    uncovered = _uncovered(ctx, covered, built)
    used = np.zeros(n2, dtype=bool)
    for seg in built:
        used[seg.film2_start:seg.film2_end] = True
    stats = {
        "film1_cuts": int(cuts1.size), "reference_cuts": int(reference.size), "segments": len(built),
        "change_points": int(metadata.change_points.size), "points_on_cuts": len(on_cut),
        "points_by_image": len(positions), "ambiguous_segments": len(ambiguous), "content_checked": content_checked,
        "film2_decoded": film2.decoded, "film2_full": film2.full is not None, "film2_full_reason": film2.full_reason,
        "band": ctx.band, "dx": ctx.dx, "dy": ctx.dy, "dp_score": best_score,
    }
    return AlignmentMap(n1, n2, tuple(built), _runs(~used), uncovered,
                        _expected_cuts(metadata, reference, built, uncovered, n1), stats)


def reference_has(reference: np.ndarray, j: int) -> bool:
    """L'image ``j`` de Film 2 est une coupe de référence."""
    index = int(np.searchsorted(reference, j))
    return index < reference.size and int(reference[index]) == j


def _absorb_blank(runs: list[tuple[int, int, int | None]], informative: np.ndarray) -> list[list[int]]:
    """Suites de plans sans aucune image informative (noir, uniforme) : sans preuve propre, elles prennent le
    décalage du segment informatif suivant (sinon précédent) s'il les place juste après le précédent dans
    Film 2 sans le chevaucher ; sinon elles restent sans correspondance. Rend [début, fin, décalage]."""
    evidence = [bool(np.any(informative[a:b])) for a, b, _d in runs]
    out: list[list[int]] = []
    for index, (a, b, d) in enumerate(runs):
        if not evidence[index] or d is None:
            following = next((runs[k][2] for k in range(index + 1, len(runs))
                              if evidence[k] and runs[k][2] is not None), None)
            previous = out[-1][2] if out else None
            previous_end = out[-1][1] + out[-1][2] if out else None
            choice = None
            for value in (following, previous):
                if value is None:
                    continue
                if previous_end is not None and a + value < previous_end:
                    continue
                choice = value
                break
            if not evidence[index]:
                d = choice
        if d is None:
            continue
        if out and out[-1][2] == d and out[-1][1] == a:
            out[-1][1] = b
        else:
            out.append([a, b, d])
    return out


def _refine_all(ctx: _Context, raw: list[list[int]]) -> None:
    """Jonctions des segments contigus (plans consécutifs) affinées par l'image ; fenêtre élargie à
    l'insertion, bornes contrôlées."""
    for index in range(len(raw) - 1):
        ctx.hooks.check()
        first_seg, second_seg = raw[index], raw[index + 1]
        if second_seg[0] != first_seg[1]:
            continue  # plans sans correspondance entre les deux : bornes de plans conservées
        span = 240 + abs(first_seg[2] - second_seg[2])
        lo, hi = max(first_seg[0], first_seg[1] - span), min(second_seg[1], second_seg[0] + span)
        start, inserted = _refine_junction(ctx, lo, hi, first_seg[2], second_seg[2])
        if not first_seg[0] <= start - inserted <= start <= second_seg[1]:
            raise AlignmentError(f"Jonction {lo}–{hi} hors des segments.", ((lo, hi),))
        first_seg[1], second_seg[0] = start - inserted, start


def _geometry(film1: Signatures, film2: Film2Frames, runs: list, cuts1: np.ndarray, reference: np.ndarray,
              n1: int, n2: int, band1: Band, hooks: ProcessHooks, say: Callable[[str], None]) -> _Context:
    """Bande commune et translation ``(dx, dy)`` de Film 2, estimées sur au moins ``GEOMETRY_PAIRS`` paires
    d'images à coupe concordante (toutes si moins), cohérentes d'une paire à l'autre."""
    reference_set = np.zeros(n2 + 1, dtype=bool)
    reference_set[reference[(reference >= 0) & (reference < n2)]] = True
    matched: list[tuple[int, int]] = []
    for a, b, d, _k0, _k1 in runs:
        if d is None:
            continue
        for c in cuts1[(cuts1 >= a) & (cuts1 < b) & (cuts1 > 0)].tolist():
            if 0 <= c + d < n2 and reference_set[c + d]:
                matched.append((c, d))
    if len(matched) > 2 * GEOMETRY_PAIRS:
        spots = np.linspace(0, len(matched) - 1, 2 * GEOMETRY_PAIRS).round().astype(int)
        picks = list(dict.fromkeys(matched[int(k)] for k in spots))
    else:
        picks = matched
    if not picks:
        first = next((a, d) for a, _b, d, _k0, _k1 in runs if d is not None)
        picks = [(int(first[0]), int(first[1]))]
    parts1, parts2, groups = [], [], []
    offset = 0
    for c, d in picks:
        hooks.check()
        indices = np.arange(max(0, c - 3), min(n1, c + 4))
        indices = indices[(indices + d >= 0) & (indices + d < n2)]
        if indices.size == 0:
            continue
        parts1.append(take(film1, indices))
        parts2.append(film2.get(int(indices[0] + d), int(indices.size)))
        groups.append(np.arange(offset, offset + indices.size))
        offset += indices.size
    if not parts2:
        raise AlignmentError("Aucune image de Film 2 comparable.")
    joined1, joined2 = _concat(parts1), _concat(parts2)
    margin_y, margin_x = cell_margins(film1, joined2)
    band = band1.intersect(active_band(joined2), margin_y, margin_x)
    if band.empty:
        raise AlignmentError("Zones actives des deux films disjointes : cadrages incompatibles.")
    dx, dy, score, agreement = best_shift(joined1, joined2, band, groups)
    say(f"Cadrage : bande commune {band.top:+.3f}…{band.bottom:+.3f} (largeur), translation de Film 2 "
        f"{dx:+.3f}/{dy:+.3f}, corrélation {score:.2f}, cohérence {agreement:.0%} sur {len(groups)} paires.")
    if score < GEOMETRY_CORRELATION:
        raise AlignmentError(f"Cadrages incompatibles (corrélation {score:.2f} sur la bande commune).")
    if len(groups) >= 3 and agreement < GEOMETRY_AGREEMENT:
        raise AlignmentError(f"Cadrage de Film 2 variable d'un plan à l'autre (cohérence {agreement:.0%}).")
    return _Context(film1, film2, band, dx, dy, hooks)


def _bounded(raw: list[list[int]], n2: int) -> list[Segment]:
    built: list[Segment] = []
    for a, b, d in raw:
        a = max(a, -d, 0)
        b = min(b, n2 - d)
        if b > a:
            built.append(Segment(a, b - a, a + d))
    if not built:
        raise AlignmentError("Aucune image de Film 1 ne correspond à Film 2.")
    return built


def _frame_refine(ctx: _Context, built: list[Segment], n2: int, say: Callable[[str], None]) -> list[Segment]:
    """Passe complète : écarts locaux trouvés image par image découpés en sous-segments, jonctions affinées."""
    for _round in range(8):
        changed = False
        out: list[Segment] = []
        for seg in built:
            gaps = _frame_scan(ctx, seg)
            if not gaps:
                out.append(seg)
                continue
            changed = True
            cursor = seg.film1_start
            pieces: list[list[int]] = []
            for lo, hi, d in gaps:
                if lo > cursor:
                    pieces.append([cursor, lo, seg.offset])
                pieces.append([lo, hi, d])
                cursor = hi
            if cursor < seg.film1_end:
                pieces.append([cursor, seg.film1_end, seg.offset])
            _refine_all(ctx, pieces)
            out.extend(_bounded([p for p in pieces if p[1] > p[0]], n2))
        built = _merge_segments(out)
        if not changed:
            return built
        say(f"Passe image par image : {len(built)} segments après découpe des écarts locaux.")
    raise AlignmentError("Passe image par image : correspondance instable.")


def _merge_segments(segments: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for seg in segments:
        if out and out[-1].offset == seg.offset and out[-1].film1_end == seg.film1_start:
            out[-1] = Segment(out[-1].film1_start, out[-1].length + seg.length, out[-1].film2_start)
        else:
            out.append(seg)
    return out


def _check_order(built: list[Segment]) -> None:
    """Segments strictement croissants et disjoints dans les deux films."""
    for before, after in zip(built, built[1:]):
        if after.film1_start < before.film1_end or after.film2_start < before.film2_end:
            raise AlignmentError(f"Correspondance non monotone entre les images {before.film1_start}–"
                                 f"{after.film1_end} de Film 1 (Film 2 relu à rebours).",
                                 ((before.film1_start, after.film1_end),))


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


def _merge(windows: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Fenêtres ``(début, fin, décalage)`` regroupées si elles se touchent (≤ 48 images)."""
    merged: list[list[int]] = []
    for lo, hi, d in sorted(windows):
        if hi <= lo:
            continue
        if merged and merged[-1][2] == d and lo <= merged[-1][1] and hi - merged[-1][0] <= 48:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi, d])
    return [(lo, hi, d) for lo, hi, d in merged]


def _check_luminance(ctx: _Context, metadata: Film2Metadata, cuts1: np.ndarray, n1: int,
                     on_cut: list[tuple[int, int]], confirmed: np.ndarray, say: Callable[[str], None]) -> int:
    """Contenu des plans dont le début (point de changement) est sur une coupe commune : relation de luminance
    (moyenne et pic) **étalonnée uniquement sur les plans confirmés par l'image**, jamais réajustée sur ceux
    qu'elle valide ; hors tolérance ou sans information → position et contenu confirmés par l'image ;
    étalonnage impossible (moins de ``MIN_CALIBRATION`` plans) → passe complète de Film 2."""
    if not on_cut:
        return 0
    bounds = np.append(cuts1, n1)

    def shot(i: int) -> tuple[int, int]:
        k = int(np.searchsorted(bounds, i, side="right")) - 1
        return int(bounds[k]), int(bounds[k + 1])

    def film1_levels(i: int) -> tuple[float, float]:
        a, b = shot(i)
        return float(np.mean(ctx.film1.mean[a:b])), float(np.max(ctx.film1.peak[a:b]))

    def film2_levels(i: int, d: int) -> tuple[float, float] | None:
        if not metadata.luminance.size:
            return None
        a, b = shot(i)
        lo, hi = max(0, a + d), min(metadata.frames, b + d)
        if hi <= lo:
            return None
        mean = float(np.mean(metadata.luminance[lo:hi]))
        return mean, float(np.max(metadata.peak[lo:hi])) if metadata.peak.size else mean

    def informative(i: int) -> bool:
        a, b = shot(i)
        return float(np.mean(ctx.film1.std[a:b])) >= MIN_STD

    pending = [(i, d) for i, d in on_cut if not confirmed[i]]
    if ctx.film2.full is None:
        pairs = []
        for i, d in on_cut:
            levels = film2_levels(i, d)
            if confirmed[i] and informative(i) and levels is not None:
                pairs.append((levels, film1_levels(i)))
        if len(pairs) < MIN_CALIBRATION:
            ctx.film2.load_full(f"relation de luminance non étalonnable ({len(pairs)} plans confirmés)")
        else:
            fits = []
            for axis in (0, 1):
                xs = np.asarray([p[0][axis] for p in pairs])
                ys = np.asarray([p[1][axis] for p in pairs])
                centers, values = _isotonic(xs, ys)
                residuals = ys - np.interp(xs, centers, values)
                mad = float(np.median(np.abs(residuals - np.median(residuals))))
                fits.append((centers, values, max(3.0 * 1.4826 * mad, 12.0)))
            remaining = []
            for i, d in pending:
                levels = film2_levels(i, d)
                if levels is not None and informative(i):
                    observed = film1_levels(i)
                    if all(abs(observed[axis] - float(np.interp(levels[axis], fits[axis][0], fits[axis][1])))
                           <= fits[axis][2] for axis in (0, 1)):
                        continue
                remaining.append((i, d))
            say(f"Relation de luminance (moyenne, pic) étalonnée sur {len(pairs)} plans confirmés par l'image "
                f"(tolérances {fits[0][2]:.0f} / {fits[1][2]:.0f} codes) ; {len(remaining)} plans hors relation "
                "confirmés par l'image.")
            pending = remaining
    if ctx.film2.full is not None:
        say(f"Film 2 lu entièrement : {len(pending)} plans à coupe commune confirmés par l'image.")
    for i, d in pending:
        if not ctx.confirm_position(i, d):
            raise AlignmentError(f"Plan à l'image {i} de Film 1 : contenu différent de Film 2.",
                                 ((i - POSITION_RADIUS, i + POSITION_RADIUS),))
    return len(pending)


def _uncovered(ctx: _Context, covered: np.ndarray, built: list[Segment]) -> tuple[Uncovered, ...]:
    out = []
    for start, length in _runs(~covered):
        black = bool(np.all(ctx.film1.top[start:start + length] < 0))
        before = next((s for s in reversed(built) if s.film1_end <= start), None)
        after = next((s for s in built if s.film1_start >= start + length), None)
        source = (before.film2_end - 1) if before is not None else (after.film2_start if after is not None else 0)
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
        inside = cuts[(cuts >= seg.film2_start) & (cuts < seg.film2_end)] - seg.offset
        out.update(int(i) for i in inside)
        if previous_end_f2 is not None and seg.film2_start != previous_end_f2:
            out.add(seg.film1_start)
        previous_end_f2 = seg.film2_end
    for gap in uncovered:
        out.add(gap.start)
        if gap.start + gap.length < n1:
            out.add(gap.start + gap.length)
    return np.asarray(sorted(i for i in out if 0 <= i < n1), dtype=np.int64)
