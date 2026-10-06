"""
core/workflows/encode/planning/size_budget.py — Budget du mode taille cible.

La cible porte sur le fichier complet (Mio) : ``EncodeConfig.target_size_mb``,
sinon la valeur commune des pistes en mode taille. Chaque poste déduit a une
provenance : mesuré (statistiques de flux copiés, débits demandés, pièces
jointes), estimé (FLAC, qualité constante, conteneur) ou inconnu (flux sans
débit, signalé). Seuls les postes mesurés peuvent rendre une cible
inatteignable. Les sondes de flux sont fournies par l'appelant.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from core.workflows.encode.catalog import resolve_rate_control
from core.workflows.encode.models import (
    EncodeConfig,
    QualityMode,
    VideoEncodeSettings,
    normalize_audio_bitrate_kbps,
)

# Réserve de conteneur (estimation) : ~0,5 % de la cible.
CONTAINER_OVERHEAD = 0.005
# Débit vidéo par piste en deçà duquel un avertissement est émis.
LOW_VIDEO_BITRATE_BPS = 500_000


@dataclass(frozen=True)
class SizeBudget:
    """Taille cible du fichier convertie en débits (bit/s), postes par provenance."""

    target_bps: float
    reserved_bps: float
    video_bps: float
    video_shares_bps: tuple[tuple[VideoEncodeSettings, float], ...]
    duration_s: float
    target_size_mb: int
    estimated_bps: float = 0.0
    warnings: tuple[str, ...] = ()
    measured_bps: float = 0.0
    unknown: tuple[str, ...] = ()


@dataclass(frozen=True)
class SizeBudgetProbes:
    """Accès aux pistes et aux flux sources, fournis par le workflow."""

    video_tracks: Callable[[EncodeConfig], list[VideoEncodeSettings]]
    primary_video: Callable[[EncodeConfig], VideoEncodeSettings]
    video_source: Callable[[EncodeConfig, VideoEncodeSettings], Path]
    video_stream: Callable[[VideoEncodeSettings], int]
    #: Dictionnaire ffprobe du flux (``{}`` si inconnu).
    stream_info: Callable[[Path, int], dict[str, object]]
    #: Index des flux de sous-titres d'une source (copie générique).
    subtitle_streams: Callable[[Path], list[int]]


def sized_videos(videos: list[VideoEncodeSettings]) -> list[VideoEncodeSettings]:
    return [v for v in videos if v.codec != "copy" and v.quality_mode == QualityMode.SIZE]


def file_target_size_values(config: EncodeConfig, videos: list[VideoEncodeSettings]) -> list[int]:
    """Taille du fichier (Mio) : globale, sinon valeurs distinctes des pistes en mode taille."""
    if config.target_size_mb is not None:
        return [int(config.target_size_mb)]
    return sorted({int(v.target_size_mb) for v in sized_videos(videos)})


def _tags(stream: dict[str, object]) -> dict[str, object]:
    raw_tags = stream.get("tags")
    return raw_tags if isinstance(raw_tags, dict) else {}


def stream_duration_s(stream: dict[str, object], fallback: float) -> float:
    """Durée du flux (ffprobe ou tag Matroska), sinon durée de référence du job."""
    tags = _tags(stream)
    for value in (stream.get("duration"), tags.get("DURATION"), tags.get("DURATION-eng")):
        try:
            parts = str(value).split(":")
            seconds = float(parts[-1])
            if len(parts) == 3:
                seconds += int(parts[0]) * 3600 + int(parts[1]) * 60
            elif len(parts) != 1:
                continue
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    return fallback


def stream_bitrate_bps(stream: dict[str, object]) -> float:
    """Débit d'un flux source : ``bit_rate`` ffprobe, sinon statistiques Matroska (BPS) ; 0 si inconnu."""
    tags = _tags(stream)
    for value in (stream.get("bit_rate"), tags.get("BPS"), tags.get("BPS-eng")):
        try:
            bitrate = float(str(value))
        except (TypeError, ValueError):
            continue
        if math.isfinite(bitrate) and bitrate > 0:
            return bitrate
    return 0.0


