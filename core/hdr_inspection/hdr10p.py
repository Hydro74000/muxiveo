"""Analyse complète des métadonnées HDR10+ (JSON de ``hdr10plus_tool extract``)."""
from __future__ import annotations

import re
from array import array
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core.hdr_inspection.json_stream import iter_json_array, read_json_head

_PROFILE_RE = re.compile(r'"HDR10plusProfile"\s*:\s*"([^"]*)"')
#: MaxScl au maximum du format (10 000 nits) : valeur saturée, jamais un pic mesuré crédible.
SATURATED_NITS = 9999.0


@dataclass(frozen=True, eq=False)
class Hdr10PlusAnalysis:
    frames: int
    profile: str
    scene_starts: np.ndarray
    #: Nits par image : plus grand MaxScl (R, G, B), AverageRGB, percentile 99,98 % de maxRGB.
    max_scl: np.ndarray
    average_rgb: np.ndarray
    p99: np.ndarray
    windows: tuple[int, ...]
    #: TargetedSystemDisplayMaximumLuminance distincts (nits, 0 = non renseigné).
    targeted_display: tuple[int, ...]
    bezier_frames: int

    @property
    def scenes(self) -> int:
        return int(self.scene_starts.size)

    def peak_nits(self) -> float:
        return float(self.max_scl.max()) if self.frames else 0.0

    def saturated(self) -> np.ndarray:
        """Images dont le MaxScl vaut le maximum du format."""
        return np.nonzero(self.max_scl >= SATURATED_NITS)[0]

    def credible_peak_nits(self) -> float:
        """Pic MaxScl hors images saturées."""
        credible = self.max_scl[self.max_scl < SATURATED_NITS]
        return float(credible.max()) if credible.size else 0.0

    def max_average_nits(self) -> float:
        return float(self.average_rgb.max()) if self.frames else 0.0


def _tenths(value: object) -> float:
    return float(value) / 10.0 if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def parse_hdr10plus_json(
    path: Path,
    *,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Hdr10PlusAnalysis:
    """Analyse le JSON HDR10+ (une entrée ``SceneInfo`` par image, ordre d'affichage)."""
    match = _PROFILE_RE.search(read_json_head(path))
    profile = match.group(1) if match else "?"
    max_scl, average, p99 = array("f"), array("f"), array("f")
    starts = array("i")
    windows: Counter[int] = Counter()
    targets: set[int] = set()
    bezier = 0
    frame = -1
    for frame, info in enumerate(iter_json_array(path, key="SceneInfo")):
        if cancelled is not None and frame % 4096 == 0 and cancelled():
            raise InterruptedError
        if progress is not None and frame % 16384 == 0:
            progress(frame)
        if not isinstance(info, dict):
            raise ValueError(f"JSON HDR10+ invalide (image {frame}).")
        luminance = info.get("LuminanceParameters") or {}
        scl = luminance.get("MaxScl") or (0, 0, 0)
        max_scl.append(max(_tenths(value) for value in scl) if scl else 0.0)
        average.append(_tenths(luminance.get("AverageRGB")))
        distribution = luminance.get("LuminanceDistributions") or {}
        indices: list[object] = list(distribution.get("DistributionIndex") or ())
        values: list[object] = list(distribution.get("DistributionValues") or ())
        p99.append(_tenths(values[indices.index(99)]) if 99 in indices and len(values) == len(indices) else 0.0)
        if info.get("SceneFrameIndex") == 0 or frame == 0:
            starts.append(frame)
        windows[int(info.get("NumberOfWindows") or 1)] += 1
        targets.add(int(info.get("TargetedSystemDisplayMaximumLuminance") or 0))
        if info.get("BezierCurveData"):
            bezier += 1
    return Hdr10PlusAnalysis(
        frames=frame + 1,
        profile=profile,
        scene_starts=np.frombuffer(starts, dtype=np.int32).copy(),
        max_scl=np.frombuffer(max_scl, dtype=np.float32).copy(),
        average_rgb=np.frombuffer(average, dtype=np.float32).copy(),
        p99=np.frombuffer(p99, dtype=np.float32).copy(),
        windows=tuple(sorted(windows)),
        targeted_display=tuple(sorted(targets)),
        bezier_frames=bezier,
    )
