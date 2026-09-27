"""
core/frame_count.py — Lecture fiable du nombre de frames vidéo.

mediainfo lit ``%FrameCount%`` instantanément, mais en Matroska il reprend les
tags de statistiques (``NUMBER_OF_FRAMES``) sans vérifier leur fraîcheur : un
fichier coupé ou remuxé par un outil qui recopie les tags de piste annonce alors
le nombre de frames de la source d'origine. La valeur est donc confrontée à la
durée du conteneur × cadence (ffprobe, lecture d'en-tête) ; si elle est
implausible, on compte réellement les paquets vidéo (``-count_packets``).
"""

from __future__ import annotations

import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path

from core.subprocess_utils import subprocess_text_kwargs

# Seule la précision des durées justifie une marge d'une image. Une tolérance
# proportionnelle masquerait des coupes entières sur les longs métrages.
_DURATION_ROUNDING_FRAMES = 1


def _run(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, check=False, **subprocess_text_kwargs())
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def mediainfo_frame_count(mediainfo_bin: str, path: Path) -> int | None:
    """Valeur brute ``mediainfo %FrameCount%`` (None si absente/illisible)."""
    raw = (_run([mediainfo_bin, "--Inform=Video;%FrameCount%", str(path)]) or "").strip()
    return int(raw) if re.fullmatch(r"\d+", raw) else None


def probe_duration_and_fps(ffprobe_bin: str, path: Path) -> tuple[float | None, float | None]:
    """Durée du conteneur (s) et cadence moyenne de la 1ʳᵉ piste vidéo (en-têtes seuls)."""
    raw = _run([
        ffprobe_bin, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ])
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    duration: float | None = None
    try:
        duration = float(str((data.get("format") or {}).get("duration")))
    except (TypeError, ValueError):
        duration = None
    fps: float | None = None
    for stream in data.get("streams") or []:
        for key in ("avg_frame_rate", "r_frame_rate"):
            try:
                value = float(Fraction(str(stream.get(key) or "0/0")))
            except (ValueError, ZeroDivisionError):
                continue
            if value > 0:
                fps = value
                break
        if fps:
            break
    return duration, fps


def frame_count_is_plausible(count: int, duration_s: float | None, fps: float | None) -> bool:
    """True si ``count`` concorde avec durée × cadence (ou si l'estimation est impossible)."""
    if not duration_s or not fps or duration_s <= 0 or fps <= 0:
        return True
    estimate = duration_s * fps
    return abs(count - estimate) <= _DURATION_ROUNDING_FRAMES


def ffprobe_packet_count(ffprobe_bin: str, path: Path) -> int | None:
    """Compte réel des paquets de la 1ʳᵉ piste vidéo (lecture du conteneur, sans décodage)."""
    raw = (_run([
        ffprobe_bin, "-v", "error", "-select_streams", "v:0", "-count_packets",
        "-show_entries", "stream=nb_read_packets", "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]) or "").strip().rstrip(",")
    return int(raw) if re.fullmatch(r"\d+", raw) else None


def reliable_frame_count(
    path: Path,
    *,
    mediainfo_bin: str,
    ffprobe_bin: str | None,
    log=None,
) -> int | None:
    """mediainfo si plausible, sinon compte réel des paquets vidéo (ffprobe)."""
    count = mediainfo_frame_count(mediainfo_bin, path)
    if not ffprobe_bin:
        return count
    if count is not None:
        duration, fps = probe_duration_and_fps(ffprobe_bin, path)
        if frame_count_is_plausible(count, duration, fps):
            return count
        if log is not None:
            log(
                f"Frame count mediainfo implausible pour {path.name} ({count} frames pour "
                f"{duration:.3f} s à {fps:.3f} fps : statistiques Matroska périmées) — comptage réel."
            )
    return ffprobe_packet_count(ffprobe_bin, path) or count
