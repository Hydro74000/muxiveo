"""
core/extractor.py — Extraction de pistes individuelles vers des fichiers autonomes.

Plans d'extraction (codec FFmpeg, extension, format de sortie) des sous-titres, de l'audio et de la
vidéo ; les couches Dolby Vision / HDR10+ et les pièces jointes sont extraites par ``core.jobs.extract``.
Le format de sortie (``-f``) est toujours explicite : la sortie est d'abord un candidat ``.partial``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from core.subtitle_codec import CONVERT_TO_SRT, MKV_COPY_SAFE, UNSUPPORTED


@dataclass(frozen=True)
class ExtractPlan:
    """Plan d'extraction d'une piste vers un fichier autonome."""

    codec_arg:    str                       # argument -c:s / -c:a / -c:v
    extension:    str                       # extension avec point (".srt")
    format_label: str                       # libellé affiché à l'utilisateur
    file_filter:  str                       # filtre QFileDialog
    extra_args:   tuple[str, ...] = field(default_factory=tuple)
    muxer:        str = ""                  # format FFmpeg (-f) ; "" : déduit de l'extension

    @property
    def output_format(self) -> str:
        return self.muxer or MUXER_BY_EXTENSION.get(self.extension, "matroska")


#: Format FFmpeg (-f) de chaque extension produite.
MUXER_BY_EXTENSION: dict[str, str] = {
    ".srt": "srt", ".ass": "ass", ".ssa": "ass", ".vtt": "webvtt", ".sup": "sup", ".mks": "matroska",
    ".mka": "matroska", ".mkv": "matroska", ".thd": "truehd", ".eac3": "eac3", ".ac3": "ac3", ".dts": "dts",
    ".m4a": "ipod", ".flac": "flac", ".opus": "opus", ".ogg": "ogg", ".mp3": "mp3", ".wav": "wav",
    ".hevc": "hevc", ".h264": "h264", ".obu": "obu", ".ivf": "ivf", ".m2v": "mpeg2video",
}


class TrackExtractor:
    """Construit les plans et commandes ffmpeg d'extraction de pistes."""

    _SUBTITLE_PLANS: dict[str, ExtractPlan] = {
        "subrip":             ExtractPlan("copy", ".srt", "SubRip",   "SubRip (*.srt)"),
        "srt":                ExtractPlan("copy", ".srt", "SubRip",   "SubRip (*.srt)"),
        "ass":                ExtractPlan("copy", ".ass", "ASS",      "SSA/ASS (*.ass *.ssa)"),
        "ssa":                ExtractPlan("copy", ".ssa", "SSA",      "SSA/ASS (*.ass *.ssa)"),
        "webvtt":             ExtractPlan("copy", ".vtt", "WebVTT",   "WebVTT (*.vtt)"),
        "hdmv_pgs_subtitle":  ExtractPlan("copy", ".sup", "PGS",      "PGS (*.sup)"),
        # FFmpeg ne sait pas écrire VobSub (.idx/.sub) : piste conservée dans un conteneur Matroska.
        "dvd_subtitle":       ExtractPlan("copy", ".mks", "VobSub (Matroska)", "Matroska (*.mks)"),
        "hdmv_text_subtitle": ExtractPlan("srt",  ".srt", "SubRip",   "SubRip (*.srt)"),
    }

    _SRT_PLAN = ExtractPlan("srt", ".srt", "SubRip", "SubRip (*.srt)")

    @classmethod
    def plan_subtitle(cls, codec: str) -> ExtractPlan:
        """Retourne le plan d'extraction pour un codec de sous-titre.

        Lève ``ValueError`` pour les codecs explicitement non supportés
        (DVB, teletext, etc.) qui requièrent un OCR ou un outil externe.
        """
        c = (codec or "").lower().strip()

        if c in UNSUPPORTED:
            raise ValueError(
                f"Codec de sous-titre '{codec}' non extractible nativement "
                "(nécessite OCR ou outil externe)."
            )
        if c in cls._SUBTITLE_PLANS:
            return cls._SUBTITLE_PLANS[c]
        if c in CONVERT_TO_SRT:
            return cls._SRT_PLAN
        if c in MKV_COPY_SAFE:
            return ExtractPlan("copy", ".mks", f"{codec} (brut)", "Tous les fichiers (*)")
        # Inconnu : copie dans un conteneur Matroska (seul format sûr pour un codec quelconque)
        return ExtractPlan("copy", ".mks", f"{codec} (Matroska)", "Matroska (*.mks)")

    _AUDIO_PLANS: dict[str, tuple[str, str]] = {
        "truehd": (".thd", "TrueHD"), "eac3": (".eac3", "E-AC-3"), "ac3": (".ac3", "AC-3"), "dts": (".dts", "DTS"),
        "aac": (".m4a", "AAC"), "alac": (".m4a", "ALAC"), "flac": (".flac", "FLAC"), "opus": (".opus", "Opus"),
        "vorbis": (".ogg", "Vorbis"), "mp3": (".mp3", "MP3"),
    }

    _VIDEO_PLANS: dict[str, tuple[str, str, tuple[str, ...]]] = {
        "hevc": (".hevc", "HEVC (Annex B)", ("-bsf:v", "hevc_mp4toannexb")),
        "h264": (".h264", "H.264 (Annex B)", ("-bsf:v", "h264_mp4toannexb")),
        "av1": (".obu", "AV1 (OBU)", ()),
        "mpeg2video": (".m2v", "MPEG-2", ()),
        "vp9": (".ivf", "VP9 (IVF)", ()),
        "vp8": (".ivf", "VP8 (IVF)", ()),
    }

    @classmethod
    def plan_audio(cls, codec: str, *, container: bool = False) -> ExtractPlan:
        """Flux audio copié dans son format élémentaire (Matroska audio sinon, ou si ``container``)."""
        c = (codec or "").lower().strip()
        if not container and c.startswith("pcm_"):
            return ExtractPlan("copy", ".wav", "PCM (WAV)", "WAV (*.wav)")
        if not container and c in cls._AUDIO_PLANS:
            extension, label = cls._AUDIO_PLANS[c]
            return ExtractPlan("copy", extension, label, f"{label} (*{extension})")
        return ExtractPlan("copy", ".mka", f"{codec} (Matroska)", "Matroska audio (*.mka)")

    @classmethod
    def plan_video(cls, codec: str, *, container: bool = False) -> ExtractPlan:
        """Flux vidéo brut (Annex B pour HEVC / H.264), ou Matroska si inconnu ou ``container``."""
        c = (codec or "").lower().strip()
        if not container and c in cls._VIDEO_PLANS:
            extension, label, extra = cls._VIDEO_PLANS[c]
            return ExtractPlan("copy", extension, label, f"{label} (*{extension})", extra)
        return ExtractPlan("copy", ".mkv", f"{codec} (Matroska)", "Matroska (*.mkv)")

    @classmethod
    def build_command(
        cls,
        ffmpeg_bin: str,
        source: Path,
        stream_index: int,
        kind: str,
        plan: ExtractPlan,
        output: Path,
        *,
        progress_args: Sequence[str] | None = None,
        input_args: Sequence[str] | None = None,
    ) -> list[str]:
        """Commande FFmpeg d'extraction d'une piste (``kind`` : video, audio, subtitle), format explicite."""
        letter = {"video": "v", "audio": "a", "subtitle": "s"}[kind]
        return [
            ffmpeg_bin, "-hide_banner", "-y", *list(progress_args or ()),
            *(list(input_args) if input_args is not None else ["-i", str(source)]),
            "-map", f"0:{stream_index}", f"-c:{letter}", plan.codec_arg, *plan.extra_args,
            "-f", plan.output_format, str(output),
        ]

    @classmethod
    def build_subtitle_command(
        cls,
        ffmpeg_bin: str,
        source: Path,
        stream_index: int,
        codec: str,
        output: Path,
        *,
        progress_args: Sequence[str] | None = None,
    ) -> list[str]:
        """Construit la commande ffmpeg pour extraire un sous-titre."""
        plan = cls.plan_subtitle(codec)
        return cls.build_command(ffmpeg_bin, source, stream_index, "subtitle", plan, output,
                                 progress_args=progress_args)

    @staticmethod
    def default_output_name(
        source_stem: str,
        language: str,
        stream_index: int,
        extension: str,
    ) -> str:
        """Nom de fichier par défaut pour la boîte de dialogue : stem.lang.tN.ext"""
        lang = (language or "und").strip() or "und"
        return f"{source_stem}.{lang}.t{stream_index}{extension}"


__all__ = ["ExtractPlan", "TrackExtractor"]
