"""Politique Dolby Vision de l'onglet Video (matrice V30), partagée par le panneau et le workflow.

Une copie ne décode ni ne réencode jamais l'image : en Copy + « Conserver »,
la piste est recopiée telle quelle (P7 compris, couche d'amélioration incluse).
Une source P5 réencodée est toujours convertie en HDR10 (couleurs IPT), que le
Dolby Vision soit copié ou non.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.dovi_profile_detector import DoviSubProfile

NORMALIZE_P81 = "2"

P5_COPY_NORMALIZE_ERROR = (
    "Une source P5 doit être réencodée pour devenir P8.1 : choisissez un encodeur HEVC (x265 ou NVEncC)."
)
BASE_LAYER_NORMALIZE_ERROR = "Image de base HLG/SDR : conversion en P8.1 impossible."

# Profil Dolby Vision de sortie d'un réencodage avec copie DV, selon la source.
_REENCODE_OUTPUT_PROFILE = {
    DoviSubProfile.P8_4: "8.4",
    DoviSubProfile.P8_2: "8.2",
}
_NON_P81_BASE = frozenset({DoviSubProfile.P8_4, DoviSubProfile.P8_2})


@dataclass(frozen=True)
class DoviPlan:
    """Traitement Dolby Vision prévu pour une piste vidéo."""

    sub_profile: DoviSubProfile
    #: Copy + « Conserver » : aucune analyse ni transformation.
    passthrough: bool = False
    #: Source P5 réencodée : couleurs IPT converties en HDR10 BT.2020/PQ.
    color_convert: bool = False
    #: Copy + « Normaliser » : ``dovi_tool convert`` sans réencodage (P7).
    normalize_copy: bool = False
    #: Profil DV de sortie quand le RPU est réinjecté ("8.1", "8.4", "8.2") ; "" sinon.
    output_profile: str = ""
    #: Combinaison impossible (refus en validation, option grisée dans le panneau).
    error: str = ""
    #: Effets à annoncer (bandeau du panneau, journal).
    notes: tuple[str, ...] = ()


def sub_profile_from_value(value: str | None) -> DoviSubProfile:
    """``DoviSubProfile`` d'une valeur sérialisée ("p5", "p8_1"…), UNKNOWN sinon."""
    try:
        return DoviSubProfile(str(value or "").strip().lower())
    except ValueError:
        return DoviSubProfile.UNKNOWN


def sub_profile_from_track(profile: int | None, compat_id: int | None) -> DoviSubProfile:
    """Sous-profil depuis le profil / compat id d'une piste inspectée (``VideoTrack``)."""
    try:
        number = int(profile or 0)
    except (TypeError, ValueError):
        return DoviSubProfile.UNKNOWN
    if number == 5:
        return DoviSubProfile.P5
    if number == 7:
        return DoviSubProfile.P7_FEL
    if number == 8:
        return {
            0: DoviSubProfile.P8_0,
            1: DoviSubProfile.P8_1,
            2: DoviSubProfile.P8_2,
            4: DoviSubProfile.P8_4,
        }.get(compat_id if isinstance(compat_id, int) else -1, DoviSubProfile.UNKNOWN)
    return DoviSubProfile.UNKNOWN


