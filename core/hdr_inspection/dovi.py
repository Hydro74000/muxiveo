"""Analyse complète d'un RPU Dolby Vision (export JSON de ``dovi_tool``).

Une passe en flux sur l'export : L1 par image, plans, zones actives L5, jeux L6, cibles
des trims (L2, L8/L10), primaires L9, type de contenu L11, version CM et couche EL.
"""
from __future__ import annotations

from array import array
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core.hdr_inspection.json_stream import iter_json_array
from core.hdr_inspection.pq import nice_nits, pq12_to_nits

#: Primaires L9 indexées (Dolby Vision) : noms usuels.
L9_PRIMARY_NAMES = {0: "P3-D65", 1: "BT.709", 2: "BT.2020", 3: "SMPTE-C", 4: "BT.601", 5: "P3-DCI"}


@dataclass(frozen=True)
class ActiveArea:
    """Zone active L5 constante sur un segment d'images."""

    first_frame: int
    frames: int
    left: int
    right: int
    top: int
    bottom: int


@dataclass(frozen=True)
class L6Set:
    """Jeu L6 (métadonnées HDR10 portées par le RPU) et son nombre d'images."""

    max_mastering: int
    min_mastering: float
    max_cll: int
    max_fall: int
    frames: int


@dataclass(frozen=True, eq=False)
class DoviAnalysis:
    frames: int
    profile: int | None
    el_type: str | None
    #: Images dont l'EL contribue (FEL : résiduel non désactivé).
    residual_active_frames: int
    cm_version: str
    scene_starts: np.ndarray
    #: Codes PQ 12 bits par image.
    l1_min: np.ndarray
    l1_max: np.ndarray
    l1_avg: np.ndarray
    l1_missing: int
    active_areas: tuple[ActiveArea, ...]
    l6: tuple[L6Set, ...]
    l2_targets: tuple[int, ...]
    l8_targets: tuple[int, ...]
    l10_targets: tuple[int, ...]
    l9_primaries: tuple[int, ...]
    l11_content: tuple[int, ...]
    source_display: tuple[tuple[float, int], ...]
    reused_rpu_frames: int

    @property
    def scenes(self) -> int:
        return int(self.scene_starts.size)

    def l1_peak_nits(self) -> float:
        """Plus haut L1 max (pic robuste de l'analyseur Dolby, en nits)."""
        return float(pq12_to_nits(int(self.l1_max.max()))) if self.frames else 0.0

    def l1_fall_nits(self) -> float:
        """Plus haute moyenne L1 (équivalent MaxFALL), en nits."""
        return float(pq12_to_nits(int(self.l1_avg.max()))) if self.frames else 0.0

    def scene_lengths(self) -> np.ndarray:
        if not self.frames:
            return np.zeros(0, dtype=np.int64)
        bounds = np.append(self.scene_starts.astype(np.int64), self.frames)
        return np.diff(bounds)


def _blocks(dm: dict) -> list[tuple[str, dict]]:
    blocks: list[tuple[str, dict]] = []
    for name in ("cmv29_metadata", "cmv40_metadata"):
        meta = dm.get(name)
        if not isinstance(meta, dict):
            continue
        for block in meta.get("ext_metadata_blocks") or ():
            if isinstance(block, dict):
                for level, payload in block.items():
                    if isinstance(payload, dict):
                        blocks.append((level, payload))
    return blocks


def _target_nits(codes: set[int]) -> tuple[int, ...]:
    return tuple(sorted({nice_nits(float(nits)) for nits in pq12_to_nits(np.array(sorted(codes)))})) if codes else ()


