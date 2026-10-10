"""Remplacement de l'entrée vidéo par le producteur FEL, sans fichier d'images."""
from __future__ import annotations

from dataclasses import replace

from core.pipeline_command import PipelineCommand
from core.workflows.encode.models import VideoEncodeSettings
from core.workflows.encode.domain.codecs import resolve_output_bit_depth

RGB_TO_YUV = (
    "setparams=range=full:color_primaries=bt2020:color_trc=smpte2084:colorspace=gbr,"
    "zscale=matrix=bt2020nc:range=limited:dither=error_diffusion,format=yuv444p16le"
)


def with_fel_input(command: list[str], video: VideoEncodeSettings, threads: int | None = None) -> list[str]:
    """Le premier étage reçoit le NUT ; les filtres existants traitent ensuite les pixels reconstruits."""
    context = video.fel_context
    if context is None:
        return command
    upstream = [list(s) for s in command.upstream] if isinstance(command, PipelineCommand) else []
    first = upstream[0] if upstream else list(command)
    index = first.index("-i")
    # Le décodage appartient au moteur natif, pas à l'accélérateur de FFmpeg.
    before = first[:index]
    for flag in ("-hwaccel", "-hwaccel_output_format", "-hwaccel_device", "-r", "-f"):
        while flag in before:
            pos = before.index(flag)
            del before[pos:pos+2]
    first[:] = [*before, "-f", "nut", "-i", "pipe:0", *first[index+2:]]
    if "-map" in first:
        first[first.index("-map")+1] = "0:0"
    if "-vf" in first:
        pos = first.index("-vf")+1
        first[pos] = RGB_TO_YUV + "," + first[pos]
    else:
        # Certains constructeurs ont déjà posé la sortie « - » (NVEncC/RIFE).
        # Une option ajoutée après cette sortie serait ignorée par FFmpeg.
        position = first.index("-i") + 2
        first[position:position] = ["-vf", RGB_TO_YUV]
    pos = first.index("-vf") + 1
    depth = resolve_output_bit_depth(video)
    reduce = f"zscale=dither=error_diffusion,format=yuv420p{'10le' if depth == 10 else ''}"
    # Avant l'upload matériel ; sinon à la fin du traitement logiciel/RIFE.
    chain = first[pos]
    hardware = next((marker for marker in (",format=p010,hwupload", ",format=nv12,hwupload") if marker in chain), "")
    first[pos] = chain.replace(hardware, "," + reduce + hardware) if hardware else chain + "," + reduce
    source = replace(context.source, threads=max(1, threads)) if threads else context.source
    return PipelineCommand(command if upstream else first, upstream, producer=source)
