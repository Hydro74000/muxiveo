"""
core/workflows/encode/interpolation.py — Interpolation d'images RIFE (muxiveo-rife).

Le binaire ``muxiveo-rife`` (``native/muxiveo-rife``) lit et écrit du y4m :
la vidéo passe par un pipeline de trois processus reliés par des pipes ::

    ffmpeg (décodage + filtres logiciels) | muxiveo-rife | encodeur

Public:
    INTERPOLATION_MODELS, INTERPOLATION_FACTORS, INTERPOLATION_MODES
    PipelineCommand          — commande finale précédée d'étages amont
    InterpolationSource      — couleur / décalage de départ de la source
    probe_interpolation_source(...)
    build_decode_stage(...), build_rife_stage(...)
    expand_rpu_file(...), expand_hdr10plus_json(...)
"""

from __future__ import annotations

import functools
import json
import math
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from core.bluray import append_ffmpeg_input_args, ffprobe_input_args
from core.matroska.editors.dovi import minimum_dovi_level
from core.subprocess_utils import subprocess_text_kwargs
from core.pipeline_command import PipelineCommand, command_stages

if TYPE_CHECKING:
    from core.workflows.encode.models import FrameInterpolationSettings, VideoEncodeSettings

# Préréglage qualité → modèle RIFE embarqué (voir native/muxiveo-rife/models.json).
# v4.6 : meilleur VMAF moyen, débit le plus élevé et VRAM la plus basse sur le banc
# (docs/benchmarks) ; Light = v4.15-lite toujours en mode Fast (petites cartes).
INTERPOLATION_MODELS: dict[str, str] = {
    "fast": "rife-v4.6",
    "balanced": "rife-v4.6",
    "light": "rife-v4.15-lite",
    "max": "rife-v4.6",  # ancien préréglage Max (v4.25-heavy), conservé pour les presets enregistrés
}
# Préréglages qui imposent le mode Fast (--uhd).
INTERPOLATION_FAST_QUALITIES: frozenset[str] = frozenset({"light"})
INTERPOLATION_DEFAULT_QUALITY = "balanced"
INTERPOLATION_FACTORS: tuple[int, ...] = (2, 3, 4)
# Cadences cibles proposées (rapport non entier possible, ex. 23,976 -> 59,94 = x2,5).
INTERPOLATION_TARGET_FPS: tuple[str, ...] = ("60000/1001", "60/1")
# Mode de calcul du flux optique : "fast" = muxiveo-rife --uhd (flux à demi-résolution).
INTERPOLATION_MODES: tuple[str, ...] = ("normal", "fast")
# Version minimale de muxiveo-rife : modèles v4.6 / v4.15-lite et --uhd (release 1.1.0).
RIFE_MIN_VERSION: tuple[int, int, int] = (1, 1, 0)
# Moyennage TTA (--tta) : nombre de passes moyennées par image interpolée (1 = désactivé).
INTERPOLATION_TTA_LEVELS: tuple[int, ...] = (1, 2, 4, 8)
RIFE_TTA_MIN_VERSION: tuple[int, int, int] = (1, 2, 0)
INTERPOLATION_BACKENDS: tuple[str, ...] = ("rife", "mvtools")
MVTOOLS_MODES: tuple[str, ...] = ("standard", "uhd")
MVTOOLS_MIN_VERSION: tuple[int, int, int] = (1, 0, 0)

# Intervalle des lignes ``progress`` de muxiveo-rife : alimentent la barre de
# progression (non journalisées, log verbose uniquement).
_RIFE_PROGRESS_INTERVAL_S = 2

_RIFE_PROGRESS_RE = re.compile(r"\[muxiveo-rife\] progress in=(\d+) out=(\d+)\b.*?\bfps=([\d.]+)")

_RIFE_VERSION_RE = re.compile(r"muxiveo-rife (\d+)\.(\d+)\.(\d+)")

_RIFE_MATRICES = {"bt709", "bt2020nc", "bt2020", "bt601", "smpte170m", "bt470bg", "smpte240m", "fcc"}
_RIFE_CHROMA_LOCATIONS = {"left", "center", "topleft"}
# Noms ffprobe -> NVEncC ; une valeur inconnue reste omise.
_NVENCC_PRIMARIES = {
    **{name: name for name in ("bt709", "smpte170m", "bt470m", "bt470bg", "smpte240m", "film", "bt2020")},
    "smpte428": "st428", "smpte431": "st431-2", "smpte432": "st432-1",
    "ebu3213": "ebu3213-e", "jedec-p22": "ebu3213-e",
}
_NVENCC_TRANSFERS = {name: name for name in ("bt709", "smpte170m", "bt470m", "bt470bg", "smpte240m", "linear", "log100", "log316",
                     "iec61966-2-4", "bt1361e", "iec61966-2-1", "bt2020-10", "bt2020-12", "smpte2084",
                     "smpte428", "arib-std-b67")}
