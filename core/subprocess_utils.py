"""
Helpers de décodage texte pour les outils externes.

Sous Windows, plusieurs outils émettent une sortie UTF-8, mais
`subprocess.run(..., text=True)` la décode sinon avec la page de code locale
du système. On force donc l'UTF-8 pour éviter le mojibake du type `FranÃ§ais`.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any


_TOOL_TEXT_ENCODING = "utf-8"
_TOOL_TEXT_ERRORS = "replace"


def subprocess_windows_no_window_kwargs() -> dict[str, Any]:
    """
    Return subprocess kwargs that prevent a console window from flashing on Windows.

    Inclut systématiquement stdin=DEVNULL : sous Linux, ffmpeg/dovi_tool/etc.
    héritent sinon du tty parent et peuvent altérer ses flags termios (echo
    désactivé, mode raw) — ce qui casse le terminal après fermeture de l'app.
    Sous Windows, évite aussi WinError 50 en mode GUI sans console.

    Safe to pass to both subprocess.run() and subprocess.Popen().
    """
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL}

    if sys.platform != "win32":
        return kwargs

    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        kwargs["creationflags"] = create_no_window

    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startf_use_showwindow = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        if startf_use_showwindow:
            startupinfo.dwFlags |= startf_use_showwindow
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo

    return kwargs


def subprocess_text_kwargs() -> dict[str, Any]:
    """
    Retourne les kwargs à injecter dans subprocess.run/Popen pour lire du texte.

    Sous Windows, on force l'UTF-8 ; ailleurs, on conserve le comportement
    standard de Python pour limiter le périmètre du changement.
    """
    # stdin=DEVNULL est inclus systématiquement (cf. subprocess_windows_no_window_kwargs)
    # pour empêcher les outils externes d'altérer le tty parent.
    kwargs: dict[str, Any] = {"text": True}
    kwargs.update(subprocess_windows_no_window_kwargs())
    if sys.platform == "win32":
        kwargs["encoding"] = _TOOL_TEXT_ENCODING
        kwargs["errors"] = _TOOL_TEXT_ERRORS
    return kwargs


def decode_subprocess_output(raw: bytes) -> str:
    """Décode un buffer brut provenant d'un outil externe."""
    return raw.decode(_TOOL_TEXT_ENCODING, errors=_TOOL_TEXT_ERRORS)
