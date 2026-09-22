"""Module de détection et conversion de cadence audio/vidéo (PAL 25 FPS ↔ Cinéma 23.976/24 FPS).

Fournit :
- Détection A : par les métadonnées vidéo (master vs donneur).
- Détection B : par régression linéaire acoustique (fallback si audio pur).
- Génération des filtres de transformation (atempo préservant la tonalité vs asetrate puriste).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
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
    ATEMPO = "atempo"      # Préservation de la tonalité (moderne, recommandé par défaut)
    ASETRATE = "asetrate"  # Hauteur cinéma d'origine avec variation de pitch (puriste/historique)


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

    def __post_init__(self):
        if not self.description:
            percent = (self.speed_factor - 1.0) * 100.0
            sign = "+" if percent >= 0 else ""
            desc = f"{self.source_fps:.3f} → {self.target_fps:.3f} FPS ({sign}{percent:.1f} %)"
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


def detect_cadence_from_metadata(
    master_fps_val: str | float | None,
    donor_fps_val: str | float | None,
    tolerance: float = 0.08,
) -> CadenceMismatch | None:
    """Détection A : Identifie un écart de cadence type PAL/Cinéma via les métadonnées vidéo."""
    m_fps = parse_framerate(master_fps_val)
    d_fps = parse_framerate(donor_fps_val)
    if not m_fps or not d_fps:
        return None

    # Si les deux framerates sont très proches, aucune conversion
    if abs(m_fps - d_fps) <= tolerance:
        return None

    # 1. Donneur PAL (25 FPS) -> Master Cinéma (23.976 ou 24.0 FPS)
    if abs(d_fps - 25.0) <= 0.05:
        if abs(m_fps - 24.0) <= 0.01:
            return CadenceMismatch(
                cadence_type=CadenceType.PAL_TO_FILM_24,
                source_fps=25.0,
                target_fps=24.0,
                speed_factor=24.0 / 25.0,
                confidence=1.0,
                detection_method="metadata",
                description="PAL 25 → 24.000 FPS (+4,0 %)",
            )
        elif abs(m_fps - 23.976025) <= 0.02 or abs(m_fps - 23.976) <= 0.02:
            return CadenceMismatch(
                cadence_type=CadenceType.PAL_TO_FILM_23976,
                source_fps=25.0,
                target_fps=23.976,
                speed_factor=24000.0 / 25025.0,
                confidence=1.0,
                detection_method="metadata",
                description="PAL 25 → 23.976 FPS (+4,1 %)",
            )

    # 2. Donneur Cinéma (23.976 ou 24.0 FPS) -> Master PAL (25 FPS)
    if abs(m_fps - 25.0) <= 0.05:
        if abs(d_fps - 24.0) <= 0.01:
            return CadenceMismatch(
                cadence_type=CadenceType.FILM_24_TO_PAL,
                source_fps=24.0,
                target_fps=25.0,
                speed_factor=25.0 / 24.0,
                confidence=1.0,
                detection_method="metadata",
                description="24.000 → PAL 25 FPS (-4,0 %)",
            )
        elif abs(d_fps - 23.976025) <= 0.02 or abs(d_fps - 23.976) <= 0.02:
            return CadenceMismatch(
                cadence_type=CadenceType.FILM_23976_TO_PAL,
                source_fps=23.976,
                target_fps=25.0,
                speed_factor=25025.0 / 24000.0,
                confidence=1.0,
                detection_method="metadata",
                description="23.976 → PAL 25 FPS (-4,1 %)",
            )

    return None


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
            description="PAL 25 → 23.976 FPS (+4,1 %)",
        )

    if -0.050 <= slope <= -0.035:
        return CadenceMismatch(
            cadence_type=CadenceType.FILM_23976_TO_PAL,
            source_fps=23.976,
            target_fps=25.0,
            speed_factor=25025.0 / 24000.0,
            confidence=round(min(1.0, float(r2)), 3),
            detection_method="acoustic_slope",
            description="23.976 → PAL 25 FPS (-4,1 %)",
        )

    return None


def build_cadence_audio_filter(
    mismatch: CadenceMismatch,
    method: CadenceAudioMethod | str = CadenceAudioMethod.ATEMPO,
    sample_rate: int = 48000,
) -> str:
    """Génère la chaîne de filtres FFmpeg adaptée selon la méthode choisie (atempo vs asetrate)."""
    method_str = method.value if isinstance(method, CadenceAudioMethod) else str(method).lower()
    speed = mismatch.speed_factor

    if method_str == "asetrate":
        # Modification de cadence avec pitch shift proportionnel
        target_rate = round(sample_rate * speed)
        return f"asetrate={target_rate},aresample={sample_rate}"

    # Par défaut : atempo (préservation de la hauteur tonale)
    # Si le speed factor correspond exactement aux standards cinéma/PAL, utiliser la fraction exacte
    if mismatch.cadence_type == CadenceType.PAL_TO_FILM_23976:
        return "atempo=24000/25025"
    elif mismatch.cadence_type == CadenceType.PAL_TO_FILM_24:
        return "atempo=24/25"
    elif mismatch.cadence_type == CadenceType.FILM_23976_TO_PAL:
        return "atempo=25025/24000"
    elif mismatch.cadence_type == CadenceType.FILM_24_TO_PAL:
        return "atempo=25/24"

    return f"atempo={speed:.6f}"