def resolve_dovi_plan(
    *,
    codec: str,
    copy_dv: bool,
    dovi_profile: str | None,
    sub_profile: DoviSubProfile,
) -> DoviPlan:
    """Matrice V30 : Auto / Normaliser × sous-profil × Copy / réencodage."""
    normalize = str(dovi_profile or "0").strip() == NORMALIZE_P81
    copy = str(codec or "").strip().lower() == "copy"
    if copy:
        if not (copy_dv and normalize):
            return DoviPlan(sub_profile=sub_profile, passthrough=True)
        if sub_profile == DoviSubProfile.P5:
            return DoviPlan(sub_profile=sub_profile, error=P5_COPY_NORMALIZE_ERROR)
        if sub_profile in _NON_P81_BASE:
            return DoviPlan(sub_profile=sub_profile, error=BASE_LAYER_NORMALIZE_ERROR)
        notes: tuple[str, ...] = ()
        if sub_profile in {DoviSubProfile.P7_FEL, DoviSubProfile.P7_MEL}:
            notes = ("P7 → P8.1 sans réencodage : couche d'amélioration retirée, image de base recopiée.",)
        return DoviPlan(sub_profile=sub_profile, normalize_copy=True, output_profile="8.1", notes=notes)

    color_convert = sub_profile == DoviSubProfile.P5
    notes_list: list[str] = []
    if color_convert:
        notes_list.append("P5 : couleurs IPT converties en HDR10 BT.2020/PQ (libplacebo).")
    if not copy_dv:
        return DoviPlan(sub_profile=sub_profile, color_convert=color_convert, notes=tuple(notes_list))
    if normalize and sub_profile in _NON_P81_BASE:
        return DoviPlan(
            sub_profile=sub_profile,
            color_convert=color_convert,
            error=BASE_LAYER_NORMALIZE_ERROR,
        )
    output = _REENCODE_OUTPUT_PROFILE.get(sub_profile, "8.1")
    if sub_profile in {DoviSubProfile.P7_FEL, DoviSubProfile.P7_MEL}:
        notes_list.append("P7 → P8.1 : couche d'amélioration perdue au réencodage.")
    elif sub_profile == DoviSubProfile.P5:
        notes_list.append("P5 → P8.1 (RPU converti, repli HDR10 compatible).")
    return DoviPlan(
        sub_profile=sub_profile,
        color_convert=color_convert,
        output_profile=output,
        notes=tuple(notes_list),
    )


# bl_signal_compatibility_id du record Matroska par profil de sortie.
_OUTPUT_COMPAT_ID = {"8.1": 1, "8.4": 4, "8.2": 2}


def dovi_output_compat_id(output_profile: str) -> int | None:
    """Compatibilité de l'image de base annoncée dans le record DOVI (None : inconnue)."""
    return _OUTPUT_COMPAT_ID.get(str(output_profile or ""))


def dovi_output_compat_id_for(video: object) -> int | None:
    """Compatibilité du record DOVI d'une piste réencodée avec copie DV (matrice V30).

    Sous-profil source inconnu : None (compatibilité lue dans le RPU ou le record existant).
    """
    sub_profile = sub_profile_from_value(str(getattr(video, "dovi_source_profile", "") or ""))
    if sub_profile is DoviSubProfile.UNKNOWN:
        return None
    plan = resolve_dovi_plan(
        codec=str(getattr(video, "codec", "") or ""),
        copy_dv=bool(getattr(video, "copy_dv", False)),
        dovi_profile=str(getattr(video, "dovi_profile", "0") or "0"),
        sub_profile=sub_profile,
    )
    return dovi_output_compat_id(plan.output_profile) if not plan.error else None


def dovi_transfer_error(output_profile: str, output_transfer: str) -> str:
    """Cohérence profil DV / transfert de l'image de base ("" si cohérent ou inconnu)."""
    if output_profile == "8.1" and output_transfer == "hlg":
        return "Dolby Vision P8.1 exige une image de base PQ (HDR10) ; la sortie est HLG."
    if output_profile == "8.4" and output_transfer == "pq":
        return "Dolby Vision P8.4 exige une image de base HLG ; la sortie est PQ."
    if output_profile == "8.2" and output_transfer in {"pq", "hlg"}:
        return "Dolby Vision P8.2 exige une image de base SDR ; décochez le HDR10 statique."
    return ""


__all__ = [
    "BASE_LAYER_NORMALIZE_ERROR",
    "DoviPlan",
    "NORMALIZE_P81",
    "P5_COPY_NORMALIZE_ERROR",
    "dovi_output_compat_id",
    "dovi_output_compat_id_for",
    "dovi_transfer_error",
    "resolve_dovi_plan",
    "sub_profile_from_track",
    "sub_profile_from_value",
]
