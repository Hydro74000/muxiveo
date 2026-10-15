"""Remplacement de l'entrée vidéo par le producteur FEL, sans fichier d'images."""
from __future__ import annotations

import re
from dataclasses import replace

from core.fel.engine import FelSource
from core.pipeline_command import PipelineCommand
from core.workflows.encode.models import VideoEncodeSettings
from core.workflows.encode.domain.codecs import resolve_output_bit_depth

RGB_TO_YUV = (
    "setparams=range=full:color_primaries=bt2020:color_trc=smpte2084:colorspace=gbr,"
    "zscale=matrix=bt2020nc:range=limited:dither=error_diffusion,format=yuv444p16le"
)


# Étages sans effet sur les pixels : métadonnées, recadrage pair, format déjà final.
_PASSTHROUGH = re.compile(
    r"crop=iw-(\d+)-(\d+):ih-(\d+)-(\d+):(\d+):(\d+)|setparams=[^,]*|sidedata=mode=delete:type=[A-Z_]+"
    r"|format=(?:yuv420p10le|yuv420p10|p010le|p010|yuv420p|nv12)")
_UPLOADS = (",format=p010,hwupload", ",format=nv12,hwupload")
# Tramage déterministe des sorties finales (identique CPU/GPU, deux passes identiques).
FINAL_DITHER = "blue"


def listed_capability(engine: object, key: str) -> tuple[str, ...]:
    """Valeurs d'une capacité-liste du plugin ; vide si absente ou d'un autre type."""
    capabilities = getattr(engine, "capabilities", None)
    value = capabilities.get(key) if isinstance(capabilities, dict) else None
    return tuple(str(item) for item in value) if isinstance(value, (list, tuple)) else ()


def final_output_format(chain: str, depth: int) -> str | None:
    """Format final que le plugin peut produire lui-même, sinon None (RGB 16 bits).

    Le point de quantification actuel est la fin de la chaîne logicielle ou l'upload
    matériel : seuls les étages qui précèdent ce point doivent laisser les pixels intacts.
    """
    prefix = "," + chain
    for marker in _UPLOADS:
        prefix = prefix.split(marker)[0]
    for item in filter(None, prefix.split(",")):
        match = _PASSTHROUGH.fullmatch(item)
        if match is None:
            return None
        # 4:2:0 : un recadrage impair déplacerait la grille chroma.
        if item.startswith("crop=") and any(int(value) % 2 for value in match.groups()[:6]):
            return None
    return {10: "yuv420p10le", 8: "yuv420p"}.get(depth)


# Options d'un étage FFmpeg NUT → NUT sans autre effet (valeur attendue ou None).
_FEED_FLAGS = {"-hide_banner": False, "-y": False, "-nostdin": False, "-loglevel": True, "-i": True,
               "-chroma_sample_location": True, "-map": True, "-fps_mode": True, "-c:v": True, "-f": True,
               "-vf": True}


def direct_feed(command: object) -> FelSource | None:
    """Producteur à brancher directement sur l'encodeur si l'étage FFmpeg ne fait que recopier le NUT.

    Images finales et simple marquage : l'encodeur (NVEncC) peut lire le NUT du plugin.
    Les PTS deviennent relatifs à la première image, comme à la sortie de cet étage.
    """
    if not isinstance(command, PipelineCommand) or command.producer is None or command.upstream:
        return None
    source = command.producer
    if source.pixel_format == "gbrp16le" or "zero" not in listed_capability(source.engine, "timestamps"):
        return None
    args = list(command)
    if len(args) < 2 or args[-1] != "-":
        return None
    values: dict[str, list[str]] = {}
    position = 1
    while position < len(args) - 1:
        flag = args[position]
        if flag not in _FEED_FLAGS:
            return None
        if _FEED_FLAGS[flag]:
            if position + 1 >= len(args) - 1:
                return None
            values.setdefault(flag, []).append(args[position + 1])
            position += 2
        else:
            position += 1
    if (values.get("-i") != ["pipe:0"] or values.get("-f") != ["nut", "nut"]
            or values.get("-c:v", ["rawvideo"]) != ["rawvideo"] or values.get("-map", ["0:0"]) != ["0:0"]
            or values.get("-fps_mode", ["passthrough"]) != ["passthrough"]):
        return None
    if any(not item.startswith("setparams=") for chain in values.get("-vf", []) for item in chain.split(",")):
        return None
    return replace(source, timestamps="zero")


def plugin_crop(chain: str) -> tuple[str, tuple[int, int, int, int]] | None:
    """Recadrage pair d'une chaîne finale confié au plugin : (chaîne sans le crop, marges)."""
    items = chain.split(",") if chain else []
    crops = [(index, _PASSTHROUGH.fullmatch(item)) for index, item in enumerate(items) if item.startswith("crop=")]
    if len(crops) != 1 or crops[0][1] is None:
        return None
    index, match = crops[0]
    left, right, top, bottom, x, y = map(int, match.groups()[:6])
    if (x, y) != (left, top) or any(value % 2 for value in (left, right, top, bottom)):
        return None
    # Le crop doit précéder le point de quantification (upload matériel éventuel).
    prefix = ("," + chain).split(_UPLOADS[0])[0].split(_UPLOADS[1])[0]
    if items[index] not in prefix.split(","):
        return None
    return ",".join(items[:index] + items[index+1:]), (left, top, right, bottom)


