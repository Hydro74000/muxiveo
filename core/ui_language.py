"""
core/ui_language.py — Détection de la langue d'interface depuis le système.

Sans dépendance Qt : utilisable par setup.py avant l'installation de PySide6.
"""

from __future__ import annotations

import json
import locale
import os
from collections.abc import Iterable
from pathlib import Path

from core.lang_tags import Rfc5646LanguageTags


LOCALES_PATH = Path(__file__).parent.parent / "locales.json"
FALLBACK_UI_LANGUAGE = "eng"


def supported_ui_languages(locales_path: Path = LOCALES_PATH) -> frozenset[str]:
    """Retourne les codes ISO 639-2 présents dans locales.json (anglais toujours inclus)."""
    codes = {FALLBACK_UI_LANGUAGE}
    try:
        data = json.loads(locales_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset(codes)
    if isinstance(data, dict):
        for values in data.values():
            if isinstance(values, dict):
                codes.update(str(key).strip().lower() for key in values)
    return frozenset(codes)


def _windows_ui_locale_name() -> str | None:
    """Retourne la locale d'affichage Windows (ex. ``fr_FR``) via l'API Win32."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        lang_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return None
    return locale.windows_locale.get(lang_id)


def _system_locale_candidates() -> list[str | None]:
    """Liste ordonnée des noms de locale système à tester."""
    candidates: list[str | None] = [
        _windows_ui_locale_name(),
        os.environ.get("LC_ALL"),
        (os.environ.get("LANGUAGE") or "").split(":", 1)[0] or None,
        os.environ.get("LANG"),
    ]
    try:
        candidates.append(locale.getlocale()[0])
    except (TypeError, ValueError):
        pass
    return candidates


def system_ui_language(supported: Iterable[str] | None = None) -> str:
    """
    Langue UI déduite du système.

    La première langue système reconnue est retenue si locales.json la prend
    en charge ; sinon (ou si rien n'est détecté) : anglais.
    """
    allowed = (
        frozenset(code.strip().lower() for code in supported)
        if supported is not None
        else supported_ui_languages()
    )
    for candidate in _system_locale_candidates():
        code = Rfc5646LanguageTags.from_locale_name(candidate)
        if code:
            code = code.lower()
            return code if code in allowed else FALLBACK_UI_LANGUAGE
    return FALLBACK_UI_LANGUAGE
