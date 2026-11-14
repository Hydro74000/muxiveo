"""Bloc ``encode`` d'un job : contrat public de l'encodage (CLI, couche de service, futur daemon).

Chaque champ absent (``None``) laisse la valeur de la couche précédente : défauts de la source,
preset, ``video.default``, piste. Unités : kbit/s, Mio, pixels, nits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.service.models import Model, field
from core.hdr_formats import MASTER_DISPLAY_BODY, MAX_CLL_BODY, optional_text_pattern


@dataclass(frozen=True)
class TrackSelector(Model):
    """Désigne une piste : source (rang dans le job) et ``stream`` (index ffprobe), ``index`` (rang parmi les
    pistes du même type de la source) ou ``entry_id`` (variante audio)."""

    source: int = field(default=0, minimum=0, description="Rang de la source dans le job.")
    stream: int | None = field(default=None, minimum=0, description="Index ffprobe de la piste.")
    index: int | None = field(default=None, minimum=0, description="Rang parmi les pistes du même type.")
    entry_id: str | None = field(default=None, description="Identifiant de piste (variante audio).")


@dataclass(frozen=True)
class CropSpec(Model):
    """Recadrage : ``auto`` mesure les bandes noires (comme le bouton Auto-crop)."""

    auto: bool | None = None
    unit: Literal["px", "percent"] | None = None
    top: int | None = field(default=None, minimum=0)
    bottom: int | None = field(default=None, minimum=0)
    left: int | None = field(default=None, minimum=0)
    right: int | None = field(default=None, minimum=0)


@dataclass(frozen=True)
class ResizeSpec(Model):
    enabled: bool | None = None
    mode: Literal["preset", "percent", "size"] | None = None
    preset: str | None = field(default=None, description="720p, 1080p, 2160p…")
    percent: int | None = field(default=None, minimum=1)
    width: int | None = field(default=None, minimum=2)
    height: int | None = field(default=None, minimum=2)
    keep_aspect: bool | None = None
    allow_upscale: bool | None = None
    algorithm: str | None = None


@dataclass(frozen=True)
class FilterSpec(Model):
    """Désentrelacement (YADIF), deblock, NLMeans, lissage chroma ; ``denoise_filter`` mémorise le débruiteur."""

    yadif_enabled: bool | None = None
    yadif_mode: str | None = None
    yadif_parity: str | None = None
    yadif_deint: str | None = None
    deblock_enabled: bool | None = None
    deblock_strength: str | None = None
    deblock_block: int | None = None
    nlmeans_enabled: bool | None = None
    nlmeans_strength: str | None = None
    nlmeans_profile: str | None = None
    denoise_filter: Literal["mvo-r2", "nlmeans"] | None = None
    chroma_smooth_enabled: bool | None = None
    chroma_smooth_strength: str | None = None


@dataclass(frozen=True)
class InterpolationSpec(Model):
    """Interpolation MVO-RIFE : ``factor`` ou ``target_fps`` (prioritaire)."""

    enabled: bool | None = None
    factor: float | None = field(default=None, minimum=1)
    target_fps: str | None = field(default=None, description="Cadence cible, ex. 60000/1001.")
    quality: Literal["fast", "balanced", "quality", "ultra", "light"] | None = None
    mode: Literal["normal", "fast"] | None = None
    tta: int | None = None
    scene_threshold: float | None = None
    gpu: int | None = None


@dataclass(frozen=True)
class RestorationSpec(Model):
    """Débruitage MVO-R2."""

    enabled: bool | None = None
    level: Literal["auto", "leger", "moyen", "fort"] | None = None
    mode: Literal["auto", "normal", "fast"] | None = None
    animation: Literal["auto", "on", "off"] | None = None
    sharpen: bool | None = None


@dataclass(frozen=True)
class StaticHdrSpec(Model):
    """HDR10 statique : ``source`` (valeurs de la source), ``manual`` (saisies), ``off`` (non écrit)."""

    mode: Literal["source", "manual", "off"] | None = None
    master_display: str | None = field(default=None, description="G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min) (x265).",
                                       pattern=optional_text_pattern(MASTER_DISPLAY_BODY))
    max_cll: str | None = field(default=None, description="MaxCLL,MaxFALL (nits).",
                                pattern=optional_text_pattern(MAX_CLL_BODY))
    estimate: Literal["fast", "precise"] | None = field(
        default=None, description="Estimation (source sans HDR10 statique, ex. Dolby Vision P5), sur champs vides : "
                                  "exclut mode off et toute valeur saisie ; une couche suivante avec mode la retire.")


@dataclass(frozen=True)
class CopySpec(Model):
    mode: Literal["copy", "off"] | None = None


@dataclass(frozen=True)
class DolbyVisionSpec(CopySpec):
    profile: str | None = field(default=None, description="Profil de sortie (0 = celui de la source, 2…).")


@dataclass(frozen=True)
class FelSpec(Model):
    """Intégration de la couche Dolby Vision FEL (extension mvo-fel)."""

    bake: bool | None = None
    precision: Literal["exact", "standard", "fast"] | None = None
    device: str | None = field(default=None, description="auto, cpu ou identifiant Vulkan.")
    hdr10plus: Literal["recompute", "source"] | None = None


@dataclass(frozen=True)
class TonemapSpec(Model):
    enabled: bool | None = None
    algorithm: str | None = None


@dataclass(frozen=True)
class VideoEncodeSpec(Model):
    """Réglages vidéo publics d'une piste."""

    codec: str | None = field(default=None, description="copy, libx265, hevc_nvenc… (voir `capabilities`).")
    rate_control: str | None = field(default=None, description="Mode de débit du codec (crf, vbr_cq, cbr…).")
    crf: int | None = field(default=None, minimum=0, maximum=63)
    cq: int | None = field(default=None, minimum=0, maximum=63)
    bitrate_kbps: int | None = field(default=None, minimum=1)
    encoder_preset: str | None = field(default=None, description="Préréglage de l'encodeur (slow, p5…).")
    extra_params: str | None = field(default=None, description="Paramètres libres de l'encodeur.")
    bit_depth: Literal["auto", "8", "10"] | None = None
    crop: CropSpec | None = None
    resize: ResizeSpec | None = None
    filters: FilterSpec | None = None
    interpolation: InterpolationSpec | None = None
    restoration: RestorationSpec | None = None
    static_hdr: StaticHdrSpec | None = None
    dolby_vision: DolbyVisionSpec | None = None
    hdr10plus: CopySpec | None = None
    fel: FelSpec | None = None
    tonemap: TonemapSpec | None = None