_NVENCC_MATRICES = {
    **{name: name for name in ("bt709", "smpte170m", "bt470bg", "smpte240m", "fcc", "bt2020nc", "bt2020c")},
    "gbr": "GBR", "ycgco": "YCgCo",
    "chroma-derived-nc": "derived-ncl", "chroma-derived-c": "derived-cl",
    "ictcp": "ictco",
}

_RPU_START_CODE = b"\x00\x00\x00\x01"
# Niveau Dolby Vision maximal signalé (compatibilité des décodeurs TV, cf. sanitize_dovi_level).
_DOVI_MAX_COMPAT_LEVEL = 9


@dataclass(frozen=True)
class InterpolationSource:
    """Propriétés de la source utiles à l'étage RIFE et au réhorodatage."""

    matrix: str = "bt709"
    color_range: str = "limited"
    chroma_location: str = "left"
    # Écart (s) entre le départ du flux vidéo et celui du conteneur : le y4m
    # ne transporte pas de timestamps, l'encodeur le réapplique (-itsoffset).
    start_offset_s: float = 0.0
    is_vfr: bool = False
    # Cadence nominale imposée au décodage quand la source est VFR (y4m = CFR).
    cfr_rate: str = ""
    # Cadence du flux (avg_frame_rate, sinon r_frame_rate).
    frame_rate: str = ""
    # Marquage couleur déclaré par la source ("" si absent) : le y4m ne le
    # transporte pas, l'encodeur doit le recevoir explicitement.
    primaries: str = ""
    transfer: str = ""
    colorspace: str = ""

    def setparams_filter(self, *, hdr_pq: bool = False) -> str:
        """Filtre ``setparams`` posant le marquage couleur sur les images y4m.

        Depuis ffmpeg 7, l'encodeur lit ces propriétés sur les images : les options
        de sortie ``-color_*`` ne suffisent plus. ``hdr_pq`` : sortie HDR10 (BT.2020/PQ).
        """
        if hdr_pq:
            values = [("color_primaries", "bt2020"), ("color_trc", "smpte2084"), ("colorspace", "bt2020nc")]
            color_range = "tv"
        else:
            values = [(key, value) for key, value in (("color_primaries", self.primaries),
                                                      ("color_trc", self.transfer),
                                                      ("colorspace", self.colorspace)) if value]
            if not values:
                return ""
            color_range = "pc" if self.color_range == "full" else "tv"
        return "setparams=" + ":".join(f"{key}={value}" for key, value in values) + f":range={color_range}"

    def nvencc_color_args(self) -> list[str]:
        """Options NVEncC rétablissant le marquage couleur de la source."""
        args = [opt for flag, value, mapping in (("--colorprim", self.primaries, _NVENCC_PRIMARIES),
                                                 ("--transfer", self.transfer, _NVENCC_TRANSFERS),
                                                 ("--colormatrix", self.colorspace, _NVENCC_MATRICES))
                if value in mapping for opt in (flag, mapping[value])]
        return [*args, "--colorrange", self.color_range] if args else []

    @property
    def decode_rate(self) -> str:
        """Cadence des trames remises à RIFE (après normalisation CFR éventuelle)."""
        return self.cfr_rate or self.frame_rate


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def interpolation_source_from_probe(
    stream: dict[str, object],
    *,
    format_start_time: object = None,
    tonemap_to_sdr: bool = False,
) -> InterpolationSource:
    """Construit les propriétés d'interpolation depuis un flux ``ffprobe -show_streams``."""
    def declared(key: str) -> str:
        value = str(stream.get(key) or "").strip().lower()
        return "" if value in {"", "unknown", "unspecified", "reserved"} else value

    if tonemap_to_sdr:
        # Le tone mapping (avant RIFE) produit du BT.709 limité.
        matrix = "bt709"
        color_range = "limited"
        primaries = transfer = colorspace = "bt709"
    else:
        primaries, transfer, colorspace = declared("color_primaries"), declared("color_transfer"), declared("color_space")
        matrix = str(stream.get("color_space") or "").strip().lower()
        if matrix not in _RIFE_MATRICES:
            # matrice de calcul RIFE devinée (n'affecte pas le marquage déclaré)
            height = int(_float_or_none(stream.get("height")) or 0)
            if str(stream.get("color_transfer") or "").strip().lower() in {"smpte2084", "arib-std-b67"}:
                matrix = "bt2020nc"
            else:
                matrix = "bt709" if height > 576 else "bt601"
        color_range = "full" if str(stream.get("color_range") or "").strip().lower() in {"pc", "jpeg"} else "limited"

    chroma = str(stream.get("chroma_location") or "").strip().lower()
    if chroma not in _RIFE_CHROMA_LOCATIONS:
        chroma = "left"

    start_offset = 0.0
    stream_start = _float_or_none(stream.get("start_time"))
    container_start = _float_or_none(format_start_time)
    if stream_start is not None:
        start_offset = max(0.0, stream_start - (container_start or 0.0))

    r_rate = str(stream.get("r_frame_rate") or "")
    avg_rate = str(stream.get("avg_frame_rate") or "")
    is_vfr = bool(r_rate and avg_rate and r_rate != avg_rate and _rates_differ(r_rate, avg_rate))

    return InterpolationSource(
        matrix=matrix,
        color_range=color_range,
        chroma_location=chroma,
        start_offset_s=round(start_offset, 6),
        is_vfr=is_vfr,
        cfr_rate=nominal_cfr_rate(r_rate, avg_rate) if is_vfr else "",
        frame_rate=avg_rate if _rate_value(avg_rate) else r_rate,
        primaries=primaries,
        transfer=transfer,
        colorspace=colorspace,
    )


