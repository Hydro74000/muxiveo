from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Callable

from core.file_types import windows_filename_error, windows_path_length_error
from core.workflows.common.track_types import TrackTimeOffset
from core.workflows.common.path_safety import same_filesystem_target
from core.workflows.encode.planning.sources import resolve_source_layout
from core.workflows.encode.catalog import (
    resolve_rate_control,
    supports_dovi,
    supports_hdr10plus,
    supports_hdr_output,
)
from core.workflows.encode.domain.codecs import bit_depth_error, extra_params_syntax_error, preset_problem
from core.workflows.encode.models import EncodeConfig, QualityMode, VideoEncodeSettings
from core.workflows.encode.planning.plan_models import PlannedVideoTrack

_MASTER_DISPLAY_RE = re.compile(r"^G\(\d+,\d+\)B\(\d+,\d+\)R\(\d+,\d+\)WP\(\d+,\d+\)L\(\d+,\d+\)$")
_MAX_CLL_RE = re.compile(r"^\d+,\d+$")


def is_dir_writable(path: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path,
            prefix="muxiveo_write_probe_",
            delete=True,
        ):
            pass
        return True
    except OSError:
        return False


def validate_encode_config(
    config: EncodeConfig,
    *,
    planned_video_tracks: tuple[PlannedVideoTrack, ...] | None = None,
    video_tracks: list[VideoEncodeSettings] | None = None,
    video_source_from_settings: Callable[[EncodeConfig, VideoEncodeSettings], Path] | None = None,
    dir_writable: Callable[[Path], bool] = is_dir_writable,
) -> list[str]:
    errors: list[str] = []
    if not config.source.is_file():
        errors.append(f"Fichier source introuvable : {config.source}")
    if planned_video_tracks is None:
        if video_tracks is None or video_source_from_settings is None:
            raise ValueError("planned_video_tracks or (video_tracks + video_source_from_settings) is required")
        planned_video_tracks = tuple(
            PlannedVideoTrack(
                source=Path(video_source_from_settings(config, video)),
                stream_index=int(getattr(video, "stream_index", 0) or 0),
                codec=str(video.codec),
                quality_mode=video.quality_mode,
                inject_hdr_meta=bool(video.inject_hdr_meta),
                has_transform=bool(video.has_video_transform()),
                tonemap_to_sdr=bool(video.tonemap_to_sdr),
                copy_dv=bool(video.copy_dv),
                copy_hdr10plus=bool(video.copy_hdr10plus),
                master_display=str(video.master_display or ""),
                max_cll=str(video.max_cll or ""),
                static_hdr_analysis_request=str(
                    video.static_hdr_metadata_analysis_request or ""
                ),
            )
            for video in video_tracks
        )
    if not planned_video_tracks:
        errors.append("Aucune piste vidéo sélectionnée.")
        return errors

    for index, video in enumerate(planned_video_tracks, start=1):
        source = video.source
        if not source.is_file():
            errors.append(f"Piste vidéo #{index} — source introuvable : {source}")
        if video.codec == "copy" and video.has_transform:
            errors.append(
                f"Piste vidéo #{index} — codec copy incompatible avec les transformations vidéo."
            )
        if video.inject_hdr_meta and not video.tonemap_to_sdr and not supports_hdr_output(video.codec):
            errors.append(
                f"Piste vidéo #{index} — Le codec '{video.codec}' ne peut pas porter de métadonnées HDR. "
                "Suggestion : utilisez un codec HEVC/AV1 ou le tone-mapping HDR → SDR."
            )
        if video.tonemap_to_sdr and (video.copy_dv or video.copy_hdr10plus):
            errors.append(
                f"Piste vidéo #{index} — le tone-mapping HDR → SDR est incompatible "
                "avec la copie Dolby Vision / HDR10+."
            )
        if video.copy_dv and not supports_dovi(video.codec):
            if str(video.codec or "").startswith("nvencc_"):
                errors.append(
                    f"Piste vidéo #{index} — {video.codec} ne supporte pas le profil Dolby Vision. "
                    "Suggestion : sélectionnez 'nvencc_hevc'."
                )
            else:
                errors.append(
                    f"Piste vidéo #{index} — L'encodeur '{video.codec}' ne supporte pas l'injection Dolby Vision. "
                    "Suggestion : choisissez un encodeur HEVC (x265, NVEncC, NVENC, QSV, VAAPI, AMF) ou 'copy' (passthrough)."
                )
        if video.copy_hdr10plus and not supports_hdr10plus(video.codec):
            errors.append(
                f"Piste vidéo #{index} — Le codec '{video.codec}' ne supporte pas l'injection HDR10+."
            )
        analysis_request = str(video.static_hdr_analysis_request or "").strip()
        if analysis_request and (
            len(planned_video_tracks) != 1
            or str(video.codec or "").strip().lower().startswith("nvencc_")
        ):
            errors.append(
                f"Piste vidéo #{index} — l’analyse HDR10 P5→P8.1 intégrée "
                "requiert un pipeline vidéo unique FFmpeg/copy."
            )

    # Le muxage final (transaction FFmpeg comme assembleur natif) écrit
    # toujours du Matroska : une extension .mp4/.mov produirait un fichier
    # au contenu incohérent avec son nom.
    if config.output.suffix.lower() != ".mkv":
        errors.append("La sortie d'encodage doit être un fichier .mkv.")
    name_error = windows_filename_error(config.output.name)
    if name_error:
        errors.append(name_error)
    output_dir = config.output.parent
    length_error = windows_path_length_error(config.output)
    if length_error:
        errors.append(length_error)
    elif not output_dir.exists():
        if not bool(getattr(config, "allow_missing_output_dir", False)):
            errors.append(f"Dossier de sortie inexistant : {output_dir}")
    elif not dir_writable(output_dir):
        errors.append(
            "Dossier de sortie non inscriptible : "
            f"{output_dir} (vérifiez les protections Windows sur les dossiers Bibliothèques)."
        )
    input_paths = dict.fromkeys((
        *resolve_source_layout(config).sources,
        *config.tag_sources,
        *config.extra_attachments,
    ))
    for path in input_paths:
        if same_filesystem_target(Path(path), config.output):
            errors.append(
                "Le fichier de sortie doit être différent du fichier source. "
                f"Source concernée : {path}"
            )
    if any(video.quality_mode == QualityMode.SIZE and video.codec != "copy" for video in planned_video_tracks) and not (config.duration_s or 0) > 0:
        errors.append("Durée du fichier source inconnue — mode taille cible impossible.")

    for index, video in enumerate(planned_video_tracks, start=1):
        if video.inject_hdr_meta and not video.tonemap_to_sdr:
            if video.master_display and not _MASTER_DISPLAY_RE.match(video.master_display.strip()):
                errors.append(
                    f"Piste vidéo #{index} — format master_display invalide. "
                    "Attendu : G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)"
                )
            if video.max_cll and not _MAX_CLL_RE.match(video.max_cll.strip()):
                errors.append(
                    f"Piste vidéo #{index} — format MaxCLL invalide. Attendu : MaxCLL,MaxFALL  ex. 1000,400"
                )

    for raw in config.track_time_offsets:
        if not isinstance(raw, TrackTimeOffset):
            continue
        track_type = str(raw.track_type or "").strip().lower()
        if track_type == "video" and int(raw.offset_ms) < 0:
            errors.append(
                "Décalage vidéo négatif interdit : "
                f"source={Path(raw.source_path)}, stream={int(raw.stream_index)}, "
                f"offset={int(raw.offset_ms)} ms"
            )
    return errors


