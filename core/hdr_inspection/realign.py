"""Réalignement des métadonnées HDR sur la mesure de l'image (HDR Studio).

Chaque écart entre les métadonnées du fichier et l'image mesurée n'est proposé que s'il a un effet
visible : écrêtage ou assombrissement des hautes lumières simulés (``tonemap.analyse``) sur l'écran
configuré, HDR10+ sous-estimé (écrêtage) ou surévalué (image assombrie), L6 absent ou contredit.
Le L1 Dolby Vision n'est pas réécrit (``dovi_tool`` ne le modifie pas sans perdre les trims) :
diagnostic seulement.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from core.hdr_inspection.inspector import InspectionReport
from core.hdr_inspection.measure import ImageMeasurement
from core.hdr_inspection.patch import MasteringEdit, standard_mastering
from core.hdr_inspection.pq import pq12_to_nits
from core.hdr_inspection.snapshot import PRIMARY_SETS
from core.hdr_inspection.tonemap import ToneMapRisk, TvModel, analyse, frame_levels
from core.i18n import translate_text

#: Part des images au-delà de laquelle un écart dynamique (HDR10+, L1) est jugé visible.
DYNAMIC_SHARE = 0.02
#: Écart relatif toléré entre une valeur du fichier et la mesure (arrondis, outils d'analyse).
TOLERANCE = 0.10


@dataclass(frozen=True)
class Proposal:
    key: str  # "static" | "mdcv" | "l6" | "hdr10plus" | "l1"
    title: str
    current: str
    proposed: str
    impact: str
    #: Faux : diagnostic seulement (aucune correction possible sans perte).
    applicable: bool = True


@dataclass(frozen=True, eq=False)
class Realignment:
    proposals: tuple[Proposal, ...] = ()
    light_levels: tuple[int, int] | None = None
    mastering: MasteringEdit | None = None
    #: L6 : mastering max. (nits), min. (nits), MaxCLL, MaxFALL.
    l6: tuple[int, float, int, int] | None = None
    #: HDR10+ recalculé depuis la mesure, sur ces débuts de plans.
    hdr10plus_scenes: np.ndarray | None = field(default=None)

    def applicable(self) -> tuple[Proposal, ...]:
        return tuple(p for p in self.proposals if p.applicable)


def _score(risk: ToneMapRisk) -> float:
    """Gêne visuelle simulée : écrêtage visible pondéré, puis assombrissement."""
    return 4.0 * risk.visible_clip_frames + risk.darkened_frames


def _ev(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _risk_text(risk: ToneMapRisk, frames: int) -> str:
    parts = []
    if risk.visible_clip_frames:
        parts.append(translate_text("écrêtage visible sur {share} % des images",
                                    share=_share(risk.visible_clip_frames, frames)))
    if risk.darkened_frames:
        parts.append(translate_text("hautes lumières assombries de {ev} EV sur {share} % des images",
                                    ev=_ev(risk.darkening_median_ev), share=_share(risk.darkened_frames, frames)))
    return ", ".join(parts) or translate_text("aucun défaut simulé")


def _share(count: int, total: int) -> str:
    value = 100.0 * count / max(1, total)
    return f"{value:.1f}".replace(".", ",") if value < 10 else str(round(value))


def _primaries(report: InspectionReport) -> str:
    current = report.snapshot.mastering
    if current is not None and current.primaries in PRIMARY_SETS:
        return current.primaries
    dovi = report.dovi
    if dovi is not None and dovi.l9_primaries:
        return {0: "Display P3", 1: "BT.709", 2: "BT.2020"}.get(dovi.l9_primaries[0], "Display P3")
    return "Display P3"


def realign(report: InspectionReport, measurement: ImageMeasurement, display_peak: float = 1000.0) -> Realignment:
    """Propositions de réalignement (vides si les métadonnées conviennent à l'image mesurée)."""
    levels = frame_levels(report, measurement)
    if levels is None or not measurement.frames:
        return Realignment()
    snapshot = report.snapshot
    frames = levels.frames
    proposals: list[Proposal] = []
    cll, fall = measurement.max_cll(), measurement.max_fall()
    light_levels: tuple[int, int] | None = None
    mastering_edit: MasteringEdit | None = None
    mdcv_max = snapshot.mastering.max_nits if snapshot.mastering is not None else None

    # MaxCLL / MaxFALL : TV suivant la MaxCLL (lecture HDR10).
    current_cll, current_fall = snapshot.max_cll, snapshot.max_fall
    file_risk = analyse(levels, model=TvModel.MAXCLL, max_cll=current_cll or None, mdcv_max=mdcv_max,
                        display_peak=display_peak)
    measured_risk = analyse(levels, model=TvModel.MAXCLL, max_cll=cll, mdcv_max=mdcv_max, display_peak=display_peak)
    if not current_cll or _score(measured_risk) < _score(file_risk):
        light_levels = (cll, fall)
        impact = (translate_text("absents : les TV prennent une valeur par défaut") if not current_cll
                  else translate_text("TV suivant la MaxCLL, écran {peak} nits : {now} ; mesurés : {after}",
                                      peak=round(display_peak), now=_risk_text(file_risk, frames),
                                      after=_risk_text(measured_risk, frames)))
        proposals.append(Proposal("static", translate_text("MaxCLL / MaxFALL"),
                                  f"{current_cll if current_cll is not None else '—'} / "
                                  f"{current_fall if current_fall is not None else '—'}", f"{cll} / {fall}", impact))

    # Écran de mastering (MDCV) : TV suivant le MDCV ; valeurs usuelles ou pic mesuré arrondi.
    primaries = _primaries(report)
    current = snapshot.mastering
    candidates = {1000.0, 4000.0, 10000.0, float(min(10000, int(np.ceil(max(cll, 1) / 100.0)) * 100))}
    scored = []
    for candidate in sorted(candidates):
        risk = analyse(levels, model=TvModel.MDCV, max_cll=None, mdcv_max=candidate, display_peak=display_peak)
        # À gêne égale : valeur usuelle, puis la plus basse.
        scored.append((_score(risk), candidate not in (1000.0, 4000.0, 10000.0), candidate, risk))
    best_score, _unusual, best, best_risk = min(scored, key=lambda item: item[:3])
    if current is None or not current.max_nits:
        mastering_edit = standard_mastering(float(cll), primaries)
        proposals.append(Proposal("mdcv", translate_text("Écran de mastering (MDCV)"), translate_text("absent"),
                                  f"{mastering_edit.primaries}, {mastering_edit.min_nits:g} – {mastering_edit.max_nits:g} nits",
                                  translate_text("absent : les TV supposent un écran par défaut")))
    else:
        current_risk = analyse(levels, model=TvModel.MDCV, max_cll=None, mdcv_max=current.max_nits,
                               display_peak=display_peak)
        if best != current.max_nits and best_score < _score(current_risk) - max(1.0, 0.01 * frames):
            low = current.min_nits if current.min_nits is not None else (0.005 if best >= 4000 else 0.0001)
            mastering_edit = MasteringEdit(primaries, float(low), best)
            proposals.append(Proposal(
                "mdcv", translate_text("Écran de mastering (MDCV)"),
                f"{current.primaries}, {current.min_nits if current.min_nits is not None else '?'} – "
                f"{current.max_nits:g} nits",
                f"{mastering_edit.primaries}, {mastering_edit.min_nits:g} – {mastering_edit.max_nits:g} nits",
                translate_text("TV suivant le MDCV, écran {peak} nits : {now} ; proposé : {after}. Le MDCV décrit "
                               "l'écran d'étalonnage : le changer adapte le tone mapping des TV, pas l'image.",
                               peak=round(display_peak), now=_risk_text(current_risk, frames),
                               after=_risk_text(best_risk, frames))))

    # L6 : HDR10 porté par le RPU (lecteurs Dolby Vision en repli HDR10, conversions DV → HDR10).
    dovi = report.dovi
    l6_target: tuple[int, float, int, int] | None = None
    if dovi is not None and dovi.frames:
        target_mastering = mastering_edit or (MasteringEdit(primaries, float(current.min_nits or 0.0001),
                                                            float(current.max_nits))
                                              if current is not None and current.max_nits else None)
        reference = target_mastering or standard_mastering(float(cll), primaries)
        sets = [item for item in dovi.l6]
        main = max(sets, key=lambda item: item.frames) if sets else None
        off = (main is None or not main.max_cll
               or abs(main.max_cll - cll) > TOLERANCE * cll or abs(main.max_fall - fall) > max(5, TOLERANCE * fall)
               or main.max_mastering != round(reference.max_nits))
        if off:
            l6_target = (int(round(reference.max_nits)), float(reference.min_nits), cll, fall)
            proposals.append(Proposal(
                "l6", translate_text("L6 (HDR10 du RPU)"),
                translate_text("absent") if main is None else
                f"{main.max_cll} / {main.max_fall}, {main.min_mastering:g} – {main.max_mastering} nits",
                f"{cll} / {fall}, {reference.min_nits:g} – {round(reference.max_nits)} nits",
                translate_text("absent ou contredit par la mesure : lecture HDR10 de secours des lecteurs Dolby "
                               "Vision et conversions DV → HDR10") if main is None or not main.max_cll else
                translate_text("contredit par la mesure : lecture HDR10 de secours des lecteurs Dolby Vision et "
                               "conversions DV → HDR10")))

    # HDR10+ : MaxScl face au pic mesuré, image par image.
    hdr10plus = report.hdr10plus
    scenes: np.ndarray | None = None
    if hdr10plus is not None and hdr10plus.frames:
        count = min(hdr10plus.frames, measurement.frames)
        # Par plan du HDR10+ (MaxScl constant sur le plan = maximum du plan).
        measured = _per_scene(measurement.peaks()[:count], hdr10plus.scene_starts, np.fmax)
        declared = _per_scene(hdr10plus.max_scl[:count], hdr10plus.scene_starts, np.fmax)
        valid = measured > 50
        under = valid & (measured > 1.25 * declared)
        over = valid & (declared > 2 * measured) & (declared > 400)
        saturated = int(hdr10plus.saturated().size)
        mismatch = hdr10plus.frames != measurement.frames
        if under.mean() > DYNAMIC_SHARE or over.mean() > DYNAMIC_SHARE or saturated or mismatch:
            if hdr10plus.scenes >= 2 and not mismatch:
                scenes = hdr10plus.scene_starts.astype(np.int64)
            elif dovi is not None and dovi.scenes >= 2 and dovi.frames == measurement.frames:
                scenes = dovi.scene_starts.astype(np.int64)
            else:
                scenes = scene_cuts(measurement)
            reasons = []
            if under.any():
                reasons.append(translate_text("MaxScl sous le pic de l'image sur {share} % des images (écrêtage)",
                                              share=_share(int(under.sum()), count)))
            if over.any():
                reasons.append(translate_text("MaxScl au double du pic sur {share} % des images (image assombrie)",
                                              share=_share(int(over.sum()), count)))
            if saturated:
                reasons.append(translate_text("{count} images à 10 000 nits", count=saturated))
            if mismatch:
                reasons.append(translate_text("{hdr10plus} trames pour {video} images", hdr10plus=hdr10plus.frames,
                                              video=measurement.frames))
            proposals.append(Proposal(
                "hdr10plus", translate_text("HDR10+"),
                translate_text("profil {profile}, pic MaxScl {peak} nits", profile=hdr10plus.profile,
                               peak=round(hdr10plus.peak_nits())),
                translate_text("recalculé depuis la mesure (profil A, {scenes} plans)", scenes=int(scenes.size)),
                "; ".join(reasons)))

    # L1 Dolby Vision : pic robuste par image face au percentile 99,98 % mesuré (diagnostic).
    if dovi is not None and dovi.frames:
        count = min(dovi.frames, measurement.frames)
        # Par plan du RPU : L1 max du plan face au pic robuste (99,98 %) et au pic mesurés sur le plan.
        l1 = _per_scene(pq12_to_nits(dovi.l1_max[:count]), dovi.scene_starts, np.fmax)
        high = _per_scene(measurement.percentiles[:count, -1], dovi.scene_starts, np.fmax)
        peak = _per_scene(measurement.peaks()[:count], dovi.scene_starts, np.fmax)
        valid = high > 50
        under = valid & (high > 1.3 * l1)
        over = valid & (l1 > 2 * peak) & (l1 > 400)
        if under.mean() > DYNAMIC_SHARE or over.mean() > DYNAMIC_SHARE:
            reasons = []
            if under.any():
                reasons.append(translate_text("L1 max sous l'image sur {share} % des images (écrêtage en lecture "
                                              "Dolby Vision)", share=_share(int(under.sum()), count)))
            if over.any():
                reasons.append(translate_text("L1 max au double du pic sur {share} % des images (image assombrie)",
                                              share=_share(int(over.sum()), count)))
            proposals.append(Proposal(
                "l1", translate_text("L1 Dolby Vision"), translate_text("pic L1 {peak} nits",
                                                                        peak=round(dovi.l1_peak_nits())),
                translate_text("non corrigé"),
                "; ".join(reasons) + ". " + translate_text(
                    "dovi_tool ne réécrit pas le L1 sans perdre les trims (L2/L8) : ré-analyser la source "
                    "Dolby Vision avec l'outil d'origine."), applicable=False))
    return Realignment(tuple(proposals), light_levels, mastering_edit, l6_target, scenes)


def _per_scene(values: np.ndarray, starts: np.ndarray, reduce: np.ufunc) -> np.ndarray:
    """Valeur réduite sur chaque plan, recopiée sur ses images (même longueur que ``values``)."""
    values = np.asarray(values, dtype=np.float64)
    if not values.size:
        return values
    bounds = np.unique(np.clip(np.concatenate([[0], np.asarray(starts, dtype=np.int64)]), 0, values.size - 1))
    reduced = reduce.reduceat(values, bounds)
    lengths = np.diff(np.append(bounds, values.size))
    return np.repeat(reduced, lengths)


def scene_cuts(measurement: ImageMeasurement, threshold_ev: float = 1.0) -> np.ndarray:
    """Débuts de plans de la mesure, sinon détectés : saut de la moyenne de plus de ``threshold_ev``."""
    if measurement.scene_starts.size >= 2:
        return measurement.scene_starts.astype(np.int64)
    average = np.maximum(measurement.average.astype(np.float64), 0.01)
    jumps = np.abs(np.diff(np.log2(average))) > threshold_ev
    starts = np.concatenate([[0], np.flatnonzero(jumps) + 1]).astype(np.int64)
    # Plans d'au moins une demi-seconde (environ 12 images) : pas de rafales de coupes.
    kept = [int(starts[0])]
    for start in starts[1:]:
        if start - kept[-1] >= 12:
            kept.append(int(start))
    return np.asarray(kept, dtype=np.int64)
