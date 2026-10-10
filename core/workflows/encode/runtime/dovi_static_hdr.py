"""HDR10 statique (MDCV, MaxCLL/MaxFALL) estimé depuis le RPU Dolby Vision d'une source P5.

Les valeurs viennent des métadonnées du RPU, pas d'une mesure de l'image
encodée. Règles par champ :
- luminances du mastering : L6, sinon ``source_min_pq`` / ``source_max_pq``
  (codes PQ 12 bits lus en données structurées), sinon 0,0001 / 1000 nits ;
- primaires : L9 (présélection indexée ou coordonnées personnalisées, échelle
  1/32767 de dovi_tool convertie en 1/50000 MDCV), sinon P3-D65 ;
- MaxCLL puis MaxFALL, chacun séparément : L6 s'il est non nul, sinon le
  maximum des L1 (résumé dovi_tool).
Plusieurs jeux L6 : maximum des pics, minimum des minima. Plusieurs jeux L9 :
jeu complet le plus fréquent (comptage sur toutes les trames), avec avertissement.
Le résumé ne nomme qu'une présélection par jeu (des jeux personnalisés partagent
l'index 255 et un même libellé) : le comptage complet est lancé dès qu'un L9
personnalisé, plusieurs libellés ou une première trame sans L9 laissent un doute.
Export temporaire d'environ 3 Ko par trame (≈ 520 Mo pour 2 h à 24 i/s), dans
le dossier du RPU, supprimé après lecture.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


_Primaries = tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]

_DV_PRIMARY_SCALE = 32767
_MDCV_SCALE = 50000
# Présélections L9 de dovi_tool (``extension_metadata/primaries.rs``) :
# R(x, y), G(x, y), B(x, y), point blanc(x, y).
_L9_PRESETS: tuple[tuple[float, float, float, float, float, float, float, float], ...] = (
    (0.68, 0.32, 0.265, 0.69, 0.15, 0.06, 0.3127, 0.329),             # 0 DCI-P3 D65
    (0.64, 0.33, 0.30, 0.60, 0.15, 0.06, 0.3127, 0.329),               # 1 BT.709
    (0.708, 0.292, 0.170, 0.797, 0.131, 0.046, 0.3127, 0.329),         # 2 BT.2020
    (0.63, 0.34, 0.31, 0.595, 0.155, 0.07, 0.3127, 0.329),             # 3 BT.601 NTSC / SMPTE-C
    (0.64, 0.33, 0.29, 0.60, 0.15, 0.06, 0.3127, 0.329),               # 4 BT.601 PAL
    (0.68, 0.32, 0.265, 0.69, 0.15, 0.06, 0.314, 0.351),               # 5 DCI-P3
    (0.7347, 0.2653, 0.0, 1.0, 0.0001, -0.077, 0.32168, 0.33767),      # 6 ACES
    (0.73, 0.28, 0.14, 0.855, 0.10, -0.05, 0.3127, 0.329),             # 7 S-Gamut
    (0.766, 0.275, 0.225, 0.80, 0.089, -0.087, 0.3127, 0.329),         # 8 S-Gamut-3.Cine
)
_DEFAULT_PRESET = 0
_DEFAULT_MAX_NITS = 1000.0
_DEFAULT_MIN_NITS = 0.0001

_L6_RE = re.compile(
    r"Mastering display:\s*([0-9.]+)/([0-9.]+)\s*nits\.\s*MaxCLL:\s*([0-9.]+)\s*nits,\s*MaxFALL:\s*([0-9.]+)\s*nits"
)
_L1_RE = re.compile(r"\(L1\):\s*MaxCLL:\s*([0-9.]+)\s*nits,\s*MaxFALL:\s*([0-9.]+)\s*nits")
_L9_RE = re.compile(r"L9 MDP:\s*(.+)")
_L9_BLOCK_RE = re.compile(r'"Level9":\s*(\{[^{}]*\})')
_L9_KEY = '"Level9"'
_EXPORT_CHUNK = 1 << 20
# Un bloc L9 sérialisé fait moins de 400 octets : au-delà, export malformé.
_L9_BLOCK_MAX = 4096

_LABELS = {
    "rpu_l6": "L6 du RPU",
    "rpu_source_pq": "mastering du RPU (source_max_pq)",
    "rpu_l1": "maxima L1 du RPU",
    "rpu_l9": "L9 du RPU",
    "default": "valeur par défaut 1000 nits",
    "default_p3_d65": "P3-D65 supposé",
    "": "indisponible",
}


@dataclass(frozen=True)
class RpuStaticHdr:
    """Valeurs HDR10 statiques estimées et leur provenance (par champ)."""

    master_display: str
    max_cll: str
    #: "rpu_l6" | "rpu_source_pq" | "default"
    luminance_source: str
    #: "rpu_l9" | "default_p3_d65"
    primaries_source: str
    #: MaxCLL : "rpu_l6" | "rpu_l1" | "" (aucune valeur)
    max_cll_source: str
    #: MaxFALL : "rpu_l6" | "rpu_l1" | "" (aucune valeur)
    max_fall_source: str = ""
    warnings: tuple[str, ...] = ()

    def describe_master_display(self) -> str:
        return f"luminance {_LABELS[self.luminance_source]}, primaires {_LABELS[self.primaries_source]}"

    def describe_light_levels(self) -> str:
        return f"MaxCLL {_LABELS[self.max_cll_source]}, MaxFALL {_LABELS[self.max_fall_source]}"

    def describe(self) -> str:
        return f"{self.describe_master_display()}, {self.describe_light_levels()}"


@dataclass(frozen=True)
class RpuSummary:
    """Données utiles du résumé ``dovi_tool info --summary``."""

    #: Jeux L6 : (luminance min, luminance max, MaxCLL, MaxFALL) en nits.
    l6: tuple[tuple[float, float, float, float], ...] = ()
    #: Maxima calculés depuis les L1 : (MaxCLL, MaxFALL) en nits.
    l1: tuple[float, float] | None = None
    #: Nombre de jeux L9 distincts (les noms du résumé ne décrivent pas les jeux personnalisés).
    l9_sets: int = 0


@dataclass(frozen=True)
class RpuFrame:
    """Données structurées d'une trame (``dovi_tool info -f``)."""

    source_min_pq: int | None = None
    source_max_pq: int | None = None
    #: Primaires L9 de la trame (×50000), présélection ou coordonnées personnalisées.
    primaries: _Primaries | None = None
    #: L9 à coordonnées personnalisées (index 255) : non distingué par le résumé.
    l9_custom: bool = False


