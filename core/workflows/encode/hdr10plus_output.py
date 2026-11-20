"""HDR10+ d'une piste de sortie : copié de la source ou généré depuis le Dolby Vision (deux prédicats).

Toute garde qui décide « la sortie porte du HDR10+ » (routage, transfert PQ, réserves de stockage, contrôles)
utilise :func:`outputs_hdr10plus` ; toute extraction ``hdr10plus_tool extract`` de la source utilise
:func:`hdr10plus_from_source` (en génération, le JSON préparé ``generated_hdr10plus_json`` la remplace).
"""
from __future__ import annotations


def generates_hdr10plus(video: object) -> bool:
    """HDR10+ généré depuis le Dolby Vision (réglage effectif de la piste)."""
    return bool(getattr(video, "generate_hdr10plus", False))


def outputs_hdr10plus(video: object) -> bool:
    """La sortie porte du HDR10+ : copié de la source ou généré."""
    return bool(getattr(video, "copy_hdr10plus", False)) or generates_hdr10plus(video)


def hdr10plus_from_source(video: object) -> bool:
    """HDR10+ extrait de la source (copie seule, jamais en génération)."""
    return bool(getattr(video, "copy_hdr10plus", False)) and not generates_hdr10plus(video)


def outputs_dynamic_hdr(video: object) -> bool:
    """Métadonnées dynamiques en sortie : Dolby Vision copié, HDR10+ copié ou généré."""
    return bool(getattr(video, "copy_dv", False)) or outputs_hdr10plus(video)
