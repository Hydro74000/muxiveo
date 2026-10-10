"""Raccordement FEL Vulkan/CUDA expérimental, encodeur FFmpeg conservé tel quel."""
from __future__ import annotations

import re
import sys
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from core.fel.devices import pipeline_devices, probe_load
from core.pipeline_command import PipelineCommand
from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.encode.domain.codecs import resolve_output_bit_depth

if TYPE_CHECKING:
    from core.workflows.encode.models import VideoEncodeSettings


def filter_string(value: str) -> str:
    """Échappe les deux niveaux FFmpeg : valeur AVOption puis graphe."""
    escaped = "".join("\\"+c if c in "\\':" else c for c in value)
    return "".join("\\"+c if c in "\\'[],;" else c for c in escaped)


def compatible_driver(uuid: str) -> bool:
    """Les en-têtes NVENC 13 épinglés exigent un pilote Linux 570 ou supérieur."""
    key = f"GPU-{uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]}"
    try:
        result = subprocess.run(  # nosec B603
            ["nvidia-smi", f"--id={key}", "--query-gpu=driver_version", "--format=csv,noheader"],
            timeout=2, check=False, capture_output=True, **subprocess_text_kwargs(),
        )
        return result.returncode == 0 and int(result.stdout.strip().split(".")[0]) >= 570
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def direct_filter(chain: str) -> str | None:
    """Accepter seulement une géométrie sans rééchantillonnage et les tags HDR."""
    crop = ""
    for item in chain.split(",") if chain else []:
        if item.startswith("setparams="):
            if not all(p in {"color_primaries=bt2020", "color_trc=smpte2084", "colorspace=bt2020nc",
                             "range=tv", "range=limited"}
                       for p in item.removeprefix("setparams=").split(":")):
                return None
        elif item in {"sidedata=mode=delete:type=MASTERING_DISPLAY_METADATA",
                      "sidedata=mode=delete:type=CONTENT_LIGHT_LEVEL",
                      "format=p010le", "format=p010", "format=yuv420p10le"}:
            continue
        elif match := re.fullmatch(r"crop=iw-(\d+)-(\d+):ih-(\d+)-(\d+):(\d+):(\d+)", item):
            if crop:
                return None
            left, right, top, bottom, x, y = map(int, match.groups())
            if (x, y) != (left, top) or any(p % 2 for p in (left, right, top, bottom)):
                return None
            crop = f":crop_x={left}:crop_y={top}:crop_w=iw-{left}-{right}:crop_h=ih-{top}-{bottom}:w=iw-{left}-{right}:h=ih-{top}-{bottom}"
        else:
            return None
    return ("libplacebo=inherit_device=1:format=p010le:colorspace=bt2020nc:color_primaries=bt2020:"
            "color_trc=smpte2084:range=limited:apply_dolbyvision=0:apply_filmgrain=0:"
            "peak_detect=0:tonemapping=clip:gamut_mode=clip:contrast_recovery=0:chroma_location=topleft"
            + crop + ",mvo_fel_cuda")


class DirectFelCommand(PipelineCommand):
    """Réservation au lancement ; mêmes options d'encodage et repli NUT si inadapté."""

    def __init__(self, fallback: PipelineCommand, original: list[str], video: VideoEncodeSettings,
                 binary: Path, conversion: str, threads: int | None):
        super().__init__(fallback, fallback.upstream, producer=fallback.producer)
        self.original = original
        self.video = video
        self.binary = binary
        self.conversion = conversion
        self.threads = threads
        self.base_size = len(fallback)

    def display(self) -> str:
        return "[FEL direct Vulkan/CUDA si compatible] → " + " ".join(self)

    @contextmanager
    def execution(self) -> Iterator[list[str]]:
        context = self.video.fel_context
        assert context is not None and context.source.device_plan is not None
        source = context.source
        plan = source.device_plan
        assert plan is not None
        key, release = plan.acquire()
        try:
            chosen = next((d for d in plan.devices if d.uuid == key), None)
            occupied = pipeline_devices(self.video, plan.devices, probe_load(plan.devices),
                                        (tuple([*self.original, *self[self.base_size:]]),))
            if not chosen or chosen.vendor != 0x10DE or occupied != {key} or not compatible_driver(key):
                release()
                plan.log("INFO", "FEL — transport NUT : placement GPU ou pilote incompatible avec le raccordement CUDA direct.")
                yield PipelineCommand(self, self.upstream, producer=self.producer)
                return
            uuid = f"{key[:8]}-{key[8:12]}-{key[12:16]}-{key[16:20]}-{key[20:]}"
            graph = (f"mvo_fel=source={filter_string(str(source.path.resolve()))}:"
                     f"library={filter_string(str(source.engine.path.resolve()))}:stream={source.stream}:"
                     f"threads={max(1,self.threads or source.threads)},{self.conversion}[fel]")
            first = self.original
            index = first.index("-i")
            before = list(first[1:index])
            for flag in ("-hwaccel", "-hwaccel_device", "-hwaccel_output_format", "-f", "-r"):
                while flag in before:
                    pos = before.index(flag)
                    del before[pos:pos+2]
            after = list(first[index+2:])
            if "-vf" in after:
                pos = after.index("-vf")
                del after[pos:pos+2]
            if "-map" in after:
                after[after.index("-map")+1] = "[fel]"
            suffix = list(self[self.base_size:])
            # CUDA est le transport ; le contenu du pool reste P010 10 bits.
            for args in (after, suffix):
                for i, arg in enumerate(args[:-1]):
                    if arg in {"-pix_fmt", "-pix_fmt:v"}:
                        args[i+1] = "cuda"
            plan.log("INFO", "FEL — transport direct Vulkan/CUDA ; aucun NUT ni transfert intermédiaire en RAM.")
            yield [str(self.binary), *before, "-init_hw_device", f"vulkan=fel:{uuid},disable_multiplane=1",
                   "-filter_hw_device", "fel", "-filter_complex", graph, *after, *suffix]
        finally:
            release()


def direct_command(fallback: PipelineCommand, original: list[str], video: VideoEncodeSettings,
                   threads: int | None) -> list[str]:
    context = video.fel_context
    binary = getattr(context.source.engine, "direct_ffmpeg", None) if context else None
    if (sys.platform != "linux" or not isinstance(binary, Path) or not binary.is_file() or context is None
            or context.source.device_plan is None or video.codec != "hevc_nvenc"
            or video.interpolates() or video.tonemap_to_sdr or video.p5_to_hdr10 or fallback.upstream):
        return fallback
    if video.extra_params.strip() or any(flag in original for flag in ("-itsoffset", "-ss", "-filter_complex")):
        return fallback
    if resolve_output_bit_depth(video) != 10:
        return fallback
    if any(original[i+1] not in {"p010", "p010le", "yuv420p10le", "cuda"}
           for i, arg in enumerate(original[:-1]) if arg in {"-pix_fmt", "-pix_fmt:v"}):
        return fallback
    chain = original[original.index("-vf")+1] if "-vf" in original else ""
    conversion = direct_filter(chain)
    return DirectFelCommand(fallback, original, video, binary, conversion, threads) if conversion else fallback
