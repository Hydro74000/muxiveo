"""Types de fichiers supportés en entrée.

Couvre les formats sources manipulables par FFmpeg : conteneurs vidéo,
streams élémentaires, pistes audio, sous-titres, playlists Blu-ray.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path, PurePath


# (titre, extensions séparées par espace)
SUPPORTED_FILE_TYPES: list[tuple[str, str]] = [
    ("Dolby Digital/Dolby Digital Plus (AC-3, E-AC-3)", "ac3 eac3 eb3 ec3"),
    ("AAC (Advanced Audio Coding)", "aac m4a mp4"),
    ("AVC/H.264 elementary streams", "264 avc h264 x264"),
    ("AVI (Audio/Video Interleaved)", "avi"),
    ("ALAC (Apple Lossless Audio Codec)", "caf m4a mp4"),
    ("Dirac", "drc"),
    ("Dolby TrueHD", "mlp thd thd+ac3 truehd true-hd"),
    ("DTS/DTS-HD (Digital Theater System)", "dts dtshd dts-hd dtsma"),
    ("FLAC (Free Lossless Audio Codec)", "flac ogg"),
    ("FLV (Flash Video)", "f4v flv"),
    ("HDMV TextST", "textst"),
    ("HEVC/H.265 elementary streams", "265 hevc h265 x265"),
    ("IVF (AV1, VP8, VP9)", "ivf"),
    ("MP4 audio/video files", "mp4 m4v"),
    ("MPEG-1/2 Audio Layer II/III elementary streams", "mp2 mp3"),
    ("MPEG program streams", "mpg mpeg m2v mpv evo evob vob"),
    ("MPEG transport streams", "ts m2ts mts"),
    ("MPEG-1/2 video elementary streams", "m1v m2v mpv"),
    ("MPLS Blu-ray playlist", "mpls"),
    ("Matroska audio/video files", "mk3d mka mks mkv"),
    ("PGS/SUP subtitles", "sup"),
    ("QuickTime audio/video files", "mov"),
    ("AV1 Open Bitstream Units stream", "av1 obu"),
    ("Ogg/OGM audio/video files", "ogg ogm ogv"),
    ("Opus (in Ogg) audio files", "opus ogg"),
    ("RealMedia audio/video files", "ra ram rm rmvb rv"),
    ("SRT text subtitles", "srt"),
    ("SSA/ASS text subtitles", "ass ssa"),
    ("TTA (The lossless True Audio codec)", "tta"),
    ("USF text subtitles", "usf xml"),
    ("VC-1 elementary streams", "vc1"),
    ("VobButtons", "btn"),
    ("VobSub subtitles", "idx"),
    ("WAVE (uncompressed PCM audio)", "wav"),
    ("WAVPACK v4 audio", "wv"),
    ("WebM audio/video files", "weba webm webma webmv"),
    ("WebVTT subtitles", "vtt webvtt"),
    ("Blu-ray index files", "bdmv"),
]


def _all_extensions() -> list[str]:
    """Retourne la liste triée et dédupliquée de toutes les extensions (sans point)."""
    exts: set[str] = set()
    for _, ext_group in SUPPORTED_FILE_TYPES:
        for ext in ext_group.split():
            exts.add(ext)
    return sorted(exts)


#: Extensions acceptées, forme ".ext" minuscule (pour drag&drop et filtrage Python).
ACCEPTED_EXTENSIONS: frozenset[str] = frozenset(f".{e}" for e in _all_extensions())


#: Sous-ensemble "vidéo / conteneur" pour les panneaux qui ne doivent accepter
#: que des fichiers pouvant contenir de la vidéo (inspecteur, encodeur, remux).
VIDEO_CONTAINER_EXTENSIONS: frozenset[str] = frozenset({
    ".mkv", ".mk3d", ".mks",
    ".mp4", ".m4v",
    ".mov",
    ".avi",
    ".ts", ".m2ts", ".mts",
    ".mpg", ".mpeg", ".m2v", ".mpv", ".m1v", ".evo", ".evob", ".vob",
    ".webm", ".webmv",
    ".flv", ".f4v",
    ".ogg", ".ogm", ".ogv",
    ".rm", ".rmvb", ".rv",
    ".mpls", ".bdmv",
    # streams élémentaires vidéo (utiles pour injection DoVi/HDR10+)
    ".hevc", ".h265", ".265", ".x265",
    ".avc", ".h264", ".264", ".x264",
    ".av1", ".obu", ".ivf",
    ".vc1",
})


# MIME types déclarés dans les intégrations desktop/app bundle.
# L'objectif n'est pas l'exhaustivité parfaite, mais une liste pratique
# suffisamment large pour que Muxiveo apparaisse dans les dialogues
# "Ouvrir avec..." des fichiers média usuels.
DESKTOP_MIME_TYPES: tuple[str, ...] = (
    "audio/aac",
    "audio/flac",
    "audio/mp4",
    "audio/ogg",
    "audio/opus",
    "audio/vnd.dts",
    "audio/vnd.dts.hd",
    "audio/x-m4a",
    "audio/x-matroska",
    "audio/x-ms-wma",
    "audio/x-wav",
    "audio/x-wavpack",
    "application/x-subrip",
    "application/x-matroska",
    "application/x-mpegURL",
    "application/x-ssa",
    "application/x-ass",
    "text/plain",
    "text/vtt",
    "text/x-ass",
    "text/x-ssa",
    "video/mp4",
    "video/mpeg",
    "video/quicktime",
    "video/webm",
    "video/x-flv",
    "video/x-matroska",
    "video/x-msvideo",
    "video/x-ms-wmv",
    "video/x-m2ts",
    "video/x-matroska",
    "video/x-mpeg",
    "video/x-h264",
    "video/x-h265",
    "video/x-av1",
    "video/x-vc1",
)


def build_desktop_mime_type_string() -> str:
    """Construit la valeur MimeType= d'un fichier .desktop."""
    return ";".join(dict.fromkeys(DESKTOP_MIME_TYPES)) + ";"


