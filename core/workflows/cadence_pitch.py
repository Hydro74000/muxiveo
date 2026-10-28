"""Module d'analyse acoustique de hauteur tonale (Pitch / Hz) pour la conversion de cadence.

Permet de discriminer automatiquement entre :
- 'asetrate' : PAL Speedup historique non compensé (la chaîne TV a accéléré l'audio
  de +4.1%, la hauteur est montée de +0.707 demi-ton / +72 cents). Le ré-échantillonnage
  linéaire rétablit la vitesse ET la hauteur tonale naturelle d'origine, sans artefacts.
- 'atempo' : PAL Speedup compensé en studio (la hauteur originale avait déjà été
  corrigée lors de la diffusion). Le time-stretching préserve la hauteur tonale existante.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from numpy import ndarray

try:
    import numpy as _numpy
except ImportError:
    np: ModuleType | None = None
else:
    np = _numpy

from core.workflows.cadence import CadenceMismatch


@dataclass(frozen=True)
class CadencePitchAnalysis:
    """Résultat de l'analyse acoustique de hauteur tonale."""
    selected_method: str                       # 'asetrate' ou 'atempo'
    pitch_shift_semitones: float               # Décalage estimé en demi-tons par rapport à la référence
    spectral_corr_asetrate: float              # Corrélation spectrale Welch (Ref vs Asetrate)
    spectral_corr_atempo: float                # Corrélation spectrale Welch (Ref vs Atempo)
    f0_ref_hz: float                           # Fréquence fondamentale médiane mesurée sur la référence (Hz)
    f0_donor_hz: float                         # Fréquence fondamentale médiane mesurée sur le donneur (Hz)
    confidence: float = 1.0                    # Indice de confiance de la décision (0.0 à 1.0)
    details: str = ""                          # Explication détaillée en français

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> CadencePitchAnalysis | None:
        if not payload or not isinstance(payload, dict):
            return None
        try:
            return cls(
                selected_method=str(payload.get("selected_method", "atempo")),
                pitch_shift_semitones=float(payload.get("pitch_shift_semitones", 0.0)),
                spectral_corr_asetrate=float(payload.get("spectral_corr_asetrate", 0.0)),
                spectral_corr_atempo=float(payload.get("spectral_corr_atempo", 0.0)),
                f0_ref_hz=float(payload.get("f0_ref_hz", 0.0)),
                f0_donor_hz=float(payload.get("f0_donor_hz", 0.0)),
                confidence=float(payload.get("confidence", 1.0)),
                details=str(payload.get("details", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def compute_welch_psd(x: ndarray, sr: int, nperseg: int = 2048) -> ndarray:
    """Densité spectrale de puissance moyenne (Welch) en pur NumPy."""
    if np is None:
        raise ImportError("numpy est requis pour l'analyse spectrale Welch.")
    if len(x) < 256:
        return np.array([], dtype=float)
    seg_len = min(len(x), nperseg)
    hop = max(1, seg_len // 2)
    window = np.hanning(seg_len)
    win_sq_sum = np.sum(window**2)
    if win_sq_sum <= 0:
        return np.array([], dtype=float)

    segments = [x[i : i + seg_len] * window for i in range(0, len(x) - seg_len + 1, hop)]
    if not segments:
        return np.array([], dtype=float)

    ffts = [np.abs(np.fft.rfft(seg)) ** 2 / (sr * win_sq_sum) for seg in segments]
    return np.mean(ffts, axis=0)


def extract_f0_candidates(
    x: ndarray,
    sr: int,
    frame_size: int = 1024,
    hop_size: int = 512,
    min_f0: float = 75.0,
    max_f0: float = 400.0,
) -> list[float]:
    """Extrait les fréquences fondamentales candidates F0 par autocorrélation temporelle."""
    if np is None:
        raise ImportError("numpy est requis pour l'extraction de fréquence fondamentale.")
    if len(x) < frame_size:
        return []
    min_lag = max(1, int(sr / max_f0))
    max_lag = min(frame_size - 1, int(sr / min_f0))
    if min_lag >= max_lag:
        return []

    results: list[float] = []
    window = np.hanning(frame_size)
    for i in range(0, len(x) - frame_size + 1, hop_size):
        frame = x[i : i + frame_size]
        if np.max(np.abs(frame)) < 0.02:
            continue
        w = frame * window
        r = np.fft.irfft(np.abs(np.fft.rfft(w, 2 * frame_size)) ** 2)
        r0 = r[0]
        if r0 <= 1e-12:
            continue
        r_search = r[min_lag:max_lag]
        best_idx = int(np.argmax(r_search))
        peak_val = float(r_search[best_idx])
        if peak_val / r0 > 0.35:
            lag = min_lag + best_idx
            results.append(float(sr / lag))
    return results


# Transposition lue sur le spectre en échelle logarithmique : fiable entre deux mixages
# du même son (corrélation ≥ 0,95), pas entre deux doublages (dialogues différents).
_SPECTRAL_SHIFT_MIN_SCORE = 0.95
_SPECTRAL_SHIFT_TOLERANCE_CENTS = 25.0


def spectral_pitch_shift_cents(
    reference: ndarray,
    donor: ndarray,
    sr: int,
    max_cents: int = 200,
) -> tuple[float, float]:
    """Hauteur du donneur face à la référence (cents, positif = plus aigu) et corrélation des spectres.

    Spectres moyens rééchantillonnés au cent entre 150 Hz et 3,8 kHz : une transposition
    y devient une translation, cherchée à ±`max_cents`.
    """
    if np is None:
        raise ImportError("numpy est requis pour l'analyse spectrale.")
    size = 8192
    if min(len(reference), len(donor)) < size:
        return 0.0, 0.0
    window = np.hanning(size)
    grid = 150.0 * 2 ** (np.arange(int(1200 * math.log2(3800 / 150))) / 1200)
    freqs = np.fft.rfftfreq(size, 1 / sr)

    def log_spectrum(values: ndarray) -> ndarray:
        frames = [values[i:i + size] * window for i in range(0, len(values) - size + 1, size // 4)]
        power = np.mean([np.abs(np.fft.rfft(frame)) ** 2 for frame in frames], axis=0)
        spectrum = np.log(np.interp(grid, freqs, power) + 1e-12)
        return spectrum - spectrum.mean()

    ref_spec, donor_spec = log_spectrum(reference), log_spectrum(donor)
    span = len(grid) - 2 * max_cents
    target = donor_spec[max_cents:max_cents + span]
    scores = []
    for lag in range(-max_cents, max_cents + 1):
        shifted = ref_spec[max_cents + lag:max_cents + lag + span]
        scores.append(float(np.corrcoef(shifted, target)[0, 1]) if shifted.std() > 0 and target.std() > 0 else 0.0)
    best = int(np.argmax(scores))
    # Le donneur reproduit à f·2^(c/1200) ce que la référence a à f : décalage de +c cents.
    return float(max_cents - best), float(scores[best])


def analyze_cadence_pitch(
    ref_samples: ndarray,
    donor_ase_samples: ndarray,
    donor_ate_samples: ndarray,
    sr: int = 16000,
    mismatch: CadenceMismatch | None = None,
) -> CadencePitchAnalysis:
    """Analyse acoustique comparant les hypothèses 'asetrate' et 'atempo' contre la référence."""
    if np is None:
        raise ImportError("numpy est requis pour l'analyse acoustique de pitch.")
    n = min(len(ref_samples), len(donor_ase_samples), len(donor_ate_samples))
    if n < sr:  # moins de 1 seconde d'audio
        return CadencePitchAnalysis(
            selected_method="atempo",
            pitch_shift_semitones=0.0,
            spectral_corr_asetrate=0.0,
            spectral_corr_atempo=0.0,
            f0_ref_hz=0.0,
            f0_donor_hz=0.0,
            confidence=0.5,
            details="Échantillon audio trop court pour l'analyse spectrale (défaut 'atempo')",
        )

    ref = ref_samples[:n]
    ase = donor_ase_samples[:n]
    ate = donor_ate_samples[:n]

    # 1. Corrélation spectrale de puissance (Welch)
    psd_ref = compute_welch_psd(ref, sr)
    psd_ase = compute_welch_psd(ase, sr)
    psd_ate = compute_welch_psd(ate, sr)

    corr_ase, corr_ate = 0.0, 0.0
    k = min(len(psd_ref), len(psd_ase), len(psd_ate), 1000)
    if k > 10:
        std_ref = np.std(psd_ref[:k])
        std_ase = np.std(psd_ase[:k])
        std_ate = np.std(psd_ate[:k])
        if std_ref > 1e-12 and std_ase > 1e-12:
            corr_ase = float(np.corrcoef(psd_ref[:k], psd_ase[:k])[0, 1])
        if std_ref > 1e-12 and std_ate > 1e-12:
            corr_ate = float(np.corrcoef(psd_ref[:k], psd_ate[:k])[0, 1])

    # 2. Mesure F0 sur la voix / harmoniques
    f0_ref_list = extract_f0_candidates(ref, sr)
    f0_ase_list = extract_f0_candidates(ase, sr)
    f0_ate_list = extract_f0_candidates(ate, sr)

    f0_ref_med = float(np.median(f0_ref_list)) if f0_ref_list else 0.0
    f0_ase_med = float(np.median(f0_ase_list)) if f0_ase_list else 0.0
    f0_ate_med = float(np.median(f0_ate_list)) if f0_ate_list else 0.0

    # Calcul du décalage de pitch en demi-tons par rapport à la référence
    pitch_shift = 0.0
    if f0_ref_med > 0 and f0_ate_med > 0:
        pitch_shift = 12.0 * math.log2(f0_ate_med / f0_ref_med)

    # Décalage attendu selon la différence de cadence (ex: +0.722 demi-ton pour PAL 25 -> 23.976)
    expected_shift = 0.707
    if mismatch and mismatch.speed_factor > 0:
        expected_shift = 12.0 * math.log2(1.0 / mismatch.speed_factor)

    # Transposition mesurée sur le spectre : tranche seule quand elle est nette et proche
    # de l'une des deux hypothèses (hauteur d'origine ou montée de la conversion).
    shift_cents, shift_score = spectral_pitch_shift_cents(ref, ate, sr)
    expected_cents = expected_shift * 100.0
    spectral = (
        shift_score >= _SPECTRAL_SHIFT_MIN_SCORE
        and min(abs(shift_cents), abs(shift_cents - expected_cents)) <= _SPECTRAL_SHIFT_TOLERANCE_CENTS
    )
    if spectral:
        selected = "asetrate" if abs(shift_cents - expected_cents) < abs(shift_cents) else "atempo"
        state = (
            "PAL Speedup non compensé : la méthode 'asetrate' rétablit le pitch naturel d'origine"
            if selected == "asetrate"
            else "hauteur déjà compensée : la méthode 'atempo' la conserve"
        )
        return CadencePitchAnalysis(
            selected_method=selected,
            pitch_shift_semitones=round(shift_cents / 100.0, 3),
            spectral_corr_asetrate=round(max(0.0, corr_ase), 4),
            spectral_corr_atempo=round(max(0.0, corr_ate), 4),
            f0_ref_hz=round(f0_ref_med, 1),
            f0_donor_hz=round(f0_ate_med if f0_ate_med > 0 else f0_ase_med, 1),
            confidence=round(shift_score, 3),
            details=(
                f"Hauteur du donneur {shift_cents / 100.0:+.2f} demi-ton face à la référence "
                f"(spectre, corrélation {shift_score:.2f}) : {state}"
            ),
        )

    # Détection de l'adéquation :
    delta_corr = corr_ase - corr_ate
    if delta_corr > 0.03:
        is_ase_favored = True
    elif delta_corr < -0.03:
        is_ase_favored = False
    else:
        # Corrélations spectrales très proches : départage par la fréquence fondamentale F0
        if abs(expected_shift) > 0.2 and f0_ref_med > 0 and f0_ate_med > 0:
            err_ase = abs(f0_ase_med - f0_ref_med)
            err_ate = abs(f0_ate_med - f0_ref_med)
            is_ase_favored = (err_ase + 1.5 < err_ate) and (abs(pitch_shift - expected_shift) < 0.45)
        else:
            is_ase_favored = False

    if is_ase_favored:
        selected = "asetrate"
        conf = min(1.0, max(0.7, 0.7 + abs(delta_corr)))
        details = (
            f"PAL Speedup non compensé détecté (écart de {pitch_shift:+.2f} demi-ton) : "
            f"la méthode 'asetrate' rétablit le pitch naturel d'origine "
            f"(corrélation spectrale : {corr_ase:.3f} vs {corr_ate:.3f})"
        )
    else:
        selected = "atempo"
        conf = min(1.0, max(0.65, 0.65 + max(0.0, -delta_corr)))
        details = (
            f"Hauteur originale préservée sur le donneur (écart {pitch_shift:+.2f} demi-ton) : "
            f"la méthode 'atempo' préserve la tonalité "
            f"(corrélation spectrale : {corr_ate:.3f} vs {corr_ase:.3f})"
        )

    return CadencePitchAnalysis(
        selected_method=selected,
        pitch_shift_semitones=round(pitch_shift, 3),
        spectral_corr_asetrate=round(max(0.0, corr_ase), 4),
        spectral_corr_atempo=round(max(0.0, corr_ate), 4),
        f0_ref_hz=round(f0_ref_med, 1),
        f0_donor_hz=round(f0_ate_med if f0_ate_med > 0 else f0_ase_med, 1),
        confidence=round(conf, 3),
        details=details,
    )