def parse_dovi_export(
    path: Path,
    *,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> DoviAnalysis:
    """Analyse l'export ``dovi_tool export -d all=…`` (une entrée par image, ordre d'affichage)."""
    l1_min, l1_max, l1_avg = array("H"), array("H"), array("H")
    scene_starts = array("i")
    profiles: Counter[int] = Counter()
    el_types: Counter[str] = Counter()
    residual_active = 0
    cm40 = False
    missing = 0
    reused = 0
    areas: list[list[int]] = []
    l6: Counter[tuple[int, float, int, int]] = Counter()
    l2: set[int] = set()
    l8: set[int] = set()
    l10: set[int] = set()
    l9: set[int] = set()
    l11: set[int] = set()
    # Codes PQ bruts, convertis une seule fois en fin d'analyse (un film : ~250 000 RPU).
    sources: set[tuple[int, int]] = set()
    last_l1 = (0, 0, 0)
    last_area = (0, 0, 0, 0)
    frame = -1
    for frame, rpu in enumerate(iter_json_array(path)):
        if cancelled is not None and frame % 4096 == 0 and cancelled():
            raise InterruptedError
        if progress is not None and frame % 16384 == 0:
            progress(frame)
        if not isinstance(rpu, dict):
            raise ValueError(f"Export RPU invalide (image {frame}).")
        profile = rpu.get("dovi_profile")
        if isinstance(profile, int):
            profiles[profile] += 1
        header = rpu.get("header") or {}
        if header.get("use_prev_vdr_rpu_flag"):
            reused += 1
        el_type = rpu.get("el_type")
        if isinstance(el_type, str):
            el_types[el_type] += 1
            if not header.get("disable_residual_flag", True):
                residual_active += 1
        dm = rpu.get("vdr_dm_data") or {}
        if isinstance(dm.get("cmv40_metadata"), dict):
            cm40 = True
        if dm.get("scene_refresh_flag") or frame == 0:
            scene_starts.append(frame)
        source_min, source_max = dm.get("source_min_pq"), dm.get("source_max_pq")
        if isinstance(source_min, int) and isinstance(source_max, int):
            sources.add((source_min, source_max))
        level1 = None
        area = None
        for level, block in _blocks(dm):
            if level == "Level1":
                level1 = (int(block.get("min_pq", 0)), int(block.get("max_pq", 0)), int(block.get("avg_pq", 0)))
            elif level == "Level2":
                l2.add(int(block.get("target_max_pq", 0)))
            elif level == "Level5":
                area = (int(block.get("active_area_left_offset", 0)), int(block.get("active_area_right_offset", 0)),
                        int(block.get("active_area_top_offset", 0)), int(block.get("active_area_bottom_offset", 0)))
            elif level == "Level6":
                l6[(int(block.get("max_display_mastering_luminance", 0)),
                    int(block.get("min_display_mastering_luminance", 0)) / 10000.0,
                    int(block.get("max_content_light_level", 0)),
                    int(block.get("max_frame_average_light_level", 0)))] += 1
            elif level == "Level8":
                l8.add(int(block.get("target_display_index", 0)))
            elif level == "Level9":
                l9.add(int(block.get("source_primary_index", 255)))
            elif level == "Level10":
                l10.add(int(block.get("target_max_pq", 0)))
            elif level == "Level11":
                l11.add(int(block.get("content_type", 0)))
        if level1 is None:
            missing += 1
            level1 = last_l1
        last_l1 = level1
        l1_min.append(level1[0])
        l1_max.append(level1[1])
        l1_avg.append(level1[2])
        # Sans L5, la zone active vaut l'image entière.
        area = area if area is not None else (0, 0, 0, 0)
        if not areas or area != last_area:
            areas.append([frame, 1, *area])
        else:
            areas[-1][1] += 1
        last_area = area
    frames = frame + 1
    return DoviAnalysis(
        frames=frames,
        profile=profiles.most_common(1)[0][0] if profiles else None,
        el_type=el_types.most_common(1)[0][0] if el_types else None,
        residual_active_frames=residual_active,
        cm_version="4.0" if cm40 else "2.9",
        scene_starts=np.frombuffer(scene_starts, dtype=np.int32).copy(),
        l1_min=np.frombuffer(l1_min, dtype=np.uint16).copy(),
        l1_max=np.frombuffer(l1_max, dtype=np.uint16).copy(),
        l1_avg=np.frombuffer(l1_avg, dtype=np.uint16).copy(),
        l1_missing=missing,
        active_areas=tuple(ActiveArea(*values) for values in areas),
        l6=tuple(L6Set(*key, frames=count) for key, count in l6.most_common()),
        l2_targets=_target_nits(l2),
        l8_targets=tuple(sorted(l8)),
        l10_targets=_target_nits(l10),
        l9_primaries=tuple(sorted(l9)),
        l11_content=tuple(sorted(l11)),
        source_display=tuple(sorted({(round(float(pq12_to_nits(lo)), 4), nice_nits(float(pq12_to_nits(hi))))
                                     for lo, hi in sources})),
        reused_rpu_frames=reused,
    )
