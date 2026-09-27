"""
ui/desktop.py — Ouverture d'URL et de fichiers dans les applications de l'hôte.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import sys

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from core.subprocess_utils import host_environment


def _is_frozen_linux() -> bool:
    return sys.platform.startswith("linux") and bool(os.environ.get("APPDIR") or getattr(sys, "frozen", False))


def open_external(url: QUrl) -> bool:
    """
    Ouvre `url` (web ou fichier local) avec l'application par défaut de l'hôte.

    Dans une AppImage, QDesktopServices lance xdg-open avec l'environnement du
    bundle (LD_LIBRARY_PATH embarqué) : le navigateur ou gio échouent en silence.
    On lance donc xdg-open avec un environnement hôte nettoyé.
    """
    if _is_frozen_linux():
        env = host_environment()
        opener = shutil.which("xdg-open", path=env.get("PATH"))
        if opener:
            target = url.toLocalFile() if url.isLocalFile() else bytes(url.toEncoded().data()).decode("ascii")
            try:
                # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
                subprocess.Popen(  # nosec B603  # xdg-open hôte, argument unique, sans shell
                    [opener, target],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True
            except OSError:
                pass
    return QDesktopServices.openUrl(url)
