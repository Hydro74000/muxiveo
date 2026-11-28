"""Outil factice qui signale son démarrage puis ne rend jamais la main : annulations tuant le processus (POSIX et
Windows)."""

from __future__ import annotations

import stat
import sys
from pathlib import Path


def slow_tool(directory: Path, name: str = "ffprobe") -> tuple[Path, Path]:
    """(exécutable, témoin de démarrage). POSIX : script ``/bin/sh`` ; Windows : script ``.cmd`` lançant un Python
    enfant (l'annulation doit arrêter toute l'arborescence, sinon le tube reste ouvert 30 s)."""
    started = directory / f"{name}.started"
    if sys.platform == "win32":
        script = directory / f"{name}.cmd"
        script.write_text(f'@echo off\r\ntype nul > "{started}"\r\n'
                          f'"{sys.executable}" -c "import time; time.sleep(30)"\r\n', encoding="utf-8")
        return script, started
    script = directory / name
    script.write_text(f"#!/bin/sh\ntouch '{started}'\nexec sleep 30\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, started
