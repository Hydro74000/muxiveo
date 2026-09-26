"""Recherche de plateaux et de jonctions sur des enveloppes audio à 1 kHz.

Les coordonnées internes sont celles du donneur après conversion de cadence.
Une jonction doit être justifiée par les correspondances de ses deux côtés.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.workflows.audio_sync import AudioSyncError
from core.workflows.sync_calibration import SyncSegment


@dataclass(frozen=True)
class Anchor:
    donor_ms: int
    shift_ms: int
    confidence: float


def match_window(reference, donor, start: int, size: int, max_offset: int) -> Anchor:
    """Cherche une courte fenêtre de référence dans une plage donneur élargie.

    Le recouvrement est complet à chaque lag : une fenêtre de 8 s peut ainsi
    mesurer un décalage de 30 s sans raccourcir la comparaison à quelques samples.
    """
    import numpy as np

    left = max(0, start - max_offset)
    right = min(len(donor), start + size + max_offset)
    a = np.asarray(reference[start:start + size], dtype=float)
    b = np.asarray(donor[left:right], dtype=float)
    if len(a) < size or len(b) < size or size < 500:
        raise AudioSyncError("Fenêtre audio insuffisante.")
    a = a - a.mean()
    power = float(a @ a)
    if power < 1e-12:
        raise AudioSyncError("Fenêtre audio silencieuse.")
    nfft = 1 << (len(a) + len(b) - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(b, nfft) * np.fft.rfft(a[::-1], nfft), nfft)
    corr = corr[size - 1:len(b)]
    sums = np.r_[0.0, np.cumsum(b)]
    squares = np.r_[0.0, np.cumsum(b * b)]
    variance = squares[size:] - squares[:-size] - (sums[size:] - sums[:-size]) ** 2 / size
    # Les sommes cumulées peuvent donner une variance légèrement négative
    # dans le silence. Diviser le bruit FFT par epsilon créerait de faux pics
    # de confiance > 1 : exclure ces fenêtres, au lieu de les normaliser.
    valid = variance > max(1e-12, float(variance.max()) * 1e-8)
    scores = np.divide(corr, np.sqrt(power * np.maximum(variance, 1e-20)),
                       out=np.full_like(corr, -1.0), where=valid)
    best = int(np.argmax(scores))
    outside = scores[np.abs(np.arange(len(scores)) - best) > 40]
    confidence = float(scores[best])
    margin = confidence - float(outside.max()) if outside.size else confidence
    shift = start - left - best
    if not np.isfinite(confidence) or confidence < 0.55 or margin < 0.04 or abs(shift) >= max_offset:
        raise AudioSyncError("Corrélation insuffisante ou ambiguë ; calibration manuelle requise.")
    return Anchor(left + best + size // 2, shift, min(1.0, confidence))


def _local_agreement(a, b, width=500):
    """Corrélation locale centrée ; le silence n'apporte aucune preuve."""
    import numpy as np

    def rolling(values):
        sums = np.r_[0.0, np.cumsum(values, dtype=float)]
        return sums[width:] - sums[:-width]

    sa, sb = rolling(a), rolling(b)
    va = rolling(a * a) - sa * sa / width
    vb = rolling(b * b) - sb * sb / width
    cov = rolling(a * b) - sa * sb / width
    denominator = np.sqrt(np.maximum(va, 0) * np.maximum(vb, 0))
    return np.divide(cov, denominator, out=np.zeros_like(cov), where=denominator > 1e-9)