def stream_start_offset(ffprobe_bin: str, source: Path, stream_index: int) -> float:
    """Départ (s) d'un flux relativement au début du conteneur (0 si inconnu ou négatif)."""
    cmd = [ffprobe_bin, "-v", "error", "-print_format", "json", "-show_streams", "-show_format"]
    cmd.extend(ffprobe_input_args(source))
    try:
        # ffprobe configuré, arguments séparés et chemin média protégé, sans shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        result = subprocess.run(cmd, capture_output=True, check=False, timeout=30, **subprocess_text_kwargs())  # nosec B603
        payload = json.loads(result.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0
    stream: dict = next((s for s in payload.get("streams") or [] if int(s.get("index", -1)) == int(stream_index)), {})
    return interpolation_source_from_probe(
        stream, format_start_time=(payload.get("format") or {}).get("start_time")
    ).start_offset_s


def stream_start_time(ffprobe_bin: str, source: Path, stream_index: int) -> float:
    """Horodatage (s) du premier paquet d'un flux, sans référence au conteneur (0 si inconnu)."""
    stream = _probe_stream(ffprobe_bin, source, stream_index)
    value = _float_or_none(stream.get("start_time")) if stream else None
    return max(0.0, value or 0.0)


def probe_interpolation_source(
    ffprobe_bin: str, source: Path, stream_index: int, *, tonemap_to_sdr: bool = False
) -> InterpolationSource | None:
    """``InterpolationSource`` d'un flux sondé directement (None si la sonde échoue)."""
    stream = _probe_stream(ffprobe_bin, source, stream_index)
    if stream is None:
        return None
    return interpolation_source_from_probe(stream, tonemap_to_sdr=tonemap_to_sdr)


def nominal_cfr_rate(r_rate: str, avg_rate: str) -> str:
    """Cadence CFR de normalisation d'une source VFR (``r_frame_rate`` si plausible)."""
    r = _rate_value(r_rate)
    avg = _rate_value(avg_rate)
    if r and (not avg or r <= 2 * avg):
        return r_rate
    return avg_rate if avg else ""


def _rate_value(rate: str) -> float | None:
    num, _, den = rate.partition("/")
    try:
        n = float(num)
        d = float(den) if den else 1.0
    except ValueError:
        return None
    return n / d if d else None


def _rates_differ(left: str, right: str, *, tolerance: float = 0.001) -> bool:
    """Écart relatif > 0,1 % : au-delà des arrondis CFR (MP4 « 2997/100 » contre 30000/1001)."""
    a = _rate_value(left)
    b = _rate_value(right)
    if not a or not b:
        return False
    return abs(a - b) / max(a, b) > tolerance


def build_decode_stage(
    ffmpeg_bin: str,
    source: Path | str,
    *,
    stream_index: int,
    vf: str = "",
    pre_input_args: Sequence[str] = (),
    thread_args: Sequence[str] = (),
) -> list[str]:
    """Étage 1 : décodage + filtres logiciels → y4m sur stdout."""
    cmd = [str(ffmpeg_bin), "-hide_banner", "-nostdin", "-loglevel", "error"]
    cmd.extend(pre_input_args)
    append_ffmpeg_input_args(cmd, source)
    cmd.extend(["-map", f"0:{int(stream_index)}"])
    if vf:
        cmd.extend(["-vf", vf])
    cmd.extend(thread_args)
    cmd.extend([
        "-fps_mode", "passthrough",
        "-an", "-sn", "-dn",
        "-f", "yuv4mpegpipe", "-strict", "-1", "-",
    ])
    return cmd


def build_rife_stage(
    rife_bin: str,
    *,
    factor: int = 2,
    target_fps: str = "",
    quality: str,
    source: InterpolationSource,
    scene_threshold: float = 10.0,
    gpu: int = -1,
    mode: str = "normal",
    tta: int = 1,
) -> list[str]:
    """Étage 2 : muxiveo-rife (y4m stdin → y4m stdout), facteur entier ou cadence cible.

    ``mode="fast"`` : flux optique calculé à demi-résolution (``--uhd``) ;
    ``tta`` > 1 : moyenne de ``tta`` passes (sens inverse, miroirs), coût x ``tta``.
    """
    model = INTERPOLATION_MODELS.get(str(quality or ""), INTERPOLATION_MODELS[INTERPOLATION_DEFAULT_QUALITY])
    rate_args = ["--fps", str(target_fps)] if target_fps else ["--factor", str(int(factor))]
    cmd = [
        str(rife_bin),
        *rate_args,
        "--model", model,
        "--matrix", source.matrix,
        "--range", source.color_range,
        "--chroma-loc", source.chroma_location,
        "--scene-threshold", f"{max(0.0, float(scene_threshold)):g}",
        "--progress-interval", str(_RIFE_PROGRESS_INTERVAL_S),
    ]
    if mode == "fast" or quality in INTERPOLATION_FAST_QUALITIES:
        cmd.append("--uhd")
    if int(gpu) >= 0:
        cmd.extend(["--gpu", str(int(gpu))])
    if int(tta) > 1:
        cmd.extend(["--tta", str(int(tta))])
    return cmd


def mvtools_thread_count(mode: str, budget: int | None = None) -> int:
    """Part du budget CPU affectée à MVTools, après répartition entre tâches."""
    count = max(1, int(budget or os.cpu_count() or 1))
    return max(1, min(4, count // 2)) if mode == "standard" else count


def build_mvtools_stage(
    mvtools_bin: str, *, factor: int = 2, target_fps: str = "",
    mode: str = "standard", source: InterpolationSource,
    scene_threshold: float = 10.0, thread_budget: int | None = None,
) -> list[str]:
    """Étage CPU MVTools, Y4M en entrée/sortie et cadence rationnelle."""
    resolved = shutil.which(mvtools_bin)
    if resolved or Path(mvtools_bin).is_file():
        mvtools_bin = str(Path(resolved or mvtools_bin).resolve())
    rate = ["--fps", target_fps] if target_fps else ["--factor", str(int(factor))]
    return [
        str(mvtools_bin), *rate, "--mode", mode,
        "--threads", str(mvtools_thread_count(mode, thread_budget)),
        "--matrix", source.matrix, "--range", source.color_range,
        "--chroma-loc", source.chroma_location,
        "--scene-threshold", f"{max(0.0, float(scene_threshold)):g}",
        "--progress-interval", str(_RIFE_PROGRESS_INTERVAL_S),
    ]


def build_interpolation_stage(
    settings: FrameInterpolationSettings, source: InterpolationSource, *,
    rife_bin: str | None = None, mvtools_bin: str | None = None,
    thread_budget: int | None = None,
) -> list[str]:
    """Sélectionne explicitement le moteur demandé, sans repli implicite."""
    from core.workflows.encode.models import EncodeError
    if settings.backend not in INTERPOLATION_BACKENDS:
        raise EncodeError(f"Interpolation d'images : moteur « {settings.backend} » inconnu.")
    binary = mvtools_bin if settings.backend == "mvtools" else rife_bin
    if not binary:
        raise EncodeError(f"Interpolation d'images : outil muxiveo-{settings.backend} introuvable.")
    common = dict(factor=int(settings.factor), target_fps=settings.target_fps,
                  source=source, scene_threshold=settings.scene_threshold)
    if settings.backend == "mvtools":
        return build_mvtools_stage(binary, **common, mode=settings.mvtools_mode, thread_budget=thread_budget)
    return build_rife_stage(binary, **common, quality=settings.quality,
                            gpu=settings.gpu, mode=settings.mode, tta=settings.tta)


@functools.lru_cache(maxsize=8)
def _mvtools_runtime_cached(binary: str, identity: tuple[object, ...]) -> dict[str, object] | None:
    """Autotest du runtime embarqué, mémorisé jusqu'au remplacement du binaire."""
    try:
        # Outil configuré, arguments constants, aucun shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        result = subprocess.run(  # nosec B603
            [binary, "--self-test", "--json"], capture_output=True, check=False,
            timeout=30, **subprocess_text_kwargs(),
        )
        payload = json.loads(result.stdout)
        return payload if result.returncode == 0 and payload.get("ok") is True else None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


def mvtools_runtime_info(binary: str) -> dict[str, object] | None:
    """Vérifie le chargement et un rendu 8/10 bits du moteur sélectionné."""
    try:
        path = Path(shutil.which(binary) or binary).resolve()
        stat = path.stat()
        runtime = path.parent / "mvtools-runtime"
        identity = (stat.st_mtime_ns, stat.st_size, tuple(
            (p.name, p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(runtime.iterdir()) if p.is_file()
        ))
        if not runtime.is_dir():
            return None
        return _mvtools_runtime_cached(str(path), identity)
    except OSError:
        return None


def rife_model_available(rife_bin: str, model: str) -> bool:
    """Vrai si ``<dossier réel du binaire>/rife-models/<model>`` contient le modèle."""
    try:
        exe_dir = Path(rife_bin).resolve().parent
    except OSError:
        return False
    return (exe_dir / "rife-models" / model / "flownet.param").is_file()


def rife_version(rife_bin: str) -> tuple[int, int, int] | None:
    """Version annoncée par ``muxiveo-rife --version`` (None si illisible).

    Cache indexé sur l'identité du fichier : un binaire remplacé sur place par
    le setup est relu sans redémarrer l'application.
    """
    try:
        info = Path(shutil.which(str(rife_bin)) or rife_bin).stat()
        identity = (info.st_ino, info.st_size, info.st_mtime_ns)
    except OSError:
        identity = (0, 0, 0)
    return _rife_version_cached(str(rife_bin), identity)


@functools.lru_cache(maxsize=8)
def _rife_version_cached(rife_bin: str, identity: tuple[int, int, int]) -> tuple[int, int, int] | None:
    _ = identity
    try:
        # Binaire RIFE configuré ; seul argument constant --version, sans shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        result = subprocess.run(  # nosec B603
            [str(rife_bin), "--version"], capture_output=True, check=False, timeout=15, **subprocess_text_kwargs()
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _RIFE_VERSION_RE.search(result.stdout or "")
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def clear_rife_version_cache() -> None:
    """Vide le cache des versions ``muxiveo-rife`` (tests)."""
    _rife_version_cached.cache_clear()


@dataclass(frozen=True)
class RifeProgress:
    """Ligne ``progress`` de muxiveo-rife : trames source lues / produites, débit de sortie."""

    frames_in: int
    frames_out: int
    fps_out: float

    def percent(self, total_source_frames: int | None) -> float | None:
        if not total_source_frames or total_source_frames <= 0:
            return None
        return min(99.0, self.frames_in * 100.0 / total_source_frames)

    def eta_seconds(self, total_source_frames: int | None) -> float | None:
        """Reste à traiter au débit moyen observé (trames source / s)."""
        if not total_source_frames or self.fps_out <= 0 or self.frames_out <= 0:
            return None
        rate_in = self.fps_out * self.frames_in / self.frames_out
        if rate_in <= 0:
            return None
        return max(0, total_source_frames - self.frames_in) / rate_in


def parse_rife_progress(line: str) -> RifeProgress | None:
    """Reconnaît ``[muxiveo-rife] progress in=… out=… fps=…`` (lignes relayées du pipeline)."""
    match = _RIFE_PROGRESS_RE.search(line)
    if match is None:
        return None
    return RifeProgress(int(match.group(1)), int(match.group(2)), float(match.group(3)))


# Compatibilité des appelants historiques : même format et mêmes calculs.
InterpolationProgress = RifeProgress
_INTERPOLATION_PROGRESS_RE = re.compile(r"\[muxiveo-(?:rife|mvtools)\] progress in=(\d+) out=(\d+)\b.*?\bfps=([\d.]+)")


def parse_interpolation_progress(line: str) -> InterpolationProgress | None:
    """Reconnaît la progression de chacun des moteurs d'interpolation."""
    match = _INTERPOLATION_PROGRESS_RE.search(line)
    return InterpolationProgress(int(match[1]), int(match[2]), float(match[3])) if match else None


# =============================================================================
# Métadonnées dynamiques par trame (DoVi RPU, HDR10+)
# =============================================================================

def frame_repeats(index: int, ratio: Fraction | int) -> int:
    """Trames de sortie issues de la trame source ``index`` (sortie = source × ``ratio``).

    Même règle que muxiveo-rife : la trame de sortie ``k`` provient de la trame
    source ``floor(k / ratio)`` (x2 -> 2,2,2… ; x2,5 -> 3,2,3,2…).
    """
    r = Fraction(ratio)
    return math.ceil((index + 1) * r) - math.ceil(index * r)


def ratio_label(ratio: Fraction | int) -> str:
    """Libellé court du rapport de cadence (``x2``, ``x2,5``, ``x2,5025``)."""
    r = Fraction(ratio)
    if r.denominator == 1:
        return f"x{r.numerator}"
    return "x" + f"{float(r):.4f}".rstrip("0").rstrip(".").replace(".", ",")


def expand_rpu_file(source: Path, dest: Path, ratio: Fraction | int) -> int:
    """Répète chaque RPU Dolby Vision selon le rapport de cadence ; retourne le nombre de RPU écrits.

    ``dovi_tool extract-rpu`` écrit une suite de NAL UNSPEC62 préfixées par un
    start code 4 octets ; l'émulation de start code garantit qu'aucun préfixe
    n'apparaît dans une charge utile. Chaque trame interpolée hérite des
    métadonnées de la trame source qui la précède.
    """
    data = source.read_bytes()
    if not data.startswith(_RPU_START_CODE):
        raise ValueError(f"RPU illisible (start code absent) : {source}")
    units = [unit for unit in data.split(_RPU_START_CODE) if unit]
    written = 0
    with dest.open("wb") as fh:
        for index, unit in enumerate(units):
            for _ in range(frame_repeats(index, ratio)):
                fh.write(_RPU_START_CODE)
                fh.write(unit)
                written += 1
    return written


def expand_hdr10plus_json(source: Path, dest: Path, ratio: Fraction | int) -> int:
    """Répète chaque trame d'un JSON ``hdr10plus_tool extract`` selon le rapport de cadence.

    Index de séquence, index dans la scène et résumé des scènes sont
    recalculés ; retourne le nombre de trames écrites.
    """
    payload = json.loads(source.read_text(encoding="utf-8"))
    scenes = payload.get("SceneInfo")
    if not isinstance(scenes, list):
        raise ValueError(f"JSON HDR10+ sans SceneInfo : {source}")

    expanded: list[dict[str, object]] = []
    scene_starts: list[int] = []
    for index, entry in enumerate(e for e in scenes if isinstance(e, dict)):
        if entry.get("SceneFrameIndex") == 0 or not scene_starts:
            scene_starts.append(len(expanded))
        for _ in range(frame_repeats(index, ratio)):
            frame = dict(entry)
            out_index = len(expanded)
            if isinstance(entry.get("SequenceFrameIndex"), int):
                frame["SequenceFrameIndex"] = out_index
            if isinstance(entry.get("SceneFrameIndex"), int):
                frame["SceneFrameIndex"] = out_index - scene_starts[-1]
            expanded.append(frame)
    payload["SceneInfo"] = expanded

    summary = payload.get("SceneInfoSummary")
    if isinstance(summary, dict):
        bounds = [*scene_starts, len(expanded)]
        summary["SceneFirstFrameIndex"] = scene_starts
        summary["SceneFrameNumbers"] = [bounds[i + 1] - bounds[i] for i in range(len(scene_starts))]

    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return len(expanded)


def dovi_scene_cut_edit(scene_frames: Iterable[int], ratio: Fraction | int) -> dict[str, object]:
    """Édition ``dovi_tool editor`` retirant le drapeau de coupe des copies interpolées.

    Après répétition, la coupe de la trame source ``s`` est portée par toutes
    ses trames de sortie ; seule la première doit rester une coupe.
    """
    cuts: dict[str, bool] = {}
    for frame in sorted(set(int(f) for f in scene_frames)):
        repeats = frame_repeats(frame, ratio)
        if repeats > 1:
            first = math.ceil(frame * Fraction(ratio))
            cuts[f"{first + 1}-{first + repeats - 1}"] = False
    return {"scene_cuts": cuts} if cuts else {}


def expand_dynamic_hdr_metadata(
    *,
    ratio: Fraction | int,
    rpu_bin: Path | None = None,
    hdr10p_json: Path | None = None,
    dovi_tool_bin: str | None = None,
    run_cmd: Callable[[list[str]], object] | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Étend en place le RPU DoVi et le JSON HDR10+ à la cadence interpolée.

    Avec ``dovi_tool_bin`` et ``run_cmd``, les coupes de scène Dolby Vision
    restent sur la seule trame d'origine (sinon chaque coupe serait doublée).
    """
    ratio = Fraction(ratio)
    if ratio <= 1:
        return
    label = ratio_label(ratio)
    tag = label.replace(",", "_")
    if rpu_bin is not None and rpu_bin.is_file():
        scenes_txt = rpu_bin.with_name(f"{rpu_bin.stem}.scenes.txt")
        scene_frames: list[int] = []
        if dovi_tool_bin and run_cmd is not None:
            try:
                run_cmd([dovi_tool_bin, "export", "-i", str(rpu_bin), "-d", f"scenes={scenes_txt}"])
                scene_frames = [int(line) for line in scenes_txt.read_text(encoding="utf-8").split() if line.isdigit()]
            except Exception as exc:  # noqa: BLE001 — nettoyage cosmétique, non bloquant
                if log:
                    log(f"Interpolation {label} : coupes Dolby Vision non relues ({exc}) ; drapeaux dupliqués conservés.")
            finally:
                scenes_txt.unlink(missing_ok=True)

        expanded = rpu_bin.with_name(f"{rpu_bin.stem}.{tag}{rpu_bin.suffix}")
        count = expand_rpu_file(rpu_bin, expanded, ratio)
        edit = dovi_scene_cut_edit(scene_frames, ratio)
        if edit and dovi_tool_bin and run_cmd is not None:
            edit_json = rpu_bin.with_name(f"{rpu_bin.stem}.scenes.json")
            fixed = rpu_bin.with_name(f"{rpu_bin.stem}.{tag}.scenes{rpu_bin.suffix}")
            edit_json.write_text(json.dumps(edit), encoding="utf-8")
            try:
                run_cmd([dovi_tool_bin, "editor", "-i", str(expanded), "-j", str(edit_json), "-o", str(fixed)])
                fixed.replace(expanded)
            finally:
                edit_json.unlink(missing_ok=True)
                fixed.unlink(missing_ok=True)
        expanded.replace(rpu_bin)
        if log:
            log(
                f"Interpolation {label} : RPU Dolby Vision étendu à {count} trames "
                f"({len(scene_frames)} coupe(s) de scène conservée(s))."
            )
    if hdr10p_json is not None and hdr10p_json.is_file():
        expanded = hdr10p_json.with_name(f"{hdr10p_json.stem}.{tag}{hdr10p_json.suffix}")
        count = expand_hdr10plus_json(hdr10p_json, expanded, ratio)
        expanded.replace(hdr10p_json)
        if log:
            log(f"Interpolation {label} : métadonnées HDR10+ étendues à {count} trames.")


def multiply_fps_expr(expr: str | float | None, ratio: Fraction | int) -> str | None:
    """Cadence ``expr`` (``"24000/1001"`` ou nombre) multipliée par ``ratio``."""
    if expr in (None, ""):
        return None
    try:
        value = Fraction(str(expr)) * Fraction(ratio)
    except (ValueError, ZeroDivisionError):
        return None
    return f"{value.numerator}/{value.denominator}"


def ffprobe_beside(ffmpeg_bin: str) -> str:
    """ffprobe livré à côté de ``ffmpeg_bin`` (sinon résolution par le PATH)."""
    path = Path(ffmpeg_bin)
    if path.parent != Path("."):
        return str(path.with_name("ffprobe" + path.suffix))
    return "ffprobe"


def _probe_stream(ffprobe_bin: str, source: Path, stream_index: int) -> dict | None:
    cmd = [ffprobe_bin, "-v", "error", "-print_format", "json", "-show_streams"]
    cmd.extend(ffprobe_input_args(source))
    try:
        # ffprobe configuré, arguments séparés et chemin média protégé, sans shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
        result = subprocess.run(cmd, capture_output=True, check=False, timeout=30, **subprocess_text_kwargs())  # nosec B603
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return next((s for s in streams if int(s.get("index", -1)) == int(stream_index)), None)


def _stream_rate(stream: dict) -> str:
    avg = str(stream.get("avg_frame_rate") or "")
    return avg if _rate_value(avg) else str(stream.get("r_frame_rate") or "")


def probe_frame_rate(ffprobe_bin: str, source: Path, stream_index: int) -> str | None:
    """Cadence du flux vidéo ``stream_index`` (avg_frame_rate, sinon r_frame_rate)."""
    stream = _probe_stream(ffprobe_bin, source, stream_index)
    rate = _stream_rate(stream) if stream else ""
    return rate if _rate_value(rate) else None


def resolve_frame_ratio(
    video: VideoEncodeSettings,
    *,
    ffprobe_bin: str,
    source: Path,
    stream_index: int,
) -> Fraction:
    """Rapport de cadence effectif d'une piste (sonde ffprobe seulement pour une cadence cible)."""
    if not video.interpolates():
        return Fraction(1)
    rate = probe_frame_rate(ffprobe_bin, source, stream_index) if video.interpolation.target_fps else None
    return video.frame_ratio(rate)


def required_dovi_level(ffprobe_bin: str, source: Path, stream_index: int, ratio: Fraction | int) -> int | None:
    """Niveau Dolby Vision minimal de la sortie interpolée (dimensions source, cadence × ratio)."""
    stream = _probe_stream(ffprobe_bin, source, stream_index)
    if stream is None:
        return None
    fps = _rate_value(_stream_rate(stream)) or 0.0
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if fps <= 0 or width <= 0 or height <= 0:
        return None
    # Plafond 9 (UHD 60 i/s) comme sanitize_dovi_level : un niveau >= 10 fait échouer
    # les décodeurs Dolby Vision des téléviseurs, même au-delà de 60 i/s.
    return min(minimum_dovi_level(width, height, fps * float(Fraction(ratio))), _DOVI_MAX_COMPAT_LEVEL)


def extract_hdr10plus_metadata(
    *,
    source: Path,
    stream_index: int,
    ffmpeg_bin: str,
    hdr10plus_bin: str,
    output_json: Path,
    work_dir: Path,
    run_cmd: Callable[[list[str]], object],
    cleanup_paths: list[Path],
) -> Path:
    """Extrait le JSON HDR10+ (MKV/HEVC direct, autres conteneurs via Annex B)."""
    meta_input = source
    if source.suffix.lower() not in {".mkv", ".hevc", ".h265", ".265", ".x265"} or stream_index != 0:
        meta_input = work_dir / "source_hdr10plus.hevc"
        cleanup_paths.append(meta_input)
        cmd = [ffmpeg_bin, "-nostdin", "-y"]
        append_ffmpeg_input_args(cmd, source)
        cmd.extend(["-map", f"0:{stream_index}", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                    "-f", "hevc", str(meta_input)])
        run_cmd(cmd)
    output_json.unlink(missing_ok=True)
    run_cmd([hdr10plus_bin, "extract", str(meta_input), "-o", str(output_json)])
    if meta_input != source:
        meta_input.unlink(missing_ok=True)
    return output_json


__all__ = [
    "INTERPOLATION_BACKENDS", "MVTOOLS_MODES", "MVTOOLS_MIN_VERSION",
    "build_mvtools_stage", "build_interpolation_stage", "mvtools_thread_count",
    "mvtools_runtime_info", "parse_interpolation_progress", "InterpolationProgress",
    "INTERPOLATION_DEFAULT_QUALITY",
    "INTERPOLATION_FACTORS",
    "INTERPOLATION_TARGET_FPS",
    "INTERPOLATION_MODELS",
    "INTERPOLATION_MODES",
    "INTERPOLATION_FAST_QUALITIES",
    "RIFE_MIN_VERSION",
    "RIFE_TTA_MIN_VERSION",
    "INTERPOLATION_TTA_LEVELS",
    "rife_model_available",
    "InterpolationSource",
    "PipelineCommand",
    "RifeProgress",
    "build_decode_stage",
    "build_rife_stage",
    "clear_rife_version_cache",
    "rife_version",
    "probe_interpolation_source",
    "stream_start_offset",
    "stream_start_time",
    "command_stages",
    "dovi_scene_cut_edit",
    "expand_dynamic_hdr_metadata",
    "expand_hdr10plus_json",
    "expand_rpu_file",
    "extract_hdr10plus_metadata",
    "frame_repeats",
    "probe_frame_rate",
    "ratio_label",
    "ffprobe_beside",
    "interpolation_source_from_probe",
    "multiply_fps_expr",
    "parse_rife_progress",
    "required_dovi_level",
    "resolve_frame_ratio",
]
