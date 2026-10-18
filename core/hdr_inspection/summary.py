"""Résumés lisibles d'une inspection HDR (libellé, valeur) : panneau HDR Studio, rapports exportés, CLI."""
from __future__ import annotations

import numpy as np

from core.hdr_inspection.dovi import L9_PRIMARY_NAMES, DoviAnalysis
from core.hdr_inspection.hdr10p import Hdr10PlusAnalysis
from core.hdr_inspection.inspector import InspectionReport
from core.hdr_inspection.measure import ImageMeasurement
from core.hdr_inspection.snapshot import DoviRecord, HdrSnapshot
from core.i18n import translate_text

L11_CONTENT = {0: "Défaut", 1: "Cinéma", 2: "Jeu", 3: "Sport", 4: "Contenu utilisateur"}
Rows = list[tuple[str, str]]
Table = tuple[list[str], list[list[str]]]


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def bucket(values: np.ndarray, start: int, end: int, count: int, mode: str) -> np.ndarray:
    """Agrège ``values[start:end]`` en ``count`` colonnes (maximum ou moyenne, NaN ignorés)."""
    window = np.asarray(values[start:end], dtype=np.float64)
    if window.size == 0 or count <= 0:
        return np.zeros(0)
    count = min(count, window.size)
    edges = np.linspace(0, window.size, count + 1).astype(np.int64)[:-1]
    if mode == "mean":
        valid = np.isfinite(window)
        sums = np.add.reduceat(np.where(valid, window, 0.0), edges)
        sizes = np.add.reduceat(valid.astype(np.float64), edges)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(sizes > 0, sums / np.maximum(sizes, 1), np.nan)
    return np.fmax.reduceat(window, edges)


def nits_text(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.4f}".rstrip("0").rstrip(".") if value < 1 else f"{round(value)}"


def track_rows(snapshot: HdrSnapshot) -> Rows:
    frames = "?" if snapshot.frames is None else (
        f"{snapshot.frames}" if snapshot.frames_exact
        else translate_text("≈ {count} (d'après la durée)", count=snapshot.frames))
    return [
        (translate_text("Codec"), snapshot.codec.upper()),
        (translate_text("Résolution"), f"{snapshot.width or '?'}×{snapshot.height or '?'}"),
        (translate_text("Cadence"), translate_text("{fps} i/s", fps=f"{snapshot.fps:.3f}") if snapshot.fps else "?"),
        (translate_text("Images"), frames),
        (translate_text("Profondeur"), f"{snapshot.bit_depth} bits" if snapshot.bit_depth else "?"),
        (translate_text("Transfert"), snapshot.transfer or "?"),
        (translate_text("Primaires"), snapshot.primaries or "?"),
        (translate_text("Matrice"), snapshot.matrix or "?"),
    ]


def static_rows(snapshot: HdrSnapshot, measurement: ImageMeasurement | None = None) -> Rows:
    mastering = snapshot.mastering
    mdcv = translate_text("absent") if mastering is None else (
        f"{mastering.primaries}, {nits_text(mastering.min_nits)} – {nits_text(mastering.max_nits)} nits")
    levels = translate_text("absents") if snapshot.max_cll is None else f"{snapshot.max_cll} / {snapshot.max_fall}"
    rows = [(translate_text("Écran de mastering (MDCV)"), mdcv), (translate_text("MaxCLL / MaxFALL"), levels)]
    if measurement is not None:
        rows.append((translate_text("Mesurés sur l'image"), f"{measurement.max_cll()} / {measurement.max_fall()}"))
    return rows


def container_dovi_rows(record: DoviRecord) -> Rows:
    layers = "+".join(name for name, present in (("BL", record.bl), ("EL", record.el), ("RPU", record.rpu)) if present)
    return [(translate_text("Profil"), record.label), (translate_text("Niveau"), str(record.level or "?")),
            (translate_text("Couches"), layers)]