def build_qt_filter(video_only: bool = False) -> str:
    """Construit la chaîne de filtre QFileDialog.

    Format : "All supported media files (*.mkv *.mp4 ...);;All files (*);;Type1 (*.ext1 ...);;..."
    """
    if video_only:
        exts = sorted(VIDEO_CONTAINER_EXTENSIONS)
        all_line = "Fichiers vidéo (" + " ".join(f"*{e}" for e in exts) + ")"
        return f"{all_line};;Tous les fichiers (*)"

    per_type: list[str] = []
    for title, ext_group in sorted(SUPPORTED_FILE_TYPES, key=lambda t: t[0].lower()):
        globs = " ".join(f"*.{e}" for e in ext_group.split())
        per_type.append(f"{title} ({globs})")

    all_globs = " ".join(f"*{e}" for e in sorted(ACCEPTED_EXTENSIONS))
    parts = [
        f"Tous les fichiers médias supportés ({all_globs})",
        "Tous les fichiers (*)",
        *per_type,
    ]
    return ";;".join(parts)


def is_accepted(path: str | Path, video_only: bool = False) -> bool:
    """Teste si l'extension du chemin est acceptée."""
    suffix = Path(path).suffix.lower()
    if video_only:
        return suffix in VIDEO_CONTAINER_EXTENSIONS
    return suffix in ACCEPTED_EXTENSIONS


# Caractères interdits dans un nom de fichier Windows (NTFS/FAT) + noms réservés.
_WINDOWS_FORBIDDEN_NAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_WINDOWS_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def windows_filename_error(name: str, *, platform: str | None = None) -> str | None:
    """Retourne un message si ``name`` n'est pas un nom de fichier Windows valide."""
    if (platform or sys.platform) != "win32":
        return None
    bad = sorted(set(_WINDOWS_FORBIDDEN_NAME_CHARS.findall(name)))
    if bad:
        shown = " ".join(repr(c) if ord(c) < 32 else c for c in bad)
        return f"Nom de sortie invalide sous Windows (caractères interdits : {shown}) : {name}"
    if name.rstrip(" .") != name:
        return f"Nom de sortie invalide sous Windows (espace ou point final) : {name}"
    if name.split(".", 1)[0].strip().lower() in _WINDOWS_RESERVED_STEMS:
        return f"Nom de sortie réservé par Windows : {name}"
    return None


_WINDOWS_MAX_PATH = 260


def _windows_long_paths_enabled() -> bool:
    """Lit ``LongPathsEnabled`` (registre) ; False si indisponible."""
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return bool(winreg.QueryValueEx(key, "LongPathsEnabled")[0])
    except (ImportError, OSError):
        return False


def windows_path_length_error(path: PurePath, *, platform: str | None = None, long_paths: bool | None = None) -> str | None:
    """Message si ``path`` dépasse MAX_PATH sous Windows sans chemins longs activés."""
    if (platform or sys.platform) != "win32" or len(str(path)) < _WINDOWS_MAX_PATH:
        return None
    # Le namespace étendu (lecteur local ou UNC) contourne MAX_PATH sans
    # dépendre de l'option système LongPathsEnabled.
    if str(path).startswith("\\\\?\\"):
        return None
    if long_paths is None:
        long_paths = _windows_long_paths_enabled()
    if long_paths:
        return None
    return (
        f"Chemin de sortie trop long ({len(str(path))} caractères, limite Windows {_WINDOWS_MAX_PATH - 1}) : "
        f"raccourcissez-le ou activez les chemins longs (LongPathsEnabled). {path}"
    )
