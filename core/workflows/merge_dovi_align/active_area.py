"""Zone active (L5) de Film 1 (plan § 3.5, décision D3).

Barres mesurées sur toutes les images de Film 1 (passe complète, masque de dépassement à pleine
résolution). Par plan, une barre est faite des lignes/colonnes **noires dans toutes les images du plan** : son
épaisseur ne dépasse jamais le minimum mesuré sur le plan. Valeur ramenée à la plus grande valeur dominante
du film qui ne la dépasse pas (plans sombres : pas de barre fantôme), sinon gardée telle que mesurée.
Garantie : aucune ligne ni colonne contenant un pixel visible n'est déclarée barre.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .align import AlignmentMap
from .probe import Signatures

#: Tolérance de regroupement des valeurs dominantes et de concordance avec le L5 de Film 2 (pixels).
TOLERANCE = 2
#: Part des images d'un plan au minimum mesuré pour que ce minimum fasse preuve d'un bord franc.
PROOF_SHARE = 0.8
#: Part des plans « preuves » qu'une valeur doit réunir pour être dominante.
DOMINANT_SHARE = 0.02

Area = tuple[int, int, int, int]  # gauche, droite, haut, bas


@dataclass(frozen=True)
class ActiveAreaPlan:
    """L5 à écrire : ``keep`` (celui de Film 2, reporté par les éditions de positions) ou plages de Film 1
    ``(début, fin, (gauche, droite, haut, bas))`` en pixels de Film 1."""

    keep: bool
    ranges: tuple[tuple[int, int, Area], ...]
    reason: str

    def describe(self) -> str:
        if self.keep:
            return f"L5 de Film 2 conservé ({self.reason})"
        values = sorted({area for _a, _b, area in self.ranges})
        shown = ", ".join(f"{t}/{b}" + (f" · {left}/{right}" if left or right else "")
                          for left, right, t, b in values[:4])
        return f"L5 réécrit d'après Film 1 ({self.reason}) : haut/bas {shown}" + ("…" if len(values) > 4 else "")


def l5_per_frame(level5_json: Path, frames: int) -> np.ndarray:
    """L5 par image ``(images, 4)`` (gauche, droite, haut, bas) depuis ``dovi_tool export -d level5=``."""
    document = json.loads(Path(level5_json).read_text(encoding="utf-8"))
    presets = {int(p["id"]): (int(p.get("left", 0)), int(p.get("right", 0)), int(p.get("top", 0)),
                              int(p.get("bottom", 0))) for p in document.get("presets") or []}
    out = np.zeros((frames, 4), dtype=np.int32)
    for key, preset in (document.get("edits") or {}).items():
        area = presets.get(int(preset), (0, 0, 0, 0))
        if key == "all":
            out[:] = area
            continue
        first, _sep, last = str(key).partition("-")
        out[int(first):int(last or first) + 1] = area
    return out


def _shot_sides(signatures: Signatures, a: int, b: int) -> tuple[Area | None, bool]:
    """Barres (minimum sur les images non noires du plan) et preuve d'un bord franc ; None si tout est noir."""
    sides = np.stack([signatures.left[a:b], signatures.right[a:b], signatures.top[a:b], signatures.bottom[a:b]],
                     axis=1).astype(np.int64)
    visible = signatures.top[a:b] >= 0
    if not np.any(visible):
        return None, False
    rows = sides[visible]
    minimum = rows.min(axis=0)
    proof = bool(np.all(np.mean(rows == minimum, axis=0) >= PROOF_SHARE))
    return (int(minimum[0]), int(minimum[1]), int(minimum[2]), int(minimum[3])), proof


def _dominants(values: list[int]) -> list[int]:
    if not values:
        return []
    counts: dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    threshold = max(2, int(np.ceil(DOMINANT_SHARE * len(values))))
    out: list[int] = []
    for value in sorted(counts, key=lambda v: (-counts[v], v)):
        if counts[value] < threshold:
            break
        if all(abs(value - kept) > TOLERANCE for kept in out):
            out.append(value)
    return sorted(out)