def locate_transition(reference, donor, left: Anchor, right: Anchor) -> tuple[int, float]:
    """Choisit la jonction qui maximise les correspondances avant/après.

    Aucun milieu d'intervalle n'est utilisé en cas d'échec. Les correspondances
    locales doivent confirmer les deux plateaux et borner la zone de jonction.
    """
    import numpy as np

    low = max(0, left.donor_ms - 4000, -left.shift_ms, -right.shift_ms)
    high = min(len(donor), right.donor_ms + 4000,
               len(reference) - left.shift_ms, len(reference) - right.shift_ms)
    if high - low < 1000:
        raise AudioSyncError("Jonctions trop proches pour une validation acoustique.")
    d = np.asarray(donor[low:high], dtype=float)
    before = _local_agreement(d, np.asarray(reference[low + left.shift_ms:high + left.shift_ms], dtype=float))
    after = _local_agreement(d, np.asarray(reference[low + right.shift_ms:high + right.shift_ms], dtype=float))
    # Une fenêtre courte précise la jonction ; la fenêtre longue ci-dessus
    # sert uniquement à la validation. Normaliser localement évite qu'un
    # changement de volume décide à lui seul de l'emplacement de la coupe.
    fine_before = np.maximum(_local_agreement(
        d, np.asarray(reference[low + left.shift_ms:high + left.shift_ms], dtype=float), 50), 0)
    fine_after = np.maximum(_local_agreement(
        d, np.asarray(reference[low + right.shift_ms:high + right.shift_ms], dtype=float), 50), 0)
    prior_score = np.r_[0.0, np.cumsum(fine_before)]
    next_score = np.r_[0.0, np.cumsum(fine_after)]
    removed = max(0, left.shift_ms - right.shift_ms)
    indices = np.arange(750, len(fine_before) - removed - 750)
    if not len(indices):
        raise AudioSyncError("Jonctions trop proches pour une validation acoustique.")
    scores = prior_score[indices] - next_score[indices + removed]
    best = int(indices[np.argmax(scores)])
    tied = indices[scores >= scores.max() - 0.1]
    near = tied[np.abs(tied - best) <= 500]
    cut_sample = int((near[0] + near[-1]) // 2) + 25
    cut_index = cut_sample - 250
    if cut_index < 500 or cut_index > len(before) - 500:
        raise AudioSyncError("Jonction non bornée par des correspondances acoustiques.")
    prior = before[max(0, cut_index - 2500):cut_index - 250]
    following = after[cut_index + removed + 250:min(len(after), cut_index + removed + 2500)]
    prior_conf = float(np.quantile(prior, 0.75)) if len(prior) else 0.0
    following_conf = float(np.quantile(following, 0.75)) if len(following) else 0.0
    if min(prior_conf, following_conf) < 0.6:
        raise AudioSyncError(f"Jonction vers {(low + cut_sample) / 1000:.3f} s non vérifiée des deux côtés ({prior_conf:.2f}, {following_conf:.2f}) ; calibration manuelle requise.")
    return low + cut_index + 250, min(prior_conf, following_conf, left.confidence, right.confidence)


def scan_envelopes(reference, donor, *, max_offset_ms=30000, tolerance_ms=25,
                   speed_factor=1.0, check_cancelled=lambda: None, log=lambda message: None):
    """Sonde toute la piste, y compris le début et les plateaux intermédiaires."""
    size = min(8000, min(len(reference), len(donor)) // 3)
    if size < 1000:
        raise AudioSyncError("Piste trop courte pour une calibration multi-segments.")
    step = size // 2
    stop = len(reference) - size
    positions = sorted(set([*range(0, stop + 1, step), stop]))
    anchors = []
    for start in positions:
        check_cancelled()
        try:
            anchor = match_window(reference, donor, start, size, max_offset_ms)
        except AudioSyncError:
            continue
        # Une fenêtre chevauchant une jonction peut reculer dans le donneur.
        if anchors and anchor.donor_ms <= anchors[-1].donor_ms:
            continue
        anchors.append(anchor)
    if not anchors:
        raise AudioSyncError("Aucune correspondance acoustique fiable.")
    first_reference_start = anchors[0].donor_ms + anchors[0].shift_ms - size // 2
    if first_reference_start > max(0, anchors[0].shift_ms) + size:
        raise AudioSyncError("Début de piste non vérifié ; calibration manuelle requise.")
    segments = [SyncSegment(0, anchors[0].shift_ms)]
    confidence = anchors[0].confidence
    for left, right in zip(anchors, anchors[1:]):
        check_cancelled()
        if right.donor_ms - left.donor_ms > max(30000, size * 3):
            raise AudioSyncError(f"Intervalle audio non vérifié ({left.donor_ms / 1000:.1f}–{right.donor_ms / 1000:.1f} s) ; calibration manuelle requise.")
        if abs(right.shift_ms - segments[-1].shift_ms) <= tolerance_ms:
            confidence = min(confidence, right.confidence)
            continue
        cut, cut_confidence = locate_transition(reference, donor, left, right)
        if cut * speed_factor <= segments[-1].start_ms:
            raise AudioSyncError("Ordre des jonctions incohérent.")
        segments.append(SyncSegment(cut * speed_factor, right.shift_ms))
        confidence = min(confidence, cut_confidence)
        log(f"Jonction vérifiée à {cut * speed_factor / 1000:.3f} s : {right.shift_ms:+.0f} ms ({cut_confidence:.2f})")
    # Une queue différente (générique/localisation) ne doit pas cacher une zone
    # non mesurée au milieu de l'épisode ; elle est signalée explicitement.
    covered = anchors[-1].donor_ms + size // 2
    if len(donor) - covered > size + max_offset_ms:
        log(f"Fin donneur sans correspondance : {(len(donor) - covered) / 1000:.1f} s conservées.")
    samples = tuple(dict(start_ms=a.donor_ms * speed_factor, shift_ms=a.shift_ms,
                         confidence=a.confidence) for a in anchors)
    return tuple(segments), float(confidence), samples
