"""Routage des codecs de sous-titres pour une sortie Matroska.

MKV accepte en muxage direct ("copy") :
    - srt / subrip
    - ass / ssa
    - webvtt
    - hdmv_pgs_subtitle (PGS/SUP)
    - dvd_subtitle (VobSub)
    - hdmv_text_subtitle (TextST)

Tout autre codec doit être converti avant muxage. Cas courants :
    - mov_text        (MP4/MOV)       → convertir en srt
    - eia_608/cea_608 (TS broadcast)  → convertir en srt
    - cea_708         (TS broadcast)  → convertir en srt
    - microdvd                        → convertir en srt
    - jacosub / mpl2 / pjs / realtext → convertir en srt

Codecs raster non-PGS/VobSub non gérés en copy ni en conversion simple :
    - dvb_subtitle (broadcast) → nécessite OCR, hors scope
    - dvb_teletext              → hors scope
"""

from __future__ import annotations


#: Codecs que MKV accepte en muxage direct via ``-c:s copy``.
MKV_COPY_SAFE: frozenset[str] = frozenset({
    "subrip", "srt",
    "ass", "ssa",
    "webvtt",
    "hdmv_pgs_subtitle",
    "dvd_subtitle",
    "hdmv_text_subtitle",
})

#: Codecs texte convertibles vers srt par ffmpeg sans outil externe.
CONVERT_TO_SRT: frozenset[str] = frozenset({
    "mov_text",
    "eia_608", "cea_608", "cea_708",
    "microdvd",
    "jacosub",
    "mpl2",
    "pjs",
    "realtext",
    "sami",
    "stl",
    "subviewer", "subviewer1",
    "vplayer",
})

#: Codecs raster non gérés (nécessitent OCR ou non convertibles nativement).
UNSUPPORTED: frozenset[str] = frozenset({
    "dvb_subtitle",
    "dvb_teletext",
    "arib_caption",
    "hdmv_text_subtitle_raw",
})


def plan_subtitle_codec(codec: str) -> tuple[str, str | None]:
    """Retourne ``(ffmpeg_codec_arg, warning_message_or_None)``.

    - ``("copy", None)`` si le codec passe en muxage direct.
    - ``("srt", None)`` si conversion vers srt possible.
    - ``("copy", "<msg>")`` si codec inconnu : tentative en copy avec avertissement.
    - Lève ``ValueError`` si le codec est explicitement non supporté.
    """
    c = (codec or "").lower().strip()

    if c in MKV_COPY_SAFE:
        return ("copy", None)

    if c in CONVERT_TO_SRT:
        return ("srt", None)

    if c in UNSUPPORTED:
        raise ValueError(
            f"Sous-titre au format '{codec}' non supporté pour une sortie MKV "
            "(nécessite un OCR ou une conversion externe hors scope)."
        )

    # Codec inconnu : on tente copy, ffmpeg échouera si le muxer Matroska
    # refuse, et l'utilisateur verra l'erreur dans le log.
    return ("copy", f"Codec de sous-titre inconnu '{codec}' — tentative en copy.")
