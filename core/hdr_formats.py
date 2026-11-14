r"""Formats texte du HDR10 statique (MDCV x265, MaxCLL/MaxFALL) : une seule définition.

Partagés par la validation de l'encodage (``planning/validation.py``) et par la lecture stricte des
requêtes (``core.service.schemas.encode``), qui accepte aussi le texte vide (valeurs de la source).

Alphabet : chiffres ASCII ``[0-9]`` et espaces ``[ \t]`` autour de la valeur — même sens en Python (``re``) et
en ECMA-262 (``pattern`` du schéma JSON publié), contrairement à ``\d`` / ``\s`` (Unicode en Python).
"""

from __future__ import annotations

#: ``G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)`` (unités x265).
MASTER_DISPLAY_BODY = r"G\([0-9]+,[0-9]+\)B\([0-9]+,[0-9]+\)R\([0-9]+,[0-9]+\)WP\([0-9]+,[0-9]+\)L\([0-9]+,[0-9]+\)"
#: ``MaxCLL,MaxFALL`` en nits.
MAX_CLL_BODY = r"[0-9]+,[0-9]+"


def optional_text_pattern(body: str) -> str:
    """Motif complet d'un champ facultatif : vide, ou ``body`` entouré d'espaces ou tabulations éventuels."""
    return rf"^[ \t]*(?:{body})?[ \t]*$"
