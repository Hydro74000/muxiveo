"""
ui/media_player.py — Lecteur Qt Multimedia sans écriture sur la console.

Le moteur FFmpeg de Qt Multimedia laisse libav écrire sur stderr (description
de chaque fichier ouvert, avertissements) et annonce sa version au chargement :
ces lignes atterrissent dans le terminal qui a lancé l'application. Les erreurs
de lecture restent signalées par ``QMediaPlayer.errorOccurred``.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QLoggingCategory, QObject

if TYPE_CHECKING:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

# AV_LOG_QUIET (libavutil/log.h).
_AV_LOG_QUIET = -8
# Emplacements de libavutil dans les paquets PySide6 (Linux, macOS, Windows).
_PYSIDE_LIBAVUTIL_PATTERNS = ("Qt/lib/libavutil.so.*", "Qt/lib/libavutil.*.dylib", "avutil-*.dll")


def create_media_player(parent: QObject) -> tuple[QMediaPlayer, QAudioOutput]:
    """Crée un lecteur audio dont le moteur FFmpeg n'écrit rien sur la console."""
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    # Avant le chargement du plugin ; QT_LOGGING_RULES garde la priorité.
    QLoggingCategory.setFilterRules("qt.multimedia.*.info=false\nqt.multimedia.*.warning=false")
    player = QMediaPlayer(parent)
    audio_output = QAudioOutput(parent)
    player.setAudioOutput(audio_output)
    _silence_libav()
    return player, audio_output


def _silence_libav() -> None:
    """Coupe le journal de la libavutil chargée par le plugin FFmpeg de Qt."""
    for path in _libavutil_candidates():
        try:
            ctypes.CDLL(str(path)).av_log_set_level(_AV_LOG_QUIET)
        except (OSError, AttributeError):
            continue
        return


def _libavutil_candidates() -> list[Path]:
    """libavutil réellement chargée (Linux), sinon celle livrée avec PySide6."""
    found: list[Path] = []
    if sys.platform.startswith("linux"):
        try:
            maps = Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace")
        except OSError:
            maps = ""
        for line in maps.splitlines():
            name = line.rsplit(None, 1)[-1] if line.strip() else ""
            if "/libavutil.so" in name and Path(name) not in found:
                found.append(Path(name))
    try:
        import PySide6

        root = Path(PySide6.__file__).parent
    except (ImportError, TypeError):
        return found
    for pattern in _PYSIDE_LIBAVUTIL_PATTERNS:
        found.extend(path for path in sorted(root.glob(pattern)) if path not in found)
    return found