def stream_pixel_rate(stream: dict[str, object]) -> float:
    """Poids d'une piste vidéo dans la répartition : largeur × hauteur × cadence."""
    try:
        pixels = float(str(stream.get("width") or 0)) * float(str(stream.get("height") or 0))
    except (TypeError, ValueError):
        pixels = 0.0
    try:
        fps = float(Fraction(str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0")))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    weight = pixels * fps
    return weight if math.isfinite(weight) and weight > 0 else 1.0


def compute_size_budget(config: EncodeConfig, probes: SizeBudgetProbes) -> SizeBudget:
    """Répartition de la taille cible (fichier complet) entre les flux.

    Des valeurs divergentes sont refusées par :func:`size_target_errors` ;
    l'aperçu retient la plus petite, indépendamment de l'ordre des pistes.
    Postes déduits : audio encodé (débit demandé, mesuré), audio copié
    (statistiques du flux, mesuré), FLAC (débit source, estimé), sous-titres
    et vidéo copiée (statistiques, mesuré), pièces jointes (taille des
    fichiers, mesuré), vidéos à débit imposé (mesuré), vidéos en qualité
    constante (débit source, estimé) et conteneur (estimé). Le reste est
    réparti entre les pistes en taille cible au prorata pixels × cadence × durée.
    """
    videos = probes.video_tracks(config)
    sized = sized_videos(videos)
    target_values = file_target_size_values(config, videos)
    target_size_mb = min(target_values) if target_values else int(probes.primary_video(config).target_size_mb)
    duration = float(config.duration_s or 3600.0)
    if not math.isfinite(duration) or duration <= 0:
        duration = 3600.0  # aperçu ; la durée invalide est refusée à la validation
    target_bps = target_size_mb * 8 * 1024 * 1024 / duration
    measured_bps = 0.0
    estimated_bps = target_bps * CONTAINER_OVERHEAD
    unknown: list[str] = []
    warnings: list[str] = []

    for number, audio in enumerate(config.audio_tracks, start=1):
        stream = probes.stream_info(Path(audio.source_path or config.source), int(audio.stream_index))
        share = stream_duration_s(stream, duration) / duration
        if audio.codec in ("copy", "flac"):
            bitrate = stream_bitrate_bps(stream)
            if bitrate <= 0:
                unknown.append(f"audio #{number}")
            elif audio.codec == "flac":
                # Compression FLAC inconnue avant encodage : débit source
                # retenu comme estimation haute, jamais comme preuve.
                estimated_bps += bitrate * share
                warnings.append(
                    f"Taille cible : piste audio #{number} en FLAC estimée depuis le débit source "
                    f"({bitrate / 1000:.0f} kbps) ; taille finale approchée."
                )
            else:
                measured_bps += bitrate * share
        else:
            measured_bps += normalize_audio_bitrate_kbps(
                audio.codec, audio.bitrate_kbps, audio.input_channels, None, audio.input_channel_layout,
            ) * 1000 * share

    subtitle_refs = list(config.subtitle_tracks)
    if not subtitle_refs and config.copy_subtitles:
        subtitle_refs = [(Path(config.source), index) for index in probes.subtitle_streams(Path(config.source))]
    for number, (src, idx) in enumerate(subtitle_refs, start=1):
        stream = probes.stream_info(Path(src), int(idx))
        bitrate = stream_bitrate_bps(stream)
        if bitrate <= 0:
            unknown.append(f"sous-titres #{number}")
            continue
        measured_bps += bitrate * stream_duration_s(stream, duration) / duration

    attachment_bytes = sum(Path(p).stat().st_size for p in config.extra_attachments if Path(p).is_file())
    measured_bps += attachment_bytes * 8 / duration

    encoded: list[tuple[VideoEncodeSettings, float, float]] = []
    for track_number, video in enumerate(videos, start=1):
        stream = probes.stream_info(probes.video_source(config, video), probes.video_stream(video))
        track_duration = stream_duration_s(stream, duration)
        if video.codec != "copy" and (video.quality_mode == QualityMode.SIZE or not sized):
            encoded.append((video, stream_pixel_rate(stream) * track_duration, track_duration))
            continue
        share = track_duration / duration
        spec = resolve_rate_control(video.codec, video.rate_control, video.quality_mode)
        if video.codec != "copy" and spec is not None and spec.bitrate:
            measured_bps += video.bitrate_kbps * 1000 * share
            continue
        bitrate = stream_bitrate_bps(stream)
        if bitrate <= 0:
            unknown.append(f"vidéo #{track_number}")
        elif video.codec == "copy":
            measured_bps += bitrate * share
        else:
            estimated_bps += bitrate * share
            warnings.append(
                f"Taille cible : budget de la piste vidéo #{track_number} en qualité constante "
                f"estimé depuis le débit source ({bitrate / 1000:.0f} kbps) ; taille finale approchée."
            )
    if unknown:
        warnings.append(
            "Taille cible : débit inconnu pour " + ", ".join(unknown)
            + " (non déduit du budget) ; la taille finale peut dépasser la cible."
        )

    reserved_bps = measured_bps + estimated_bps
    video_bps = target_bps - reserved_bps
    total_weight = sum(weight for _video, weight, _duration in encoded) or 1.0
    shares = tuple(
        (video, video_bps * duration * weight / total_weight / track_duration)
        for video, weight, track_duration in encoded
    )
    return SizeBudget(
        target_bps=target_bps, reserved_bps=reserved_bps, video_bps=video_bps,
        video_shares_bps=shares, duration_s=duration, target_size_mb=target_size_mb,
        estimated_bps=estimated_bps, warnings=tuple(warnings), measured_bps=measured_bps,
        unknown=tuple(unknown),
    )


def size_target_errors(config: EncodeConfig, probes: SizeBudgetProbes) -> list[str]:
    """Taille cible invalide ou contradictoire, ou inatteignable d'après les seuls débits mesurés."""
    videos = probes.video_tracks(config)
    if not sized_videos(videos):
        return []
    if config.duration_s is not None and not math.isfinite(config.duration_s):
        return ["Durée du fichier source non finie — mode taille cible impossible."]
    if config.target_size_mb is not None and int(config.target_size_mb) <= 0:
        return ["Taille du fichier invalide (Mio > 0 attendue)."]
    values = file_target_size_values(config, videos)
    if len(values) > 1:
        return [
            "Tailles cibles contradictoires entre pistes vidéo ("
            + ", ".join(f"{value} Mio" for value in values)
            + ") : la taille cible porte sur le fichier complet ; indiquez une seule valeur."
        ]
    if not (config.duration_s or 0) > 0:
        return []
    budget = compute_size_budget(config, probes)
    # Une estimation (FLAC, qualité constante, conteneur) ne prouve pas
    # qu'une cible est impossible : seuls les débits mesurés sont bloquants.
    if budget.target_bps > budget.measured_bps:
        return []
    reserved_mib = budget.measured_bps * budget.duration_s / 8 / 1024 / 1024
    return [
        "Taille cible inatteignable : audio, sous-titres, pièces jointes et vidéos hors taille cible "
        f"occupent déjà ≈ {reserved_mib:.0f} Mio pour "
        f"{budget.target_size_mb} Mio demandés."
    ]


def size_target_warnings(config: EncodeConfig, probes: SizeBudgetProbes) -> list[str]:
    """Postes estimés ou inconnus et débits vidéo très bas (< 500 kbps par piste)."""
    videos = probes.video_tracks(config)
    if not sized_videos(videos) or not (config.duration_s or 0) > 0:
        return []
    if len(file_target_size_values(config, videos)) > 1:
        return []
    budget = compute_size_budget(config, probes)
    warnings = list(budget.warnings)
    if budget.video_bps <= 0 and budget.target_bps > budget.measured_bps:
        warnings.append(
            "Taille cible : les postes estimés (FLAC, qualité constante, conteneur) dépassent le budget "
            "disponible ; taille finale non garantie."
        )
    track_numbers = {id(video): index for index, video in enumerate(videos, start=1)}
    warnings.extend(
        f"Taille cible : débit vidéo de la piste #{track_numbers[id(video)]} ≈ {share / 1000:.0f} kbps (très bas)."
        for video, share in budget.video_shares_bps
        if 0 < share < LOW_VIDEO_BITRATE_BPS
    )
    return warnings


__all__ = [
    "SizeBudget", "SizeBudgetProbes", "compute_size_budget", "file_target_size_values",
    "size_target_errors", "size_target_warnings", "sized_videos", "stream_bitrate_bps",
    "stream_duration_s", "stream_pixel_rate",
]
