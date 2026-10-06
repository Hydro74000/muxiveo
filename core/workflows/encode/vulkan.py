"""Capacité Vulkan de FFmpeg (filtres GPU portables), sondée sur le matériel réel.

Utilisée pour le débruitage NLMeans en 10 bits : ``nlmeans`` (CPU) n'accepte que
le 8 bits, ``nlmeans_vulkan`` garde la précision. Images envoyées en ``p010le``
(10 bits alignés en haut) : en ``yuv420p10le`` le shader lit des valeurs 64 fois
plus petites et la force n'agit plus (contraste écrasé, constaté FFmpeg 8.1).

Choix du GPU (banc du 2026-10-06, NLMeans 1080p, 48 images) : RTX 4070 Ti 1,7 s,
CPU 16 threads 24 s, iGPU AMD (RADV) 38 s et perte du périphérique en 4K
(chien de garde du pilote). Seul un GPU dédié est retenu, désigné par son index
(FFmpeg prend sinon le périphérique 0, parfois l'iGPU) ; jamais llvmpipe.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from core.subprocess_utils import subprocess_text_kwargs

_LISTING_RE = re.compile(
    r"^\[Vulkan @ [^\]]+\]\s+(\d+):\s+(.+?)\s+\((discrete|integrated|virtual|software|cpu|other)\)",
    re.MULTILINE,
)

# Tampon intégral de nlmeans_vulkan (FFmpeg 8.1) : largeur × hauteur × 16 octets
# × 3 plans, par unité de parallélisme ``t`` ; plus deux tampons de poids
# (≈ 12 octets par pixel × t). Budget prudent d'une allocation : 2 Gio.
_INTEGRAL_BYTES_PER_PIXEL = 16 * 3
_ALLOCATION_BUDGET = 2 << 30
# Au-delà de 2, aucun gain mesuré sur GPU dédié (1080p : t=1 2,0 s ; t=2 1,7 s ;
# t=4 1,7 s ; t=8 1,9 s) ; 4K : t=8 épuise l'allocation (ENOMEM, 16 Go de VRAM).
_BEST_PARALLELISM = 2


@dataclass(frozen=True)
class VulkanDevice:
    index: int
    name: str
    #: "discrete" | "integrated" | "virtual" | "software" | "cpu" | "other"
    kind: str


@dataclass(frozen=True)
class VulkanCapability:
    """Vulkan utilisable par FFmpeg pour les filtres GPU (GPU dédié retenu)."""

    available: bool = False
    #: GPU retenu (nom) et son index Vulkan ; vide / None sans GPU dédié.
    device: str = ""
    index: int | None = None
    #: ``nlmeans_vulkan`` fonctionnel sur ce GPU (une image 10 bits débruitée).
    nlmeans: bool = False
    reason: str = ""
    devices: tuple[VulkanDevice, ...] = ()


def parse_vulkan_devices(output: str) -> tuple[VulkanDevice, ...]:
    """Liste « GPU listing » du journal FFmpeg (``-v verbose``)."""
    return tuple(
        VulkanDevice(index=int(index), name=name.strip(), kind=kind)
        for index, name, kind in _LISTING_RE.findall(output or "")
    )


def choose_vulkan_device(devices: tuple[VulkanDevice, ...]) -> VulkanDevice | None:
    """Premier GPU dédié ; iGPU (plus lent que le CPU, instable en 4K) et rendu logiciel exclus."""
    return next((device for device in devices if device.kind == "discrete"), None)


def vulkan_parallelism(width: int, height: int) -> int:
    """``t`` de nlmeans_vulkan : 2 (optimum mesuré), 1 si le tampon dépasserait le budget.

    Dimensions inconnues : 1 (toujours sûr ; sortie identique quel que soit ``t``).
    """
    pixels = int(width or 0) * int(height or 0)
    if pixels <= 0:
        return 1
    fitting = _ALLOCATION_BUDGET // (pixels * _INTEGRAL_BYTES_PER_PIXEL)
    return max(1, min(_BEST_PARALLELISM, int(fitting)))


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(cmd, capture_output=True, check=False, timeout=timeout, **subprocess_text_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return None


def detect_vulkan(ffmpeg_bin: str) -> VulkanCapability:
    """GPU dédié Vulkan et ``nlmeans_vulkan`` fonctionnel dessus (≈ 1 s, au lancement)."""
    filters = _run([ffmpeg_bin, "-hide_banner", "-filters"], 15)
    if filters is None:
        return VulkanCapability(reason="FFmpeg introuvable")
    if not re.search(r"\bnlmeans_vulkan\b", filters.stdout or ""):
        return VulkanCapability(reason="FFmpeg compilé sans filtres Vulkan")
    listing = _run(
        [ffmpeg_bin, "-hide_banner", "-nostdin", "-v", "verbose", "-init_hw_device", "vulkan=vk",
         "-f", "lavfi", "-i", "nullsrc=s=16x16", "-frames:v", "1", "-f", "null", "-"],
        30,
    )
    devices = parse_vulkan_devices(((listing.stdout or "") + (listing.stderr or "")) if listing else "")
    if not devices:
        return VulkanCapability(reason="aucun périphérique Vulkan")
    chosen = choose_vulkan_device(devices)
    if chosen is None:
        return VulkanCapability(
            reason="aucun GPU dédié (GPU intégré plus lent que le CPU pour NLMeans)", devices=devices,
        )
    probe = _run(
        [ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error",
         "-init_hw_device", f"vulkan=vk:{chosen.index}", "-filter_hw_device", "vk",
         "-f", "lavfi", "-i", "color=c=gray:s=64x64,format=p010le", "-frames:v", "1",
         "-vf", "hwupload,nlmeans_vulkan,hwdownload,format=p010le", "-f", "null", "-"],
        60,
    )
    if probe is None or probe.returncode != 0:
        lines = [line for line in ((probe.stderr or "") if probe else "").splitlines() if line.strip()]
        return VulkanCapability(
            available=True, device=chosen.name, index=chosen.index, devices=devices,
            reason=lines[-1] if lines else "sonde nlmeans_vulkan en échec",
        )
    return VulkanCapability(available=True, device=chosen.name, index=chosen.index, nlmeans=True, devices=devices)


__all__ = [
    "VulkanCapability",
    "VulkanDevice",
    "choose_vulkan_device",
    "detect_vulkan",
    "parse_vulkan_devices",
    "vulkan_parallelism",
]