def pq_code_to_nits(code: int, bits: int = 12) -> float:
    """Code PQ (SMPTE ST 2084) sur ``bits`` bits → luminance en nits."""
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    value = max(0.0, min(1.0, int(code) / float((1 << bits) - 1)))
    power = value ** (1.0 / m2)
    return 10000.0 * (max(power - c1, 0.0) / (c2 - c3 * power)) ** (1.0 / m1)


def master_display_string(primaries: _Primaries, max_nits: float, min_nits: float) -> str:
    (gx, gy), (bx, by), (rx, ry), (wx, wy) = primaries
    return (
        f"G({gx},{gy})B({bx},{by})R({rx},{ry})WP({wx},{wy})"
        f"L({int(round(max_nits * 10000))},{max(1, int(round(min_nits * 10000)))})"
    )


def _mdcv(value: float) -> int:
    """Chromaticité (0–1) en unités MDCV 1/50000, bornée au domaine représentable."""
    return max(0, min(_MDCV_SCALE, int(round(value * _MDCV_SCALE))))


def _preset_primaries(index: int) -> _Primaries:
    rx, ry, gx, gy, bx, by, wx, wy = _L9_PRESETS[index]
    return ((_mdcv(gx), _mdcv(gy)), (_mdcv(bx), _mdcv(by)), (_mdcv(rx), _mdcv(ry)), (_mdcv(wx), _mdcv(wy)))


def l9_primaries(level9: object) -> _Primaries | None:
    """Primaires d'un bloc L9 : coordonnées personnalisées (1/32767) ou présélection indexée."""
    if not isinstance(level9, dict):
        return None
    if "source_primary_red_x" in level9:
        try:
            def custom(name: str) -> tuple[int, int]:
                return (
                    _mdcv(int(level9[f"source_primary_{name}_x"]) / _DV_PRIMARY_SCALE),
                    _mdcv(int(level9[f"source_primary_{name}_y"]) / _DV_PRIMARY_SCALE),
                )

            return (custom("green"), custom("blue"), custom("red"), custom("white"))
        except (KeyError, TypeError, ValueError):
            return None
    index = level9.get("source_primary_index")
    if isinstance(index, int) and 0 <= index < len(_L9_PRESETS):
        return _preset_primaries(index)
    return None


