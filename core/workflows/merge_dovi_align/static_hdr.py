"""HDR10 statique de la sortie, résolu une seule fois par champ (plan § 4.3).

La même décision alimente les SEI injectés, l'élément Colour du conteneur et le L6 du RPU : le L6 décrit
ainsi toujours l'image de base réellement écrite. Champ présent dans Film 1 → Film 1 (c'est son image) ;
sinon Film 2 ; absent des deux → L6 du RPU conservé pour ce champ.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_LUMINANCE = re.compile(r"L\((\d+),(\d+)\)")


@dataclass(frozen=True)
class ResolvedStatic:
    """Valeurs retenues (format x265 ``G(…)B(…)R(…)WP(…)L(max,min)`` et ``"MaxCLL,MaxFALL"``) et provenance
    (« film1 », « film2 » ou vide)."""

    master_display: str
    max_cll: str
    master_display_from: str
    max_cll_from: str

    @property
    def is_complete(self) -> bool:
        return bool(self.master_display and self.max_cll)

    def describe(self) -> str:
        parts = []
        if self.master_display:
            parts.append(f"écran de mastering de {_label(self.master_display_from)}")
        if self.max_cll:
            parts.append(f"MaxCLL/MaxFALL de {_label(self.max_cll_from)}")
        return ", ".join(parts) or "aucune valeur statique"


def _label(origin: str) -> str:
    return {"film1": "Film 1", "film2": "Film 2"}.get(origin, "?")


def resolve_static_hdr(film1: object, film2: object) -> ResolvedStatic:
    """Décision par champ : Film 1 s'il le porte, sinon Film 2 (objets à attributs ``master_display`` et
    ``max_cll``, chaînes vides si absents)."""
    def pick(name: str) -> tuple[str, str]:
        first = str(getattr(film1, name, "") or "")
        second = str(getattr(film2, name, "") or "")
        if first:
            return first, "film1"
        if second:
            return second, "film2"
        return "", ""

    mdcv, mdcv_from = pick("master_display")
    cll, cll_from = pick("max_cll")
    return ResolvedStatic(mdcv, cll, mdcv_from, cll_from)


def level6_edit(resolved: ResolvedStatic, current: tuple[int, int, int, int] | None) -> dict | None:
    """Bloc ``level6`` de ``dovi_tool editor`` alignant le L6 sur ``resolved`` ; champs absents → valeurs
    ``current`` du RPU (mastering max en nits, min en 0,0001 nit, MaxCLL, MaxFALL). None si rien ne change
    ou si une valeur manque des deux côtés."""
    max_nits = min_units = cll = fall = None
    match = _LUMINANCE.search(resolved.master_display)
    if match is not None:
        max_nits = round(int(match.group(1)) / 10000)
        min_units = int(match.group(2))
    if resolved.max_cll:
        try:
            cll, fall = (int(value) for value in resolved.max_cll.split(",")[:2])
        except ValueError:
            cll = fall = None
    if current is not None:
        max_nits = current[0] if max_nits is None else max_nits
        min_units = current[1] if min_units is None else min_units
        cll = current[2] if cll is None else cll
        fall = current[3] if fall is None else fall
    if None in (max_nits, min_units, cll, fall):
        return None
    values = (int(max_nits), int(min_units), int(cll), int(fall))  # type: ignore[arg-type]
    if current is not None and tuple(current) == values:
        return None
    return {
        "max_display_mastering_luminance": values[0],
        "min_display_mastering_luminance": values[1],
        "max_content_light_level": values[2],
        "max_frame_average_light_level": values[3],
    }
