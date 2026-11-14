"""Formats texte du HDR10 statique (MDCV x265, MaxCLL/MaxFALL) : une seule définition.

Partagés par la validation de l'encodage (``planning/validation.py``) et par la lecture stricte des
requêtes (``core.service.schemas.encode``), qui accepte aussi le texte vide (valeurs de la source).
"""

from __future__ import annotations

#: ``G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)`` (unités x265).
MASTER_DISPLAY_BODY = r"G\(\d+,\d+\)B\(\d+,\d+\)R\(\d+,\d+\)WP\(\d+,\d+\)L\(\d+,\d+\)"
#: ``MaxCLL,MaxFALL`` en nits.
MAX_CLL_BODY = r"\d+,\d+"


def optional_text_pattern(body: str) -> str:
    """Motif complet d'un champ facultatif : vide, ou ``body`` entouré d'espaces éventuels."""
    return rf"^\s*(?:{body})?\s*$"
