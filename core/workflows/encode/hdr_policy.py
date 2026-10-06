"""Politique HDR de l'onglet Video, partagée par le panneau et le workflow.

Toute option HDR reste accessible tant qu'elle est compatible avec le codec et
la sortie (voir ``docs`` de relecture) ; les combinaisons discutables donnent un
avertissement non bloquant plutôt qu'une désactivation.
"""

from __future__ import annotations

from core.inspector import HDRType
from core.workflows.encode.catalog import supports_hdr_output
from core.workflows.encode.domain.codecs import (
    interpolated_static_hdr_lost,
    output_hdr_transfer,
    transfer_kind,
)
from core.workflows.encode.models import VideoEncodeSettings

_PQ_SOURCES = frozenset({
    HDRType.HDR10,
    HDRType.HDR10PLUS,
    HDRType.DOLBY_VISION,
    HDRType.DOLBY_VISION_HDR10PLUS,
})


_DOVI_SOURCES = frozenset({HDRType.DOLBY_VISION, HDRType.DOLBY_VISION_HDR10PLUS})


def dovi_sdr_base(source_hdr: HDRType | None, source_transfer: str | None = "") -> bool:
    """Dolby Vision sur image de base SDR déclarée (P8.2, BT.709) ; P5 (VUI non spécifiée) exclu."""
    return source_hdr in _DOVI_SOURCES and transfer_kind(source_transfer) == "sdr"


def source_is_hdr(source_hdr: HDRType | None, source_transfer: str | None = "") -> bool:
    """Image source HDR : transfert PQ / HLG, ou HDR détecté (P5 compris) hors base DV SDR."""
    if transfer_kind(source_transfer) in {"pq", "hlg"}:
        return True
    if dovi_sdr_base(source_hdr, source_transfer):
        return False
    return source_hdr not in (None, HDRType.NONE)


def default_static_hdr_checked(source_hdr: HDRType | None, source_transfer: str | None = "") -> bool:
    """Case HDR10 statique cochée par défaut : source PQ (HDR10, HDR10+, DoVi).

    Base HLG (HLG, Dolby Vision P8.4) : décochée par défaut, la case reste
    disponible ; la VUI HLG de la source est conservée dans tous les cas.
    Base SDR (Dolby Vision P8.2) : décochée, la sortie reste SDR.
    """
    if source_hdr not in _PQ_SOURCES or dovi_sdr_base(source_hdr, source_transfer):
        return False
    return transfer_kind(source_transfer) != "hlg"


def hdr_warnings(video: VideoEncodeSettings) -> list[str]:
    """Combinaisons HDR permises mais à risque (avertissements non bloquants)."""
    warnings: list[str] = []
    if video.codec == "copy":
        return warnings
    source_kind = transfer_kind(video.source_color_transfer)
    hdr_source = source_kind in {"pq", "hlg"} or bool(getattr(video, "p5_to_hdr10", False))
    if hdr_source and not video.tonemap_to_sdr and not supports_hdr_output(video.codec):
        warnings.append(
            f"source HDR encodée en {video.codec} sans tone-mapping : ce codec ne porte pas le HDR, "
            "l'image sera délavée (activez le tone-mapping HDR → SDR)."
        )
    if video.tonemap_to_sdr and source_kind == "sdr":
        warnings.append(
            "tone-mapping HDR → SDR sur une source détectée SDR "
            f"(transfert {video.source_color_transfer}) : image probablement assombrie."
        )
    # Politique de sortie effective (copie DV d'une P8.2 : base SDR conservée).
    output_transfer = output_hdr_transfer(video)
    if source_kind == "sdr" and output_transfer == "pq":
        warnings.append(
            f"HDR demandé sur une source détectée SDR (transfert {video.source_color_transfer}) : "
            "la sortie sera signalée PQ / BT.2020."
        )
    dynamic = video.copy_dv or video.copy_hdr10plus
    if dynamic and not video.inject_hdr_meta and output_transfer:
        kinds = " / ".join(
            name for name, enabled in (("Dolby Vision", video.copy_dv), ("HDR10+", video.copy_hdr10plus)) if enabled
        )
        warnings.append(
            f"{kinds} sans HDR10 statique (MDCV / MaxCLL) : rendu possiblement délavé "
            "sur les téléviseurs qui s'appuient sur ces métadonnées."
        )
    if interpolated_static_hdr_lost(video):
        warnings.append(
            f"l'interpolation ne transmet pas les métadonnées HDR10 statiques à {video.codec} "
            "(aucune réinjection possible en AV1 matériel)."
        )
    return warnings