@dataclass(frozen=True)
class VideoTrackSpec(VideoEncodeSpec):
    """Réglages d'une piste vidéo désignée."""

    select: TrackSelector = field(default_factory=TrackSelector, description="Piste visée (défaut : 1re vidéo).")


@dataclass(frozen=True)
class VideoBlock(Model):
    default: VideoEncodeSpec | None = field(default=None, description="Réglages de toutes les pistes vidéo.")
    tracks: tuple[VideoTrackSpec, ...] = field(default=(), description="Réglages propres à une piste.")


@dataclass(frozen=True)
class AudioTrackSpec(Model):
    """Réencodage d'une piste audio (défaut : copie)."""

    select: TrackSelector
    codec: str = field(description="copy, aac, ac3, eac3, flac.")
    bitrate_kbps: int | None = field(default=None, minimum=1, description="Défaut : selon les canaux.")
    truehd_core: bool = field(default=False, description="Garder le cœur AC-3 d'une piste TrueHD.")


@dataclass(frozen=True)
class EncodeBlock(Model):
    """Bloc ``encode`` d'un job."""

    preset: str | None = field(default=None, description="Preset d'encodage : nom enregistré ou chemin JSON.")
    video: VideoBlock | None = None
    audio: tuple[AudioTrackSpec, ...] = ()
    target_size_mb: int | None = field(default=None, minimum=1, description="Taille du fichier (mode taille).")
    accept_dovi_nvenc_crop: bool = field(
        default=False, description="Accepter le recadrage aligné imposé par Dolby Vision + NVENC.")


def encode_block_issues(block: object, path: str) -> list[tuple[str, str]]:
    """Clés inconnues d'un bloc ``encode`` porté par une requête (``document_issues``).

    Une erreur de type n'est pas reprise ici : la résolution du bloc la refuse avec son message.
    """
    from core.service.errors import ApiError

    if not isinstance(block, dict):
        return []
    try:
        _block, warnings = EncodeBlock.parse(block, strict=False)
    except ApiError:
        return []
    return [(f"{path}.{warning.path}", "clé inconnue du bloc encode") for warning in warnings]


def encode_block_errors(block: object, path: str) -> list[tuple[str, str]]:
    """Erreurs de valeur d'un bloc ``encode`` (types, bornes, énumérations) ; clés inconnues exclues."""
    from core.service.errors import ApiError

    if block is None:
        return []
    try:
        EncodeBlock.parse(block, strict=False)
    except ApiError as error:
        return [(f"{path}.{detail.path}" if detail.path else path, detail.message) for detail in error.details] or [
            (path, error.message)]
    return []
