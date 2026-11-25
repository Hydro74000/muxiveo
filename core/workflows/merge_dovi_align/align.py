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
from typing import Any

import numpy as np

from core.i18n import translate_text

from .features import (MIN_STD, Band, active_band, band_features, band_features_contrast, best_shift, cell_margins,
                       pair_masks)
from .probe import Signatures
from .process import ProcessHooks
from .signals import Film2Metadata

#: Corrélation de vignettes consécutives sous laquelle Film 1 change de plan (coupe franche).
CUT_CORRELATION = 0.5
#: Contenu confirmé : corrélation minimale de chaque paire d'images informatives.
CONFIRM_CORRELATION = 0.80
#: Avance minimale du décalage retenu sur ses voisins pour confirmer une position.
POSITION_LEAD = 1e-3
#: Ampleur minimale (corrélation) d'un glissement pris en compte, la même que l'avance exigée par le balayage
#: image par image pour découper un écart local : en deçà (plan quasi fixe, minutage d'un générique un peu
#: différent d'une édition à l'autre), un glissement est visuellement négligeable et ne contredit pas une position.
SLIP_EFFECT = 0.02
#: Images voisines quasi identiques (plan fixe) : une égalité de décalages y est sans effet.
STATIC_CORRELATION = 0.995
POSITION_RADIUS = 3
#: Bande de recherche autour des décalages candidats (images).
BAND = 48
BLOCK_SECONDS = 300
#: Au-delà de cette part de Film 2 lue par fenêtres : passe complète de Film 2.
WINDOW_BUDGET = 0.25
#: Pénalité d'un changement de décalage entre deux plans (en coupes concordantes).
CHANGE_PENALTY = 4.0
NONE_PENALTY = 0.5
#: Insertion maximale (images de Film 1) traitée à la jonction de deux plans consécutifs.
MAX_INSERT = 200
#: Plafond des états de la programmation dynamique et pas des points de reprise (mémoire bornée).
MAX_STATES = 1024
#: Couples (score, position) non dominés gardés pour l'état « sans correspondance ».
MAX_NONE_PAIRS = 32
CHECKPOINT = 64
GEOMETRY_CORRELATION = 0.6
GEOMETRY_AGREEMENT = 0.8
GEOMETRY_PAIRS = 10
#: Recadrage propre à un plan (ex. extraction 2.39 calée en haut ou en bas d'une image IMAX) : pas de la
#: recherche de translation locale, gain de corrélation médiane exigé, hauteur et largeur minimales de la
#: bande commune locale (part de la bande globale).
REFRAME_STEP = 0.0025
REFRAME_GAIN = 0.05
REFRAME_MIN_EXTENT = 0.5
#: Corrélation médiane au-delà de laquelle la géométrie globale explique un plan (aucun cadrage propre cherché),
#: et nombre d'images d'un plan examinées pour estimer son cadrage propre.
REFRAME_TRIGGER = 0.95
REFRAME_SAMPLE = 24
#: Échelles admises entre l'image d'un plan de Film 1 et celle de Film 2 (extraction zoomée).
REFRAME_SCALES = (0.8, 1.25)
#: Passe complète : bande du balayage image par image et longueur minimale d'un écart retenu.
FRAME_BAND = 24
FRAME_RUN = 3
#: Validation plan par plan : images examinées par plan, part minimale de plans concordants (sinon refus : les
#: deux films ne se correspondent pas).
SHOT_SAMPLE = 12
MIN_CONCORDANCE = 0.9
#: Lecture par fenêtres : au plus un plan vérifié par intervalle (secondes de Film 1), dans le budget de lecture.
SAMPLE_SECONDS = 60
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

    @property
    def remaining(self) -> int:
        """Images encore lisibles par fenêtres avant la passe complète (budget)."""
        return max(0, int(self._budget * self.frames) - self.decoded)

    def load_full(self, reason: str, **args: Any) -> Signatures:
        """Passe complète de Film 2 (une fois) ; ``reason`` : motif (modèle en français, paramètres ``args``).
        :class:`AlignmentError` si impossible."""
        if self.full is None:
            self.hooks.check()
            text = translate_text(reason, **args)
            if self._loader is None:
                raise AlignmentError(translate_text("Film 2 doit être lu entièrement ({reason}) mais n'est pas "
                                                    "décodable.", reason=text))
            self.log(translate_text("Passe complète de Film 2 : {reason}.", reason=text))
            reason = reason.format(**args)
            full = self._loader()
            if len(full) != self.frames:
                raise AlignmentError(translate_text(
                    "Film 2 : {count} images décodées pour {frames} images de métadonnées.",
                    count=len(full), frames=self.frames))
            self.full, self.full_reason = full, reason
        return self.full

    def get(self, start: int, count: int) -> Signatures:
        self.hooks.check()
        start = max(0, start)
        stop = min(self.frames, start + count)
        if stop <= start:
            raise AlignmentError(translate_text("Fenêtre de Film 2 hors du film."))
        if self.full is None and self._window is None:
            self.load_full("entrée non indexable")
        if self.full is None and self.decoded + (stop - start) > self._budget * self.frames:
            self.load_full("plus de {percent} % de Film 2 à lire par fenêtres", percent=int(self._budget * 100))
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

    def _step(self, score: np.ndarray, pairs: tuple[np.ndarray, np.ndarray], emission: np.ndarray,
              k: int) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray], np.ndarray, np.ndarray]:
        """Un plan de plus.

        ``score`` : états placés ; ``pairs`` : « sans correspondance », couples non dominés (score, position)
        triés par position croissante — position = fin dans Film 2 du dernier plan placé du chemin ; un couple
        à position plus tardive n'est gardé que s'il a un meilleur score. Reprise d'un état placé depuis un
        couple seulement si le plan repris commence après sa position. Rend (scores placés, couples, sources
        des états placés, origines des couples) ; codage des sources : ``s`` (état placé) ou ``états + p``
        (couple p du plan précédent).
        """
        start = int(self.bounds[k])
        none_scores, none_positions = pairs
        prefix_best = np.maximum.accumulate(score)
        prefix_arg = _prefix_argmax(score)
        allowed, allowed_arg = prefix_best[self.limit], prefix_arg[self.limit]
        reach = np.searchsorted(none_positions, start + self.candidates, side="right") - 1
        from_none_score = np.where(reach >= 0, none_scores[np.clip(reach, 0, None)], -np.inf) \
            if none_scores.size else np.full(self.candidates.size, -np.inf)
        from_none = from_none_score > allowed
        best = np.where(from_none, from_none_score, allowed)
        arg = np.where(from_none, self.states + np.clip(reach, 0, None), allowed_arg)
        switch = best - CHANGE_PENALTY
        stay = switch <= score
        sources = np.where(stay, np.arange(self.candidates.size), arg)
        mapped = np.where(stay, score, switch) + emission[:self.none]
        # Couples : rester sans correspondance, ou y entrer depuis un état placé (position = fin de son plan).
        stay_scores = none_scores + emission[self.none]
        enter_scores = score - CHANGE_PENALTY + emission[self.none]
        enter_positions = (start + self.candidates).astype(np.float64)
        all_scores = np.concatenate([stay_scores, enter_scores])
        all_positions = np.concatenate([none_positions, enter_positions])
        origins = np.concatenate([self.states + np.arange(none_scores.size), np.arange(self.candidates.size)])
        order = np.lexsort((-all_scores, all_positions))
        kept: list[int] = []
        running = -np.inf
        for index in order:
            if all_scores[index] > running:
                kept.append(int(index))
                running = float(all_scores[index])
        if len(kept) > MAX_NONE_PAIRS:
            spots = np.linspace(0, len(kept) - 1, MAX_NONE_PAIRS).round().astype(int)
            kept = [kept[int(spot)] for spot in sorted(set(spots.tolist()))]
        chosen = np.asarray(kept, dtype=np.int64)
        return mapped, (all_scores[chosen], all_positions[chosen]), sources, origins[chosen]

    def _start(self, emission: np.ndarray, entry: int | None,
               k: int) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray], np.ndarray, np.ndarray]:
        empty_pairs = (np.zeros(0), np.zeros(0))
        if entry is None:
            pairs = (np.asarray([emission[self.none]]), np.asarray([-np.inf]))
            return emission[:self.none].copy(), pairs, np.arange(self.candidates.size), np.asarray([-1])
        score = np.full(self.candidates.size, -1e12)
        pairs = empty_pairs
        if entry == self.none:
            pairs = (np.asarray([0.0]), np.asarray([-np.inf]))
        else:
            score[entry] = 0.0
        return self._step(score, pairs, emission, k)

    def solve(self, first: int = 0, last: int | None = None, *, entry: int | None = None,
              exit: int | None = None, forbid: tuple[int, int, int] | None = None) -> tuple[np.ndarray, float]:
        """Meilleur chemin sur les plans ``[first, last)`` et son score. Chemin : état placé ``s`` ou
        ``self.none`` (sans correspondance). ``entry`` : état imposé au plan précédant ``first`` ; ``exit`` :
        état imposé au dernier plan ; ``forbid`` : (plans début, fin, état) interdit."""
        last = self.shots if last is None else last
        checkpoints: list[tuple[np.ndarray, tuple[np.ndarray, np.ndarray]]] = []
        score = np.zeros(0)
        pairs: tuple[np.ndarray, np.ndarray] = (np.zeros(0), np.zeros(0))
        for k in range(first, last):
            if (k - first) % 256 == 0:
                self.hooks.check()
            emission = self.emission(k, forbid)
            if k == first:
                score, pairs, _sources, _origins = self._start(emission, entry, k)
            else:
                score, pairs, _sources, _origins = self._step(score, pairs, emission, k)
            if (k - first) % CHECKPOINT == CHECKPOINT - 1:
                checkpoints.append((score.copy(), (pairs[0].copy(), pairs[1].copy())))
        best_none = int(np.argmax(pairs[0])) if pairs[0].size else -1
        none_value = float(pairs[0][best_none]) if best_none >= 0 else -np.inf
        if exit is not None and exit != self.none:
            state, total = int(exit), float(score[exit])
        elif exit == self.none or none_value > float(score.max()):
            state, total = self.states + best_none, none_value
        else:
            state = int(np.argmax(score))
            total = float(score[state])
        path = np.zeros(last - first, dtype=np.int64)
        # Remontée bloc par bloc : recalcul des sources depuis le point de reprise du bloc.
        block_end = last
        while block_end > first:
            block_start = first + ((block_end - 1 - first) // CHECKPOINT) * CHECKPOINT
            self.hooks.check()
            steps: list[tuple[np.ndarray, np.ndarray]] = []
            if block_start == first:
                score, pairs, sources, origins = self._start(self.emission(first, forbid), entry, first)
                steps.append((sources, origins))
                begin = first + 1
            else:
                saved, saved_pairs = checkpoints[(block_start - first) // CHECKPOINT - 1]
                score, pairs = saved.copy(), saved_pairs
                begin = block_start
            for k in range(begin, block_end):
                score, pairs, sources, origins = self._step(score, pairs, self.emission(k, forbid), k)
                steps.append((sources, origins))
            for k in range(block_end - 1, block_start - 1, -1):
                path[k - first] = self.none if state >= self.states else state
                sources, origins = steps[k - block_start]
                state = int(origins[state - self.states]) if state >= self.states else int(sources[state])
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
    #: Zones actives des deux films (bornes de la translation locale d'un plan recadré) ; None : sans recadrage.
    area1: Band | None = None
    area2: Band | None = None
    #: Coupes de Film 1 (un recadrage vaut pour un plan entier).
    cuts1: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    film1_features: np.ndarray = field(init=False)
    film1_contrast: np.ndarray = field(init=False)
    film1_informative: np.ndarray = field(init=False)
    film1_static: np.ndarray = field(init=False)
    #: Translation locale retenue par (plan de Film 1, décalage) : plans cadrés autrement dans les deux films.
    reframed: dict[tuple[int, int], tuple[float, float, float]] = field(init=False)
    #: Cadrage propre estimé par (plan de Film 1, décalage d'ancrage) ; None : géométrie globale.
    _framings: dict[tuple[int, int], tuple[float, float, float, Band, Band] | None] = field(init=False)
    #: Balayage image par image de Film 2 appliqué à la construction des segments (passe complète chargée avant).
    exhaustive: bool = field(init=False)
    #: Passages où les deux films diffèrent localement de quelques images puis reviennent au décalage du segment
    #: (balayage image par image) : décalage du segment gardé, signalés.
    excursions: list[tuple[int, int, int]] = field(init=False)
    #: Dernière fenêtre de Film 2 lue (contrôle puis comparaison d'une même position : une seule lecture).
    _last_window: tuple[int, int, Signatures] | None = field(init=False)

    def __post_init__(self) -> None:
        self.film1_features, self.film1_contrast = band_features_contrast(self.film1, self.band)
        self.film1_informative = self.film1_contrast >= MIN_STD
        static = np.zeros(len(self.film1), dtype=bool)
        if len(self.film1) > 1:
            static[1:] = np.sum(self.film1_features[1:] * self.film1_features[:-1], axis=1) >= STATIC_CORRELATION
        self.film1_static = static
        self.reframed = {}
        self._framings = {}
        self.excursions = []
        self.exhaustive = self.film2.full is not None   # passe complète présente dès la construction
        self._last_window = None

    def _window(self, indices: np.ndarray, offset: int, spread: int) -> tuple[Signatures, int]:
        self.hooks.check()
        lo = int(indices.min()) + offset - spread
        hi = int(indices.max()) + offset + spread + 1
        lo_c, hi_c = max(0, lo), min(self.film2.frames, hi)
        if hi_c <= lo_c:
            raise AlignmentError(translate_text("Fenêtre de Film 2 hors du film."))
        last = self._last_window
        if last is not None and last[0] == lo_c and last[1] == hi_c:
            return last[2], lo_c
        window = self.film2.get(lo_c, hi_c - lo_c)
        self._last_window = (lo_c, hi_c, window)
        return window, lo_c

    @staticmethod
    def _scores(f1: np.ndarray, c1: np.ndarray, f2: np.ndarray, c2: np.ndarray, indices: np.ndarray,
                rows: np.ndarray, offset: int, spread: int, lo_c: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Matrices (lignes ``rows`` de ``f1``/``c1`` — vecteurs, contrastes — pour les images ``indices``) ×
        décalages ; paires comparables / en désaccord selon :func:`pair_masks`."""
        width = 2 * spread + 1
        corr = np.full((indices.size, width), -1.0, dtype=np.float32)
        both = np.zeros((indices.size, width), dtype=bool)
        one = np.zeros((indices.size, width), dtype=bool)
        for column, shift in enumerate(range(-spread, spread + 1)):
            j = indices + offset + shift - lo_c
            ok = (j >= 0) & (j < f2.shape[0])
            corr[ok, column] = np.sum(f1[rows[ok]] * f2[j[ok]], axis=1)
            both[ok, column], one[ok, column] = pair_masks(c1[rows[ok]], c2[j[ok]], corr[ok, column])
        return corr, both, one

    def pair_scores(self, indices: np.ndarray, offset: int, spread: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Corrélations (images × décalages offset-spread…offset+spread) ; masques « deux informatives » et
        « une seule informative » (désaccord). Géométrie globale."""
        window, lo_c = self._window(indices, offset, spread)
        f2, c2 = band_features_contrast(window, self.band.shifted(self.dy, self.dx))
        return self._scores(self.film1_features, self.film1_contrast, f2, c2, indices, indices, offset, spread, lo_c)

    def checked_scores(self, indices: np.ndarray, offset: int, spread: int, *,
                       anchor: int | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Comme :meth:`pair_scores`, avec le cadrage propre de chaque plan de Film 1 recadré (extraction 2.39
        décalée dans l'image IMAX…) : estimé une fois par plan au décalage d'ancrage ``anchor`` (celui du segment,
        établi par les coupes, indépendamment de l'image ; ``offset`` par défaut), puis appliqué à tout le plan et à
        tous les décalages comparés — le cadrage est une propriété du plan, pas du décalage testé. Seuils
        inchangés. Sans marge de glissement entre les zones actives : géométrie globale seule."""
        window, lo_c = self._window(indices, offset, spread)
        f2, c2 = band_features_contrast(window, self.band.shifted(self.dy, self.dx))
        corr, both, one = self._scores(self.film1_features, self.film1_contrast, f2, c2, indices, indices,
                                       offset, spread, lo_c)
        if not self._can_reframe():
            return corr, both, one
        anchor = offset if anchor is None else anchor
        shots = np.searchsorted(self.cuts1, indices, side="right")
        for shot in np.unique(shots).tolist():
            rows = np.flatnonzero(shots == shot)
            hint = (indices[rows], window, lo_c) if anchor == offset else None
            framing = self._shot_framing(int(shot), anchor, hint)
            if framing is None:
                continue
            _scale, _ty, _tx, band, band2 = framing
            g1, k1 = band_features_contrast(take(self.film1, indices[rows]), band)
            g2, k2 = band_features_contrast(window, band2)
            c, b, o = self._scores(g1, k1, g2, k2, indices[rows], np.arange(rows.size), offset, spread, lo_c)
            # Jamais pire que la géométrie globale sur les images comparées (garde contre une estimation faussée).
            kept = both[rows, spread] & b[:, spread]
            if np.any(kept) and float(np.median(c[kept, spread])) < float(np.median(corr[rows[kept], spread])):
                continue
            corr[rows], both[rows], one[rows] = c, b, o
        return corr, both, one

    def _can_reframe(self) -> bool:
        """Marge de glissement entre les zones actives (verticale ou horizontale) : un recadrage propre à un plan
        n'est admissible que si la zone de l'un des films peut se déplacer dans celle de l'autre."""
        if self.area1 is None or self.area2 is None:
            return False
        a1, a2 = self.area1, self.area2
        return (abs((a2.top - a1.top) - (a2.bottom - a1.bottom)) > 2 * REFRAME_STEP
                or abs((a2.left - a1.left) - (a2.right - a1.right)) > 2 * REFRAME_STEP)

    def _shot_framing(self, shot: int, anchor: int, hint: tuple[np.ndarray, Signatures, int] | None
                      ) -> tuple[float, float, float, Band, Band] | None:
        """Cadrage propre du plan ``shot`` de Film 1 au décalage d'ancrage : (échelle, translation y, x, bande de
        Film 1, bande de Film 2), ou None. Images fournies (``hint``) expliquées par la géométrie globale
        (corrélation médiane ≥ ``REFRAME_TRIGGER``) : None, sans rien mémoriser. Sinon, estimation unique pour le
        plan (mémorisée) sur ces images si elles sont assez nombreuses (``REFRAME_SAMPLE`` / 2 informatives), sinon
        sur un bloc central du plan ; None si aucune similitude admissible ne gagne ``REFRAME_GAIN``."""
        key = (shot, anchor)
        if key in self._framings:
            return self._framings[key]
        n1 = len(self.film1)
        start = int(self.cuts1[shot - 1]) if shot > 0 else 0
        stop = int(self.cuts1[shot]) if shot < self.cuts1.size else n1
        frames, window, lo_c = hint if hint is not None else (np.zeros(0, dtype=np.int64), None, 0)
        if hint is not None and window is not None and self._explained(frames, anchor, window, lo_c):
            return None                         # ces images-ci : géométrie globale (rien de mémorisé pour le plan)
        if hint is None or int(np.sum(self.film1_informative[frames])) < REFRAME_SAMPLE // 2:
            middle = (start + stop) // 2
            frames = self.inside(np.arange(max(start, middle - REFRAME_SAMPLE // 2),
                                           min(stop, middle + REFRAME_SAMPLE // 2)), anchor)
            if frames.size == 0:
                self._framings[key] = None
                return None
            window, lo_c = self._window(frames, anchor, 0)
        assert window is not None
        result = self._estimate_framing(frames, anchor, window, lo_c)
        self._framings[key] = result
        if result is not None:
            self.reframed[key] = (result[0], result[1], result[2])
        return result

    def _explained(self, frames: np.ndarray, anchor: int, window: Signatures, lo_c: int) -> bool:
        """Images comparables (au moins trois) expliquées par la géométrie globale au décalage d'ancrage :
        corrélation médiane ≥ ``REFRAME_TRIGGER`` (aucun cadrage propre à chercher, aucune lecture de plus)."""
        j = frames + anchor - lo_c
        ok = (j >= 0) & (j < len(window))
        if int(np.sum(ok)) < 3:
            return False
        g2, k2 = band_features_contrast(take(window, j[ok]), self.band.shifted(self.dy, self.dx))
        corr = np.sum(self.film1_features[frames[ok]] * g2, axis=1)
        comparable, _opposed = pair_masks(self.film1_contrast[frames[ok]], k2, corr)
        return int(np.sum(comparable)) >= 3 and float(np.median(corr[comparable])) >= REFRAME_TRIGGER

    def _estimate_framing(self, frames: np.ndarray, anchor: int, window: Signatures,
                          lo_c: int) -> tuple[float, float, float, Band, Band] | None:
        """Cadrage propre d'un plan : similitude (échelle ``s``, translation ``ty``, ``tx`` : point ``p`` de Film 1 ↔
        ``s·p + t`` de Film 2 ; extraction 2.39 décalée ou zoomée dans l'image IMAX) maximisant la corrélation
        médiane des paires comparables avec la géométrie globale, toutes gardées ; bandes bornées par les zones
        actives des deux films ; recherche grossière puis affinée. None sans gain ``REFRAME_GAIN``."""
        assert self.area1 is not None and self.area2 is not None
        a1, a2 = self.area1, self.area2
        sub = take(self.film1, frames)
        j = frames + anchor - lo_c
        ok = (j >= 0) & (j < len(window))
        sub2 = take(window, np.clip(j, 0, len(window) - 1))     # images comparées seules (pas toute la fenêtre)
        margin_y, margin_x = cell_margins(self.film1, window)
        height, width = self.band.bottom - self.band.top, self.band.right - self.band.left

        def evaluate(band1: Band, band2: Band) -> tuple[np.ndarray, np.ndarray]:
            g1, k1 = band_features_contrast(sub, band1)
            g2, k2 = band_features_contrast(sub2, band2)
            corr = np.full(frames.size, -1.0, dtype=np.float32)
            corr[ok] = np.sum(g1[ok] * g2[ok], axis=1)
            comparable, _opposed = pair_masks(k1, k2, corr)
            return corr, ok & comparable

        base, kept = evaluate(self.band, self.band.shifted(self.dy, self.dx))
        if not np.any(kept):
            return None
        reference = float(np.median(base[kept]))
        if reference >= REFRAME_TRIGGER:
            return None
        cache: dict[tuple[float, float, float], tuple[float, Band, Band] | None] = {}

        def score(scale: float, ty: float, tx: float) -> tuple[float, Band, Band] | None:
            key = (round(scale, 5), round(ty, 5), round(tx, 5))
            if key in cache:
                return cache[key]
            back = Band((a2.top - ty) / scale, (a2.bottom - ty) / scale, (a2.left - tx) / scale,
                        (a2.right - tx) / scale)
            band1 = a1.intersect(back, margin_y, margin_x)
            result = None
            if not (band1.empty or band1.bottom - band1.top < REFRAME_MIN_EXTENT * height
                    or band1.right - band1.left < REFRAME_MIN_EXTENT * width):
                band2 = Band(band1.top * scale + ty, band1.bottom * scale + ty, band1.left * scale + tx,
                             band1.right * scale + tx)
                corr, comparable = evaluate(band1, band2)
                if bool(np.all(comparable[kept])):      # une paire comparable ne doit pas sortir du contrôle
                    result = (float(np.median(corr[kept])), band1, band2)
            cache[key] = result
            return result

        best: tuple[float, float, float, float, Band, Band] | None = None
        for scale in np.arange(REFRAME_SCALES[0], REFRAME_SCALES[1] + 1e-9, 0.05):
            for ty in np.arange(-0.2, 0.2001, 0.04):
                for tx in np.arange(-0.2, 0.2001, 0.04):
                    found = score(float(scale), float(ty), float(tx))
                    if found is not None and (best is None or found[0] > best[0]):
                        best = (found[0], float(scale), float(ty), float(tx), found[1], found[2])
        if best is None:
            return None
        steps = (0.025, 0.02, 0.02)
        for _level in range(7):                     # affinage : voisinage 3 × 3 × 3, pas divisés par deux
            centre = best
            for ds in (-steps[0], 0.0, steps[0]):
                for dty in (-steps[1], 0.0, steps[1]):
                    for dtx in (-steps[2], 0.0, steps[2]):
                        found = score(centre[1] + ds, centre[2] + dty, centre[3] + dtx)
                        if found is not None and found[0] > best[0]:
                            best = (found[0], centre[1] + ds, centre[2] + dty, centre[3] + dtx, found[1], found[2])
            steps = (steps[0] / 2, steps[1] / 2, steps[2] / 2)
        if best[0] < reference + REFRAME_GAIN:
            return None
        return best[1], best[2], best[3], best[4], best[5]

    def inside(self, indices: np.ndarray, offset: int) -> np.ndarray:
        indices = np.asarray(indices, dtype=np.int64)
        indices = indices[(indices >= 0) & (indices < len(self.film1))]
        return indices[(indices + offset >= 0) & (indices + offset < self.film2.frames)]


@dataclass
class ShotReport:
    """Validation plan par plan : plans mesurés (pondérés par leur durée), part concordante, plans non concordants
    et plans où un décalage voisin fait nettement mieux (début, fin en images de Film 1)."""

    measured: int = 0
    share: float = 1.0
    discordant: list[tuple[int, int]] = field(default_factory=list)
    better: list[tuple[int, int]] = field(default_factory=list)


def _validate_shots(ctx: _Context, built: list[Segment], every: int) -> ShotReport:
    """Chaque plan de Film 1 (partie d'un segment) : bloc central d'au plus ``SHOT_SAMPLE`` images comparé à
    Film 2 au décalage du segment (cadrage propre au plan compris). Paires mesurées : comparables (corrélation)
    ou en désaccord (image informative face à une image plate : −1) ; concordant si leur médiane atteint
    ``CONFIRM_CORRELATION`` ; « décalage voisin » si un décalage ±1…±rayon fait mieux de plus de ``SLIP_EFFECT``
    en médiane sur les mêmes images tout en concordant ; sans paire mesurée (noir des deux côtés) : non mesuré.
    Part concordante pondérée par la durée des plans. Lecture par fenêtres (chacune un décodage) : plans
    échantillonnés, un toutes les ``every`` images de Film 1, dans la moitié du budget restant (cadrage compris) ;
    l'échantillon s'arrête avant d'épuiser le budget."""
    n1 = len(ctx.film1)
    bounds = np.unique(np.concatenate([ctx.cuts1, [0, n1]]))
    parts: list[tuple[Segment, int, int]] = []
    for seg in built:
        inside = bounds[(bounds > seg.film1_start) & (bounds < seg.film1_end)]
        edges = np.concatenate([[seg.film1_start], inside, [seg.film1_end]])
        parts += [(seg, a, b) for a, b in zip(edges[:-1].tolist(), edges[1:].tolist())]
    cost = SHOT_SAMPLE + 2 * POSITION_RADIUS + (REFRAME_SAMPLE if ctx._can_reframe() else 0)
    if ctx.film2.full is None and parts:
        ends = np.asarray([b for _seg, _a, b in parts])
        end = int(ends[-1])
        times = np.arange(min(every, end) // 2, end, max(1, every))    # au moins le milieu d'un film court
        chosen = np.unique(np.searchsorted(ends, times, side="right"))
        chosen = chosen[chosen < len(parts)]
        limit = max(1, ctx.film2.remaining // (2 * cost))
        if chosen.size > limit:
            chosen = np.unique(chosen[np.linspace(0, chosen.size - 1, limit).round().astype(np.int64)])
        parts = [parts[k] for k in chosen.tolist()]
    center = POSITION_RADIUS
    report = ShotReport()
    total = agreeing = 0
    windowed = ctx.film2.full is None
    for seg, a, b in parts:
        if windowed and report.measured and ctx.film2.remaining < 2 * cost:
            break                                   # échantillon arrêté avant d'épuiser le budget des fenêtres
        middle = (a + b) // 2
        sample = ctx.inside(np.arange(max(a, middle - SHOT_SAMPLE // 2), min(b, middle + SHOT_SAMPLE // 2)),
                            seg.offset)
        if sample.size == 0:
            continue
        corr, both, one = ctx.checked_scores(sample, seg.offset, POSITION_RADIUS)
        measured = both | one
        values = np.where(both, corr, -1.0)         # désaccord informative / plate : −1
        if not np.any(measured[:, center]):
            continue
        here = float(np.median(values[measured[:, center], center]))
        weight = b - a
        total += weight
        report.measured += 1
        if here >= CONFIRM_CORRELATION:
            agreeing += weight
        else:
            report.discordant.append((a, b))
        for column in range(2 * POSITION_RADIUS + 1):
            pairs = measured[:, center] & measured[:, column]
            if column == center or not np.any(pairs):
                continue
            rival = float(np.median(values[pairs, column]))
            if rival >= CONFIRM_CORRELATION and rival - float(np.median(values[pairs, center])) > SLIP_EFFECT:
                report.better.append((a, b))
                break
    report.share = agreeing / total if total else 1.0
    return report


def _ranges(spans: list[tuple[int, int]], limit: int = 6) -> str:
    """Plages d'images lisibles au journal (les premières seulement)."""
    shown = ", ".join(f"{a}–{b - 1}" for a, b in spans[:limit])
    return shown + (" …" if len(spans) > limit else "")


# --- Ruptures et passe image par image ------------------------------------------------------------------


def _refine_junction(ctx: _Context, lo: int, hi: int, da: int, db: int) -> tuple[int, int]:
    """Jonction exacte entre deux segments de décalages ``da`` puis ``db`` dans ``[lo, hi)`` de Film 1.

    Rend (première image de Film 1 au décalage ``db``, nombre d'images de Film 1 sans correspondance avant
    elle). ``db > da`` : images de Film 2 retirées ; ``db < da`` : ``da - db`` images de Film 1 insérées.
    """
    inserted = max(0, da - db)
    if hi - lo < inserted + 2:
        raise AlignmentError(translate_text(
            "Jonction {lo}–{hi} : insertion de {inserted} images plus grande que la fenêtre.",
            lo=lo, hi=hi, inserted=inserted),
                             ((lo, hi),))
    indices = np.arange(lo, hi, dtype=np.int64)
    scores = {}
    for offset in (da, db):
        values = np.full(indices.size, -1.0)
        valid = ctx.inside(indices, offset)
        if valid.size:
            corr, both, one = ctx.checked_scores(valid, offset, 0)
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
    décalage absolu). Calcul par blocs, suites lues sur tout le segment."""
    indices = np.arange(segment.film1_start, segment.film1_end, dtype=np.int64)
    best = np.zeros(indices.size, dtype=np.int64)
    strong = np.zeros(indices.size, dtype=bool)
    for start in range(0, indices.size, 4096):
        chunk = indices[start:start + 4096]
        corr, both, _one = ctx.pair_scores(chunk, segment.offset, FRAME_BAND)
        masked = np.where(both, corr, -np.inf)
        best[start:start + chunk.size] = np.argmax(masked, axis=1) - FRAME_BAND
        center = both[:, FRAME_BAND]
        # Avance seulement là où l'image du décalage courant est informative (sinon -inf − -inf).
        lead = np.where(center, masked.max(axis=1) - np.where(center, masked[:, FRAME_BAND], 0.0), 0.0)
        # Écart retenu seulement si le nouveau décalage correspond vraiment (≥ CONFIRM_CORRELATION) : dans un
        # passage au contenu différent, toutes les corrélations sont basses et un « meilleur » décalage de hasard
        # ne découpe pas le segment.
        strong[start:start + chunk.size] = (both[:, FRAME_BAND] & (best[start:start + chunk.size] != 0)
                                            & (lead > SLIP_EFFECT) & (masked.max(axis=1) >= CONFIRM_CORRELATION)
                                            & ~ctx.film1_static[chunk])
    out: list[tuple[int, int, int]] = []
    k = 0
    while k < indices.size:
        if not strong[k]:
            k += 1
            continue
        end = k
        while end + 1 < indices.size and strong[end + 1] and best[end + 1] == best[k]:
            end += 1
        if end - k + 1 >= FRAME_RUN:
            run = (int(indices[k]), int(indices[end]) + 1, segment.offset + int(best[k]))
            framed = (run[0] - segment.film1_start >= FRAME_BAND and segment.film1_end - run[1] >= FRAME_BAND)
            if not _framed_lead(ctx, indices[k:end + 1], segment.offset, run[2]):
                pass                            # écart apparent non confirmé (biais de fondu, cadrage) : ignoré
            elif framed:
                ctx.excursions.append(run)      # encadré par le décalage du segment : excursion locale, signalée
            else:
                out.append(run)

        k = end + 1
    return out


def _framed_lead(ctx: _Context, run: np.ndarray, offset: int, other: int) -> bool:
    """Écart local confirmé avec le cadrage propre du plan (ancré au décalage du segment) : ``other = offset + k`` garde
    son avance sur ``offset`` pour chaque image comparable du passage. Lecture symétrique quand l'image ``r − k``
    appartient au même passage (moyenne de ``r`` et ``r − k`` au décalage ``other`` : biais de netteté d'un fondu
    compensé), lecture brute aux bords et dans un passage plus court que ``|k|``."""
    k = other - offset
    rows = np.union1d(run, run - k)
    rows = rows[(rows >= 0) & (rows < len(ctx.film1))]
    base, base_both, _one = ctx.checked_scores(run, offset, 0)
    alt, alt_both, _alt_one = ctx.checked_scores(rows, other, 0, anchor=offset)   # cadrage du plan au segment
    position = {int(frame): n for n, frame in enumerate(rows.tolist())}
    members = {int(frame) for frame in run.tolist()}
    leads = []
    for n, frame in enumerate(run.tolist()):
        a = position[int(frame)]
        if not bool(base_both[n, 0]) or not bool(alt_both[a, 0]):
            continue
        b = position.get(int(frame) - k) if int(frame) - k in members else None
        value = float(alt[a, 0])
        if b is not None and bool(alt_both[b, 0]):
            value = (value + float(alt[b, 0])) / 2.0
        leads.append(value - float(base[n, 0]))
    return bool(leads) and bool(np.all(np.asarray(leads) > SLIP_EFFECT))


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
        raise AlignmentError(translate_text("Film vide."))
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
        raise AlignmentError(translate_text(
            "Aucune correspondance de coupes entre les deux films : contenus différents ?"))
    bounds = np.append(cuts1, n1)
    model = _ShotModel(bounds, film1_cut, reference, n2, candidates, hooks)
    path, best_score = model.solve()
    runs = _segments_from_shots(bounds, model.offsets(path))
    if all(value is None for _a, _b, value, _k0, _k1 in runs):
        raise AlignmentError(translate_text("Aucun plan de Film 1 ne correspond à Film 2."))

    ctx = _geometry(film1, film2, runs, cuts1, reference, n1, n2, band1, hooks, say)

    # Segments : suites sans image informative rattachées à un voisin, jonctions de plans consécutifs
    # affinées par l'image, bornés à Film 2, monotones.
    raw = _absorb_blank([(a, b, d) for a, b, d, _k0, _k1 in runs], ctx.film1_informative)
    _refine_all(ctx, raw)
    built = _bounded(raw, n2)
    full_at_build = film2.full is not None
    ctx.exhaustive = full_at_build
    if full_at_build:
        built = _frame_refine(ctx, built, n2, say)
    _check_order(built)

    # Validation plan par plan : un échantillon d'images de chaque plan au décalage de son segment (cadrage propre
    # du plan compris). Refus seulement si les films ne se correspondent pas globalement ; tout écart local est
    # signalé.
    covered = np.zeros(n1, dtype=bool)
    for seg in built:
        covered[seg.film1_start:seg.film1_end] = True
    uncovered = _uncovered(ctx, covered, built)
    report = _validate_shots(ctx, built, max(1, int(SAMPLE_SECONDS * fps)))
    if report.better and film2.full is None:
        film2.load_full("décalage voisin plus net sur {count} plan(s) : vérification image par image",
                        count=len(report.better))
    if report.measured and report.share < MIN_CONCORDANCE and film2.full is None:
        film2.load_full("{percent} % seulement des plans échantillonnés concordants : vérification de tous les plans",
                        percent=f"{100 * report.share:.0f}")
    if film2.full is not None and not full_at_build:
        # Passe complète chargée en cours de validation : glissements découpés par le balayage, validation reprise.
        say(translate_text("Vérification reprise sur la passe complète de Film 2."))
        return align(film1, metadata, film2, frame_rate=frame_rate, hooks=hooks, log=log)
    if report.measured and report.share < MIN_CONCORDANCE:
        raise AlignmentError(translate_text(
            "Films non concordants : {percent} % seulement de la durée des plans vérifiés correspond à l'image au "
            "décalage trouvé (plans {ranges}).", percent=f"{100 * report.share:.0f}", ranges=_ranges(report.discordant)),
            tuple(report.discordant[:20]))
    if report.measured:
        say(translate_text("Concordance de l'image : {percent} % de la durée des plans vérifiés ({count} plans).",
                           percent=f"{100 * report.share:.0f}", count=report.measured))
    else:
        say("[WARN] " + translate_text(
            "Aucun plan vérifiable sur l'image (images noires ou uniformes) : correspondance établie par les coupes."))
    if report.discordant:
        say("[WARN] " + translate_text(
            "{count} plan(s) ou passage(s) non concordant(s) (version propre à un master, fondu, image très sombre) : "
            "métadonnées selon le décalage de leur segment (images de Film 1 : {ranges}).",
            count=len(report.discordant), ranges=_ranges(report.discordant)))
    if report.better:
        say("[WARN] " + translate_text(
            "{count} plan(s) où un décalage voisin semble plus net (minutage des masters) : décalage du segment "
            "conservé (images de Film 1 : {ranges}).", count=len(report.better), ranges=_ranges(report.better)))
    if ctx.excursions:
        say("[WARN] " + translate_text(
            "{count} passage(s) où les deux films diffèrent localement de quelques images puis se réalignent "
            "(minutage des masters) : décalage du segment conservé (images de Film 1 : {ranges}).",
            count=len(ctx.excursions), ranges=_ranges([(a, b) for a, b, _d in ctx.excursions])))
    if ctx.reframed:
        largest = max(max(abs(ty - ctx.dy), abs(tx - ctx.dx)) for _s, ty, tx in ctx.reframed.values()) * film1.width
        say(translate_text(
            "{count} plan(s) cadré(s) autrement dans les deux films (cadrage propre au plan : décalage jusqu'à "
            "{pixels} px, échelle {scales}) : contenu vérifié sur leur cadrage.",
            count=len({shot for shot, _d in ctx.reframed}), pixels=f"{largest:.0f}",
            scales="–".join(f"{v:.2f}" for v in (min(f[0] for f in ctx.reframed.values()),
                                                  max(f[0] for f in ctx.reframed.values())))))
    used = np.zeros(n2, dtype=bool)
    for seg in built:
        used[seg.film2_start:seg.film2_end] = True
    stats = {
        "film1_cuts": int(cuts1.size), "reference_cuts": int(reference.size), "segments": len(built),
        "change_points": int(metadata.change_points.size), "shots_checked": report.measured,
        "shot_concordance": report.share, "discordant_shots": len(report.discordant),
        "better_offset_shots": len(report.better),
        "film2_decoded": film2.decoded, "film2_full": film2.full is not None, "film2_full_reason": film2.full_reason,
        "exhaustive": full_at_build,
        "band": ctx.band, "dx": ctx.dx, "dy": ctx.dy, "dp_score": best_score,
        "reframed_shots": len({shot for shot, _d in ctx.reframed}),
        "excursions": len(ctx.excursions),
    }
    return AlignmentMap(n1, n2, tuple(built), _runs(~used), uncovered,
                        _expected_cuts(metadata, reference, built, uncovered, n1), stats)


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
            raise AlignmentError(translate_text("Jonction {lo}–{hi} hors des segments.", lo=lo, hi=hi), ((lo, hi),))
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
        raise AlignmentError(translate_text("Aucune image de Film 2 comparable."))
    joined1, joined2 = _concat(parts1), _concat(parts2)
    margin_y, margin_x = cell_margins(film1, joined2)
    band = band1.intersect(active_band(joined2), margin_y, margin_x)
    if band.empty:
        raise AlignmentError(translate_text("Zones actives des deux films disjointes : cadrages incompatibles."))
    dx, dy, score, agreement = best_shift(joined1, joined2, band, groups)
    say(translate_text(
        "Cadrage : bande commune {top}…{bottom} (largeur), translation de Film 2 {dx}/{dy}, corrélation {score}, "
        "cohérence {agreement} sur {pairs} paires.", top=f"{band.top:+.3f}", bottom=f"{band.bottom:+.3f}",
        dx=f"{dx:+.3f}", dy=f"{dy:+.3f}", score=f"{score:.2f}", agreement=f"{agreement:.0%}", pairs=len(groups)))
    if score < GEOMETRY_CORRELATION:
        raise AlignmentError(translate_text(
            "Cadrages incompatibles (corrélation {score} sur la bande commune).", score=f"{score:.2f}"))
    if len(groups) >= 3 and agreement < GEOMETRY_AGREEMENT:
        say(translate_text("Cadrage de Film 2 variable d'un plan à l'autre (cohérence {agreement}) : chaque plan "
                           "est vérifié sur son propre cadrage.", agreement=f"{agreement:.0%}"))
    return _Context(film1, film2, band, dx, dy, hooks, area1=band1, area2=active_band(joined2), cuts1=cuts1)


def _bounded(raw: list[list[int]], n2: int) -> list[Segment]:
    built: list[Segment] = []
    for a, b, d in raw:
        a = max(a, -d, 0)
        b = min(b, n2 - d)
        if b > a:
            built.append(Segment(a, b - a, a + d))
    if not built:
        raise AlignmentError(translate_text("Aucune image de Film 1 ne correspond à Film 2."))
    return built


def _frame_refine(ctx: _Context, built: list[Segment], n2: int, say: Callable[[str], None]) -> list[Segment]:
    """Passe complète : écarts locaux trouvés image par image découpés en sous-segments, jonctions affinées."""
    for _round in range(8):
        changed = False
        out: list[Segment] = []
        ctx.excursions = []
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
        say(translate_text("Passe image par image : {count} segments après découpe des écarts locaux.",
                           count=len(built)))
    raise AlignmentError(translate_text("Passe image par image : correspondance instable."))


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
            raise AlignmentError(translate_text(
                "Correspondance non monotone entre les images {film1_start}–{film1_end} de Film 1 (Film 2 relu à "
                "rebours).", film1_start=before.film1_start, film1_end=after.film1_end),
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
