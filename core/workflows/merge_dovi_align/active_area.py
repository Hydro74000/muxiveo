"""Zone active (L5) de Film 1 (plan § 3.5, décision D3).

Barres mesurées sur toutes les images de Film 1 (passe complète, masque de dépassement à pleine
résolution). Par plan, une barre est faite des lignes/colonnes **noires dans toutes les images du plan** : son
épaisseur ne dépasse jamais le minimum mesuré sur le plan. Valeur ramenée à la plus grande valeur dominante
du film qui ne la dépasse pas (plans sombres : pas de barre fantôme), sinon gardée telle que mesurée.
Garantie : aucune ligne ni colonne contenant un pixel visible n'est déclarée barre.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from core.i18n import translate_text

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
    #: Motif (modèle en français, paramètres dans ``reason_args``).
    reason: str
    reason_args: dict = field(default_factory=dict)

    def describe(self) -> str:
        """Description traduite (journal)."""
        reason = translate_text(self.reason, **self.reason_args)
        if self.keep:
            return translate_text("L5 de Film 2 conservé ({reason})", reason=reason)
        values = sorted({area for _a, _b, area in self.ranges})
        shown = ", ".join(f"{t}/{b}" + (f" · {left}/{right}" if left or right else "")
                          for left, right, t, b in values[:4]) + ("…" if len(values) > 4 else "")
        return translate_text("L5 réécrit d'après Film 1 ({reason}) : haut/bas {values}", reason=reason, values=shown)


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
    """Valeurs dominantes : valeurs regroupées à ± ``TOLERANCE`` px **avant** comptage, groupe retenu s'il
    réunit assez de plans ; représentant = plus petite valeur du groupe (arrondi inférieur sûr)."""
    if not values:
        return []
    threshold = max(2, int(np.ceil(DOMINANT_SHARE * len(values))))
    groups: list[list[int]] = []
    for value in sorted(values):
        if groups and value - groups[-1][0] <= 2 * TOLERANCE:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [group[0] for group in groups if len(group) >= threshold]


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
        reason, args = _concordance(shots, filled, alignment, film2_l5)
        if not reason:
            return ActiveAreaPlan(True, tuple(ranges), "concordance avec les barres de Film 1")
        return ActiveAreaPlan(False, tuple(ranges), reason, args)
    if film2_l5 is None:
        return ActiveAreaPlan(False, tuple(ranges), "L5 absent de Film 2")
    return ActiveAreaPlan(False, tuple(ranges), "Film 1 en {w1}×{h1}, Film 2 en {w2}×{h2}",
                          {"w1": width, "h1": height, "w2": film2_size[0], "h2": film2_size[1]})


def _concordance(shots: list[tuple[int, int, Area | None, bool]], filled: list[tuple[int, int, Area]],
                 alignment: AlignmentMap, film2_l5: np.ndarray) -> tuple[str, dict]:
    """Motif (modèle, paramètres) de non-concordance du L5 de Film 2, **projeté** sur chaque image de Film 1
    (images sans correspondance comprises, qui en recevraient une copie), avec les barres retenues ; vide si
    concordant."""
    projected = np.zeros((alignment.film1_frames, 4), dtype=np.int64)
    for seg in alignment.segments:
        projected[seg.film1_start:seg.film1_end] = film2_l5[seg.film2_start:seg.film2_end]
    for gap in alignment.uncovered:
        projected[gap.start:gap.start + gap.length] = film2_l5[gap.source]
    for (a, b, measured, _proof), (_a, _b, planned) in zip(shots, filled):
        if measured is None:
            continue  # plan entièrement noir : aucune ligne visible à couvrir
        authored = projected[a:b]
        if np.any(authored > np.asarray(measured)):
            return "L5 de Film 2 couvrant des lignes visibles de Film 1 (images {first}–{last})", \
                {"first": a, "last": b - 1}
        if np.any(np.abs(authored - np.asarray(planned)) > TOLERANCE):
            return "L5 de Film 2 différent des barres de Film 1 (images {first}–{last})", \
                {"first": a, "last": b - 1}
    return "", {}