def parse_rpu_summary(text: str) -> RpuSummary:
    """Jeux L6, maxima L1 et nombre de jeux L9 du résumé ``dovi_tool info --summary``."""
    l6 = tuple(
        (float(lo), float(hi), float(cll), float(fall))
        for lo, hi, cll, fall in (match.groups() for match in _L6_RE.finditer(text))
    )
    l1_match = _L1_RE.search(text)
    l9_match = _L9_RE.search(text)
    l9_sets = len([item for item in l9_match.group(1).split(",") if item.strip()]) if l9_match else 0
    return RpuSummary(
        l6=l6,
        l1=(float(l1_match.group(1)), float(l1_match.group(2))) if l1_match else None,
        l9_sets=l9_sets,
    )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_rpu_frame(text: str) -> RpuFrame:
    """``source_min_pq`` / ``source_max_pq`` et L9 d'une trame (``dovi_tool info -f``)."""
    start = text.find("{")
    if start < 0:
        return RpuFrame()
    try:
        data = json.loads(text[start:])
    except json.JSONDecodeError:
        return RpuFrame()
    dm = data.get("vdr_dm_data") if isinstance(data, dict) else None
    if not isinstance(dm, dict):
        return RpuFrame()
    primaries: _Primaries | None = None
    custom = False
    for key in ("cmv40_metadata", "cmv29_metadata"):
        block = dm.get(key)
        entries = block.get("ext_metadata_blocks", []) if isinstance(block, dict) else []
        for entry in entries:
            if isinstance(entry, dict) and "Level9" in entry:
                primaries = l9_primaries(entry["Level9"]) or primaries
                custom = custom or (isinstance(entry["Level9"], dict) and "source_primary_red_x" in entry["Level9"])
    return RpuFrame(
        source_min_pq=_int_or_none(dm.get("source_min_pq")),
        source_max_pq=_int_or_none(dm.get("source_max_pq")),
        primaries=primaries,
        l9_custom=custom,
    )


def needs_l9_count(summary: RpuSummary, frame: RpuFrame) -> bool:
    """Comptage complet requis : plusieurs libellés, L9 personnalisé ou première trame sans L9."""
    return summary.l9_sets > 1 or frame.l9_custom or (summary.l9_sets >= 1 and frame.primaries is None)


