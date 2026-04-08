"""
core/workflows/encode/models.py — Data models, enums and constants for encoding.

Public:
    QualityMode
    SOFTWARE_VIDEO_CODECS, HARDWARE_VIDEO_CODECS, AUDIO_CODECS
    X265_PRESETS, X264_PRESETS, SVTAV1_PRESETS, NVENC_PRESETS
    TONEMAP_ALGORITHMS
    presets_for_codec()
    VideoEncodeSettings, AudioTrackSettings, EncodeConfig, EncodePreset
    EncodeError
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


# =============================================================================
# Enums et constantes
# =============================================================================

class QualityMode(str, Enum):
    CRF     = "crf"
    BITRATE = "bitrate"
    SIZE    = "size"

    def label(self) -> str:
        return {"crf": "CRF", "bitrate": "Débit (kbps)", "size": "Taille cible (Mo)"}[self.value]


SOFTWARE_VIDEO_CODECS: list[tuple[str, str]] = [
    ("libx265",   "x265 — HEVC (logiciel)"),
    ("libx264",   "x264 — H.264 (logiciel)"),
    ("libsvtav1", "SVT-AV1 (logiciel)"),
]

HARDWARE_VIDEO_CODECS: list[tuple[str, str]] = [
    ("hevc_nvenc",  "NVENC — HEVC (NVIDIA)"),
    ("hevc_amf",    "AMF — HEVC (AMD-WIN)"),
    ("hevc_vaapi",  "VAAPI — HEVC (AMD)"),
    ("hevc_qsv",    "QSV — HEVC (Intel)"),
    ("h264_nvenc",  "NVENC — H.264 (NVIDIA)"),
    ("h264_amf",    "AMF — H.264 (AMD-WIN)"),
    ("h264_vaapi",  "VAAPI — H.264 (AMD)"),
    ("h264_qsv",    "QSV — H.264 (Intel)"),
    ("av1_nvenc",   "NVENC — AV1 (NVIDIA RTX 40+)"),
    ("av1_amf",     "AMF — AV1 (AMD RX 7000+)"),
    ("av1_vaapi",   "VAAPI — AV1 (AMD/Intel)"),
    ("av1_qsv",     "QSV — AV1 (Intel Arc/12e gen+)"),
]

AUDIO_CODECS: list[tuple[str, str]] = [
    ("copy",  "Copie (sans réencodage)"),
    ("aac",   "AAC"),
    ("ac3",   "AC-3 (Dolby Digital)"),
    ("eac3",  "EAC-3 (Dolby Digital+)"),
    ("flac",  "FLAC (sans perte)"),
]

X265_PRESETS   = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                  "medium", "slow", "slower", "veryslow", "placebo"]
X264_PRESETS   = X265_PRESETS
SVTAV1_PRESETS = [str(i) for i in range(13)]   # 0 = qualité max, 12 = vitesse max
NVENC_PRESETS  = ["p1", "p2", "p3", "p4", "p5", "p6", "p7",
                  "slow", "medium", "fast", "hp", "hq"]

TONEMAP_ALGORITHMS = ["hable", "mobius", "reinhard", "gamma", "linear", "clip"]


def presets_for_codec(codec: str) -> list[str]:
    """Retourne la liste de presets appropriée pour le codec donné."""
    if codec == "libsvtav1":
        return SVTAV1_PRESETS
    if codec in ("hevc_nvenc", "h264_nvenc"):
        return NVENC_PRESETS
    if codec in ("hevc_amf", "hevc_qsv", "h264_amf", "h264_qsv"):
        return []   # pas de preset standardisé
    return X265_PRESETS   # libx265, libx264


# =============================================================================
# Dataclasses
# =============================================================================

@dataclass
class VideoEncodeSettings:
    """Paramètres d'encodage vidéo."""
    codec:            str          = "libx265"
    quality_mode:     QualityMode  = QualityMode.CRF
    crf:              int          = 18
    bitrate_kbps:     int          = 5000
    target_size_mb:   int          = 4000
    preset:           str          = "slow"
    extra_params:     str          = ""    # x265-params / svtav1-params passthrough
    # HDR statique
    inject_hdr_meta:  bool         = False
    master_display:   str          = ""   # ex. "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(40000000,50)"
    max_cll:          str          = ""   # ex. "1000,400"
    # Tone mapping
    tonemap_to_sdr:   bool         = False
    tonemap_algorithm: str         = "hable"


@dataclass
class AudioTrackSettings:
    """Paramètres d'encodage pour une piste audio."""
    stream_index:        int         # index global ffprobe dans le fichier source
    codec:               str = "copy"
    bitrate_kbps:        int = 384
    extract_truehd_core: bool = False   # strip Atmos via BSF truehd_core
    input_channels:      int | None = None   # nb de canaux de la piste source (ffprobe)
    input_channel_layout: str | None = None  # layout source (ex: "7.1", "5.1(side)")
    source_path:         Path | None = None  # None = même fichier que la vidéo (config.source)


