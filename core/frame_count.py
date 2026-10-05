"""
core/frame_count.py — Lecture fiable du nombre de frames vidéo.

mediainfo lit ``%FrameCount%`` instantanément, mais en Matroska il reprend les
tags de statistiques (``NUMBER_OF_FRAMES``) sans vérifier leur fraîcheur : un
fichier coupé ou remuxé par un outil qui recopie les tags de piste annonce alors
le nombre de frames de la source d'origine. La valeur est donc confrontée à la
durée de la piste vidéo × cadence (ffprobe, lecture d'en-tête), la durée du
conteneur — celle de la piste la plus longue — ne servant que de borne haute ;
si elle est implausible, on compte réellement les paquets vidéo
(``-count_packets``).
"""

from __future__ import annotations

import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, NamedTuple

from core.subprocess_utils import subprocess_text_kwargs

# Seule la précision des durées justifie une marge d'une image. Une tolérance
# proportionnelle masquerait des coupes entières sur les longs métrages.
_DURATION_ROUNDING_FRAMES = 1


def _run(cmd: list[str], run_command: Callable | None = None) -> str | None:
    try:
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        result = (run_command or subprocess.run)(  # nosec B603  # nosemgrep  # argv liste, binaire issu de la config
            cmd, capture_output=True, check=False, **subprocess_text_kwargs()
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def mediainfo_frame_count(mediainfo_bin: str, path: Path, *, run_command: Callable | None = None) -> int | None:
    """Valeur brute ``mediainfo %FrameCount%`` (None si absente/illisible)."""
    raw = (_run([mediainfo_bin, "--Inform=Video;%FrameCount%", str(path)], run_command) or "").strip()
    return int(raw) if re.fullmatch(r"\d+", raw) else None


class VideoTiming(NamedTuple):
    """Durées (s) et cadence lues dans les en-têtes, sans parcourir le fichier."""

    container_s: float | None
    video_s: float | None
    fps: float | None


def _positive_float(value: Any) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _parse_tag_duration(value: Any) -> float | None:
    """Tag Matroska ``DURATION`` (``HH:MM:SS.nnnnnnnnn``) en secondes."""
    match = re.fullmatch(r"\s*(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*", str(value or ""))
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    return _positive_float(int(hours) * 3600 + int(minutes) * 60 + float(seconds))


def stream_duration_s(stream: dict[str, Any]) -> float | None:
    """Durée propre d'un flux ffprobe : ``duration`` (MP4…) sinon tag Matroska ``DURATION``."""
    duration = _positive_float(stream.get("duration"))
    if duration is not None:
        return duration
    tags = stream.get("tags") or {}
    if not isinstance(tags, dict):
        return None
    for key, value in tags.items():
        name = str(key).upper()
        if name == "DURATION" or name.startswith("DURATION-"):
            duration = _parse_tag_duration(value)
            if duration is not None:
                return duration
    return None


def stream_fps(stream: dict[str, Any]) -> float | None:
    """Cadence moyenne d'un flux ffprobe (``avg_frame_rate`` puis ``r_frame_rate``)."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            value = float(Fraction(str(stream.get(key) or "0/0")))
        except (ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return value
    return None


def probe_video_timing(ffprobe_bin: str, path: Path, *, run_command: Callable | None = None) -> VideoTiming:
    """Durée du conteneur, durée et cadence de la 1ʳᵉ piste vidéo (en-têtes seuls)."""
    raw = _run([
        ffprobe_bin, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate,duration:stream_tags:format=duration",
        "-of", "json", str(path),
    ], run_command)
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return VideoTiming(None, None, None)
    if not isinstance(data, dict):
        return VideoTiming(None, None, None)
    container = _positive_float((data.get("format") or {}).get("duration"))
    for stream in data.get("streams") or []:
        if isinstance(stream, dict):
            return VideoTiming(container, stream_duration_s(stream), stream_fps(stream))
    return VideoTiming(container, None, None)


def frame_count_is_plausible(
    count: int,
    duration_s: float | None,
    fps: float | None,
    *,
    video_duration_s: float | None = None,
) -> bool:
    """True si ``count`` concorde avec la durée vidéo × cadence (ou si l'estimation est impossible).

    ``duration_s`` est la durée du conteneur, c'est-à-dire celle de la piste la
    plus longue : une piste audio qui déborde la vidéo est courante, elle ne
    borne donc le compte que par le haut. Seule ``video_duration_s`` (durée
    propre du flux) permet un contrôle exact.
    """
    if not fps or fps <= 0:
        return True
    if duration_s and duration_s > 0 and count > duration_s * fps + _DURATION_ROUNDING_FRAMES:
        return False
    reference = video_duration_s if video_duration_s and video_duration_s > 0 else duration_s
    if not reference or reference <= 0:
        return True
    return abs(count - reference * fps) <= _DURATION_ROUNDING_FRAMES


def ffprobe_packet_count(ffprobe_bin: str, path: Path, *, run_command: Callable | None = None, stream_index: int | None = None) -> int | None:
    """Compte réel des paquets vidéo ; index absolu optionnel, première vidéo par défaut."""
    raw = (_run([
        ffprobe_bin, "-v", "error", "-select_streams", "v:0" if stream_index is None else str(stream_index), "-count_packets",
        "-show_entries", "stream=nb_read_packets", "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], run_command) or "").strip().rstrip(",")
    return int(raw) if re.fullmatch(r"\d+", raw) else None


def reliable_frame_count(
    path: Path,
    *,
    mediainfo_bin: str,
    ffprobe_bin: str | None,
    log=None,
    full_scan: bool = True,
    exact: bool = False,
    run_command: Callable | None = None,
) -> int | None:
    """mediainfo si vérifiablement plausible, sinon compte réel des paquets vidéo (ffprobe).

    Sans durée ni cadence (flux HEVC brut), la valeur mediainfo n'est qu'une
    estimation invérifiable — parfois très fausse : les paquets sont comptés,
    ce qui lit tout le fichier. ``full_scan=False`` (affichage) n'effectue
    jamais cette lecture et retourne None quand la valeur rapide est invérifiable.
    ``exact=True`` exige le comptage des paquets et ne reprend jamais un tag
    plausible quand le scan échoue (contrôles stricts de métadonnées).
    """
    if exact:
        # Une estimation plausible ne constitue pas une preuve pour une
        # politique stricte ; aucun repli sur les tags si le scan échoue.
        return ffprobe_packet_count(ffprobe_bin, path, run_command=run_command) if ffprobe_bin and full_scan else None
    count = mediainfo_frame_count(mediainfo_bin, path, run_command=run_command)
    if not ffprobe_bin:
        return count
    if count is not None:
        timing = probe_video_timing(ffprobe_bin, path, run_command=run_command)
        duration = timing.video_s or timing.container_s
        verifiable = bool(duration and timing.fps)
        if verifiable and frame_count_is_plausible(
            count, timing.container_s, timing.fps, video_duration_s=timing.video_s,
        ):
            return count
        if log is not None and verifiable:
            log(
                f"Frame count mediainfo implausible pour {path.name} ({count} frames pour "
                f"{duration:.3f} s à {timing.fps:.3f} fps : statistiques Matroska périmées) — comptage réel."
            )
    if not full_scan:
        return None
    return ffprobe_packet_count(ffprobe_bin, path, run_command=run_command) or count
