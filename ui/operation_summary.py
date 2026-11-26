"""Résumé de l'opération Remux & Encode affiché par la barre d'opération (le chemin est dans la barre de sortie)."""
from __future__ import annotations

from core.i18n import translate_text
from core.workflows.encode.catalog import video_codec_spec
from core.workflows.encode.domain.codecs import rate_control_values
from core.workflows.encode.models import EncodeConfig, QualityMode, VideoEncodeSettings
from core.workflows.remux_models import RemuxConfig


def media_operation_summary(remux: RemuxConfig | None, encode: EncodeConfig | None) -> str:
    """« 2 sources · 7 pistes · vidéo x265 — HEVC CRF 18 · audio copié ».

    ``encode`` None : remux sans réencodage. Sources et pistes lues dans la configuration du Conteneur,
    qui porte la sélection commune aux deux pages.
    """
    parts: list[str] = []
    if remux is not None:
        parts.append(_count(len(remux.sources), "{n} source", "{n} sources"))
        parts.append(_count(len(remux.track_order), "{n} piste", "{n} pistes"))
    if encode is None:
        parts.append(translate_text("sans réencodage"))
        return " · ".join(parts)
    videos: list[str] = []
    for video in encode.video_tracks or ([encode.video] if encode.video is not None else []):
        if video.codec == "copy":
            continue
        text = f"{_codec_name(video.codec)} {_rate_text(video, encode.target_size_mb)}"
        if text not in videos:
            videos.append(text)
    parts.append(translate_text("vidéo {codecs}", codecs=", ".join(videos)) if videos
                 else translate_text("vidéo copiée"))
    reencoded = sum(1 for track in encode.audio_tracks if track.codec != "copy")
    parts.append(_count(reencoded, "{n} piste audio réencodée", "{n} pistes audio réencodées") if reencoded
                 else translate_text("audio copié"))
    return " · ".join(parts)


def _count(n: int, singular: str, plural: str) -> str:
    return translate_text(singular if n == 1 else plural, n=n)


def _codec_name(codec: str) -> str:
    """Nom court du codec : libellé du catalogue sans la précision entre parenthèses (« x265 — HEVC »)."""
    spec = video_codec_spec(codec)
    return spec.label.split(" (", 1)[0] if spec is not None else codec


def _rate_text(video: VideoEncodeSettings, target_size_mb: int | None) -> str:
    """Valeurs du mode de débit, sans son libellé long (« CRF 18 », « Qualité 23, 8000 kbps », « 4000 Mo »)."""
    spec, quality = rate_control_values(video)
    if spec is None:
        return f"CRF {quality}"
    values: list[str] = []
    if spec.uses_quality:
        values.append(f"{translate_text(spec.quality_label)} {quality}")
    if spec.bitrate:
        values.append(f"{video.bitrate_kbps} kbps")
    if spec.family == QualityMode.SIZE.value:
        values.append(f"{target_size_mb or video.target_size_mb} Mo")
    return ", ".join(values) if values else translate_text(spec.label)
