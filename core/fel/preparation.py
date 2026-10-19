"""Confirmation au lancement et état de reprise BL d'une piste."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Callable

from core.fel.context import FelExecution
from core.fel.devices import FelDevicePlan
from core.fel.engine import FelCancelled, FelEngine, FelError, FelSource, frame_statistics_supported, precision_supported
from core.runner import TaskCancelledError
from core.workflows.encode.dovi_policy import sub_profile_from_value, wants_fel_bake
from core.workflows.encode.models import VideoEncodeSettings
from core.workflows.encode.runtime.dovi_geometry import extract_dovi_rpu
from core.workflows.encode.runtime.dovi_static_hdr import estimate_static_hdr_from_rpu


CHROMA_LOCATIONS = frozenset({"left", "center", "topleft", "top", "bottomleft", "bottom"})


def supports_chroma_setparams(ffmpeg: str, capture: Callable[[list[str]], str]) -> bool:
    """Sonde l'option réelle, y compris les builds anciens avec rétroportage.

    Appelé seulement pendant la préparation du worker : les constructeurs de
    commandes et les aperçus synchrones de l'interface n'exécutent aucune sonde.
    """
    help_text = str(capture([ffmpeg, "-hide_banner", "-h", "filter=setparams"]) or "")
    return re.search(r"(?m)^\s+chroma_location\s+<", help_text) is not None


def source_chroma_location(ffprobe: str, source: Path, stream: int, capture: Callable[[list[str]], str]) -> str:
    """Position chroma déclarée par la piste ; à défaut celle du HEVC non signalé (left)."""
    text = str(capture([ffprobe, "-v", "error", "-select_streams", str(stream), "-show_entries",
                        "stream=chroma_location", "-of", "json", str(source)]) or "")
    try:
        # JSON : le CSV ajoute des champs vides pour la side data Dolby Vision.
        streams = json.loads(text[text.index("{"):text.rindex("}")+1]).get("streams") or [{}]
        value = str(streams[0].get("chroma_location") or "").lower()
    except (ValueError, AttributeError, IndexError):
        value = ""
    return value if value in CHROMA_LOCATIONS else "left"


def requested(video: VideoEncodeSettings) -> bool:
    return wants_fel_bake(video.bake_dovi_fel, sub_profile_from_value(video.dovi_source_profile), video.codec)


def _hevc_output(video: VideoEncodeSettings) -> bool:
    codec = str(video.codec or "").lower()
    return codec == "libx265" or "hevc" in codec


def recomputes_hdr10plus(video: VideoEncodeSettings) -> bool:
    """HDR10+ recalculé demandé : copie HDR10+ active, sortie HEVC (hdr10plus_tool), choix « recompute »."""
    return bool(video.copy_hdr10plus and video.fel_hdr10plus == "recompute" and _hevc_output(video)
                and not video.tonemap_to_sdr)


#: Provenances des MaxCLL/MaxFALL déduites automatiquement (source, RPU) : remplacées par la mesure.
_AUTOMATIC_LIGHT_LEVELS = {"source", "rpu_estimate", "source_metadata"}


def measures_light_levels(video: VideoEncodeSettings) -> bool:
    """MaxCLL/MaxFALL exacts de l'image reconstruite : HDR10 statique écrit, valeurs non saisies, sortie HEVC.

    Les valeurs de la source décrivent la BL ; le plugin mesure l'image produite et les valeurs exactes
    remplacent celles du flux après l'encodage (SEI CLL et élément Colour).
    """
    provenance = video.static_hdr_light_level_source or video.static_hdr_metadata_source
    return bool(video.inject_hdr_meta and not video.tonemap_to_sdr and _hevc_output(video)
                and (not video.max_cll or provenance in _AUTOMATIC_LIGHT_LEVELS))


def prepare_fel(
    video: VideoEncodeSettings, *, source: Path, work_dir: Path, index: int,
    ffmpeg: str, dovi: str, threads: int, run: Callable[[list[str]], object],
    ffprobe: str = "ffprobe",
    capture: Callable[[list[str]], str], cancelled: Callable[[], bool], log: Callable[[str, str], None],
    engine_loader: Callable[[], FelEngine] | None = None,
) -> VideoEncodeSettings:
    if not requested(video):
        return video
    prefix = f"Piste vidéo #{index} — "
    try:
        engine = (engine_loader or FelEngine.installed)()
        rpu = work_dir / f"fel_original_{index}.bin"
        extract_dovi_rpu(source=source, stream_index=video.stream_index, ffmpeg_bin=ffmpeg,
                         dovi_tool_bin=dovi, output_rpu=rpu, work_dir=work_dir,
                         run_cmd=run, cleanup_paths=[], mode="0")
        kind = engine.classify(rpu, cancelled)
        if kind != "fel":
            log("INFO", prefix + f"Reconstruction FEL sans effet ({kind}).")
            return replace(video, bake_dovi_fel=False)
        devices = engine.devices()
        choice = video.fel_device
        if choice not in {"auto", "cpu"} and not any(d.uuid == choice for d in devices):
            log("WARN", prefix + "GPU FEL mémorisé indisponible : choix automatique pour cette exécution.")
            choice = "auto"
        plan = FelDevicePlan(choice, devices, video, log, precision_supported=precision_supported(engine))
        # Interpolation RIFE : elle limite le débit et partage le GPU ; une seule image FEL en vol
        # suffit (mesures de bout en bout du plugin) et économise la mémoire vidéo.
        workers = 1 if video.interpolation.enabled else None
        statistics: Path | None = None
        if recomputes_hdr10plus(video) or measures_light_levels(video):
            if frame_statistics_supported(engine):
                statistics = work_dir / f"fel_statistics_{index}.jsonl"
            else:
                log("WARN", prefix + "Extension mvo-fel antérieure à 0.3.0 : HDR10+ et MaxCLL/MaxFALL de la source "
                    "conservés (mettre à jour l'extension pour les mesurer sur l'image reconstruite).")
        context = FelExecution(FelSource(engine, source, video.stream_index, max(1, threads), video.input_frame_rate, plan,
                                         precision=video.fel_precision, render_workers=workers,
                                         statistics_path=statistics),
                               rpu, video.max_cll, video.static_hdr_light_level_source,
                               source_chroma_location(ffprobe, source, video.stream_index, capture),
                               supports_chroma_setparams(ffmpeg, capture), statistics)
        result = replace(video, fel_context=context, source_color_transfer="smpte2084")
        if statistics is not None:
            # MaxCLL/MaxFALL exacts mesurés pendant l'encodage, insérés ensuite : rien de provisoire
            # n'est transmis à l'encodeur.
            if measures_light_levels(video):
                result = replace(result, max_cll="", static_hdr_light_level_source="fel_frame_statistics")
            log("INFO", prefix + ("HDR10+ et MaxCLL/MaxFALL recalculés" if recomputes_hdr10plus(video)
                                  else "MaxCLL/MaxFALL mesurés") + " sur l'image reconstruite (statistiques du plugin).")
        elif video.inject_hdr_meta and not video.tonemap_to_sdr:
            if video.max_cll:
                # Valeurs de la source (mesurées sur le master) : bien plus proches que le L1 du RPU, maximum
                # robuste et moyenne de codes PQ qui sous-estiment MaxCLL et MaxFALL.
                log("INFO", prefix + f"MaxCLL/MaxFALL de la source conservés : {video.max_cll}.")
            else:
                estimate = estimate_static_hdr_from_rpu(dovi, rpu, capture, prefer_l1=True)
                result = replace(result, max_cll=estimate.max_cll, static_hdr_light_level_source="fel_rpu_l1_estimate")
                log("WARN", prefix + f"MaxCLL/MaxFALL absents de la source, estimés depuis L1 (sous-estimés) : "
                    f"{estimate.max_cll or 'indisponibles'}.")
        if video.copy_hdr10plus and (statistics is None or not recomputes_hdr10plus(video)):
            log("WARN", prefix + "HDR10+ conservé depuis la source : il décrit la BL, pas l'image reconstruite.")
        log("INFO", prefix + "FEL confirmé : reconstruction active avant les filtres et l'encodage.")
        return result
    except FelCancelled as exc:
        raise TaskCancelledError() from exc
    except (FelError, OSError, RuntimeError) as exc:
        if cancelled() or isinstance(exc, TaskCancelledError):
            raise TaskCancelledError() from exc
        log("WARN", prefix + f"Reconstruction indisponible ({exc}) : repli BL.")
        return replace(video, bake_dovi_fel=False, fel_context=None)


def fallback_video(video: VideoEncodeSettings) -> VideoEncodeSettings:
    context = video.fel_context
    if context is None:
        return video
    return replace(video, fel_context=None, bake_dovi_fel=False,
                   max_cll=context.original_max_cll, static_hdr_light_level_source=context.original_light_source)