def _snap(value: int, dominants: list[int]) -> int:
    below = [d for d in dominants if d <= value]
    return max(below) if below else value


def plan_active_area(film1: Signatures, cuts1: np.ndarray, alignment: AlignmentMap, *,
                     film2_l5: np.ndarray | None, film2_size: tuple[int, int]) -> ActiveAreaPlan:
    """L5 de la sortie : conservé si Film 1 a les dimensions de Film 2 et que le L5 de Film 2 concorde avec les
    barres mesurées de Film 1 sur toutes les images (jamais une ligne visible couverte, bords francs à
    ± ``TOLERANCE`` px) ; sinon réécrit d'après Film 1."""
    width, height = film1.width, film1.height
    bounds = np.append(np.asarray(cuts1, dtype=np.int64), len(film1))
    shots: list[tuple[int, int, Area | None, bool]] = []
    for a, b in zip(bounds[:-1].tolist(), bounds[1:].tolist()):
        area, proof = _shot_sides(film1, a, b)
        shots.append((a, b, area, proof))
    proofs = [area for _a, _b, area, proof in shots if area is not None and proof]
    dominants = [_dominants([area[side] for area in proofs]) for side in range(4)]
    planned: list[tuple[int, int, Area | None]] = []
    for a, b, area, _proof in shots:
        if area is None:
            planned.append((a, b, None))
            continue
        snapped = tuple(_snap(area[side], dominants[side]) for side in range(4))
        planned.append((a, b, (snapped[0], snapped[1], snapped[2], snapped[3])))
    # Plans entièrement noirs : toute ligne y est noire, la valeur du plan voisin y est donc sûre.
    known = [area for _a, _b, area in planned if area is not None]
    fallback: Area = known[0] if known else (0, 0, 0, 0)
    filled: list[tuple[int, int, Area]] = []
    for a, b, area in planned:
        value = area if area is not None else (filled[-1][2] if filled else fallback)
        left, right, top, bottom = value
        if top + bottom >= height or left + right >= width:
            value = (0, 0, 0, 0)
        filled.append((a, b, value))
    ranges: list[tuple[int, int, Area]] = []
    for a, b, area in filled:
        if ranges and ranges[-1][2] == area and ranges[-1][1] == a:
            ranges[-1] = (ranges[-1][0], b, area)
        else:
            ranges.append((a, b, area))

    if film2_l5 is not None and (width, height) == tuple(film2_size):
        reason = _concordance(shots, filled, alignment, film2_l5)
        if not reason:
            return ActiveAreaPlan(True, tuple(ranges), "concordance avec les barres de Film 1")
        return ActiveAreaPlan(False, tuple(ranges), reason)
    if film2_l5 is None:
        return ActiveAreaPlan(False, tuple(ranges), "L5 absent de Film 2")
    return ActiveAreaPlan(False, tuple(ranges),
                          f"Film 1 en {width}×{height}, Film 2 en {film2_size[0]}×{film2_size[1]}")


def _concordance(shots: list[tuple[int, int, Area | None, bool]], filled: list[tuple[int, int, Area]],
                 alignment: AlignmentMap, film2_l5: np.ndarray) -> str:
    """Motif de non-concordance du L5 de Film 2 avec Film 1 ; vide si concordant."""
    for (a, b, measured, proof), (_a, _b, planned) in zip(shots, filled):
        for seg in alignment.segments:
            lo, hi = max(a, seg.film1_start), min(b, seg.film1_end)
            if hi <= lo:
                continue
            authored = film2_l5[lo + seg.offset:hi + seg.offset]
            if measured is not None and np.any(authored > np.asarray(measured)):
                return f"L5 de Film 2 couvrant des lignes visibles de Film 1 (images {lo}–{hi - 1})"
            if proof and np.any(np.abs(authored - np.asarray(planned)) > TOLERANCE):
                return f"L5 de Film 2 différent des barres de Film 1 (images {lo}–{hi - 1})"
    return ""
