"""État FEL d'exécution, séparé des profils sauvegardés."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.fel.engine import FelSource


@dataclass(frozen=True)
class FelExecution:
    source: FelSource
    original_rpu: Path
    # Valeurs à restaurer intégralement lors d'une reprise BL.
    original_max_cll: str
    original_light_source: str
    # Position chroma du BL : la conversion 4:2:0 des images reconstruites la reprend.
    chroma_location: str = "left"
    # Capacité réellement sondée dans le worker de préparation (FFmpeg 7.1+).
    chroma_setparams: bool = False
