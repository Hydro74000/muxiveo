"""Choix matériel FEL par UUID, occupation du pipe et réservations concurrentes."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from core.subprocess_utils import subprocess_text_kwargs

if TYPE_CHECKING:
    from core.workflows.encode.models import VideoEncodeSettings


@dataclass(frozen=True)
class FelDevice:
    uuid: str
    name: str
    vendor: int
    index: int
    kind: str
    memory_bytes: int


@dataclass(frozen=True)
class DeviceLoad:
    # « busy » décrit le calcul ; NVENC/NVDEC sont des moteurs distincts.
    busy: float | None = None
    free_bytes: int | None = None
    cuda_index: int | None = None
    encoder_busy: float | None = None
    decoder_busy: float | None = None


def _percentage(value: str) -> float | None:
    try:
        result = float(value)
        return result if 0 <= result <= 100 else None
    except ValueError:
        return None


def device_choice(value: object) -> str:
    """Les profils conservent un UUID stable, jamais un index inter-API."""
    text = str(value or "auto").lower().strip()
    if text in {"auto", "cpu"} or re.fullmatch(r"[0-9a-f]{32}", text):
        return text
    return "auto"


def probe_load(devices: tuple[FelDevice, ...]) -> dict[str, DeviceLoad]:
    """Mesures facultatives : absence de télémétrie = charge inconnue."""
    loads: dict[str, DeviceLoad] = {}
    if any(d.vendor == 0x10DE for d in devices):
        try:
            # Arguments constants, aucun shell ; cet outil reste facultatif.
            result = subprocess.run(  # nosec B603
                ["nvidia-smi", "--query-gpu=index,uuid,utilization.gpu,utilization.encoder,utilization.decoder,memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, check=False, timeout=2, **subprocess_text_kwargs(),
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    fields = [v.strip() for v in line.split(",")]
                    if len(fields) != 6:
                        continue
                    index, uuid, busy, encoder, decoder, memory = fields
                    key = uuid.lower().removeprefix("gpu-").replace("-", "")
                    if not re.fullmatch(r"[0-9a-f]{32}", key):
                        continue
                    loads[key] = DeviceLoad(
                        _percentage(busy), int(memory) << 20 if memory.isdigit() else None,
                        int(index) if index.isdigit() else None,
                        _percentage(encoder), _percentage(decoder),
                    )
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    # Sans identité PCI, exiger une seule carte du constructeur des deux côtés.
    for vendor, prefix in ((0x1002, "0x1002"), (0x8086, "0x8086")):
        matching = [d for d in devices if d.vendor == vendor]
        if len(matching) != 1:
            continue
        nodes = []
        for node in Path("/sys/class/drm").glob("card[0-9]*/device"):
            try:
                if (node / "vendor").read_text().strip() == prefix:
                    nodes.append(node)
            except OSError:
                pass
        if len(nodes) != 1:
            continue
        node = nodes[0]
        busy = None
        free = None
        try:
            busy = _percentage((node / "gpu_busy_percent").read_text().strip())
        except OSError:
            pass
        # La VRAM dédiée de l'iGPU ne décrit pas son budget mémoire partagé.
        if matching[0].kind != "integrated":
            try:
                total = int((node / "mem_info_vram_total").read_text().strip())
                used = int((node / "mem_info_vram_used").read_text().strip())
                if 0 <= used <= total:
                    free = total-used
            except (OSError, ValueError):
                pass
        loads[matching[0].uuid] = DeviceLoad(busy, free)
    return loads


def pipeline_devices(video: VideoEncodeSettings, devices: tuple[FelDevice, ...],
                     loads: dict[str, DeviceLoad], commands: tuple[tuple[str, ...], ...] = ()) -> frozenset[str]:
    """Réserve les candidats possibles si l'outil aval choisit automatiquement."""
    occupied: set[str] = set()
    codec = video.codec.lower()
    vendor = (0x10DE if "nvenc" in codec else 0x8086 if "qsv" in codec else
              0x1002 if "amf" in codec else None)
    if vendor is not None:
        candidates = [d for d in devices if d.vendor == vendor]
        try:
            args = shlex.split(video.extra_params)
        except ValueError:
            args = []
        # Les commandes finales contiennent les choix matériels résolus par Muxiveo.
        if commands:
            args.extend(commands[-1])
        explicit = None
        for i, arg in enumerate(args):
            if arg in {"--device", "-gpu"} and i+1 < len(args) and args[i+1].isdigit():
                explicit = int(args[i+1])
            elif arg.startswith(("--device=", "-gpu=")) and arg.split("=", 1)[1].isdigit():
                explicit = int(arg.split("=", 1)[1])
        if vendor == 0x10DE and explicit is not None:
            mapped = [d for d in candidates if loads.get(d.uuid, DeviceLoad()).cuda_index == explicit]
            candidates = mapped or candidates
        occupied.update(d.uuid for d in candidates)
    elif "vaapi" in codec:
        occupied.update(d.uuid for d in devices if d.vendor in {0x8086, 0x1002})
    # Les indices Vulkan de FFmpeg/ncnn ne sont pas assimilés à ceux du plugin.
    # Sans identité confirmée, réserver leurs candidats évite une fausse certitude.
    if video.interpolation.is_active():
        matched: set[str] = set()
        for stage in commands:
            if not stage or "rife" not in Path(stage[0]).stem.lower():
                continue
            try:
                result = subprocess.run(  # nosec B603
                    [stage[0], "--list-gpus"], capture_output=True, check=False,
                    timeout=5, **subprocess_text_kwargs(),
                )
                payload = json.loads(result.stdout) if result.returncode == 0 else {}
                if not isinstance(payload, dict) or not isinstance(payload.get("gpus", []), list):
                    continue
                index = video.interpolation.gpu
                if index < 0:
                    index = int(payload.get("default", -1))
                entry: dict[str, Any] = next((g for g in payload.get("gpus", [])
                                             if isinstance(g, dict) and g.get("index") == index), {})
                uuid = str(entry.get("uuid", "")).replace("-", "").lower()
                matched.update(d.uuid for d in devices if d.uuid == uuid)
                if not matched:
                    matched.update(d.uuid for d in devices if d.name == entry.get("name"))
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                pass
        occupied.update(matched or (d.uuid for d in devices if d.kind == "discrete"))
    if video.nlmeans_vulkan or video.filters.nlmeans_enabled:
        occupied.update(d.uuid for d in devices if d.kind == "discrete")
    return frozenset(occupied)


