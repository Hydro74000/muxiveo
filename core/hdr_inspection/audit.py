"""Contrôles de cohérence entre HDR10 statique, Dolby Vision et HDR10+."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from core.hdr_inspection.dovi import DoviAnalysis
from core.hdr_inspection.hdr10p import Hdr10PlusAnalysis
from core.hdr_inspection.measure import ImageMeasurement
from core.hdr_inspection.snapshot import HdrSnapshot
from core.i18n import translate_text
from core.matroska.editors.dovi import minimum_dovi_level

#: Décalage maximal (images) recherché entre les plans Dolby Vision et HDR10+.
_SCENE_SHIFT_MAX = 48


class Severity(str, Enum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    category: str  # "hdr10" | "dv" | "hdr10plus" | "coherence" | "format"
    title: str
    detail: str = ""


def _timecode(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _frames_label(snapshot: HdrSnapshot) -> str:
    if snapshot.frames is None:
        return "?"
    return f"{snapshot.frames}" if snapshot.frames_exact else f"≈ {snapshot.frames}"


def _count_check(name: str, category: str, count: int, snapshot: HdrSnapshot) -> Finding:
    """Nombre de trames de métadonnées comparé à la vidéo."""
    video = snapshot.frames
    if video is None:
        return Finding(Severity.INFO, category, translate_text("{name} : {count} images", name=name, count=count))
    delta = count - video
    if delta == 0:
        return Finding(Severity.OK, category,
                       translate_text("{name} présent sur les {count} images", name=name, count=count))
    if not snapshot.frames_exact:
        # Durée × cadence : approximation (durée du conteneur, coupe d'un extrait).
        return Finding(Severity.INFO, category, translate_text(
            "{name} : {count} images, vidéo {video} d'après la durée (comptage non exact)",
            name=name, count=count, video=_frames_label(snapshot)))
    severity = Severity.ERROR
    if delta < 0:
        return Finding(severity, category, translate_text(
            "{name} absent sur {missing} images", name=name, missing=-delta),
            translate_text("{count} trames de métadonnées pour {video} images vidéo.", count=count,
                           video=_frames_label(snapshot)))
    return Finding(severity, category, translate_text(
        "{name} : {extra} trames de plus que la vidéo", name=name, extra=delta),
        translate_text("{count} trames de métadonnées pour {video} images vidéo.", count=count,
                       video=_frames_label(snapshot)))


def _aligned_share(reference: np.ndarray, starts: np.ndarray, shift: int) -> float:
    return float(np.isin(starts + shift, reference).sum()) / starts.size


def scene_alignment(first: np.ndarray, second: np.ndarray) -> tuple[int, float]:
    """Décalage à ajouter aux débuts de plans de ``second`` pour retrouver ceux de ``first``, et la part alignée."""
    if first.size < 2 or second.size < 2:
        return 0, 0.0
    reference = np.unique(np.asarray(first[1:], dtype=np.int64))
    starts = np.asarray(second[1:], dtype=np.int64)
    best_shift, best = 0, -1.0
    # Du plus petit décalage au plus grand en valeur absolue : à égalité, le plus proche de 0.
    for shift in sorted(range(-_SCENE_SHIFT_MAX, _SCENE_SHIFT_MAX + 1), key=abs):
        share = _aligned_share(reference, starts, shift)
        if share > best:
            best_shift, best = shift, share
    return best_shift, best


def _static_checks(snapshot: HdrSnapshot, dovi: DoviAnalysis | None,
                   hdr10plus: Hdr10PlusAnalysis | None, measurement: ImageMeasurement | None = None) -> list[Finding]:
    findings: list[Finding] = []
    # Profil 5 et P8.0 : aucune couche HDR10, les métadonnées statiques ne servent pas.
    hdr10_layer = snapshot.is_pq and not (snapshot.dovi is not None and (
        snapshot.dovi.profile == 5 or (snapshot.dovi.profile == 8 and snapshot.dovi.compat_id == 0)))
    if not hdr10_layer:
        return findings
    if snapshot.mastering is None:
        findings.append(Finding(Severity.WARN, "hdr10", translate_text("MDCV absent (SMPTE ST 2086)"),
                                translate_text("Une TV HDR10 suppose alors un écran de mastering par défaut.")))
    cll, fall = snapshot.max_cll, snapshot.max_fall
    if cll is None:
        findings.append(Finding(Severity.WARN, "hdr10", translate_text("MaxCLL/MaxFALL absents"),
                                translate_text("Le tone mapping HDR10 se fonde alors sur le MDCV seul.")))
        return findings
    if cll == 0:
        findings.append(Finding(Severity.WARN, "hdr10", translate_text("MaxCLL/MaxFALL inconnus (0)"),
                                translate_text("Valeur « inconnue » de CTA-861.3 : la TV se fonde sur le MDCV.")))
        return findings
    if (cll, fall) == (1000, 400):
        findings.append(Finding(Severity.INFO, "hdr10", translate_text("MaxCLL/MaxFALL 1000/400 : valeurs par défaut probables"),
                                translate_text("Valeurs souvent écrites par les encodeurs sans mesure du contenu.")))
    if measurement is not None and measurement.frames:
        # Mesure exacte de l'image : référence, préférée aux métadonnées dynamiques.
        measured_cll, measured_fall = measurement.max_cll(), measurement.max_fall()
        if measurement.is_estimate:
            # Mesure rapide (images clés) : borne basse ; seule une valeur déclarée trop basse est certaine.
            if cll < measured_cll * 0.95:
                findings.append(Finding(Severity.WARN, "hdr10", translate_text(
                    "MaxCLL déclarée ({cll}) sous l'estimation de la mesure rapide ({measured} nits)", cll=cll,
                    measured=measured_cll),
                    translate_text("Une TV HDR10 qui suit la MaxCLL peut écrêter les hautes lumières au-delà. "
                                   "Mesure complète conseillée avant correction.")))
            return findings
        if cll < measured_cll * 0.95:
            findings.append(Finding(Severity.WARN, "hdr10", translate_text(
                "MaxCLL déclarée ({cll}) sous la valeur mesurée ({measured} nits)", cll=cll, measured=measured_cll),
                translate_text("Une TV HDR10 qui suit la MaxCLL peut écrêter les hautes lumières au-delà.")))
        elif cll > measured_cll * 1.5 and cll - measured_cll > 200:
            findings.append(Finding(Severity.INFO, "hdr10", translate_text(
                "MaxCLL déclarée ({cll}) très au-dessus de la valeur mesurée ({measured} nits)", cll=cll,
                measured=measured_cll),
                translate_text("Une TV HDR10 qui suit la MaxCLL compresse les hautes lumières sans nécessité.")))
        else:
            findings.append(Finding(Severity.OK, "hdr10", translate_text(
                "MaxCLL déclarée ({cll}) cohérente avec la mesure ({measured} nits)", cll=cll, measured=measured_cll)))
        if fall and abs(fall - measured_fall) > max(20, 0.25 * measured_fall):
            findings.append(Finding(Severity.INFO, "hdr10", translate_text(
                "MaxFALL déclarée ({fall}) différente de la mesure ({measured} nits)", fall=fall,
                measured=measured_fall)))
        return findings
    # Pic des métadonnées dynamiques : le plus haut (MaxScl HDR10+ strict, L1 robuste donc plus bas).
    peaks: list[tuple[float, str]] = []
    if dovi is not None and dovi.frames:
        peaks.append((dovi.l1_peak_nits(), "L1"))
    if hdr10plus is not None and hdr10plus.frames:
        peaks.append((hdr10plus.credible_peak_nits(), "HDR10+"))
    if peaks:
        peak, name = max(peaks)
        if peak > cll * 1.25:
            findings.append(Finding(Severity.WARN, "hdr10", translate_text(
                "MaxCLL ({cll} nits) sous le pic {name} ({peak} nits)", cll=cll, name=name, peak=round(peak)),
                translate_text("Une TV HDR10 qui suit la MaxCLL peut écrêter les hautes lumières au-delà.")))
        elif peak and cll > peak * 2.5 and cll > 1000:
            findings.append(Finding(Severity.INFO, "hdr10", translate_text(
                "MaxCLL ({cll} nits) très au-dessus du pic {name} ({peak} nits)", cll=cll, name=name, peak=round(peak)),
                translate_text("Une TV HDR10 qui suit la MaxCLL compresse les hautes lumières sans nécessité.")))
    return findings


def _l6_checks(snapshot: HdrSnapshot, dovi: DoviAnalysis) -> list[Finding]:
    """L6 (HDR10 porté par le RPU) face au HDR10 statique de la couche de base."""
    findings: list[Finding] = []
    if not dovi.l6 or not snapshot.is_pq or snapshot.dovi is None or snapshot.dovi.profile == 5:
        return findings
    main = dovi.l6[0]
    static = f"{snapshot.max_cll}/{snapshot.max_fall}" if snapshot.max_cll is not None else translate_text("absents")
    if not main.max_cll and snapshot.max_cll:
        findings.append(Finding(Severity.INFO, "coherence", translate_text(
            "L6 sans MaxCLL/MaxFALL (0) ; HDR10 statique {hdr10}", hdr10=static),
            translate_text("Valeurs reportables dans le L6 depuis l'onglet Édition.")))
    if main.max_cll and (main.max_cll, main.max_fall) != (snapshot.max_cll, snapshot.max_fall):
        findings.append(Finding(Severity.INFO, "coherence", translate_text(
            "L6 Dolby Vision ({l6}) différent du HDR10 ({hdr10})",
            l6=f"{main.max_cll}/{main.max_fall}", hdr10=static),
            translate_text("Les TV Dolby Vision lisent le L6, les TV HDR10 le SEI ou le conteneur.")))
    mdcv = snapshot.mastering.max_nits if snapshot.mastering else None
    if mdcv and main.max_mastering and abs(main.max_mastering - mdcv) > 1:
        findings.append(Finding(Severity.INFO, "coherence", translate_text(
            "MDCV ({mdcv} nits) différent du L6 ({l6} nits)", mdcv=round(mdcv), l6=main.max_mastering)))
    return findings


def _dovi_checks(snapshot: HdrSnapshot, dovi: DoviAnalysis) -> list[Finding]:
    findings = [_count_check(translate_text("RPU Dolby Vision"), "dv", dovi.frames, snapshot)]
    if dovi.l1_missing:
        findings.append(Finding(Severity.WARN, "dv", translate_text("L1 absent sur {count} images", count=dovi.l1_missing)))
    record = snapshot.dovi
    if record is not None and dovi.profile is not None and record.profile != dovi.profile:
        findings.append(Finding(Severity.WARN, "dv", translate_text(
            "Profil du conteneur (P{record}) différent du RPU (P{rpu})", record=record.profile, rpu=dovi.profile)))
    if record is not None and record.level and snapshot.width and snapshot.height and snapshot.fps:
        needed = minimum_dovi_level(snapshot.width, snapshot.height, snapshot.fps)
        if record.level < needed:
            findings.append(Finding(Severity.WARN, "dv", translate_text(
                "Niveau Dolby Vision {level} insuffisant (minimum {needed})", level=record.level, needed=needed)))
    if dovi.el_type == "FEL":
        share = dovi.residual_active_frames / dovi.frames if dovi.frames else 0.0
        if dovi.residual_active_frames == 0:
            findings.append(Finding(Severity.INFO, "dv", translate_text("FEL vide : résiduel désactivé sur toutes les images"),
                                    translate_text("La couche d'amélioration n'apporte rien de plus qu'un MEL.")))
        else:
            findings.append(Finding(Severity.INFO, "dv", translate_text(
                "FEL : résiduel actif sur {share} % des images", share=round(share * 100, 1))))
    if len(dovi.active_areas) > 1:
        distinct = len({(a.left, a.right, a.top, a.bottom) for a in dovi.active_areas})
        findings.append(Finding(Severity.INFO, "dv", translate_text(
            "Zone active variable : {changes} changements, {distinct} formats", changes=len(dovi.active_areas) - 1,
            distinct=distinct), translate_text("Format d'image variable (scènes IMAX, par exemple).")))
    if len(dovi.l6) > 1:
        findings.append(Finding(Severity.WARN, "dv", translate_text("{count} jeux L6 différents dans le film", count=len(dovi.l6)),
                                translate_text("Le L6 devrait être constant : RPU assemblé depuis plusieurs sources ?")))
    return findings


def _hdr10plus_checks(snapshot: HdrSnapshot, hdr10plus: Hdr10PlusAnalysis) -> list[Finding]:
    findings = [_count_check(translate_text("HDR10+"), "hdr10plus", hdr10plus.frames, snapshot)]
    if hdr10plus.profile == "B":
        targets = [t for t in hdr10plus.targeted_display if t]
        findings.append(Finding(Severity.INFO, "hdr10plus", translate_text(
            "Profil B : courbe de Bézier pour un écran de {targets} nits",
            targets=", ".join(str(t) for t in targets) or "?")))
    saturated = hdr10plus.saturated()
    if saturated.size:
        scenes = np.unique(np.searchsorted(hdr10plus.scene_starts, saturated, side="right") - 1)
        first = int(saturated[0])
        when = _timecode(first / snapshot.fps) if snapshot.fps else f"#{first}"
        findings.append(Finding(Severity.WARN, "hdr10plus", translate_text(
            "MaxScl à 10 000 nits sur {frames} images ({scenes} plan(s), dès {when})",
            frames=int(saturated.size), scenes=int(scenes.size), when=when),
            translate_text("Valeur maximale du format : plan mal analysé (générique, logo, mire) ; "
                           "écarté de la comparaison avec la MaxCLL.")))
    if any(w > 1 for w in hdr10plus.windows):
        findings.append(Finding(Severity.INFO, "hdr10plus", translate_text("Plusieurs fenêtres de traitement")))
    return findings


def _coherence(dovi: DoviAnalysis, hdr10plus: Hdr10PlusAnalysis) -> list[Finding]:
    findings: list[Finding] = []
    if dovi.frames != hdr10plus.frames:
        findings.append(Finding(Severity.WARN, "coherence", translate_text(
            "RPU ({dv} images) et HDR10+ ({hdr10plus} images) de longueurs différentes",
            dv=dovi.frames, hdr10plus=hdr10plus.frames)))
    if hdr10plus.scenes < 2 or dovi.scenes < 2:
        return findings
    shift, share = scene_alignment(dovi.scene_starts, hdr10plus.scene_starts)
    unshifted = _aligned_share(np.unique(dovi.scene_starts[1:].astype(np.int64)),
                               hdr10plus.scene_starts[1:].astype(np.int64), 0)
    if shift != 0 and share >= 0.6 and share >= 2 * unshifted:
        # Décalage positif : les plans HDR10+ arrivent après ceux du RPU.
        findings.append(Finding(Severity.WARN, "coherence", translate_text(
            "Plans HDR10+ décalés de {shift} images par rapport au RPU", shift=f"{-shift:+d}"),
            translate_text("HDR10+ recalé depuis une autre source ? {share} % des plans coïncident après décalage.",
                           share=round(share * 100))))
    elif unshifted >= 0.8:
        findings.append(Finding(Severity.OK, "coherence", translate_text(
            "Plans HDR10+ alignés sur le RPU ({share} %)", share=round(unshifted * 100))))
    else:
        findings.append(Finding(Severity.INFO, "coherence", translate_text(
            "Plans HDR10+ indépendants du RPU ({share} % communs)", share=round(unshifted * 100)),
            translate_text("Découpages produits par des analyses différentes.")))
    return findings


def audit(snapshot: HdrSnapshot, dovi: DoviAnalysis | None = None,
          hdr10plus: Hdr10PlusAnalysis | None = None,
          measurement: ImageMeasurement | None = None) -> tuple[Finding, ...]:
    """Contrôles par format puis entre formats ; sans analyse dynamique, en-têtes seuls."""
    findings: list[Finding] = []
    if snapshot.formats == ("SDR",):
        findings.append(Finding(Severity.INFO, "format", translate_text("Vidéo SDR : aucune métadonnée HDR")))
    if snapshot.is_hlg:
        findings.append(Finding(Severity.INFO, "format", translate_text("HLG : aucune métadonnée statique ni dynamique requise")))
    if snapshot.is_pq and (snapshot.bit_depth or 10) < 10:
        findings.append(Finding(Severity.WARN, "format", translate_text("PQ en {bits} bits : postérisation probable",
                                                                       bits=snapshot.bit_depth)))
    findings.extend(_static_checks(snapshot, dovi, hdr10plus, measurement))
    if dovi is not None:
        findings.extend(_l6_checks(snapshot, dovi))
        findings.extend(_dovi_checks(snapshot, dovi))
    if hdr10plus is not None:
        findings.extend(_hdr10plus_checks(snapshot, hdr10plus))
    if dovi is not None and hdr10plus is not None:
        findings.extend(_coherence(dovi, hdr10plus))
    order = {Severity.ERROR: 0, Severity.WARN: 1, Severity.INFO: 2, Severity.OK: 3}
    return tuple(sorted(findings, key=lambda f: order[f.severity]))
