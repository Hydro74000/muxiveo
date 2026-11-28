"""HDR10+ d'une piste de sortie : copié de la source ou généré depuis le Dolby Vision (deux prédicats).

Toute garde qui décide « la sortie porte du HDR10+ » (routage, transfert PQ, réserves de stockage, contrôles)
utilise :func:`outputs_hdr10plus` ; toute extraction ``hdr10plus_tool extract`` de la source utilise
:func:`hdr10plus_from_source` (en génération, le JSON préparé ``generated_hdr10plus_json`` la remplace).
"""
from __future__ import annotations

from pathlib import Path


def generates_hdr10plus(video: object) -> bool:
    """HDR10+ généré depuis le Dolby Vision (réglage effectif de la piste)."""
    return bool(getattr(video, "generate_hdr10plus", False))


def outputs_hdr10plus(video: object) -> bool:
    """La sortie porte du HDR10+ : copié de la source ou généré."""
    return bool(getattr(video, "copy_hdr10plus", False)) or generates_hdr10plus(video)


def hdr10plus_from_source(video: object) -> bool:
    """HDR10+ extrait de la source (copie seule, jamais en génération)."""
    return bool(getattr(video, "copy_hdr10plus", False)) and not generates_hdr10plus(video)


def outputs_dynamic_hdr(video: object) -> bool:
    """Métadonnées dynamiques en sortie : Dolby Vision copié, HDR10+ copié ou généré."""
    return bool(getattr(video, "copy_dv", False)) or outputs_hdr10plus(video)


def hevc_output(video: object) -> bool:
    """Sortie HEVC (``hdr10plus_tool`` : injection après l'encodage possible)."""
    codec = str(getattr(video, "codec", "") or "").lower()
    return codec == "libx265" or "hevc" in codec


#: En-tête T.35 du HDR10+ (SMPTE ST 2094-40) dans un SEI user_data_registered : pays 0xB5, fournisseur 0x003C,
#: code 0x0001, application 4 (aucune séquence 00 00 : pas d'octet d'émulation possible).
_HDR10PLUS_T35 = b"\xb5\x00\x3c\x00\x01\x04"


def has_hdr10plus_sei(stream: Path, access_units: int = 64) -> bool:
    """Le flux HEVC Annex B porte du HDR10+ (premières images : métadonnées présentes à chaque image)."""
    from core.matroska.hevc.access_units import iter_hevc_access_units

    for index, unit in enumerate(iter_hevc_access_units(Path(stream))):
        if index >= access_units:
            return False
        if any(nal.nal_type in (39, 40) and _HDR10PLUS_T35 in nal.payload for nal in unit.nal_units):
            return True
    return False


def hdr10plus_json_frames(path: Path) -> int | None:
    """Entrées ``SceneInfo`` (une par image) d'un JSON HDR10+, lues en flux ; None si illisible."""
    from core.hdr_inspection.json_stream import iter_json_array

    try:
        return sum(1 for _entry in iter_json_array(Path(path), key="SceneInfo"))
    except (OSError, ValueError):
        return None
