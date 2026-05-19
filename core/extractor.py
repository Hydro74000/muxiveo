"""
core/extractor.py — Extraction de pistes individuelles vers des fichiers autonomes.

Centralise la logique d'extraction. Pour l'instant : sous-titres uniquement.
Extensible plus tard à l'audio et la vidéo via des méthodes dédiées.
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


class TrackExtractor:
    """Construit les plans et commandes ffmpeg d'extraction de pistes."""

    _SUBTITLE_PLANS: dict[str, ExtractPlan] = {
        "subrip":             ExtractPlan("copy", ".srt", "SubRip",   "SubRip (*.srt)"),
        "srt":                ExtractPlan("copy", ".srt", "SubRip",   "SubRip (*.srt)"),
        "ass":                ExtractPlan("copy", ".ass", "ASS",      "SSA/ASS (*.ass *.ssa)"),
        "ssa":                ExtractPlan("copy", ".ssa", "SSA",      "SSA/ASS (*.ass *.ssa)"),
        "webvtt":             ExtractPlan("copy", ".vtt", "WebVTT",   "WebVTT (*.vtt)"),
        "hdmv_pgs_subtitle":  ExtractPlan("copy", ".sup", "PGS",      "PGS (*.sup)"),
        "dvd_subtitle":       ExtractPlan("copy", ".idx", "VobSub",   "VobSub (*.idx *.sub)"),
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
        # Inconnu : on tente copy avec extension générique
        return ExtractPlan("copy", ".bin", f"{codec} (brut)", "Tous les fichiers (*)")

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
        return [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            *list(progress_args or ()),
            "-i", str(source),
            "-map", f"0:{stream_index}",
            "-c:s", plan.codec_arg,
            *plan.extra_args,
            str(output),
        ]

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