_LOCK = threading.Lock()
_ACTIVE: Counter[str] = Counter()


def select_device(devices: tuple[FelDevice, ...], loads: dict[str, DeviceLoad],
                  occupied: frozenset[str], active: Counter[str], memory_needed: int = 0) -> FelDevice | None:
    """Dédié puis intégré ; départager les cartes d'une même classe par charge.

    Un iGPU libre ne suffit pas à justifier le déport : son débit FEL peut être
    inférieur au débit de la carte dédiée qui partage RIFE ou l'encodeur.
    """
    eligible = [d for d in devices if d.memory_bytes >= memory_needed and
                (loads.get(d.uuid, DeviceLoad()).free_bytes is None or
                 int(loads[d.uuid].free_bytes or 0) >= memory_needed)]
    return min(eligible, key=lambda d: (
        d.kind != "discrete",
        (loads.get(d.uuid, DeviceLoad()).busy or 0) >= 90,
        int(d.uuid in occupied) + active[d.uuid],
        loads.get(d.uuid, DeviceLoad()).busy if loads.get(d.uuid, DeviceLoad()).busy is not None else 50.,
        -d.memory_bytes, d.uuid,
    ), default=None)


@dataclass
class FelDevicePlan:
    """Choix figé dès la première passe, réservation limitée à sa production."""
    choice: str = "auto"
    devices: tuple[FelDevice, ...] = ()
    video: VideoEncodeSettings | None = None
    log: Callable[[str, str], None] = lambda *_: None
    selected: str | None = None
    commands: tuple[tuple[str, ...], ...] = ()
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def acquire(self) -> tuple[str, Callable[[], None]]:
        with self._lock:
            loads = {}
            occupied: frozenset[str] = frozenset()
            if self.choice == "auto" and self.selected is None:
                # Les outils externes ne doivent jamais retenir le verrou global.
                loads = probe_load(self.devices)
                occupied = pipeline_devices(self.video, self.devices, loads, self.commands) if self.video else frozenset()
            with _LOCK:
                first = self.selected is None
                if first:
                    if self.choice == "auto":
                        # Budget UHD ; les allocations natives vérifient les dimensions.
                        selected = select_device(self.devices, loads, occupied, _ACTIVE, 768 << 20)
                        self.selected = selected.uuid if selected else "cpu"
                    else:
                        self.selected = self.choice
                key = self.selected
                assert key is not None
                if key != "cpu":
                    _ACTIVE[key] += 1
        released = False

        def release() -> None:
            nonlocal released
            with _LOCK:
                if not released and key != "cpu":
                    _ACTIVE[key] -= 1
                    if not _ACTIVE[key]:
                        del _ACTIVE[key]
                released = True

        if first:
            chosen = next((d for d in self.devices if d.uuid == key), None)
            label = chosen.name if chosen else key
            rule = " ; priorité GPU dédié > iGPU > CPU" if self.choice == "auto" else ""
            try:
                self.log("INFO", f"FEL — moteur choisi : {label} ({self.choice}{rule}).")
            except BaseException:
                release()
                raise
        return key, release
