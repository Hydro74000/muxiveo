from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from core.subprocess_utils import ProbeCancelledError, run_probe
from core.tool_versions import ToolVersionReport, resolve_tool, tool_version_report


def cli_path(path: Path | str) -> str:
    if isinstance(path, str):
        return path
    text = str(path)
    if text.startswith("\\\\.\\pipe\\"):
        return text
    return path.as_posix()


def default_ffmpeg_thread_count() -> int:
    """Default FFmpeg thread count: logical CPU count x 0.75, rounded up."""
    cpu_count = os.cpu_count() or 1
    return max(1, (cpu_count * 3 + 3) // 4)


def normalize_ffmpeg_thread_count(value: int | None) -> int:
    """Return a safe FFmpeg thread count, preserving 0 as ffmpeg auto mode."""
    if value is None or value < 0:
        return default_ffmpeg_thread_count()
    return value


# libx265 refuse de s'ouvrir au-delà de 16 images encodées en parallèle (frame-threads).
X265_MAX_FRAME_THREADS = 16


def normalize_x265_frame_threads(value: int | None) -> int:
    """frame-threads de libx265 borné à [0, 16] ; 0 (ou absent) = choix automatique de x265."""
    if value is None:
        return 0
    return max(0, min(int(value), X265_MAX_FRAME_THREADS))


def normalize_max_parallel_video_encodes(value: int | None) -> int:
    """Return a safe per-workflow parallelism value for multi-video preparation."""
    if value is None:
        return 1
    return max(1, int(value))


def ffmpeg_progress_args() -> list[str]:
    """Force a stable machine-readable ffmpeg progress output."""
    return ["-progress", "pipe:1", "-nostats"]


def ffmpeg_thread_args(thread_count: int) -> list[str]:
    """Formate `-threads N` (N=0 → ffmpeg auto). Le caller a déjà normalisé `thread_count`."""
    return ["-threads", str(max(0, int(thread_count)))]


#: TimestampScale Matroska écrit par Muxiveo (1 ms, valeur usuelle des muxeurs).
MATROSKA_TIMESTAMP_SCALE_NS = 1_000_000

_MATROSKA_HELP_TIMEOUT_S = 15.0


@lru_cache(maxsize=16)
def _cached_matroska_timestamp_scale_option(binary: str, size: int, mtime_ns: int) -> bool:
    try:
        result = run_probe(
            [binary, "-hide_banner", "-h", "muxer=matroska"],
            timeout=_MATROSKA_HELP_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError, ProbeCancelledError):
        return False
    return "-timestamp_scale" in f"{result.stdout or ''}\n{result.stderr or ''}"


def matroska_timestamp_scale_supported(ffmpeg_bin: str | Path | None) -> bool:
    """Le muxeur Matroska de ce FFmpeg expose-t-il ``-timestamp_scale`` ?

    FFmpeg récent choisit par défaut une échelle plus fine qu'un échantillon
    audio (10 µs) : un Cluster ne couvre alors plus que 0,33 s. Sonde mise en
    cache par binaire (chemin résolu, taille, date) : invalidée s'il change.
    """
    if not ffmpeg_bin:
        return False
    binary = shutil.which(str(ffmpeg_bin)) or str(ffmpeg_bin)
    try:
        path = Path(binary).resolve()
        stat = path.stat()
    except OSError:
        return False
    return _cached_matroska_timestamp_scale_option(str(path), stat.st_size, stat.st_mtime_ns)


#: Extensions dont FFmpeg déduit un muxeur Matroska / WebM (même option ``-timestamp_scale``).
MATROSKA_OUTPUT_SUFFIXES = frozenset({".mkv", ".mka", ".mks", ".mk3d", ".webm"})


def matroska_output_args(
    ffmpeg_bin: str | Path | None,
    output: str | Path | None = None,
) -> list[str]:
    """Options de sortie Matroska communes : TimestampScale 1 ms si réglable.

    À placer avant le chemin (ou ``pipe:``) de chaque sortie Matroska écrite
    par FFmpeg, fichier final comme intermédiaire. ``output`` (facultatif) :
    chemin de sortie dont l'extension décide du conteneur ; une sortie non
    Matroska (MP4…) ne reçoit rien. Sans ``output``, l'appelant garantit une
    sortie Matroska (``-f matroska``). Sans l'option (FFmpeg plus ancien), le
    muxeur écrit déjà 1 ms : rien à ajouter.
    """
    if output is not None and Path(str(output)).suffix.lower() not in MATROSKA_OUTPUT_SUFFIXES:
        return []
    if matroska_timestamp_scale_supported(ffmpeg_bin):
        return ["-timestamp_scale", str(MATROSKA_TIMESTAMP_SCALE_NS)]
    return []


@lru_cache(maxsize=16)
def _cached_ffmpeg_version_report(binary: str, size: int, mtime_ns: int) -> ToolVersionReport:
    return tool_version_report("ffmpeg", binary)


def ffmpeg_version_report(ffmpeg_bin: str | Path | None) -> ToolVersionReport:
    """Build et provenance du FFmpeg utilisé (manifeste embarqué, sinon ``-version``), en cache par binaire."""
    path = resolve_tool(ffmpeg_bin)
    if path is None:
        return ToolVersionReport(name="ffmpeg", path=None)
    try:
        resolved = path.resolve()
        stat = resolved.stat()
    except OSError:
        return ToolVersionReport(name="ffmpeg", path=None)
    # Chemin d'origine conservé : le manifeste d'un bundle se trouve à côté de
    # l'exécutable désigné, pas forcément à côté de sa cible de lien.
    return _cached_ffmpeg_version_report(str(path), stat.st_size, stat.st_mtime_ns)


def ffmpeg_version_log_line(ffmpeg_bin: str | Path | None) -> str:
    """Ligne de journal en tête de job : build FFmpeg et provenance (diagnostic)."""
    return f"FFmpeg : {ffmpeg_version_report(ffmpeg_bin).label()}"
