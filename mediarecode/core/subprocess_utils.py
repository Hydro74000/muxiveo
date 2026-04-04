"""
Helpers de décodage texte pour les outils externes.

Sous Windows, plusieurs outils émettent une sortie UTF-8, mais
`subprocess.run(..., text=True)` la décode sinon avec la page de code locale
du système. On force donc l'UTF-8 pour éviter le mojibake du type `FranÃ§ais`.
"""

from __future__ import annotations

import sys
from typing import Any


_TOOL_TEXT_ENCODING = "utf-8"
_TOOL_TEXT_ERRORS = "replace"


def subprocess_text_kwargs() -> dict[str, Any]:
    """
    Retourne les kwargs à injecter dans subprocess.run/Popen pour lire du texte.

    Sous Windows, on force l'UTF-8 ; ailleurs, on conserve le comportement
    standard de Python pour limiter le périmètre du changement.
    """
    kwargs: dict[str, Any] = {"text": True}
    if sys.platform == "win32":
        kwargs["encoding"] = _TOOL_TEXT_ENCODING
        kwargs["errors"] = _TOOL_TEXT_ERRORS
    return kwargs


def decode_subprocess_output(raw: bytes) -> str:
    """Décode un buffer brut provenant d'un outil externe."""
    return raw.decode(_TOOL_TEXT_ENCODING, errors=_TOOL_TEXT_ERRORS)