def count_l9_sets(
    export_json: Path,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[_Primaries, int]:
    """Jeux L9 complets d'un export ``dovi_tool export -d all`` (lecture en flux, fichier volumineux).

    Chaque bloc distinct n'est décodé qu'une fois ; l'annulation est vérifiée à chaque morceau.
    """
    raw_counts: dict[str, int] = {}
    tail = ""
    with export_json.open(encoding="utf-8") as handle:
        while chunk := handle.read(_EXPORT_CHUNK):
            if check_cancelled is not None:
                check_cancelled()
            text = tail + chunk
            last_end = 0
            for match in _L9_BLOCK_RE.finditer(text):
                raw = match.group(1)
                raw_counts[raw] = raw_counts.get(raw, 0) + 1
                last_end = match.end()
            # Bloc coupé en fin de morceau : repris au morceau suivant (borné si malformé).
            cut = text.find(_L9_KEY, last_end)
            tail = text[cut:] if cut >= 0 and len(text) - cut <= _L9_BLOCK_MAX else text[-len(_L9_KEY):]
    counts: dict[_Primaries, int] = {}
    for raw, count in raw_counts.items():
        try:
            primaries = l9_primaries(json.loads(raw))
        except json.JSONDecodeError:
            primaries = None
        if primaries is not None:
            counts[primaries] = counts.get(primaries, 0) + count
    return counts


def static_hdr_from_rpu_data(
    summary: RpuSummary,
    frame: RpuFrame,
    l9_counts: dict[_Primaries, int] | None = None,
    *,
    prefer_l1: bool = False,
) -> RpuStaticHdr:
    """Combine résumé, trame structurée et comptage L9 selon les règles par champ (fonction pure)."""
    warnings: list[str] = []
    if len(summary.l6) > 1:
        warnings.append(f"RPU : {len(summary.l6)} jeux L6 différents ; valeurs extrêmes retenues.")

    mastering = [(lo, hi) for lo, hi, _cll, _fall in summary.l6 if hi > 0]
    if mastering:
        max_nits = max(hi for _lo, hi in mastering)
        min_nits = min(lo for lo, _hi in mastering) or _DEFAULT_MIN_NITS
        luminance_source = "rpu_l6"
    elif frame.source_max_pq:
        # Code PQ 12 bits quantifié (1000 nits → 3079 → 1000,6) : pic à la dizaine.
        peak = pq_code_to_nits(frame.source_max_pq)
        max_nits = float(round(peak / 10) * 10 if peak >= 100 else round(peak))
        min_nits = pq_code_to_nits(frame.source_min_pq) if frame.source_min_pq else _DEFAULT_MIN_NITS
        luminance_source = "rpu_source_pq"
    else:
        max_nits, min_nits, luminance_source = _DEFAULT_MAX_NITS, _DEFAULT_MIN_NITS, "default"
        warnings.append("RPU sans luminance de mastering : 1000 / 0,0001 nits supposés.")

    primaries: _Primaries
    if l9_counts:
        primaries, primaries_source = max(l9_counts.items(), key=lambda item: item[1])[0], "rpu_l9"
        if len(l9_counts) > 1:
            warnings.append(
                f"RPU : {len(l9_counts)} jeux de primaires L9 différents ; le plus fréquent est retenu."
            )
    elif frame.primaries is not None:
        primaries, primaries_source = frame.primaries, "rpu_l9"
        if needs_l9_count(summary, frame):
            warnings.append(
                "RPU : primaires L9 non comptées sur toutes les trames ; celles de la première trame sont retenues."
            )
    else:
        primaries, primaries_source = _preset_primaries(_DEFAULT_PRESET), "default_p3_d65"
        warnings.append("RPU sans primaires L9 exploitables : P3-D65 supposé.")

    # MaxCLL et MaxFALL résolus séparément : L6 non nul, sinon maximum des L1.
    l6_cll = max((cll for _lo, _hi, cll, _fall in summary.l6 if cll > 0), default=0.0)
    l6_fall = max((fall for _lo, _hi, _cll, fall in summary.l6 if fall > 0), default=0.0)
    if prefer_l1:
        l6_cll = l6_fall = 0.0
    l1_cll, l1_fall = summary.l1 if summary.l1 is not None else (0.0, 0.0)
    cll, max_cll_source = (l6_cll, "rpu_l6") if l6_cll > 0 else ((l1_cll, "rpu_l1") if l1_cll > 0 else (0.0, ""))
    fall, max_fall_source = (
        (l6_fall, "rpu_l6") if l6_fall > 0 else ((l1_fall, "rpu_l1") if l1_fall > 0 else (0.0, ""))
    )
    max_cll = f"{round(cll)},{round(fall)}" if (max_cll_source or max_fall_source) else ""

    return RpuStaticHdr(
        master_display=master_display_string(primaries, max_nits, min_nits),
        max_cll=max_cll,
        luminance_source=luminance_source,
        primaries_source=primaries_source,
        max_cll_source=max_cll_source,
        max_fall_source=max_fall_source,
        warnings=tuple(warnings),
    )


def export_l9_counts(
    dovi_tool_bin: str,
    rpu_path: Path,
    run_capture: Callable[[list[str]], str],
    check_cancelled: Callable[[], None] | None = None,
) -> dict[_Primaries, int] | None:
    """Comptage des jeux L9 sur toutes les trames (export JSON temporaire, supprimé ensuite).

    ``run_capture`` exécute la commande (annulable : processus enregistré par le runner).
    """
    with tempfile.TemporaryDirectory(prefix="rpu_l9_", dir=str(rpu_path.parent)) as tmp:
        export = Path(tmp) / "rpu.json"
        run_capture([dovi_tool_bin, "export", "-i", str(rpu_path), "-d", f"all={export}"])
        if not export.is_file():
            return None
        return count_l9_sets(export, check_cancelled)


def estimate_static_hdr_from_rpu(
    dovi_tool_bin: str,
    rpu_path: Path,
    run_capture: Callable[[list[str]], str],
    *,
    check_cancelled: Callable[[], None] | None = None,
    count_l9: Callable[..., dict[_Primaries, int] | None] = export_l9_counts,
    prefer_l1: bool = False,
) -> RpuStaticHdr:
    """Résumé, première trame et, en cas de doute sur les L9, comptage complet des L9."""
    summary = parse_rpu_summary(run_capture([dovi_tool_bin, "info", "-i", str(rpu_path), "--summary"]))
    frame = parse_rpu_frame(run_capture([dovi_tool_bin, "info", "-i", str(rpu_path), "-f", "0"]))
    counts = (
        count_l9(dovi_tool_bin, rpu_path, run_capture, check_cancelled)
        if needs_l9_count(summary, frame)
        else None
    )
    return static_hdr_from_rpu_data(summary, frame, counts, prefer_l1=prefer_l1)


__all__ = [
    "RpuFrame",
    "RpuStaticHdr",
    "RpuSummary",
    "count_l9_sets",
    "estimate_static_hdr_from_rpu",
    "export_l9_counts",
    "l9_primaries",
    "master_display_string",
    "needs_l9_count",
    "parse_rpu_frame",
    "parse_rpu_summary",
    "pq_code_to_nits",
    "static_hdr_from_rpu_data",
]