def video_settings_errors(videos: list[VideoEncodeSettings]) -> list[str]:
    """Valeurs de l'onglet Video invalides (débit, taille, paramètres avancés)."""
    errors: list[str] = []
    for index, video in enumerate(videos, start=1):
        if video.codec == "copy":
            continue
        depth_problem = bit_depth_error(video)
        if depth_problem:
            errors.append(f"Piste vidéo #{index} — {depth_problem}")
        preset_error, _preset_warning = preset_problem(video)
        if preset_error:
            errors.append(f"Piste vidéo #{index} — {preset_error}")
        spec = resolve_rate_control(video.codec, video.rate_control, video.quality_mode)
        needs_bitrate = spec.bitrate if spec is not None else video.quality_mode == QualityMode.BITRATE
        minimum = spec.bitrate_minimum if spec is not None else 1
        if needs_bitrate and int(video.bitrate_kbps) < minimum:
            requirement = ">= 0 attendu, 0 = illimité" if minimum == 0 else "> 0 attendu"
            errors.append(f"Piste vidéo #{index} — débit vidéo invalide (kbps {requirement}).")
        if video.quality_mode == QualityMode.SIZE and int(video.target_size_mb) <= 0:
            errors.append(f"Piste vidéo #{index} — taille cible invalide (Mio > 0 attendue).")
        problem = extra_params_syntax_error(video.codec, video.extra_params)
        if problem:
            errors.append(f"Piste vidéo #{index} — paramètres avancés invalides : {problem}.")
    return errors
