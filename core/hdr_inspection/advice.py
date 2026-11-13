"""Recommandations HDR (HDR Studio, CLI, rapport) : valeurs proposées, simulation TV, réalignement.

Mêmes calculs que les onglets Luminance et Édition : MaxCLL/MaxFALL proposés (``suggested_light_levels``),
écran de mastering proposé (``suggested_mastering``), simulation du tone mapping d'une TV (``tonemap.analyse``)
sur les valeurs du fichier puis sur les valeurs proposées, réalignement d'après la mesure de l'image.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from core.hdr_inspection.inspector import InspectionReport
from core.hdr_inspection.measure import ImageMeasurement
from core.hdr_inspection.patch import MasteringEdit, suggested_mastering
from core.hdr_inspection.realign import Realignment, realign
from core.hdr_inspection.tonemap import ToneMapRisk, TvModel, analyse, frame_levels, suggested_light_levels
from core.i18n import translate_text

#: Provenance des niveaux de la simulation (libellés de l'onglet Luminance).
LEVEL_SOURCES = {
    "measure": "mesure de l'image (exacte)",
    "measure_fast": "mesure rapide de l'image (images clés, estimation)",
    "hdr10plus": "MaxScl HDR10+ (valeurs saturées écartées)",
    "l1": "L1 Dolby Vision : pic robuste, écrêtage sous-estimé ; mesurer l'image pour un résultat exact",
}
TV_MODELS = {
    TvModel.MAXCLL: "TV suivant la MaxCLL (BT.2390)",
    TvModel.MDCV: "TV suivant le MDCV seul",
    TvModel.DYNAMIC: "TV à analyse dynamique (référence)",
}


def percent(value: float) -> str:
    return f"{value * 100:.3f}".rstrip("0").rstrip(".") if value < 0.01 else f"{value * 100:.1f}"


def describe_risk(risk: ToneMapRisk) -> tuple[str, str, str]:
    """Écrêtage, assombrissement, compression : trois phrases du bilan."""
    frames = max(1, risk.frames)
    if risk.clipped_frames:
        clip = translate_text("Écrêtage : {time} % du temps ({scenes} plan(s)), jusqu'à +{ev} EV au-delà de {level} nits",
                              time=percent(risk.clipped_frames / frames), scenes=risk.clipped_scenes,
                              ev=f"{risk.worst_clip_ev:.1f}", level=round(risk.clip_level))
        if risk.clipped_pixels is not None:
            clip += translate_text(" ; {pixels} % des pixels du film", pixels=percent(risk.clipped_pixels))
    else:
        clip = translate_text("Aucun écrêtage")
    if risk.darkened_frames:
        dark = translate_text("Hautes lumières assombries de {median} EV (médiane, p90 {p90} EV) sur {time} % du temps",
                              median=f"{risk.darkening_median_ev:.2f}", p90=f"{risk.darkening_p90_ev:.2f}",
                              time=percent(risk.darkened_frames / frames))
    else:
        dark = translate_text("Pas d'assombrissement notable des hautes lumières")
    if risk.model is TvModel.DYNAMIC:
        knee = translate_text("Courbe adaptée à chaque image")
    elif risk.knee is None:
        knee = translate_text("Aucune compression : écran au moins aussi lumineux que le pic annoncé")
    else:
        knee = translate_text("Compression des hautes lumières à partir de {knee} nits (pic supposé {peak} nits)",
                              knee=round(risk.knee), peak=round(risk.source_peak or 0))
    return clip, dark, knee


@dataclass(frozen=True, eq=False)
class Advice:
    display_peak: float
    tv_model: TvModel
    #: MaxCLL, MaxFALL (None : inconnue d'après le L1) et provenance.
    light_levels: tuple[int, int | None, str] | None
    mastering: MasteringEdit
    mastering_source: str
    level_source: str | None
    #: Simulation sur les valeurs du fichier, puis sur les valeurs proposées (MaxCLL proposée, MDCV du fichier).
    file_risk: ToneMapRisk | None
    suggested_risk: ToneMapRisk | None
    realignment: Realignment | None


def build_advice(report: InspectionReport, measurement: ImageMeasurement | None = None, *,
                 display_peak: float = 1000.0, tv_model: TvModel = TvModel.MAXCLL) -> Advice:
    snapshot = report.snapshot
    levels = frame_levels(report, measurement)
    suggested = suggested_light_levels(report, measurement)
    mastering, mastering_source = suggested_mastering(report, measurement)
    mdcv = snapshot.mastering.max_nits if snapshot.mastering is not None else None
    file_risk = suggested_risk = None
    if levels is not None and snapshot.is_pq:
        file_risk = analyse(levels, model=tv_model, max_cll=snapshot.max_cll, mdcv_max=mdcv, display_peak=display_peak)
        if suggested is not None:
            suggested_risk = analyse(levels, model=tv_model, max_cll=suggested[0], mdcv_max=mdcv,
                                     display_peak=display_peak)
    level_source = levels.source if levels is not None else None
    if level_source == "measure" and measurement is not None and measurement.is_estimate:
        level_source = "measure_fast"
    realignment = realign(report, measurement, display_peak) if measurement is not None else None
    return Advice(display_peak, tv_model, suggested, mastering, mastering_source, level_source, file_risk,
                  suggested_risk, realignment)


def _risk_data(risk: ToneMapRisk | None) -> dict | None:
    if risk is None:
        return None
    clip, dark, knee = describe_risk(risk)
    finite = risk.clip_level != float("inf")
    return {
        "model": risk.model.value, "severity": risk.severity().value,
        "source_peak": risk.source_peak, "knee": None if risk.knee is None else round(risk.knee, 1),
        "clip_level": round(risk.clip_level, 1) if finite else None,
        "frames": risk.frames, "clipped_frames": risk.clipped_frames,
        "visible_clip_frames": risk.visible_clip_frames, "clipped_scenes": risk.clipped_scenes,
        "clipped_pixels": risk.clipped_pixels, "worst_clip_ev": round(risk.worst_clip_ev, 3),
        "darkened_frames": risk.darkened_frames, "darkening_median_ev": round(risk.darkening_median_ev, 3),
        "darkening_p90_ev": round(risk.darkening_p90_ev, 3), "summary": [clip, dark, knee],
    }


def advice_data(advice: Advice) -> dict:
    """Recommandations sérialisables (JSON)."""
    levels = advice.light_levels
    realignment = advice.realignment
    return {
        "display_peak": advice.display_peak,
        "tv_model": advice.tv_model.value,
        "levels_source": advice.level_source,
        "suggested": {
            "light_levels": None if levels is None else {"max_cll": levels[0], "max_fall": levels[1],
                                                          "source": levels[2]},
            "mastering": {**asdict(advice.mastering), "source": advice.mastering_source},
        },
        "tv_simulation": {"file": _risk_data(advice.file_risk), "suggested": _risk_data(advice.suggested_risk)},
        "realignment": None if realignment is None else {
            "proposals": [asdict(p) for p in realignment.proposals],
            "light_levels": list(realignment.light_levels) if realignment.light_levels else None,
            "mastering": asdict(realignment.mastering) if realignment.mastering else None,
            "l6": list(realignment.l6) if realignment.l6 else None,
            "hdr10plus_recompute": realignment.hdr10plus_scenes is not None,
        },
    }