@dataclass
class TrackMetaEdit:
    """
    Édition de métadonnées d'une piste de sortie, appliquée via mkvpropedit.

    track_order : numéro de piste 1-based dans le fichier de sortie (sélecteur @N).
    language    : balise IETF BCP-47 à écrire, ou "" pour ne pas toucher.
    title       : nom de la piste à écrire, ou None pour ne pas toucher
                  (chaîne vide "" = effacer le titre existant).
    """
    track_order: int
    language:    str        = ""
    title:       str | None = None


@dataclass
class EncodeConfig:
    """Configuration complète d'un encodage."""
    source:           Path
    output:           Path
    video:            VideoEncodeSettings
    audio_tracks:     list[AudioTrackSettings]
    copy_subtitles:   bool         = True
    # Pistes de sous-titres multi-sources : (chemin_source, stream_index_ffprobe)
    # Si non vide, remplace le copy_subtitles générique.
    subtitle_tracks:  list = field(default_factory=list)   # list[tuple[Path, int]]
    keep_chapters:    bool         = True
    #: Chapitres personnalisés à appliquer en post-traitement via mkvpropedit.
    #: None  → comportement keep_chapters (copie depuis la source ou rien).
    #: list  → écrase les chapitres existants avec ces entrées.
    chapter_overrides: list | None = None  # list[ChapterEntry] | None
    # Flux d'attachements à copier : (chemin_source, stream_index_ffprobe)
    # Sélection individuelle — remplace l'ancien attachment_sources global.
    attachment_streams: list = field(default_factory=list)   # list[tuple[Path, int]]
    # Fichiers externes à attacher (ajout manuel, via -attach ffmpeg).
    extra_attachments:  list = field(default_factory=list)   # list[Path]
    # Sources dont on copie les balises MKV (<Tags> element) via mkvpropedit post-traitement.
    tag_sources:      list = field(default_factory=list)    # list[Path]
    #: Balises MKV globales à écrire directement (prioritaire sur tag_sources).
    #: None  → utiliser tag_sources si présents.
    #: dict  → écrire ces balises et ignorer tag_sources.
    #: {}    → supprimer toutes les balises existantes.
    tag_overrides:    dict | None = None                    # dict[str, str] | None
    # Éditions de métadonnées de pistes (langue, titre) appliquées via mkvpropedit.
    track_meta_edits: list = field(default_factory=list)    # list[TrackMetaEdit]
    file_title:       str          = ""     # balise Title du segment de sortie
    duration_s:       float | None = None   # requis pour le mode taille cible
    # Passthrough métadonnées dynamiques (HEVC uniquement)
    copy_dv:          bool         = False  # injecter RPU Dolby Vision via dovi_tool
    copy_hdr10plus:   bool         = False  # injecter HDR10+ SEI via hdr10plus_tool
    dovi_profile:     str          = "0"    # flag -m dovi_tool : "0"=conserver, "2"=normaliser P8.1
    work_dir:         Path | None  = None   # dossier de travail (passlog, fichiers temp)


@dataclass
class EncodePreset:
    """Profil d'encodage sauvegardable en JSON."""
    name:                       str  = "Nouveau profil"
    description:                str  = ""
    codec:                      str  = "libx265"
    quality_mode:               str  = QualityMode.CRF.value
    crf:                        int  = 18
    bitrate_kbps:               int  = 5000
    target_size_mb:             int  = 4000
    preset:                     str  = "slow"
    extra_params:               str  = ""
    inject_hdr_meta:            bool = False
    master_display:             str  = ""
    max_cll:                    str  = ""
    tonemap_to_sdr:             bool = False
    tonemap_algorithm:          str  = "hable"
    default_audio_codec:        str  = "copy"
    default_audio_bitrate_kbps: int  = 384

    def to_video_settings(self) -> VideoEncodeSettings:
        return VideoEncodeSettings(
            codec=self.codec,
            quality_mode=QualityMode(self.quality_mode),
            crf=self.crf,
            bitrate_kbps=self.bitrate_kbps,
            target_size_mb=self.target_size_mb,
            preset=self.preset,
            extra_params=self.extra_params,
            inject_hdr_meta=self.inject_hdr_meta,
            master_display=self.master_display,
            max_cll=self.max_cll,
            tonemap_to_sdr=self.tonemap_to_sdr,
            tonemap_algorithm=self.tonemap_algorithm,
        )


# =============================================================================
# Exception
# =============================================================================

class EncodeError(RuntimeError):
    """Erreur levée lors de la validation ou de l'exécution d'un encodage."""