def dovi_rows(dovi: DoviAnalysis, fps: float) -> Rows:
    lengths = dovi.scene_lengths() / fps
    el = dovi.el_type or translate_text("aucune")
    if dovi.el_type == "FEL" and dovi.frames:
        el += " · " + translate_text("résiduel actif sur {share} % des images",
                                      share=round(100 * dovi.residual_active_frames / dovi.frames, 1))
    display = ", ".join(f"{nits_text(lo)} – {hi} nits" for lo, hi in dovi.source_display) or "?"
    rows = [
        (translate_text("Profil (RPU)"), f"P{dovi.profile}" if dovi.profile is not None else "?"),
        (translate_text("Couche EL"), el),
        (translate_text("Version CM"), dovi.cm_version),
        (translate_text("Images"), str(dovi.frames)),
        (translate_text("Plans"), translate_text("{count} (durée min. {min} s, médiane {median} s, max. {max} s)",
                                                 count=dovi.scenes, min=f"{lengths.min():.1f}",
                                                 median=f"{np.median(lengths):.1f}", max=f"{lengths.max():.1f}")
         if lengths.size else "0"),
        (translate_text("Pic L1 (MaxCLL du RPU)"), f"{round(dovi.l1_peak_nits())} nits"),
        (translate_text("Moyenne L1 max. (MaxFALL du RPU)"), f"{dovi.l1_fall_nits():.1f} nits"),
        (translate_text("Écran de mastering (RPU)"), display),
        (translate_text("Trims L2 (écrans cibles)"), ", ".join(f"{t} nits" for t in dovi.l2_targets) or "—"),
    ]
    if dovi.cm_version == "4.0":
        rows.append((translate_text("Trims L8 (index d'écran)"), ", ".join(str(t) for t in dovi.l8_targets) or "—"))
        rows.append((translate_text("Écrans L10"), ", ".join(f"{t} nits" for t in dovi.l10_targets) or "—"))
    if dovi.l9_primaries:
        rows.append((translate_text("Primaires L9"), ", ".join(L9_PRIMARY_NAMES.get(p, str(p)) for p in dovi.l9_primaries)))
    if dovi.l11_content:
        rows.append((translate_text("Type de contenu L11"),
                     ", ".join(translate_text(L11_CONTENT.get(c, str(c))) for c in dovi.l11_content)))
    return rows


def l6_table(dovi: DoviAnalysis) -> Table:
    return ([translate_text("MaxCLL / MaxFALL"), translate_text("Mastering (nits)"), translate_text("Images")],
            [[f"{s.max_cll} / {s.max_fall}", f"{nits_text(s.min_mastering)} – {s.max_mastering}", str(s.frames)]
             for s in dovi.l6])


def active_area_table(report: InspectionReport, limit: int = 500) -> Table:
    dovi = report.dovi
    assert dovi is not None
    fps = report.snapshot.fps or 24000 / 1001
    width, height = report.snapshot.width or 0, report.snapshot.height or 0
    return ([translate_text("Début"), translate_text("Durée"), translate_text("Gauche / droite / haut / bas"),
             translate_text("Image active")],
            [[format_time(a.first_frame / fps), format_time(a.frames / fps), f"{a.left} / {a.right} / {a.top} / {a.bottom}",
              f"{max(0, width - a.left - a.right)}×{max(0, height - a.top - a.bottom)}"]
             for a in dovi.active_areas[:limit]])


def hdr10plus_rows(hdr10plus: Hdr10PlusAnalysis) -> Rows:
    targets = ", ".join(f"{t} nits" for t in hdr10plus.targeted_display if t) or translate_text("non renseigné")
    saturated = hdr10plus.saturated().size
    return [
        (translate_text("Profil"), hdr10plus.profile + (translate_text(" (courbe de Bézier)") if hdr10plus.profile == "B" else "")),
        (translate_text("Images"), str(hdr10plus.frames)),
        (translate_text("Plans"), str(hdr10plus.scenes)),
        (translate_text("Fenêtres"), ", ".join(str(w) for w in hdr10plus.windows)),
        (translate_text("Écran ciblé (TSDML)"), targets),
        (translate_text("Pic MaxScl"), f"{round(hdr10plus.peak_nits())} nits"),
        *([(translate_text("Pic MaxScl hors saturation"), f"{round(hdr10plus.credible_peak_nits())} nits"),
           (translate_text("Images à 10 000 nits"), str(saturated))] if saturated else []),
        (translate_text("AverageRGB max."), f"{hdr10plus.max_average_nits():.1f} nits"),
        (translate_text("Percentile 99,98 % max."), f"{round(float(hdr10plus.p99.max()))} nits" if hdr10plus.frames else "?"),
    ]


def measurement_rows(measurement: ImageMeasurement, fps: float) -> Rows:
    x, y, width, height = measurement.window
    return [
        (translate_text("Images mesurées"), str(measurement.frames)),
        (translate_text("MaxCLL / MaxFALL mesurés"), f"{measurement.max_cll()} / {measurement.max_fall()}"),
        (translate_text("Durée"), format_time(measurement.frames / fps)),
        (translate_text("Zone mesurée"), f"{width}×{height} (x {x}, y {y})"),
    ]
