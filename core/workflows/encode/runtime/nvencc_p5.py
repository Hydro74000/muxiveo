"""Capacité de NVEncC à convertir une source Dolby Vision P5 (libplacebo + libdovi).

``--check-features`` annonce ce qui est compilé (pas ce qui fonctionne, et
n'affiche pas Vulkan sous Windows) : la conversion native n'est retenue qu'après
une sonde réelle. Un mini P5 synthétique (RPU ``dovi_tool generate`` profil 5,
aucun média embarqué) passe dans la commande native de production ; le noir doit
sortir à 64 et le gris égaler la conversion libplacebo de FFmpeg.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core.subprocess_utils import subprocess_text_kwargs, subprocess_windows_no_window_kwargs
from core.workflows.encode.domain.codecs import P5_TO_HDR10_FILTER
from core.workflows.encode.models import VideoEncodeSettings

_FEATURE_RE = re.compile(r"^\s*([\w.+-]+)\s*:\s*(yes|no)\s*$", re.IGNORECASE | re.MULTILINE)
_PROBE_FRAMES = 8
_PROBE_GRAY_FULL = 512
#: Gris attendu sans référence FFmpeg : RPU synthétique à transfert identité.
_PROBE_GRAY_EXPECTED = 502.0
_BLACK_LIMITED = 64.0
_TOLERANCE = 4.0


def binary_signature(binary: str | None) -> tuple[str, int, int] | None:
    """(chemin résolu, taille, date) : un binaire mis à jour invalide les caches."""
    if not binary:
        return None
    resolved = shutil.which(binary) or binary
    try:
        stat = Path(resolved).stat()
    except OSError:
        return None
    return (str(Path(resolved).resolve()), stat.st_size, stat.st_mtime_ns)


def parse_check_features(output: str) -> dict[str, bool]:
    """Lignes ``nom : yes/no`` de ``NVEncC --check-features``."""
    return {name.lower(): value.lower() == "yes" for name, value in _FEATURE_RE.findall(output or "")}


def nvencc_features(nvencc_bin: str) -> dict[str, bool]:
    """Bibliothèques compilées dans ce NVEncC (``--check-features``) ; vide si la sonde échoue."""
    try:
        result = subprocess.run(
            [nvencc_bin, "--check-features"],
            capture_output=True,
            check=False,
            timeout=30,
            **subprocess_text_kwargs(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return {}
    return parse_check_features((result.stdout or "") + "\n" + (result.stderr or ""))


def nvencc_p5_features(nvencc_bin: str) -> bool:
    """libdovi et libplacebo compilés dans ce NVEncC (prérequis, pas une preuve)."""
    features = nvencc_features(nvencc_bin)
    return bool(features.get("libdovi") and features.get("libplacebo"))


@dataclass(frozen=True)
class P5ProbeResult:
    ok: bool
    reason: str = ""


_GENERATE_CONFIG = {
    "cm_version": "V40",
    "profile": "5",
    "length": _PROBE_FRAMES,
    "level6": {
        "max_display_mastering_luminance": 1000,
        "min_display_mastering_luminance": 1,
        "max_content_light_level": 1000,
        "max_frame_average_light_level": 400,
    },
}


def _halves_yavg(ffmpeg_bin: str, path: Path, *, prefilter: str = "") -> tuple[float, float] | None:
    """Luminance moyenne (codes 10 bits) des moitiés gauche (noir) et droite (gris), première image."""
    vf = ",".join(
        part
        for part in (prefilter, "crop=iw:ih/2:0:ih/4", "extractplanes=y", "scale=2:1:flags=area", "format=gray10le")
        if part
    )
    try:
        result = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path), "-frames:v", "1",
             "-vf", vf, "-f", "rawvideo", "-"],
            capture_output=True,
            check=False,
            timeout=60,
            **subprocess_windows_no_window_kwargs(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    data = result.stdout or b""
    if result.returncode != 0 or len(data) < 4:
        return None
    return float(int.from_bytes(data[0:2], "little")), float(int.from_bytes(data[2:4], "little"))


def run_nvencc_p5_probe(
    *,
    nvencc_bin: str,
    ffmpeg_bin: str,
    dovi_tool_bin: str,
    work_dir: Path,
    build_command: Callable[[str, VideoEncodeSettings, Path, Path], list[str]],
    has_ffmpeg_libplacebo: bool,
) -> P5ProbeResult:
    """Conversion P5 réelle par NVEncC sur un mini P5 synthétique.

    ``build_command(nvencc, video, sortie, entrée)`` : constructeur de production
    (mêmes options natives que l'encodage). Échec, compte de trames, noir ou gris
    hors tolérance → ``ok=False`` (le workflow passe alors par le pipe FFmpeg).
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="nvencc_p5_probe_", dir=str(work_dir)))

    def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(cmd, capture_output=True, check=False, timeout=120, **subprocess_text_kwargs())

    try:
        config = tmp / "p5.json"
        config.write_text(json.dumps(_GENERATE_CONFIG), encoding="utf-8")
        rpu, base, p5_hevc, p5_mkv, out = (
            tmp / "p5.bin", tmp / "bl.hevc", tmp / "p5.hevc", tmp / "p5.mkv", tmp / "out.hevc",
        )
        steps: list[tuple[str, list[str]]] = [
            ("dovi_tool generate", [dovi_tool_bin, "generate", "-j", str(config), "-o", str(rpu)]),
            ("image de base", [
                ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                "color=c=black:s=320x240:r=24,format=yuv420p10le,"
                f"geq=lum=if(lt(X\\,W/2)\\,0\\,{_PROBE_GRAY_FULL}):cb=512:cr=512",
                "-frames:v", str(_PROBE_FRAMES), "-c:v", "libx265",
                "-x265-params", "log-level=error:lossless=1:range=full:bframes=0", str(base),
            ]),
            ("dovi_tool inject-rpu", [dovi_tool_bin, "inject-rpu", "-i", str(base), "-r", str(rpu), "-o", str(p5_hevc)]),
            ("conteneur", [
                ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-fflags", "+genpts",
                "-r", "24", "-i", str(p5_hevc), "-c", "copy", str(p5_mkv),
            ]),
        ]
        for label, cmd in steps:
            result = run(cmd)
            if result.returncode != 0:
                return P5ProbeResult(False, f"préparation de la sonde ({label}) en échec")
        video = VideoEncodeSettings(
            codec="nvencc_hevc", rate_control="cqp", cq=0, preset="", p5_to_hdr10=True,
            dovi_source_profile="p5",
        )
        result = run(build_command(nvencc_bin, video, out, p5_mkv))
        log = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 or not out.is_file():
            return P5ProbeResult(False, "NVEncC n'a pas converti le P5 de test")
        encoded = re.search(r"encoded (\d+) frames", log)
        if encoded is None or int(encoded.group(1)) != _PROBE_FRAMES:
            return P5ProbeResult(False, "NVEncC : nombre de trames inattendu sur le P5 de test")
        measured = _halves_yavg(ffmpeg_bin, out)
        if measured is None:
            return P5ProbeResult(False, "mesure de la sortie de test impossible")
        black, gray = measured
        expected_gray = _PROBE_GRAY_EXPECTED
        if has_ffmpeg_libplacebo:
            reference = _halves_yavg(ffmpeg_bin, p5_mkv, prefilter=P5_TO_HDR10_FILTER)
            if reference is not None:
                expected_gray = reference[1]
        if abs(black - _BLACK_LIMITED) > _TOLERANCE or abs(gray - expected_gray) > _TOLERANCE:
            return P5ProbeResult(
                False,
                f"niveaux NVEncC inattendus (noir {black:.0f}, gris {gray:.0f} au lieu de "
                f"{_BLACK_LIMITED:.0f} / {expected_gray:.0f})",
            )
        return P5ProbeResult(True)
    except (OSError, subprocess.SubprocessError) as exc:
        return P5ProbeResult(False, f"sonde impossible : {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


__all__ = [
    "P5ProbeResult",
    "binary_signature",
    "nvencc_p5_features",
    "parse_check_features",
    "run_nvencc_p5_probe",
]