def seek_microseconds(value: str) -> int | None:
    """Durée FFmpeg (secondes ou [HH:]MM:SS[.ms]) en microsecondes ; None si illisible."""
    match = re.fullmatch(r"(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d*)?|\.\d+)", value.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    if hours is not None and minutes is None:
        hours, minutes = None, hours
    total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds)
    return round(total * 1_000_000)


def with_fel_input(command: list[str], video: VideoEncodeSettings, threads: int | None = None) -> list[str]:
    """Le premier étage reçoit le NUT ; les filtres existants traitent ensuite les pixels reconstruits."""
    context = video.fel_context
    if context is None:
        return command
    original = list(command)
    if context.source.device_plan is not None:
        # Les capacités/filtres matériels sont maintenant résolus par le workflow.
        context.source.device_plan.video = video
        stages = [*command.upstream, list(command)] if isinstance(command, PipelineCommand) else [command]
        context.source.device_plan.commands = tuple(tuple(s) for s in stages)
    upstream = [list(s) for s in command.upstream] if isinstance(command, PipelineCommand) else []
    first = upstream[0] if upstream else list(command)
    index = first.index("-i")
    # Le décodage appartient au moteur natif, pas à l'accélérateur de FFmpeg.
    before = first[:index]
    for flag in ("-hwaccel", "-hwaccel_output_format", "-hwaccel_device", "-r", "-f"):
        while flag in before:
            pos = before.index(flag)
            del before[pos:pos+2]
    source = context.source
    if "-ss" in before and source.engine.capabilities.get("seek") == 1:
        # Le plugin se positionne lui-même : sinon FFmpeg jetterait toutes les
        # images reconstruites depuis le début du fichier jusqu'au point demandé.
        pos = before.index("-ss")
        start = seek_microseconds(before[pos+1]) if pos + 1 < len(before) else None
        if start is not None:
            del before[pos:pos+2]
            source = replace(source, start_us=start)
    # Signalisation explicite : hevc_nvenc ignore la position chroma portée par les images.
    first[:] = [*before, "-f", "nut", "-i", "pipe:0", "-chroma_sample_location", context.chroma_location,
                *first[index+2:]]
    if "-map" in first:
        first[first.index("-map")+1] = "0:0"
    depth = resolve_output_bit_depth(video)
    chain = first[first.index("-vf")+1] if "-vf" in first else ""
    final = (final_output_format(chain, depth)
             if "yuv420p10le" in listed_capability(source.engine, "output_formats") else None)
    if final is not None:
        # Images déjà finales (Y'CbCr 4:2:0 tramé par le plugin) : ni conversion ni second tramage.
        tags = ("setparams=range=tv:color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:"
                f"chroma_location={context.chroma_location}")
        moved = plugin_crop(chain) if source.engine.capabilities.get("crop") == 1 else None
        if moved is not None:
            # Recadrage dans le plugin : images plus petites et étage FFmpeg réduit au marquage.
            chain, margins = moved
            first[first.index("-vf")+1] = chain
            source = replace(source, crop=margins)
        if "-vf" in first:
            first[first.index("-vf")+1] = tags + ("," + chain if chain else "")
        else:
            position = first.index("-i") + 4
            first[position:position] = ["-vf", tags]
        source = replace(source, pixel_format=final, chroma_location=context.chroma_location, dither=FINAL_DITHER)
        source = replace(source, threads=max(1, threads)) if threads else source
        result = PipelineCommand(command if upstream else first, upstream, producer=source)
        from core.fel.direct import direct_command
        return direct_command(result, original, video, threads)
    if "-vf" in first:
        pos = first.index("-vf")+1
        first[pos] = RGB_TO_YUV + "," + first[pos]
    else:
        # Certains constructeurs ont déjà posé la sortie « - » (NVEncC/RIFE).
        # Une option ajoutée après cette sortie serait ignorée par FFmpeg.
        position = first.index("-i") + 2
        first[position:position] = ["-vf", RGB_TO_YUV]
    pos = first.index("-vf") + 1
    # Sous-échantillonnage à la position chroma du BL : celle que signalent les encodeurs.
    # zscale ne marque pas les images : sans setparams, l'encodeur signalerait « left ».
    reduce = (f"zscale=dither=error_diffusion:chromal={context.chroma_location},"
              f"format=yuv420p{'10le' if depth == 10 else ''},"
              f"setparams=chroma_location={context.chroma_location}")
    # Avant l'upload matériel ; sinon à la fin du traitement logiciel/RIFE.
    chain = first[pos]
    hardware = next((marker for marker in (",format=p010,hwupload", ",format=nv12,hwupload") if marker in chain), "")
    first[pos] = chain.replace(hardware, "," + reduce + hardware) if hardware else chain + "," + reduce
    source = replace(source, threads=max(1, threads)) if threads else source
    result = PipelineCommand(command if upstream else first, upstream, producer=source)
    from core.fel.direct import direct_command
    return direct_command(result, original, video, threads)
