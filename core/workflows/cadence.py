"""Module de détection et conversion de cadence audio/vidéo (PAL 25 FPS <-> Cinéma 23.976/24 FPS).

Fournit :
- Détection A : par les métadonnées vidéo (master vs donneur).
- Détection B : par régression linéaire acoustique (fallback si audio pur).
- Génération des filtres de transformation (atempo préservant la tonalité vs asetrate puriste).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from fractions import Fraction
import math
import re
from typing import Any


class CadenceType(str, Enum):
    NONE = "none"
    PAL_TO_FILM_23976 = "pal_to_film_23976"  # PAL 25.000 -> Film 23.976 (ralentit de ~4.1 %)
    PAL_TO_FILM_24 = "pal_to_film_24"        # PAL 25.000 -> Film 24.000 (ralentit de ~4.0 %)
    FILM_23976_TO_PAL = "film_23976_to_pal"  # Film 23.976 -> PAL 25.000 (accélère de ~4.27 %)
    FILM_24_TO_PAL = "film_24_to_pal"        # Film 24.000 -> PAL 25.000 (accélère de ~4.17 %)
    CUSTOM = "custom"


class CadenceAudioMethod(str, Enum):
    AUTO = "auto"          # Détection automatique par analyse de pitch et spectrale
    ATEMPO = "atempo"      # Préservation de la tonalité (time-stretching)
    ASETRATE = "asetrate"  # Hauteur cinéma d'origine avec variation de pitch (ré-échantillonnage)


@dataclass(frozen=True)
class CadenceMismatch:
    """Description d'une différence de cadence détectée entre deux sources."""
    cadence_type: CadenceType
    source_fps: float
    target_fps: float
    speed_factor: float          # Facteur multiplicateur de vitesse (ex: 24000/25025 ≈ 0.95904)
    confidence: float = 1.0      # Niveau de confiance (0.0 à 1.0)
    detection_method: str = "metadata"  # "metadata" ou "acoustic_slope"
    description: str = ""
    speed_ratio: str = ""        # Fraction exacte déduite à la volée des sources vidéo (ex: "24000/25025", "24/25")

    def __post_init__(self):
        if not self.description:
            percent = (self.speed_factor - 1.0) * 100.0
            sign = "+" if percent >= 0 else ""
            desc = f"{self.source_fps:.3f} -> {self.target_fps:.3f} FPS ({sign}{percent:.1f} %)"
            object.__setattr__(self, "description", desc)

    @property
    def time_stretch_ratio(self) -> float:
        """Ratio d'allongement temporel de la durée (1.0 / speed_factor)."""
        if self.speed_factor <= 0:
            return 1.0
        return 1.0 / self.speed_factor

    def to_dict(self) -> dict[str, Any]:
        return {
            "cadence_type": self.cadence_type.value,
            "source_fps": self.source_fps,
            "target_fps": self.target_fps,
            "speed_factor": self.speed_factor,
            "confidence": self.confidence,
            "detection_method": self.detection_method,
            "description": self.description,
            "speed_ratio": self.speed_ratio,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CadenceMismatch:
        return cls(
            cadence_type=CadenceType(payload.get("cadence_type", "none")),
            source_fps=float(payload.get("source_fps", 0.0)),
            target_fps=float(payload.get("target_fps", 0.0)),
            speed_factor=float(payload.get("speed_factor", 1.0)),
            confidence=float(payload.get("confidence", 1.0)),
            detection_method=str(payload.get("detection_method", "metadata")),
            description=str(payload.get("description", "")),
            speed_ratio=str(payload.get("speed_ratio", "")),
        )


def parse_framerate(value: str | float | int | None) -> float | None:
    """Convertit une chaîne ou fraction de framerate (ex: '24000/1001', '23.976025', '25/1') en float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None

    s = str(value).strip()
    s = re.sub(r"(?i)\s*fps\b", "", s).strip()
    if not s:
        return None

    if "/" in s:
        parts = s.split("/", 1)
        try:
            num = float(parts[0].strip())
            den = float(parts[1].strip())
            if den > 0 and num > 0:
                return num / den
            return None
        except (ValueError, ZeroDivisionError):
            return None

    try:
        val = float(s)
        return val if val > 0 else None
    except ValueError:
        return None


def parse_framerate_fraction(value: str | float | int | Fraction | None) -> Fraction | None:
    """Convertit une chaîne, fraction ou flottant en Fraction exacte (ex: '24000/1001', '25/1', 23.976)."""
    if value is None:
        return None
    if isinstance(value, Fraction):
        return value if value > 0 else None
    if isinstance(value, int):
        return Fraction(value, 1) if value > 0 else None
    if isinstance(value, float):
        if value <= 0:
            return None
        # Reconnaissance des cadences NTSC / Cinéma standard lorsque exprimées en float
        if abs(value - 23.976023976) < 0.002 or abs(value - 23.976) < 0.002:
            return Fraction(24000, 1001)
        if abs(value - 29.97002997) < 0.002 or abs(value - 29.97) < 0.002:
            return Fraction(30000, 1001)
        if abs(value - 59.94005994) < 0.002 or abs(value - 59.94) < 0.002:
            return Fraction(60000, 1001)
        if abs(value - 24.0) < 0.002:
            return Fraction(24, 1)
        if abs(value - 25.0) < 0.002:
            return Fraction(25, 1)
        return Fraction(value).limit_denominator(1001)

    s = str(value).strip()
    s = re.sub(r"(?i)\s*fps\b", "", s).strip()
    if not s:
        return None

    if "/" in s:
        parts = s.split("/", 1)
        try:
            num = int(float(parts[0].strip()))
            den = int(float(parts[1].strip()))
            if den > 0 and num > 0:
                return Fraction(num, den)
            return None
        except (ValueError, ZeroDivisionError):
            return None

    try:
        val = float(s)
        return parse_framerate_fraction(val)
    except ValueError:
        return None


def detect_cadence_from_metadata(
    master_fps_val: str | float | Fraction | None,
    donor_fps_val: str | float | Fraction | None,
    tolerance: float = 0.01,
) -> CadenceMismatch | None:
    """Détection A : Calcule à la volée le ratio exact entre les cadences vidéo master et donneur."""
    m_frac = parse_framerate_fraction(master_fps_val)
    d_frac = parse_framerate_fraction(donor_fps_val)
    if not m_frac or not d_frac:
        return None

    m_fps = float(m_frac)
    d_fps = float(d_frac)

    # Si les deux framerates sont très proches, aucune conversion
    if abs(m_fps - d_fps) <= tolerance:
        return None

    # Ratio exact calculé directement à la volée depuis les cadences des sources (target / source) :
    speed_ratio = m_frac / d_frac
    speed_factor = float(speed_ratio)

    # Catégorisation pour l'enum et l'UX
    cadence_type = CadenceType.CUSTOM
    if abs(d_fps - 25.0) <= 0.05:
        if abs(m_fps - 24.0) <= 0.01:
            cadence_type = CadenceType.PAL_TO_FILM_24
        elif abs(m_fps - 23.976025) <= 0.02 or abs(m_fps - 23.976) <= 0.02:
            cadence_type = CadenceType.PAL_TO_FILM_23976
    elif abs(m_fps - 25.0) <= 0.05:
        if abs(d_fps - 24.0) <= 0.01:
            cadence_type = CadenceType.FILM_24_TO_PAL
        elif abs(d_fps - 23.976025) <= 0.02 or abs(d_fps - 23.976) <= 0.02:
            cadence_type = CadenceType.FILM_23976_TO_PAL

    if cadence_type == CadenceType.PAL_TO_FILM_23976:
        description = "PAL 25 -> 23.976 FPS (+4,1 %)"
    elif cadence_type == CadenceType.PAL_TO_FILM_24:
        description = "PAL 25 -> 24.000 FPS (+4,0 %)"
    elif cadence_type == CadenceType.FILM_23976_TO_PAL:
        description = "23.976 -> PAL 25 FPS (-4,1 %)"
    elif cadence_type == CadenceType.FILM_24_TO_PAL:
        description = "24.000 -> PAL 25 FPS (-4,0 %)"
    else:
        stretch_pct = (1.0 / speed_factor - 1.0) * 100.0
        sign = "+" if stretch_pct >= 0 else ""
        desc_from = f"{d_fps:.3f}"
        desc_to = f"{m_fps:.3f}"
        description = f"{desc_from} -> {desc_to} FPS ({sign}{stretch_pct:.1f} %)"

    # Notation de fraction exacte pour atempo
    if speed_ratio == Fraction(24000, 25025):
        ratio_str = "24000/25025"
    elif speed_ratio == Fraction(25025, 24000):
        ratio_str = "25025/24000"
    elif speed_ratio == Fraction(24, 25):
        ratio_str = "24/25"
    elif speed_ratio == Fraction(25, 24):
        ratio_str = "25/24"
    else:
        ratio_str = f"{speed_ratio.numerator}/{speed_ratio.denominator}"

    return CadenceMismatch(
        cadence_type=cadence_type,
        source_fps=round(d_fps, 3),
        target_fps=round(m_fps, 3),
        speed_factor=speed_factor,
        confidence=1.0,
        detection_method="metadata",
        description=description,
        speed_ratio=ratio_str,
    )


def detect_cadence_from_acoustic_samples(
    samples: list[dict] | tuple[dict, ...],
    min_span_ms: float = 120_000.0,
    r2_threshold: float = 0.93,
) -> CadenceMismatch | None:
    """Détection B (fallback) : Calcule la pente linéaire des décalages acoustiques pour identifier une dérive PAL."""
    import numpy as np

    valid = [s for s in samples if s.get("confidence", 0) >= 0.25]
    if len(valid) < 3:
        return None

    t = np.array([float(s["start_ms"]) for s in valid])
    y = np.array([float(s["shift_ms"]) for s in valid])

    span = float(np.max(t) - np.min(t))
    if span < min_span_ms:
        return None

    # Régression linéaire y = slope * t + intercept
    denom = np.sum((t - np.mean(t)) ** 2)
    if denom <= 0:
        return None

    slope = float(np.sum((t - np.mean(t)) * (y - np.mean(y))) / denom)
    intercept = float(np.mean(y) - slope * np.mean(t))

    y_pred = slope * t + intercept
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    if r2 < r2_threshold:
        return None

    # Pente théorique PAL 25 -> 23.976 : ~ +0.0427 (ou +0.0417 pour 24)
    # Pente inverse 23.976 -> PAL 25 : ~ -0.041
    if 0.035 <= slope <= 0.050:
        return CadenceMismatch(
            cadence_type=CadenceType.PAL_TO_FILM_23976,
            source_fps=25.0,
            target_fps=23.976,
            speed_factor=24000.0 / 25025.0,
            confidence=round(min(1.0, float(r2)), 3),
            detection_method="acoustic_slope",
            description="PAL 25 -> 23.976 FPS (+4,1 %)",
            speed_ratio="24000/25025",
        )

    if -0.050 <= slope <= -0.035:
        return CadenceMismatch(
            cadence_type=CadenceType.FILM_23976_TO_PAL,
            source_fps=23.976,
            target_fps=25.0,
            speed_factor=25025.0 / 24000.0,
            confidence=round(min(1.0, float(r2)), 3),
            detection_method="acoustic_slope",
            description="23.976 -> PAL 25 FPS (-4,1 %)",
            speed_ratio="25025/24000",
        )

    return None


def build_cadence_audio_filter(
    mismatch: CadenceMismatch,
    method: CadenceAudioMethod | str = CadenceAudioMethod.AUTO,
    sample_rate: int = 48000,
) -> str:
    """Génère la chaîne de filtres FFmpeg adaptée selon la méthode choisie (atempo vs asetrate vs auto)."""
    method_str = method.value if isinstance(method, CadenceAudioMethod) else str(method).lower()
    speed = mismatch.speed_factor

    if method_str == "asetrate":
        # Modification de cadence avec pitch shift proportionnel
        target_rate = round(sample_rate * speed)
        return f"asetrate={target_rate},aresample={sample_rate}"

    # Par défaut : atempo (préservation de la hauteur tonale)
    # 1. Utiliser en priorité la fraction exacte calculée à la volée depuis les flux vidéo
    if mismatch.speed_ratio:
        return f"atempo={mismatch.speed_ratio}"

    # 2. Fractions standards de référence
    if mismatch.cadence_type == CadenceType.PAL_TO_FILM_23976:
        return "atempo=24000/25025"
    elif mismatch.cadence_type == CadenceType.PAL_TO_FILM_24:
        return "atempo=24/25"
    elif mismatch.cadence_type == CadenceType.FILM_23976_TO_PAL:
        return "atempo=25025/24000"
    elif mismatch.cadence_type == CadenceType.FILM_24_TO_PAL:
        return "atempo=25/24"

    return f"atempo={speed:.6f}"
