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
from typing import Callable

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


def probe_duration_and_fps(ffprobe_bin: str, path: Path, *, run_command: Callable | None = None) -> tuple[float | None, float | None]:
    """Durée du conteneur (s) et cadence moyenne de la 1ʳᵉ piste vidéo (en-têtes seuls)."""
    raw = _run([
        ffprobe_bin, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ], run_command)
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
        duration, fps = probe_duration_and_fps(ffprobe_bin, path, run_command=run_command)
        verifiable = bool(duration and fps and duration > 0 and fps > 0)
        if verifiable and frame_count_is_plausible(count, duration, fps):
            return count
        if log is not None and verifiable:
            log(
                f"Frame count mediainfo implausible pour {path.name} ({count} frames pour "
                f"{duration:.3f} s à {fps:.3f} fps : statistiques Matroska périmées) — comptage réel."
            )
    if not full_scan:
        return None
    return ffprobe_packet_count(ffprobe_bin, path, run_command=run_command) or count
