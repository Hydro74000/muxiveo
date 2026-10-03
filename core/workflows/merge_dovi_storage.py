"""
core/workflows/merge_dovi_storage.py — Besoin d'espace disque du workflow Merge DoVi.

Le besoin n'est pas un facteur appliqué à la taille du fichier : il découle des
fichiers **présents simultanément** à chaque phase, avec la suppression
progressive des intermédiaires consommés (voir ``MergeDoviWorkflow``), et du
volume qui reçoit la sortie finale.

Limites assumées (rappelées à l'utilisateur) :
- la taille du flux vidéo vient de la piste déclarée par le conteneur
  (``StreamSize`` mediainfo) ou, à défaut, de la taille du fichier (borne haute) ;
- un flux réencodé (conversion SDR → HDR10) est supposé de taille comparable
  au flux source, sans garantie ;
- l'espace libre peut être consommé par d'autres programmes pendant le
  traitement : des contrôles juste-à-temps, sur les tailles réelles des
  fichiers d'entrée, précèdent donc chaque écriture lourde.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Marge fixe : métadonnées injectées (RPU, SEI), en-têtes, fichiers annexes.
_MARGIN_FIXED_BYTES = 256 * 1024 * 1024
#: Marge proportionnelle : croissance d'un flux par injection de métadonnées.
_MARGIN_RATIO = 50  # 2 %


def storage_margin(size: int) -> int:
    """Marge ajoutée à un besoin estimé (2 % + 256 Mio)."""
    return max(0, size) // _MARGIN_RATIO + _MARGIN_FIXED_BYTES


def format_bytes(value: int) -> str:
    """Taille lisible (Kio, Mio, Gio, Tio)."""
    size = float(max(0, value))
    units = ("o", "Kio", "Mio", "Gio", "Tio")
    index = 0
    while size >= 1024.0 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    return f"{size:.1f} {units[index]}"


@dataclass(frozen=True)
class MergeStorageInputs:
    """Paramètres d'une exécution Merge DoVi utiles au calcul d'espace."""

    film1_bytes: int          # taille du fichier Film 1
    film1_video_bytes: int    # flux vidéo de Film 1 (estimation)
    film1_raw: bool           # Film 1 = HEVC brut (utilisé tel quel, pas d'extraction)
    film1_matroska: bool      # Film 1 MKV (sinon canonicalisation MKV au remux)
    film2_video_bytes: int    # flux vidéo de Film 2 (estimation)
    film2_extracted: bool     # Film 2 extrait en HEVC annexB dans le dossier de travail
    film2_converted: bool     # conversion Dolby Vision P7/P5 → P8.1
    sdr_to_hdr10: bool        # Film 1 SDR réencodé en HDR10
    inject_dovi: bool
    inject_hdr10plus: bool
    static_hdr_copy: bool     # injection SEI statiques (nouvelle copie du flux)


@dataclass(frozen=True)
class StoragePhase:
    """Octets présents simultanément pendant une phase."""

    label: str
    work_bytes: int           # dans le dossier de travail
    output_bytes: int = 0     # sur le volume de sortie (fichier final en cours d'écriture)


@dataclass(frozen=True)
class StorageRequirement:
    """Besoin (pic + marge) par volume."""

    phases: tuple[StoragePhase, ...]
    same_volume: bool
    work_bytes: int           # volume du dossier de travail (inclut la sortie si même volume)
    output_bytes: int         # volume de sortie (0 si même volume)
    peak_phase: str           # phase du pic sur le volume du dossier de travail


def merge_storage_phases(inputs: MergeStorageInputs) -> tuple[StoragePhase, ...]:
    """Phases du workflow et fichiers qu'elles font coexister.

    Suppression progressive supposée : ``film1.hevc`` après conversion
    SDR → HDR10, ``film2*.hevc`` après extraction des métadonnées, maillon
    précédent après chaque injection, flux final après encapsulation.
    """
    v1 = max(0, inputs.film1_video_bytes)
    v2 = max(0, inputs.film2_video_bytes)
    base = 0 if inputs.film1_raw else v1          # film1.hevc extrait
    film2_work = v2 if inputs.film2_extracted else 0
    phases: list[StoragePhase] = [StoragePhase("Extraction HEVC", base + film2_work)]

    if inputs.sdr_to_hdr10:
        phases.append(StoragePhase("Conversion SDR → HDR10", base + v1 + film2_work))
        base = v1                                  # film1_hdr10.hevc ; film1.hevc supprimé
    if inputs.film2_converted:
        phases.append(StoragePhase("Conversion Dolby Vision → P8.1", base + film2_work + v2))
        film2_work = v2                            # film2_p8.hevc ; film2.hevc supprimé
    phases.append(StoragePhase("Extraction des métadonnées", base + film2_work))

    chain = base                                   # flux courant présent dans le dossier de travail
    for label, active in (
        ("Injection RPU Dolby Vision", inputs.inject_dovi),
        ("Injection HDR10+", inputs.inject_hdr10plus),
        ("Injection SEI HDR10 statiques", inputs.static_hdr_copy),
    ):
        if active:
            phases.append(StoragePhase(label, chain + v1))
            chain = v1

    phases.append(StoragePhase("Encapsulation vidéo MKV", chain + v1))
    canonical = 0 if inputs.film1_matroska else max(0, inputs.film1_bytes)
    output = v1 + max(0, inputs.film1_bytes - inputs.film1_video_bytes)
    phases.append(StoragePhase("Assemblage final", v1 + canonical, output))
    return tuple(phases)


def storage_requirement(
    phases: tuple[StoragePhase, ...],
    *,
    same_volume: bool,
) -> StorageRequirement:
    """Pic par volume (+ marge) ; même volume → dossier de travail et sortie cumulés par phase."""
    if same_volume:
        peak = max(phases, key=lambda p: p.work_bytes + p.output_bytes)
        total = peak.work_bytes + peak.output_bytes
        return StorageRequirement(phases, True, total + storage_margin(total), 0, peak.label)
    work_peak = max(phases, key=lambda p: p.work_bytes)
    output_peak = max(p.output_bytes for p in phases)
    return StorageRequirement(
        phases,
        False,
        work_peak.work_bytes + storage_margin(work_peak.work_bytes),
        output_peak + storage_margin(output_peak) if output_peak else 0,
        work_peak.label,
    )


__all__ = [
    "MergeStorageInputs",
    "StoragePhase",
    "StorageRequirement",
    "format_bytes",
    "merge_storage_phases",
    "storage_margin",
    "storage_requirement",
]
