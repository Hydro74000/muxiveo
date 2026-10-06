"""
core/workflows/merge_dovi.py — Workflow d'injection DoVi RPU + HDR10+.

Logique métier pilotée depuis l'interface Qt via ToolRunner.

Classes publiques :
    FrameCountResult   — résultat de la comparaison des frame counts
    DoviProfile        — profil Dolby Vision cible (8.0 / 8.1)
    WorkflowStep       — énumération des étapes du workflow
    StepResult         — résultat d'une étape
    MergeDoviWorkflow  — orchestrateur du workflow

Signaux :
    MergeDoviWorkflow.step_started(step: WorkflowStep)
    MergeDoviWorkflow.step_progress(step: WorkflowStep, message: str)
    MergeDoviWorkflow.step_finished(step: WorkflowStep, result: StepResult)
    MergeDoviWorkflow.workflow_finished(output_path: str)
    MergeDoviWorkflow.workflow_failed(step: WorkflowStep, error: str)

Conventions :
    - Jamais shell=True
    - pathlib.Path pour tous les chemins
    - ThreadPoolExecutor pour les extractions parallèles
    - Signaux Qt thread-safe (QueuedConnection depuis les workers)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal
from core.dovi_profile_detector import DoviProfileDetector, DoviSubProfile
from core.frame_count import reliable_frame_count
from core.subprocess_utils import format_returncode, kill_process_tree, run_cancellable_capture, subprocess_text_kwargs
from core.workflows.common.validation_override import ValidationOverride, accept_validation_override
from core.workflows.common.validation_override import validate_final_output
from core.runner import TaskCancelledError
from core.matroska.validation import validate_matroska_output
from core.subtitle_codec import plan_subtitle_codec
from core.output_commit import OutputBusyError, OutputReservation
from core.workdir import ProcessWorkDir, create_process_work_dir, filesystem_type
from core.workflows.encode.runtime.dovi_p7_router import DoviP7Router, P7RoutingDecision
from core.workflows.encode.runtime.frame_count_guard import (
    FrameCountAudit,
    FrameCountAuditError,
    FrameCountGuard,
    MetadataAdjustment,
)
from core.workflows.merge_dovi_storage import (
    MergeStorageInputs,
    format_bytes,
    merge_storage_phases,
    storage_margin,
    storage_requirement,
)
from core.workflows.hevc_static_hdr_metadata import inject_static_hdr_sei_file
from core.matroska.assembly import (
    MatroskaAssemblyAttachment,
    MatroskaAssemblyPlan,
    MatroskaAssemblyTrack,
    MatroskaTrackFlags,
    assembly_output_contract,
    compile_assembly_plan,
)
from core.matroska.ebml import element, float_element, uint_element
from core.matroska.editors.dovi import (
    DolbyVisionConfigRecord,
    MatroskaDoviBlockAdditionEditor,
)
from core.matroska.editors.video_timecodes import MatroskaVideoTimecodePatcher
from core.matroska.ids import (
    BITS_PER_CHANNEL_ID,
    CHAPTERS_ID,
    COLOUR_ID,
    LUMINANCE_MAX_ID,
    LUMINANCE_MIN_ID,
    MASTERING_METADATA_ID,
    MATRIX_COEFFICIENTS_ID,
    MAX_CLL_ID,
    MAX_FALL_ID,
    PRIMARY_B_CHROMATICITY_X_ID,
    PRIMARY_B_CHROMATICITY_Y_ID,
    PRIMARY_G_CHROMATICITY_X_ID,
    PRIMARY_G_CHROMATICITY_Y_ID,
    PRIMARY_R_CHROMATICITY_X_ID,
    PRIMARY_R_CHROMATICITY_Y_ID,
    PRIMARIES_ID,
    RANGE_ID,
    TAGS_ID,
    TRACK_TYPE_VIDEO,
    TRANSFER_CHARACTERISTICS_ID,
    WHITE_POINT_CHROMATICITY_X_ID,
    WHITE_POINT_CHROMATICITY_Y_ID,
)
from core.matroska.mux_plan import deterministic_source_identity
from core.matroska.native_muxer import MatroskaNativeMuxer
from core.matroska.reader import MatroskaReader, strict_demuxer_reads_tracks
from core.matroska.writer import MatroskaWriter

# Outils dont la barre de progression XX% n'est émise qu'en TTY.
_PTY_PROGRESS_TOOLS: frozenset[str] = frozenset({"dovi_tool", "hdr10plus_tool"})
# Pourcentage à l'intérieur d'une ligne de progression (ex : "Extracting RPU... 73%")
_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")
# Séquences ANSI à supprimer pour garder un log lisible.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_FALLBACK_HEVC_FRAME_RATE = "24000/1001"

#: Extensions de streams HEVC bruts — pas besoin d'extraction ffmpeg.
_RAW_HEVC_EXTENSIONS: frozenset[str] = frozenset({
    ".hevc", ".h265", ".265", ".x265",
})

#: Extensions de conteneurs acceptés en entrée merge_dovi.
#: dovi_tool et hdr10plus_tool ne gèrent nativement que MKV + stream HEVC brut ;
#: pour tout autre conteneur (MP4/MOV/TS/M2TS/VOB/...), on extrait d'abord en
#: HEVC annexB via ffmpeg avec le BSF hevc_mp4toannexb (obligatoire pour MP4).
_MERGE_DOVI_CONTAINERS: frozenset[str] = frozenset({
    ".mkv", ".mk3d", ".mks",
    ".mp4", ".m4v",
    ".mov",
    ".ts", ".m2ts", ".mts",
    ".mpg", ".mpeg", ".m2v", ".mpv", ".evo", ".evob", ".vob",
    ".avi",
    ".webm",
    ".flv", ".f4v",
})

_MERGE_DOVI_ACCEPTED: frozenset[str] = _MERGE_DOVI_CONTAINERS | _RAW_HEVC_EXTENSIONS


def _is_raw_hevc(path: Path) -> bool:
    """Teste si le fichier est un stream HEVC brut (annexB)."""
    return path.suffix.lower() in _RAW_HEVC_EXTENSIONS


def _needs_hevc_extraction(path: Path) -> bool:
    """Teste si le fichier doit être extrait en HEVC annexB avant manipulation."""
    return not _is_raw_hevc(path)


# Mapping des primaires colorimétriques mediainfo → coordonnées CIE 1931 en
# unités 0.00002 (échelle utilisée par master_display côté x265/HEVC SEI 137).
# Format SEI : G(x,y)B(x,y)R(x,y)WP(x,y) avec L(max_lum, min_lum) en 0.0001 cd/m².
_PRIMARIES_DISPLAYS: dict[str, tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]]] = {
    # BT.2020 / Rec.2020 — primaires standards UHD HDR
    "bt.2020": ((8500, 39850), (6550, 2300), (35400, 14600), (15635, 16450)),
    "rec.2020": ((8500, 39850), (6550, 2300), (35400, 14600), (15635, 16450)),
    # Display P3 — souvent utilisé sur masters Apple/Disney
    "display p3": ((13250, 34500), (7500, 3000), (34000, 16000), (15635, 16450)),
    "p3": ((13250, 34500), (7500, 3000), (34000, 16000), (15635, 16450)),
}

_MASTERING_LUM_RE = re.compile(
    r"min:\s*([\d.]+)\s*cd/m\^?2.*?max:\s*([\d.]+)\s*cd/m\^?2",
    re.IGNORECASE | re.DOTALL,
)


def _format_master_display_from_mediainfo(track: dict) -> str:
    """Construit le ``master_display`` x265 depuis un track Video mediainfo.

    mediainfo expose ``MasteringDisplay_ColorPrimaries`` (ex "BT.2020") et
    ``MasteringDisplay_Luminance`` (ex "min: 0.0050 cd/m^2, max: 1000 cd/m^2").
    Renvoie "" si les deux ne sont pas extractibles."""
    primaries_raw = str(track.get("MasteringDisplay_ColorPrimaries") or "").strip().lower()
    lum_raw = str(track.get("MasteringDisplay_Luminance") or "").strip()
    if not primaries_raw or not lum_raw:
        return ""

    coords = None
    for key, value in _PRIMARIES_DISPLAYS.items():
        if key in primaries_raw:
            coords = value
            break
    if coords is None:
        return ""

    m = _MASTERING_LUM_RE.search(lum_raw)
    if not m:
        return ""
    try:
        val1 = float(m.group(1))
        val2 = float(m.group(2))
        lmin = int(round(min(val1, val2) * 10000))
        lmax = int(round(max(val1, val2) * 10000))
    except ValueError:
        return ""

    g, b, r, wp = coords
    return (
        f"G({g[0]},{g[1]})"
        f"B({b[0]},{b[1]})"
        f"R({r[0]},{r[1]})"
        f"WP({wp[0]},{wp[1]})"
        f"L({lmax},{lmin})"
    )


def _format_max_cll_from_mediainfo(track: dict) -> str:
    """Construit ``"MaxCLL,MaxFALL"`` depuis ``MaxCLL`` / ``MaxFALL`` mediainfo
    (formats : "1000 cd/m2" ou "1000"). Renvoie "" si manquant."""
    raw_cll = str(track.get("MaxCLL") or "").strip()
    raw_fall = str(track.get("MaxFALL") or "").strip()
    if not raw_cll or not raw_fall:
        return ""
    m_cll = re.search(r"(\d+)", raw_cll)
    m_fall = re.search(r"(\d+)", raw_fall)
    if not m_cll or not m_fall:
        return ""
    return f"{m_cll.group(1)},{m_fall.group(1)}"


def _build_colour_element_from_static_hdr(static_hdr: StaticHdrMetadata) -> bytes:
    """Construit l'élément EBML Colour (0x55B0) pour la piste vidéo Matroska.

    Contient les métadonnées HDR10 conteneur :
      - MatrixCoefficients (9 = BT.2020 non-constant)
      - BitsPerChannel (10)
      - Range (1 = limited)
      - TransferCharacteristics (16 = PQ)
      - Primaries (9 = BT.2020)
      - MaxCLL / MaxFALL
      - MasteringMetadata (primaires RGB, white point, luminance min/max)
    """
    mastering_children: list[bytes] = []
    if static_hdr.master_display:
        m = re.search(
            r"G\((\d+),(\d+)\)B\((\d+),(\d+)\)R\((\d+),(\d+)\)WP\((\d+),(\d+)\)L\((\d+),(\d+)\)",
            static_hdr.master_display,
        )
        if m:
            gx, gy = int(m.group(1)) / 50000.0, int(m.group(2)) / 50000.0
            bx, by = int(m.group(3)) / 50000.0, int(m.group(4)) / 50000.0
            rx, ry = int(m.group(5)) / 50000.0, int(m.group(6)) / 50000.0
            wpx, wpy = int(m.group(7)) / 50000.0, int(m.group(8)) / 50000.0
            lum_a = int(m.group(9)) / 10000.0
            lum_b = int(m.group(10)) / 10000.0
            lmax = max(lum_a, lum_b)
            lmin = min(lum_a, lum_b)
            mastering_children = [
                float_element(PRIMARY_R_CHROMATICITY_X_ID, rx),
                float_element(PRIMARY_R_CHROMATICITY_Y_ID, ry),
                float_element(PRIMARY_G_CHROMATICITY_X_ID, gx),
                float_element(PRIMARY_G_CHROMATICITY_Y_ID, gy),
                float_element(PRIMARY_B_CHROMATICITY_X_ID, bx),
                float_element(PRIMARY_B_CHROMATICITY_Y_ID, by),
                float_element(WHITE_POINT_CHROMATICITY_X_ID, wpx),
                float_element(WHITE_POINT_CHROMATICITY_Y_ID, wpy),
                float_element(LUMINANCE_MAX_ID, lmax),
                float_element(LUMINANCE_MIN_ID, lmin),
            ]

    colour_children = [
        uint_element(MATRIX_COEFFICIENTS_ID, 9),
        uint_element(BITS_PER_CHANNEL_ID, 10),
        uint_element(RANGE_ID, 1),
        uint_element(TRANSFER_CHARACTERISTICS_ID, 16),
        uint_element(PRIMARIES_ID, 9),
    ]

    if static_hdr.max_cll:
        parts = static_hdr.max_cll.split(",")
        if len(parts) == 2:
            try:
                cll, fall = int(parts[0]), int(parts[1])
                colour_children.append(uint_element(MAX_CLL_ID, cll))
                colour_children.append(uint_element(MAX_FALL_ID, fall))
            except ValueError:
                pass

    if mastering_children:
        colour_children.append(element(MASTERING_METADATA_ID, b"".join(mastering_children)))

    return element(COLOUR_ID, b"".join(colour_children))


# =============================================================================
# Types de données
# =============================================================================

class DoviProfile(Enum):
    """
    Profil Dolby Vision cible pour l'injection RPU.

    La valeur de l'enum est le flag `-m` passé en argument GLOBAL à dovi_tool
    (avant la sous-commande inject-rpu).

    Modes :
        DISABLED = "disabled" → n'injecte pas Dolby Vision. Le workflow peut
                       quand même injecter HDR10+ ou convertir un Film 1 SDR
                       vers HDR10 si Film 2 sert de référence HDR10.
        P8_1 = "2"  → normalise le RPU en Profile 8.1, supprime le mapping FEL.
                       Standard pour les remux UHD Blu-ray. Recommandé.
        P8_0 = "0"  → copie le RPU sans modification (rewrite untouched).
                       Préserve le profil source tel quel.
    """
    DISABLED = "disabled"  # Pas d'injection Dolby Vision, HDR10+ reste possible.
    P8_1 = "2"   # -m 2 : conversion Profile 8.1 (standard remux UHD)
    P8_0 = "0"   # -m 0 : copie brute sans conversion


class WorkflowStep(Enum):
    """Étapes du workflow dans l'ordre d'exécution."""
    VALIDATION        = auto()   # Vérifications préliminaires
    DETECT_DOVI       = auto()   # Détection du sous-profil DoVi de Film 2 (P5/P7/P8.x)
    FRAME_COUNT       = auto()   # Comparaison des frame counts
    STORAGE_CHECK     = auto()   # Espace disque : fichiers simultanés par phase et par volume
    EXTRACT_PARALLEL  = auto()   # Extractions parallèles (HEVC + RPU + HDR10+)
    SDR_TO_HDR10      = auto()   # Conversion Film 1 SDR → HDR10 assistée par Film 2
    CONVERT_DOVI      = auto()   # Conversion P7/P5 → P8.1 si nécessaire
    CHECK_METADATA    = auto()   # Comptage RPU / HDR10+ extraits vs Film 1, avant injection
    INJECT_DOVI       = auto()   # Injection RPU DoVi
    INJECT_HDR10PLUS  = auto()   # Injection HDR10+
    INJECT_STATIC_HDR = auto()   # Injection SEI HDR10 statiques (master_display / max_cll)
    VERIFY            = auto()   # Relecture du flux final : trames vidéo, RPU et HDR10+ injectés
    REMUX             = auto()   # Remuxage final MKV
    CLEANUP           = auto()   # Nettoyage des fichiers intermédiaires


#: Surplus maximal de trames de métadonnées retirable en fin de flux (option explicite).
_METADATA_TAIL_TOLERANCE = 4


@dataclass
class FrameCountResult:
    """Résultat de la comparaison des frame counts des deux fichiers.

    Comparaison de **comptages** : des nombres égaux ne prouvent pas
    l'alignement temporel (coupes, décalage en tête).
    """
    fc1: int | None
    fc2: int | None
    diff: int | None
    #: Film 2 HEVC brut : pas de comptage vidéo (lecture complète évitée) ;
    #: le contrôle exact porte sur ses métadonnées extraites (CHECK_METADATA).
    fc2_deferred: bool = False

    @property
    def compatible(self) -> bool:
        """Comptages identiques (seul cas injectable sans option explicite)."""
        return self.diff == 0

    @property
    def warning(self) -> bool:
        """True si l'écart est faible mais non nul (option explicite possible si Film 2 est plus long)."""
        return self.diff is not None and 0 < self.diff <= _METADATA_TAIL_TOLERANCE

    @property
    def status_text(self) -> str:
        if self.fc2_deferred and self.fc1 is not None:
            return f"{self.fc1} frames — Film 2 (flux brut) compté sur ses métadonnées"
        if self.fc1 is None or self.fc2 is None:
            return "Frame count illisible"
        if self.diff == 0:
            return f"{self.fc1} frames — comptages identiques"
        if self.warning:
            return f"{self.fc1} / {self.fc2} frames — écart {self.diff} (ajustement explicite requis)"
        return f"{self.fc1} / {self.fc2} frames — écart {self.diff} (incompatible)"

    def metadata_verdict(self, adjustment: MetadataAdjustment) -> str | None:
        """Motif empêchant d'injecter les métadonnées de Film 2 dans Film 1 ; None si possible.

        Film 2 sert d'estimation du nombre de trames de ses métadonnées ; le
        comptage réel RPU / HDR10+ est contrôlé après extraction.
        """
        if self.fc1 is None:
            return (
                "Nombre de trames de Film 1 illisible : comptage des métadonnées "
                "impossible, injection refusée."
            )
        if self.fc2_deferred:
            return None
        if self.fc2 is None:
            return (
                "Nombre de trames illisible (Film 1 ou Film 2) : comptage des métadonnées "
                "impossible, injection refusée."
            )
        delta = self.fc2 - self.fc1
        if delta == 0:
            return None
        if abs(delta) > _METADATA_TAIL_TOLERANCE:
            return (
                f"Écart de {abs(delta)} frames trop important — "
                "les deux fichiers ne semblent pas être le même contenu."
            )
        if delta < 0:
            return (
                f"Film 2 a {self.fc2} trames, Film 1 {self.fc1} ({delta}) : ses métadonnées "
                "seraient plus courtes que Film 1 — aucune trame de métadonnées n'est fabriquée."
            )
        if adjustment is MetadataAdjustment.EXACT:
            return (
                f"Film 2 a {self.fc2} trames, Film 1 {self.fc1} (+{delta}) : comptages différents, "
                "politique exacte. Si le début des deux films est aligné, activer « Retirer "
                "l'excédent de métadonnées en fin de flux »."
            )
        return None


@dataclass
class HDRFlags:
    """Résultat de la détection des formats HDR dans Film 2."""
    has_dovi: bool = False
    has_hdr10plus: bool = False

    @property
    def label(self) -> str:
        parts = []
        if self.has_dovi:
            parts.append("Dolby Vision")
        if self.has_hdr10plus:
            parts.append("HDR10+")
        return " + ".join(parts) if parts else "Aucun HDR avancé détecté"


@dataclass
class StaticHdrMetadata:
    """SEI HDR10 statiques (Mastering Display + Content Light Level).

    Lus via mediainfo. ``master_display`` est au format x265
    ``G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)`` ; ``max_cll`` au format
    ``"MaxCLL,MaxFALL"``. Une chaîne vide signifie « non disponible ».
    """
    master_display: str = ""
    max_cll: str = ""

    @property
    def has_any(self) -> bool:
        return bool(self.master_display or self.max_cll)

    @property
    def is_complete(self) -> bool:
        return bool(self.master_display and self.max_cll)


@dataclass(frozen=True)
class HdrState:
    """État HDR d'une piste vidéo, lu depuis mediainfo (``HDR_Format`` + transfert)."""
    has_dovi: bool = False
    has_hdr10plus: bool = False
    transfer: str = ""

    @classmethod
    def from_mediainfo(cls, hdr_format: str, transfer: str) -> "HdrState":
        # Match strict « App 4 » : « SMPTE ST 2094-10 » (DV legacy) n'est pas HDR10+.
        hdr_lower = hdr_format.lower()
        return cls(
            has_dovi="dolby vision" in hdr_lower,
            has_hdr10plus="smpte st 2094 app 4" in hdr_lower,
            transfer=transfer.strip().lower(),
        )

    @property
    def label(self) -> str:
        parts = ["Dolby Vision"] if self.has_dovi else []
        if self.has_hdr10plus:
            parts.append("HDR10+")
        elif any(k in self.transfer for k in ("hlg", "arib")):
            parts.append("HLG")
        elif any(k in self.transfer for k in ("pq", "2084")):
            parts.append("HDR10")
        elif self.transfer and not parts:
            parts.append("SDR")
        return " + ".join(parts) or "?"


def probe_hdr_state(path: Path, mediainfo_bin: str = "mediainfo") -> HdrState:
    """Lit l'état HDR de la première piste vidéo de ``path`` (mediainfo JSON)."""
    try:
        result = subprocess.run(
            [mediainfo_bin, "--Output=JSON", str(path)],
            capture_output=True, check=False, **subprocess_text_kwargs(),
        )
        data = json.loads(result.stdout or "{}")
    except (OSError, json.JSONDecodeError):
        return HdrState()
    for track in (data.get("media") or {}).get("track") or []:
        if isinstance(track, dict) and track.get("@type") == "Video":
            return HdrState.from_mediainfo(
                str(track.get("HDR_Format") or ""),
                str(track.get("transfer_characteristics") or ""),
            )
    return HdrState()


@dataclass(frozen=True)
class ValidationContext:
    flags: HDRFlags
    static_film1: StaticHdrMetadata
    static_film2: StaticHdrMetadata
    film1_needs_sdr_to_hdr10: bool = False
    film2_has_hdr10_reference: bool = False
    film1_has_dovi: bool = False


@dataclass
class StepResult:
    """Résultat d'une étape du workflow."""
    step:     WorkflowStep
    success:  bool
    message:  str
    duration: float = 0.0
    detail:   str   = ""


# =============================================================================
# Configuration interne du workflow
# =============================================================================

@dataclass
class _WorkflowPaths:
    """Chemins intermédiaires utilisés pendant le workflow."""
    work_dir:         Path
    film1:            Path   # Chemin original Film 1 (MKV ou HEVC brut)
    film1_hevc:       Path   # HEVC extrait de Film 1 (uniquement si Film 1 est MKV)
    film1_hdr10_hevc: Path   # Film 1 SDR transcodé en HEVC HDR10
    film2_hevc:       Path   # HEVC temporaire Film 2 (cas 2 : double extraction)
    film2_hevc_p8:    Path   # HEVC Film 2 converti P7/P5 → P8.1 (sert de source RPU)
    film2_rpu:        Path   # RPU DoVi extrait de Film 2
    film2_hdr10plus:  Path   # Métadonnées HDR10+ extraites de Film 2
    film1_with_dovi:  Path   # Film 1 + RPU DoVi injecté
    film1_final:      Path   # Film 1 + RPU DoVi + HDR10+ (résultat final HEVC)
    film1_with_static_hdr: Path  # Film 1 final + SEI HDR10 statiques injectés
    film1_wrapped_video: Path  # Encapsulation MKV de la vidéo injectée (PTS reconstruit)
    output_mkv:       Path   # Fichier de sortie final
    #: Dossier process créé par start() ; None = propriété non prouvée (aucune suppression récursive).
    owned_dir:        ProcessWorkDir | None = None

    @classmethod
    def from_config(
        cls,
        work_dir: Path,
        output_dir: Path,
        film1: Path,
        basename: str,
        *,
        owned_dir: ProcessWorkDir | None = None,
    ) -> "_WorkflowPaths":
        return cls(
            work_dir        = work_dir,
            film1           = film1,
            film1_hevc      = work_dir / "film1.hevc",
            film1_hdr10_hevc = work_dir / "film1_hdr10.hevc",
            film2_hevc      = work_dir / "film2.hevc",
            film2_hevc_p8   = work_dir / "film2_p8.hevc",
            film2_rpu       = work_dir / "film2_rpu.bin",
            film2_hdr10plus = work_dir / "film2_hdr10plus.json",
            film1_with_dovi = work_dir / "film1_with_dovi.hevc",
            film1_final     = work_dir / "film1_final.hevc",
            film1_with_static_hdr = work_dir / "film1_with_static_hdr.hevc",
            film1_wrapped_video = work_dir / "film1_wrapped_video.mkv",
            output_mkv      = output_dir / f"{basename}.mkv",
            owned_dir       = owned_dir,
        )

    @property
    def verify_rpu(self) -> Path:
        """RPU relu depuis le flux final (VERIFY)."""
        return self.work_dir / "verify_rpu.bin"

    @property
    def verify_hdr10plus(self) -> Path:
        """HDR10+ relu depuis le flux final (VERIFY)."""
        return self.work_dir / "verify_hdr10plus.json"

    def intermediates(self) -> tuple[Path, ...]:
        """Fichiers que le workflow crée dans le dossier de travail (jamais Film 1 / Film 2)."""
        return (
            self.film1_hevc,
            self.film1_hdr10_hevc,
            self.film2_hevc,
            self.film2_hevc_p8,
            self.film2_rpu,
            self.film2_hdr10plus,
            self.film1_with_dovi,
            self.film1_final,
            self.film1_with_static_hdr,
            self.film1_wrapped_video,
            self.work_dir / "film1_canonical.mkv",
            self.verify_rpu,
            self.verify_hdr10plus,
        )

    def discard(self, *candidates: Path) -> list[Path]:
        """Supprime les intermédiaires consommés parmi ``candidates`` ; retourne ceux supprimés.

        Seuls les noms connus du dossier de travail sont supprimables : un
        chemin utilisateur (Film 1 HEVC brut, Film 2) n'est jamais touché.
        """
        known = set(self.intermediates())
        removed: list[Path] = []
        for path in candidates:
            if path in known and path not in (self.film1,) and path.is_file():
                path.unlink(missing_ok=True)
                removed.append(path)
        return removed

    @property
    def film1_hevc_input(self) -> Path:
        """
        Chemin HEVC en entrée des outils d'injection.
        Si Film 1 est un conteneur (MKV/MP4/TS/…) → film1_hevc extrait en annexB.
        Si Film 1 est un stream HEVC brut           → film1 directement.
        """
        if self.film1_hdr10_hevc.exists():
            return self.film1_hdr10_hevc
        return self.film1 if _is_raw_hevc(self.film1) else self.film1_hevc

    def injection_chain_final(
        self, flags: HDRFlags, *, static_hdr_applied: bool = False,
    ) -> Path:
        """Fichier HEVC final à muxer selon les opérations effectuées."""
        if static_hdr_applied:
            return self.film1_with_static_hdr
        if flags.has_dovi and flags.has_hdr10plus:
            return self.film1_final
        if flags.has_dovi:
            return self.film1_with_dovi
        if flags.has_hdr10plus:
            return self.film1_final
        if self.film1_hdr10_hevc.exists():
            return self.film1_hdr10_hevc
        return self.film1_hevc_input


# =============================================================================
# Exceptions
# =============================================================================

class WorkflowError(RuntimeError):
    """Erreur bloquante pendant le workflow."""
    def __init__(self, step: WorkflowStep, message: str) -> None:
        self.step    = step
        self.message = message
        super().__init__(f"[{step.name}] {message}")


# =============================================================================
# MergeDoviWorkflow
# =============================================================================

class MergeDoviWorkflow(QObject):
    """
    Orchestrateur du workflow d'injection DoVi RPU + HDR10+.

    QObject émettant des signaux Qt pour chaque étape. Toutes les opérations
    lourdes s'exécutent dans des threads secondaires via ThreadPoolExecutor.

    Usage :
        wf = MergeDoviWorkflow(config)
        wf.step_started.connect(on_step_started)
        wf.step_finished.connect(on_step_finished)
        wf.workflow_finished.connect(on_done)
        wf.workflow_failed.connect(on_error)
        wf.start(film1, film2, dovi_profile=DoviProfile.P8_1)

    Arrêt :
        wf.cancel()   — demande l'arrêt propre après l'étape en cours
    """

    # --- Signaux ---
    step_started    = Signal(object)          # WorkflowStep
    step_progress   = Signal(object, str)     # WorkflowStep, message
    step_progress_pct = Signal(object, int)   # WorkflowStep, pourcentage 0..100
    step_finished   = Signal(object, object)  # WorkflowStep, StepResult
    workflow_finished = Signal(str)           # chemin du fichier de sortie
    workflow_failed   = Signal(object, str)   # WorkflowStep, message d'erreur
    workflow_cancelled = Signal()             # annulation demandée par l'utilisateur

    def __init__(
        self,
        mediainfo_bin:    str = "mediainfo",
        ffmpeg_bin:       str = "ffmpeg",
        ffprobe_bin:      str = "ffprobe",
        dovi_tool_bin:    str = "dovi_tool",
        hdr10plus_bin:    str = "hdr10plus_tool",
        max_workers:      int = 4,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._bins = {
            "mediainfo":     mediainfo_bin,
            "ffmpeg":        ffmpeg_bin,
            "ffprobe":       ffprobe_bin,
            "dovi_tool":     dovi_tool_bin,
            "hdr10plus_tool": hdr10plus_bin,
        }
        self._max_workers    = max_workers
        self._cancelled      = False
        self._procs: list[subprocess.Popen] = []
        # Tous les processus lancés pendant l'exécution courante (non retirés
        # à la fin) : leur arrêt effectif conditionne le nettoyage sur échec.
        self._run_procs: list[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        self._validation_override: ValidationOverride | None = None

    def set_validation_override(self, callback: ValidationOverride | None) -> None:
        self._validation_override = callback

    def _run_probe(self, command: list[str], **kwargs):
        return run_cancellable_capture(
            command, cancel_cb=lambda: self._cancelled,
            check_cancelled=self._check_cancel,
            on_start=self._track_proc, on_end=self._untrack_proc,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    @staticmethod
    def output_path_for(film1: Path, output_dir: Path, output_basename: str | None = None) -> Path:
        """Chemin du fichier final produit pour ``film1``."""
        basename = output_basename or f"{film1.stem}_DOVI_HDR10PLUS"
        return output_dir / f"{basename}.mkv"

    def start(
        self,
        film1:        Path,
        film2:        Path,
        work_dir:     Path,
        output_dir:   Path,
        dovi_profile: DoviProfile = DoviProfile.P8_1,
        output_basename: str | None = None,
        extra_subtitle_files: tuple[Path, ...] = (),
        metadata_adjustment: MetadataAdjustment = MetadataAdjustment.EXACT,
    ) -> None:
        """Lance le workflow dans un thread secondaire.

        ``metadata_adjustment`` : politique explicite si les métadonnées de
        Film 2 n'ont pas le nombre de trames de Film 1 (``EXACT`` par défaut).
        """
        self._cancelled = False
        output_path = self.output_path_for(film1, output_dir, output_basename)
        # Destination réservée pour toute la durée du job : un second job vers
        # la même sortie échoue ici, avant toute préparation.
        try:
            reservation = OutputReservation.acquire(output_path)
        except OutputBusyError as exc:
            self.workflow_failed.emit(WorkflowStep.VALIDATION, str(exc))
            return
        try:
            # Dossier process neuf, propriété exclusive de cette exécution.
            process_dir = create_process_work_dir(
                work_dir,
                output_path=output_path,
                fallback_name="dovi_job",
            )
            paths = _WorkflowPaths.from_config(
                process_dir.path, output_dir, film1, output_path.stem, owned_dir=process_dir,
            )

            outer = ThreadPoolExecutor(max_workers=1)
            outer.submit(
                self._run, film1, film2, paths, dovi_profile, tuple(extra_subtitle_files), metadata_adjustment,
                reservation=reservation,
            )
            outer.shutdown(wait=False)
        except BaseException:
            reservation.release()
            raise

    def cancel(self) -> None:
        """
        Demande l'annulation du workflow : les sous-processus longs en cours
        sont tués, puis _check_cancel() (ou l'échec de l'étape) arrête le run.
        """
        self._cancelled = True
        with self._procs_lock:
            procs = list(self._procs)
        for proc in procs:
            kill_process_tree(proc, timeout=0.2)

    def _track_proc(self, proc: subprocess.Popen) -> None:
        with self._procs_lock:
            self._procs.append(proc)
            self._run_procs.append(proc)
            cancelled = self._cancelled
        # cancel() peut avoir pris son instantané juste avant l'enregistrement.
        if cancelled:
            kill_process_tree(proc, timeout=0.2)

    def _untrack_proc(self, proc: subprocess.Popen) -> None:
        with self._procs_lock:
            try:
                self._procs.remove(proc)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Orchestration principale (thread secondaire)
    # ------------------------------------------------------------------

    def _run(
        self,
        film1: Path,
        film2: Path,
        paths: _WorkflowPaths,
        profile: DoviProfile,
        extra_subtitle_files: tuple[Path, ...] = (),
        metadata_adjustment: MetadataAdjustment = MetadataAdjustment.EXACT,
        *,
        reservation: OutputReservation | None = None,
    ) -> None:
        try:
            self._run_reserved(
                film1, film2, paths, profile, extra_subtitle_files, metadata_adjustment,
                reservation=reservation,
            )
        finally:
            if reservation is not None:
                reservation.release()

    def _run_reserved(
        self,
        film1: Path,
        film2: Path,
        paths: _WorkflowPaths,
        profile: DoviProfile,
        extra_subtitle_files: tuple[Path, ...],
        metadata_adjustment: MetadataAdjustment,
        *,
        reservation: OutputReservation | None,
    ) -> None:
        with self._procs_lock:
            self._run_procs = []
        outcome: tuple[str, WorkflowStep, str] = ("finished", WorkflowStep.CLEANUP, "")
        try:
            paths.work_dir.mkdir(parents=True, exist_ok=True)
            paths.output_mkv.parent.mkdir(parents=True, exist_ok=True)

            # 1 — Validation (HEVC, transfert PQ, outils, HDR Film 2)
            validation = self._step_validate(
                film1, film2, profile,
            )
            flags = validation.flags
            static_hdr_film1 = validation.static_film1
            static_hdr_film2 = validation.static_film2
            self._check_cancel()

            # 2 — Détection sous-profil DV de Film 2 (P5/P7 FEL/MEL/P8.x)
            routing = self._step_detect_dovi(film2, flags)
            self._check_cancel()

            # 2-bis — Auto-bump du profil cible : si conversion P7/P5 imposée
            # et l'utilisateur a choisi P8_0 (untouched), forcer P8_1 — sinon
            # le RPU réinjecté reste tagué P7, ce qui rend le fichier illisible
            # côté Plex/TV malgré la conversion en amont.
            effective_profile = profile
            if (
                routing is not None
                and routing.conversion_needed
                and profile == DoviProfile.P8_0
            ):
                effective_profile = DoviProfile.P8_1
                self.step_progress.emit(
                    WorkflowStep.DETECT_DOVI,
                    f"Profil cible auto-bumpé P8.0 → P8.1 "
                    f"(source {routing.sub_profile.label} convertie).",
                )

            # 3 — Comptage des trames selon la politique explicite. Film 1
            # SDR : métadonnées non injectables → conversion HDR10 seule ;
            # Film 1 HDR : arrêt (levé par l'étape).
            metadata_requested = flags.has_dovi or flags.has_hdr10plus
            frame_counts = self._step_framecount(
                film1,
                film2,
                adjustment=metadata_adjustment,
                metadata_requested=metadata_requested,
                allow_hdr10_fallback=validation.film1_needs_sdr_to_hdr10,
            )
            if metadata_requested and frame_counts.metadata_verdict(metadata_adjustment) is not None:
                flags = HDRFlags(has_dovi=False, has_hdr10plus=False)
                routing = None
            self._check_cancel()

            # 3-bis — Espace disque (fichiers simultanés par phase et par volume)
            self._step_check_storage(film1, film2, paths, flags, routing, validation)
            self._check_cancel()

            # 4 — Extractions parallèles HEVC (Film 1 et Film 2 si nécessaire)
            self._step_extract_hevc(film1, film2, paths, flags, routing)
            self._check_cancel()

            # 4-bis — Film 1 SDR : conversion HDR10 avant injection DV/HDR10+
            if validation.film1_needs_sdr_to_hdr10:
                self._step_convert_sdr_to_hdr10(paths, static_hdr_film2)
                static_hdr_film1 = static_hdr_film2
                self._discard_consumed(WorkflowStep.SDR_TO_HDR10, paths, paths.film1_hevc)
                self._check_cancel()

            # 5 — Conversion P7/P5 → P8.1 si Film 2 le demande, AVANT extract-rpu
            if routing is not None and routing.conversion_needed:
                self._step_convert_dovi(film2, paths, routing)
                self._check_cancel()

            # 6 — Extraction métadonnées DV/HDR10+ depuis la source appropriée,
            # puis contrôle de leur comptage AVANT toute injection.
            if flags.has_dovi or flags.has_hdr10plus:
                self._step_extract_metadata(film2, paths, flags, routing)
                self._discard_consumed(
                    WorkflowStep.EXTRACT_PARALLEL, paths, paths.film2_hevc, paths.film2_hevc_p8,
                )
                self._check_cancel()
                flags = self._step_check_metadata(
                    frame_counts,
                    paths,
                    flags,
                    metadata_adjustment,
                    allow_hdr10_fallback=validation.film1_needs_sdr_to_hdr10,
                )
                self._check_cancel()

            # 7 — Injection DoVi (le flux d'entrée intermédiaire est ensuite supprimé)
            if flags.has_dovi:
                dovi_input = paths.film1_hevc_input
                self._step_inject_dovi(paths, flags, effective_profile)
                self._discard_consumed(WorkflowStep.INJECT_DOVI, paths, dovi_input)
                self._check_cancel()

            # 8 — Injection HDR10+
            if flags.has_hdr10plus:
                hdr10plus_input = paths.film1_with_dovi if flags.has_dovi else paths.film1_hevc_input
                self._step_inject_hdr10plus(paths, flags)
                self._discard_consumed(WorkflowStep.INJECT_HDR10PLUS, paths, hdr10plus_input)
                self._check_cancel()

            # 9 — Injection SEI HDR10 statiques si Film 1 ne les a pas
            static_input = paths.injection_chain_final(flags, static_hdr_applied=False)
            static_applied = self._step_inject_static_hdr(
                paths, flags, static_hdr_film1, static_hdr_film2,
            )
            if static_applied:
                self._discard_consumed(WorkflowStep.INJECT_STATIC_HDR, paths, static_input)
            self._check_cancel()

            # 10 — Relecture du flux final (trames, RPU et HDR10+ réellement injectés)
            verify_overridden = False
            try:
                self._step_verify(
                    film1, paths, flags, static_hdr_applied=static_applied,
                    film1_frames=frame_counts.fc1,
                )
            except WorkflowError as exc:
                self._check_cancel()
                final = paths.injection_chain_final(flags, static_hdr_applied=static_applied)
                if not final.is_file() or not accept_validation_override(
                    self._validation_override, final, exc.message, lambda: self._cancelled,
                    lambda msg: self.step_progress.emit(WorkflowStep.VERIFY, f"[WARN] {msg}"),
                ):
                    self._check_cancel()
                    raise
                verify_overridden = True
                self.step_finished.emit(
                    WorkflowStep.VERIFY,
                    StepResult(WorkflowStep.VERIFY, True, "Contrôle final ignoré par l'utilisateur", 0),
                )
            self._check_cancel()

            # 11 — Remuxage
            chosen_static = static_hdr_film1 if static_hdr_film1.is_complete else static_hdr_film2
            self._step_remux(
                film1,
                paths,
                flags,
                film2=film2,
                static_hdr_applied=static_applied,
                dovi_profile=effective_profile,
                static_hdr_metadata=chosen_static,
                extra_subtitle_files=extra_subtitle_files,
                preserve_film1_dovi=validation.film1_has_dovi and not flags.has_dovi,
                allow_frame_count_mismatch=verify_overridden,
            )
            self._check_cancel()

            # 12 — Nettoyage
            self._step_cleanup(paths)

        except WorkflowError as exc:
            # Un échec provoqué par le kill des sous-processus à l'annulation
            # reste une annulation.
            outcome = ("cancelled", exc.step, "") if self._cancelled else ("failed", exc.step, exc.message)
        except (_CancelledError, TaskCancelledError):
            outcome = ("cancelled", WorkflowStep.CLEANUP, "")
        except Exception as exc:
            # Filet : sans signal de fin, le panneau resterait « en cours » et
            # sa fermeture attendrait indéfiniment.
            outcome = (
                ("cancelled", WorkflowStep.CLEANUP, "")
                if self._cancelled
                else ("failed", WorkflowStep.VALIDATION, f"Erreur interne inattendue : {exc}")
            )

        kind, step, message = outcome
        if kind != "finished":
            # Nettoyage AVANT le signal terminal : l'UI ne repasse au repos
            # qu'une fois les intermédiaires traités.
            self._discard_failed_run(paths, step)
        # Destination libérée avant le signal terminal : un job enchaîné sur
        # la même sortie peut la réserver aussitôt.
        if reservation is not None:
            reservation.release()
        if kind == "finished":
            self.workflow_finished.emit(str(paths.output_mkv))
        elif kind == "cancelled":
            self.workflow_cancelled.emit()
        else:
            self.workflow_failed.emit(step, message)

    def _discard_consumed(self, step: WorkflowStep, paths: _WorkflowPaths, *candidates: Path) -> None:
        """Supprime les intermédiaires devenus inutiles (libère l'espace des phases suivantes)."""
        try:
            removed = paths.discard(*candidates)
        except OSError as exc:
            self.step_progress.emit(step, f"[WARN] Intermédiaire non supprimé : {exc}")
            return
        for path in removed:
            self.step_progress.emit(step, f"Intermédiaire libéré : {path.name}")

    def _wait_run_processes(self, timeout: float = 10.0) -> bool:
        """Vrai si tous les processus lancés pendant l'exécution sont arrêtés (kill si besoin)."""
        with self._procs_lock:
            procs = list(self._run_procs)
        for proc in procs:
            if proc.poll() is None:
                kill_process_tree(proc, timeout=0.5)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
        return True

    def _discard_failed_run(self, paths: _WorkflowPaths, step: WorkflowStep) -> None:
        """Échec / annulation : supprime le dossier process seulement si c'est sûr.

        Conditions : dossier créé par ``start()`` pour cette exécution
        (jeton du marqueur), et plus aucun processus de l'exécution actif.
        Sinon le dossier est conservé et son chemin signalé.
        """
        owned = paths.owned_dir
        if owned is None:
            return
        if not self._wait_run_processes():
            self.step_progress.emit(
                step,
                f"[WARN] Un outil ne s'est pas arrêté : intermédiaires conservés dans {owned.path}.",
            )
            return
        try:
            removed = owned.remove()
        except OSError as exc:
            self.step_progress.emit(
                step, f"[WARN] Nettoyage incomplet de {owned.path} : {exc}",
            )
            return
        if removed:
            self.step_progress.emit(step, f"Intermédiaires supprimés : {owned.path.name}")
        else:
            self.step_progress.emit(
                step,
                f"[WARN] Dossier de travail conservé (propriété non prouvée) : {owned.path}",
            )

    def _check_cancel(self) -> None:
        if self._cancelled:
            raise _CancelledError()

    # ------------------------------------------------------------------
    # Étape 1 — Validation préliminaire
    # ------------------------------------------------------------------

    def _step_validate(
        self,
        film1: Path,
        film2: Path,
        profile: DoviProfile,
    ) -> ValidationContext:
        step = WorkflowStep.VALIDATION
        t0   = time.monotonic()
        self.step_started.emit(step)

        # Fichiers présents
        for label, path in [("Film 1", film1), ("Film 2", film2)]:
            if not path.is_file():
                raise WorkflowError(step, f"{label} introuvable : {path}")
            self.step_progress.emit(step, f"{label} trouvé : {path.name}")

        # Extensions supportées : conteneurs vidéo (MKV/MP4/MOV/TS/M2TS/…) + HEVC brut.
        # Les conteneurs non-MKV seront extraits en HEVC annexB via ffmpeg
        # (BSF hevc_mp4toannexb appliqué automatiquement) avant passage aux
        # outils dovi_tool / hdr10plus_tool qui ne gèrent que MKV + HEVC brut.
        for label, path in [("Film 1", film1), ("Film 2", film2)]:
            if path.suffix.lower() not in _MERGE_DOVI_ACCEPTED:
                raise WorkflowError(
                    step,
                    f"{label} : format non supporté '{path.suffix}'.",
                )

        # Outils disponibles
        missing = [n for n, cmd in self._bins.items() if not shutil.which(cmd)]
        if missing:
            raise WorkflowError(step, f"Outils manquants : {', '.join(missing)}")
        self.step_progress.emit(step, "Tous les outils sont disponibles.")

        # Flux HEVC présents
        for label, path in [("Film 1", film1), ("Film 2", film2)]:
            codec = self._mediainfo(path, "Video;%Format%").strip().upper()
            if codec != "HEVC":
                raise WorkflowError(step, f"{label} ne contient pas de flux HEVC ({codec})")
            self.step_progress.emit(step, f"{label} — flux HEVC confirmé.")

        static_film1 = self._read_static_hdr_metadata(film1)
        static_film2 = self._read_static_hdr_metadata(film2)

        # Détection HDR dans Film 2 — match strict pour éviter les faux
        # positifs sur "SMPTE ST 2094-10" (DV legacy) qui ne sont PAS HDR10+.
        hdr_raw = self._mediainfo(film2, "Video;%HDR_Format%").strip()
        hdr_lower = hdr_raw.lower()
        transfer2 = self._mediainfo(film2, "Video;%transfer_characteristics%").strip().lower()
        film2_has_hdr10_reference = static_film2.is_complete or self._is_hdr_transfer(transfer2)
        flags = HDRFlags(
            has_dovi      = "dolby vision" in hdr_lower,
            has_hdr10plus = "smpte st 2094 app 4" in hdr_lower,
        )
        if profile == DoviProfile.DISABLED and flags.has_dovi:
            flags.has_dovi = False
            self.step_progress.emit(step, "Dolby Vision désactivé — RPU ignoré.")
        elif not flags.has_dovi and profile != DoviProfile.DISABLED and self._has_dovi_rpu(film2):
            flags.has_dovi = True
            self.step_progress.emit(
                step, "Dolby Vision détecté par dovi_tool (RPU non signalé par mediainfo).",
            )

        # Validation transfert Film 1. Si Film 1 est SDR mais Film 2 porte des
        # métadonnées HDR10/HDR10+, on bascule vers le workflow SDR→HDR10.
        transfer1 = self._mediainfo(film1, "Video;%transfer_characteristics%").strip().lower()
        film1_is_hdr = self._is_hdr_transfer(transfer1)
        film1_needs_sdr_to_hdr10 = False
        if transfer1 and not film1_is_hdr:
            if film2_has_hdr10_reference:
                film1_needs_sdr_to_hdr10 = True
                self.step_progress.emit(
                    step,
                    f"Film 1 SDR détecté ({transfer1}) — conversion HDR10 assistée activée.",
                )
            else:
                raise WorkflowError(
                    step,
                    f"Film 1 n'est pas en transfert HDR (PQ/HLG) : '{transfer1}'. "
                    "Une conversion SDR→HDR10 nécessite une source HDR10/HDR10+.",
                )
        if not flags.has_dovi and not flags.has_hdr10plus and not film1_needs_sdr_to_hdr10:
            raise WorkflowError(step, "Film 2 ne contient ni Dolby Vision ni HDR10+ utilisable.")
        if film1_needs_sdr_to_hdr10 and not static_film2.is_complete:
            raise WorkflowError(
                step,
                "Conversion SDR→HDR10 impossible : Film 2 ne fournit pas "
                "Master Display + MaxCLL/MaxFALL complets.",
            )
        if flags.has_dovi or flags.has_hdr10plus:
            self.step_progress.emit(step, f"HDR avancé détecté dans Film 2 : {flags.label}")
        elif film1_needs_sdr_to_hdr10:
            self.step_progress.emit(step, "Film 2 HDR10 statique utilisé comme référence.")
        if transfer1:
            if film1_is_hdr:
                self.step_progress.emit(step, f"Film 1 — transfert HDR confirmé ({transfer1}).")

        # HDR avancé déjà présent dans Film 1 : dovi_tool inject-rpu et
        # hdr10plus_tool inject remplacent l'existant ; sans injection DV, le
        # RPU de Film 1 reste dans le flux et son signal Matroska est conservé.
        film1_state = HdrState.from_mediainfo(
            self._mediainfo(film1, "Video;%HDR_Format%").strip(), transfer1,
        )
        film1_has_dovi = film1_state.has_dovi
        if (
            not film1_has_dovi
            and not flags.has_dovi
            and not film1_needs_sdr_to_hdr10
            and self._has_dovi_rpu(film1)
        ):
            film1_has_dovi = True
        if film1_has_dovi:
            if flags.has_dovi:
                self.step_progress.emit(
                    step,
                    "Film 1 contient déjà Dolby Vision : son RPU sera remplacé par "
                    "celui de Film 2 (profil « Désactivé » pour le conserver).",
                )
            else:
                self.step_progress.emit(step, "Film 1 — Dolby Vision existant conservé.")
        if film1_state.has_hdr10plus and flags.has_hdr10plus:
            self.step_progress.emit(
                step,
                "Film 1 contient déjà HDR10+ : ses métadonnées seront remplacées "
                "par celles de Film 2.",
            )

        # Lecture des SEI HDR10 statiques (Mastering Display + MaxCLL/MaxFALL)
        # sur les deux films. Si Film 1 n'en a pas mais Film 2 oui, on
        # complétera plus tard via inject_static_hdr_sei_file.
        if static_film1.is_complete:
            self.step_progress.emit(step, "Film 1 — SEI HDR10 statiques présents.")
        elif static_film2.has_any:
            self.step_progress.emit(
                step,
                "Film 1 — SEI HDR10 statiques manquants ; "
                "fallback vers Film 2 lors de l'injection.",
            )
        else:
            self.step_progress.emit(
                step,
                "[WARN] Aucune métadonnée HDR10 statique disponible "
                "dans Film 1 ni Film 2 — la sortie ne sera pas conforme HDR10.",
            )

        duration = time.monotonic() - t0
        self.step_finished.emit(step, StepResult(step, True, flags.label, duration))
        return ValidationContext(
            flags=flags,
            static_film1=static_film1,
            static_film2=static_film2,
            film1_needs_sdr_to_hdr10=film1_needs_sdr_to_hdr10,
            film2_has_hdr10_reference=film2_has_hdr10_reference,
            film1_has_dovi=film1_has_dovi,
        )

    @staticmethod
    def _is_hdr_transfer(value: str) -> bool:
        transfer = str(value or "").lower()
        return bool(
            transfer
            and any(k in transfer for k in ("pq", "2084", "smpte st 2084", "hlg", "arib"))
        )

    # ------------------------------------------------------------------
    # Étape 2 — Détection sous-profil DoVi de Film 2
    # ------------------------------------------------------------------

    def _step_detect_dovi(
        self, film2: Path, flags: HDRFlags,
    ) -> P7RoutingDecision | None:
        """
        Détecte le sous-profil DV (P5/P7 FEL/MEL/P8.x) de Film 2 et décide
        si une conversion P7/P5 → P8.1 est nécessaire avant l'extraction RPU.

        Retourne None si Film 2 n'a pas de DoVi (HDR10+ pur).
        """
        step = WorkflowStep.DETECT_DOVI
        t0   = time.monotonic()
        self.step_started.emit(step)

        if not flags.has_dovi:
            self.step_progress.emit(step, "Film 2 sans Dolby Vision — étape ignorée.")
            duration = time.monotonic() - t0
            self.step_finished.emit(
                step, StepResult(step, True, "Pas de DV à router", duration),
            )
            return None

        detector = DoviProfileDetector(dovi_tool_bin=self._bins["dovi_tool"])
        router = DoviP7Router(detector=detector)
        mi_video = self._load_mediainfo_video(film2)
        decision = router.analyze(
            source=film2, mi_video=mi_video, fallback_to_dovi_tool=True,
        )
        self.step_progress.emit(step, decision.reason)

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(step, True, decision.sub_profile.label, duration),
        )
        return decision

    # ------------------------------------------------------------------
    # Étape 3 — Comparaison frame counts
    # ------------------------------------------------------------------

    def _step_framecount(
        self,
        film1: Path,
        film2: Path,
        *,
        adjustment: MetadataAdjustment = MetadataAdjustment.EXACT,
        metadata_requested: bool = True,
        allow_hdr10_fallback: bool = False,
    ) -> FrameCountResult:
        """Compare les comptages de trames et applique la politique d'injection.

        Métadonnées non injectables (voir :meth:`FrameCountResult.metadata_verdict`) :
        repli HDR10 seul si ``allow_hdr10_fallback`` (Film 1 SDR), arrêt sinon.
        """
        step = WorkflowStep.FRAME_COUNT
        t0   = time.monotonic()
        self.step_started.emit(step)

        # Film 1 : compte exact indispensable (cible du contrôle des
        # métadonnées et de VERIFY). Film 2 HEVC brut : un compte exact
        # imposerait une lecture complète ; ses métadonnées extraites (petits
        # fichiers) sont comptées exactement à CHECK_METADATA.
        fc1 = self._get_framecount(film1)
        fc2_deferred = _is_raw_hevc(film2)
        fc2 = None if fc2_deferred else self._get_framecount(film2)

        diff = abs(fc2 - fc1) if fc1 is not None and fc2 is not None else None
        result = FrameCountResult(fc1, fc2, diff, fc2_deferred=fc2_deferred)

        if fc2_deferred:
            self.step_progress.emit(
                step,
                f"Film 1 : {fc1} frames  |  Film 2 : flux brut, comptage exact sur ses "
                "métadonnées extraites (lecture complète évitée).",
            )
        else:
            self.step_progress.emit(step, f"Film 1 : {fc1} frames  |  Film 2 : {fc2} frames")

        verdict = result.metadata_verdict(adjustment) if metadata_requested else None
        if verdict is not None and allow_hdr10_fallback:
            self.step_progress.emit(
                step,
                f"[WARN] {verdict} Injection DoVi/HDR10+ désactivée, conversion HDR10 seule.",
            )
        elif verdict is not None:
            raise WorkflowError(step, verdict)
        elif metadata_requested and diff:
            self.step_progress.emit(
                step,
                f"[WARN] Film 2 a {diff} trame(s) de plus : l'excédent de métadonnées sera "
                "retiré en fin de flux (option activée ; début supposé aligné).",
            )
        elif metadata_requested and not fc2_deferred:
            self.step_progress.emit(
                step,
                "Comptages identiques. L'alignement temporel des deux films n'est pas "
                "vérifié par ce contrôle.",
            )

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, result.status_text, duration)
        )
        return result

    # ------------------------------------------------------------------
    # Étape 3-bis — Espace disque
    # ------------------------------------------------------------------

    def _video_stream_bytes(self, path: Path) -> tuple[int, str]:
        """Taille estimée du flux vidéo de ``path`` et sa provenance (pour le message)."""
        size = path.stat().st_size
        if _is_raw_hevc(path):
            return size, "flux brut"
        track = self._load_mediainfo_video(path) or {}
        try:
            declared = int(str(track.get("StreamSize") or "0").strip())
        except ValueError:
            declared = 0
        if 0 < declared <= size:
            return declared, "piste déclarée"
        return size, "taille du fichier, borne haute"

    def _film2_needs_extraction(self, film2: Path, routing: P7RoutingDecision | None) -> bool:
        """Film 2 extrait en annexB : conversion DoVi requise, ou conteneur non lu par les outils."""
        film2_is_raw = _is_raw_hevc(film2)
        needs_conversion = bool(routing and routing.conversion_needed)
        return (needs_conversion and not film2_is_raw) or (
            film2.suffix.lower() != ".mkv" and not film2_is_raw
        ) or (
            # Tracks hors du premier SeekHead : illisible par dovi_tool / hdr10plus_tool.
            film2.suffix.lower() == ".mkv" and not strict_demuxer_reads_tracks(film2)
        )

    @staticmethod
    def _tmpfs_note(directory: Path) -> str:
        return (
            " Ce dossier est en RAM (tmpfs) : choisir un dossier de travail sur disque."
            if filesystem_type(directory) == "tmpfs"
            else ""
        )

    def _ensure_free_space(self, step: WorkflowStep, directory: Path, needed: int, what: str) -> None:
        """Contrôle juste-à-temps sur une taille réelle, avant une écriture lourde."""
        need = needed + storage_margin(needed)
        free = shutil.disk_usage(directory).free
        if free < need:
            raise WorkflowError(
                step,
                f"Espace insuffisant pour {what} dans {directory} : ≈{format_bytes(need)} requis, "
                f"{format_bytes(free)} libres." + self._tmpfs_note(directory),
            )

    def _step_check_storage(
        self,
        film1: Path,
        film2: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        routing: P7RoutingDecision | None,
        validation: ValidationContext,
    ) -> None:
        """Besoin d'espace par volume, calculé sur les fichiers présents simultanément."""
        step = WorkflowStep.STORAGE_CHECK
        t0 = time.monotonic()
        self.step_started.emit(step)

        film1_video, film1_basis = self._video_stream_bytes(film1)
        film2_video, film2_basis = self._video_stream_bytes(film2)
        static_copy = not validation.static_film1.is_complete and (
            validation.static_film2.has_any or validation.static_film1.has_any
        )
        phases = merge_storage_phases(MergeStorageInputs(
            film1_bytes=film1.stat().st_size,
            film1_video_bytes=film1_video,
            film1_raw=_is_raw_hevc(film1),
            film1_matroska=film1.suffix.lower() == ".mkv",
            film2_video_bytes=film2_video,
            film2_extracted=bool(flags.has_dovi or flags.has_hdr10plus) and self._film2_needs_extraction(film2, routing),
            film2_converted=bool(routing and routing.conversion_needed),
            sdr_to_hdr10=validation.film1_needs_sdr_to_hdr10,
            inject_dovi=flags.has_dovi,
            inject_hdr10plus=flags.has_hdr10plus,
            static_hdr_copy=static_copy,
        ))
        work_dir = paths.work_dir
        output_dir = paths.output_mkv.parent
        try:
            same_volume = os.stat(work_dir).st_dev == os.stat(output_dir).st_dev
        except OSError:
            same_volume = True
        need = storage_requirement(phases, same_volume=same_volume)

        self.step_progress.emit(
            step,
            f"Flux vidéo estimés : Film 1 ≈ {format_bytes(film1_video)} ({film1_basis}), "
            f"Film 2 ≈ {format_bytes(film2_video)} ({film2_basis})."
            + (" Flux HDR10 réencodé supposé de taille comparable (non garanti)."
               if validation.film1_needs_sdr_to_hdr10 else ""),
        )
        checks = [(work_dir, need.work_bytes, "dossier de travail" + (" et sortie" if same_volume else ""))]
        if not same_volume and need.output_bytes:
            checks.append((output_dir, need.output_bytes, "sortie"))
        for directory, required, label in checks:
            free = shutil.disk_usage(directory).free
            self.step_progress.emit(
                step,
                f"{label.capitalize()} ({directory}) : pic ≈ {format_bytes(required)} "
                f"(phase « {need.peak_phase if directory == work_dir else 'Assemblage final'} », marge incluse), "
                f"{format_bytes(free)} libres.",
            )
            if free < required:
                raise WorkflowError(
                    step,
                    f"Espace insuffisant ({label}, {directory}) : ≈{format_bytes(required)} requis "
                    f"au pic, {format_bytes(free)} libres. Libérez de l'espace ou choisissez un "
                    "autre dossier." + self._tmpfs_note(directory),
                )

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, "Espace disque suffisant (estimation)", duration),
        )

    # ------------------------------------------------------------------
    # Étape 3 — Extractions parallèles
    # ------------------------------------------------------------------

    def _step_extract_hevc(
        self,
        film1: Path,
        film2: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        routing: P7RoutingDecision | None,
    ) -> None:
        """Extrait les flux HEVC annexB requis avant la conversion / extraction
        des métadonnées. Film 1 est extrait s'il n'est pas déjà raw HEVC.
        Film 2 est extrait s'il faut le convertir P7/P5 → P8.1 (dovi_tool
        convert n'accepte que du HEVC annexB) ou si son conteneur n'est pas
        géré nativement par dovi_tool/hdr10plus_tool (MP4/MOV/TS/…)."""
        step = WorkflowStep.EXTRACT_PARALLEL
        t0   = time.monotonic()
        self.step_started.emit(step)

        film1_needs_extract = _needs_hevc_extraction(film1)
        # Film 2 doit être extrait en annexB si :
        #   - on doit le convertir (dovi_tool convert exige annexB) ; ou
        #   - son conteneur n'est ni MKV ni raw HEVC (extract-rpu/hdr10plus
        #     n'acceptent pas MP4/MOV/TS).
        film2_needs_extract = bool(flags.has_dovi or flags.has_hdr10plus) and self._film2_needs_extraction(film2, routing)

        needed = 0
        if film1_needs_extract:
            needed += self._video_stream_bytes(film1)[0]
        if film2_needs_extract:
            needed += self._video_stream_bytes(film2)[0]
        if needed:
            self._ensure_free_space(step, paths.work_dir, needed, "l'extraction HEVC")

        errors: list[str] = []

        def _emit(msg: str) -> None:
            self.step_progress.emit(step, msg)

        tasks: dict[str, Callable] = {}
        if film1_needs_extract:
            tasks["HEVC Film 1"] = lambda: self._extract_hevc(film1, paths.film1_hevc, _emit)
        if film2_needs_extract:
            tasks["HEVC Film 2"] = lambda: self._extract_hevc(film2, paths.film2_hevc, _emit)

        if tasks:
            _emit(f"Extraction HEVC parallèle ({', '.join(tasks)})…")
            self._run_pool(tasks, errors)
            if errors:
                raise WorkflowError(step, "Extraction HEVC échouée :\n" + "\n".join(errors))
        else:
            _emit("Aucune extraction HEVC requise.")

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(step, True, "Extraction HEVC terminée", duration),
        )

    def _film2_metadata_source(
        self,
        film2: Path,
        paths: _WorkflowPaths,
        routing: P7RoutingDecision | None,
    ) -> Path:
        """Source utilisée pour `extract-rpu` et `hdr10plus_tool extract`.

        Priorité :
          1. HEVC P8.1 converti (si routing.conversion_needed) ;
          2. HEVC annexB extrait (si Film 2 n'est ni MKV ni raw HEVC) ;
          3. Film 2 d'origine (MKV ou raw HEVC).
        """
        if routing is not None and routing.conversion_needed and paths.film2_hevc_p8.exists():
            return paths.film2_hevc_p8
        if paths.film2_hevc.exists():
            return paths.film2_hevc
        return film2

    def _step_extract_metadata(
        self,
        film2: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        routing: P7RoutingDecision | None,
    ) -> None:
        """Extrait RPU DoVi et JSON HDR10+ depuis la bonne source (P8.1
        converti si nécessaire). Lancé après l'éventuelle conversion P7/P5."""
        step = WorkflowStep.EXTRACT_PARALLEL
        t0   = time.monotonic()

        source2 = self._film2_metadata_source(film2, paths, routing)
        errors: list[str] = []

        def _emit(msg: str) -> None:
            self.step_progress.emit(step, msg)

        tasks: dict[str, Callable] = {}
        if flags.has_dovi:
            tasks["RPU DoVi"] = lambda: self._extract_rpu(source2, paths.film2_rpu, _emit)
        if flags.has_hdr10plus:
            tasks["HDR10+"] = lambda: self._extract_hdr10plus(source2, paths.film2_hdr10plus, _emit)

        if not tasks:
            return

        _emit(f"Extraction métadonnées depuis {source2.name} ({', '.join(tasks)})…")
        self._run_pool(tasks, errors)
        if errors:
            raise WorkflowError(step, "Extraction métadonnées échouée :\n" + "\n".join(errors))

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(step, True, "Métadonnées HDR extraites", duration),
        )

    # ------------------------------------------------------------------
    # Étape 5 — Conversion P7/P5 → P8.1 (avant extract-rpu)
    # ------------------------------------------------------------------

    def _step_convert_dovi(
        self,
        film2: Path,
        paths: _WorkflowPaths,
        routing: P7RoutingDecision,
    ) -> None:
        """Convertit Film 2 P7 (FEL/MEL) ou P5 vers P8.1 mono-layer via
        ``dovi_tool convert``. Le HEVC P8.1 résultant servira de source pour
        extract-rpu et hdr10plus_tool extract dans l'étape suivante."""
        step = WorkflowStep.CONVERT_DOVI
        t0   = time.monotonic()
        self.step_started.emit(step)

        # Source pour la conversion : HEVC annexB obligatoire (dovi_tool
        # convert ne lit pas MKV/MP4). Film 2 brut HEVC ou film2_hevc extrait.
        source = paths.film2_hevc if paths.film2_hevc.exists() else film2
        if not _is_raw_hevc(source):
            raise WorkflowError(
                step,
                f"Conversion DV impossible : source non-HEVC annexB ({source.name}). "
                "Vérifiez l'étape d'extraction HEVC.",
            )

        self.step_progress.emit(
            step,
            f"Conversion {routing.sub_profile.label} → P8.1 "
            f"(dovi_tool -m {routing.convert_mode} convert)…",
        )

        self._ensure_free_space(step, paths.work_dir, source.stat().st_size, "la conversion Dolby Vision")

        cmd = [
            self._bins["dovi_tool"],
            "-m", routing.convert_mode or "2",
            "convert",
        ]
        if routing.sub_profile in {DoviSubProfile.P7_FEL, DoviSubProfile.P7_MEL}:
            cmd.append("--discard")
        cmd.extend(["-i", str(source), "-o", str(paths.film2_hevc_p8)])
        self._run_cmd(cmd, step)

        # L'extrait annexB intermédiaire a fini son rôle (consommé par convert).
        if paths.film2_hevc.exists():
            paths.film2_hevc.unlink(missing_ok=True)

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(
                step, True,
                f"Conversion {routing.sub_profile.label} → P8.1 réussie",
                duration,
            ),
        )

    # ------------------------------------------------------------------
    # Étape 6-bis — Comptage des métadonnées extraites (avant injection)
    # ------------------------------------------------------------------

    def _frame_count_guard(self) -> FrameCountGuard:
        return FrameCountGuard(
            mediainfo_bin=self._bins["mediainfo"],
            ffprobe_bin=self._bins["ffprobe"],
            dovi_tool_bin=self._bins["dovi_tool"],
            run_command=self._run_probe,
        )

    def _step_check_metadata(
        self,
        frame_counts: FrameCountResult,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        adjustment: MetadataAdjustment,
        *,
        allow_hdr10_fallback: bool = False,
    ) -> HDRFlags:
        """Compare le nombre de trames RPU / HDR10+ extraits à Film 1, avant toute injection.

        Applique la politique explicite (``EXACT`` ou ``TRIM_TAIL``, jamais de
        trame fabriquée). Refus : Film 1 SDR → HDR10 seul (flags vidés),
        sinon arrêt. Retourne les flags d'injection effectifs.
        """
        step = WorkflowStep.CHECK_METADATA
        t0 = time.monotonic()
        self.step_started.emit(step)

        target = frame_counts.fc1
        rpu = paths.film2_rpu if flags.has_dovi else None
        hdr = paths.film2_hdr10plus if flags.has_hdr10plus else None
        guard = self._frame_count_guard()
        try:
            if target is None:
                raise FrameCountAuditError("Nombre de trames de Film 1 illisible : contrôle impossible.")
            for path in (rpu, hdr):
                if path is not None and not path.is_file():
                    raise FrameCountAuditError(f"Métadonnées extraites introuvables : {path.name}.")
            audit = FrameCountAudit(
                source=target,
                encoded=target,
                rpu=guard.rpu_frame_count(rpu) if rpu is not None else None,
                hdr10p=guard.hdr10p_frame_count(hdr) if hdr is not None else None,
            )
            self.step_progress.emit(
                step,
                f"Film 1 : {target} trames"
                + (f"  |  RPU : {audit.rpu}" if rpu is not None else "")
                + (f"  |  HDR10+ : {audit.hdr10p}" if hdr is not None else ""),
            )
            guard.enforce(
                audit,
                adjustment=adjustment,
                rpu_bin=rpu,
                hdr10p_json=hdr,
                on_warn=lambda msg: self.step_progress.emit(step, f"[WARN] {msg}"),
                on_info=lambda msg: self.step_progress.emit(step, msg),
            )
        except FrameCountAuditError as exc:
            if not allow_hdr10_fallback:
                raise WorkflowError(step, f"Métadonnées non injectables : {exc}") from exc
            self.step_progress.emit(
                step,
                f"[WARN] Métadonnées non injectables ({exc}) : conversion HDR10 seule.",
            )
            flags = HDRFlags(has_dovi=False, has_hdr10plus=False)

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(
                step, True,
                "Comptage des métadonnées conforme" if (flags.has_dovi or flags.has_hdr10plus)
                else "Métadonnées dynamiques écartées (HDR10 seul)",
                duration,
            ),
        )
        return flags

    # ------------------------------------------------------------------
    # Étape 4-bis — Conversion Film 1 SDR → HDR10
    # ------------------------------------------------------------------

    def _step_convert_sdr_to_hdr10(
        self,
        paths: _WorkflowPaths,
        static_hdr: StaticHdrMetadata,
    ) -> None:
        """Transcode Film 1 SDR en HEVC HDR10 10-bit/PQ avant injection.

        Ce chemin ne prétend pas reconstruire l'information HDR absente ; il
        produit une base layer HDR10 cohérente, guidée par les métadonnées
        statiques de Film 2, afin que l'injection HDR10+/DoVi ne repose pas sur
        un BL SDR invalide.
        """
        step = WorkflowStep.SDR_TO_HDR10
        t0 = time.monotonic()
        self.step_started.emit(step)

        hevc_input = paths.film1_hevc_input
        if not hevc_input.exists():
            raise WorkflowError(
                step,
                f"Fichier HEVC source introuvable pour conversion SDR→HDR10 : {hevc_input.name}",
            )
        if not static_hdr.is_complete:
            raise WorkflowError(step, "Métadonnées HDR10 de référence incomplètes.")
        # Taille du flux réencodé inconnue : supposée comparable au flux source.
        self._ensure_free_space(step, paths.work_dir, hevc_input.stat().st_size, "la conversion SDR → HDR10")

        x265_params = (
            f"repeat-headers=1:"
            f"colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
            f"master-display={static_hdr.master_display}:"
            f"max-cll={static_hdr.max_cll}"
        )
        vf = (
            "zscale=transfer=linear:npl=100,"
            "format=gbrpf32le,"
            "zscale=primaries=bt2020,"
            "tonemap=tonemap=mobius:desat=0,"
            "zscale=transfer=smpte2084:matrix=bt2020nc:range=tv,"
            "format=yuv420p10le"
        )

        self.step_progress.emit(
            step,
            "Conversion SDR→HDR10 : HEVC Main10 BT.2020/PQ avec métadonnées Film 2…",
        )
        self._run_cmd([
            self._bins["ffmpeg"],
            "-hide_banner",
            "-y",
            "-f", "hevc",
            "-i", str(hevc_input),
            "-map", "0:v:0",
            "-vf", vf,
            "-c:v", "libx265",
            "-preset", "slow",
            "-crf", "18",
            "-pix_fmt", "yuv420p10le",
            "-color_primaries", "bt2020",
            "-color_trc", "smpte2084",
            "-colorspace", "bt2020nc",
            "-color_range", "tv",
            "-x265-params", x265_params,
            "-an",
            "-sn",
            "-dn",
            "-f", "hevc",
            str(paths.film1_hdr10_hevc),
        ], step)

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(step, True, "Film 1 SDR converti en base HDR10", duration),
        )

    # ------------------------------------------------------------------
    # Étape 4 — Injection DoVi RPU
    # ------------------------------------------------------------------

    def _step_inject_dovi(
        self,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        profile: DoviProfile,
    ) -> None:
        step = WorkflowStep.INJECT_DOVI
        t0   = time.monotonic()
        self.step_started.emit(step)
        self.step_progress.emit(step, f"Injection RPU DoVi (dovi_tool -m {profile.value})…")

        # film1_hevc_input résout automatiquement MKV extrait vs HEVC direct
        hevc_input = paths.film1_hevc_input
        if not hevc_input.exists():
            raise WorkflowError(
                step,
                f"Fichier HEVC source introuvable : {hevc_input.name} — "
                "vérifiez que l'étape d'extraction s'est bien déroulée.",
            )
        self._ensure_free_space(step, paths.work_dir, hevc_input.stat().st_size, "l'injection RPU")

        # -m est un flag GLOBAL de dovi_tool placé avant la sous-commande.
        # La valeur de DoviProfile.value est directement le flag -m à passer.
        # P8_1 → -m 2 (normalise en Profile 8.1, supprime mapping FEL)
        # P8_0 → -m 0 (rewrite untouched, préserve le profil source)
        self._run_cmd([
            self._bins["dovi_tool"],
            "-m", profile.value,
            "inject-rpu",
            "-i", str(hevc_input),
            "-r", str(paths.film2_rpu),
            "-o", str(paths.film1_with_dovi),
        ], step)

        self.step_progress.emit(step, f"RPU injecté → {paths.film1_with_dovi.name}")
        duration = time.monotonic() - t0
        mode_label = "Profile 8.1 (standard remux)" if profile == DoviProfile.P8_1 else "mode 0 (untouched)"
        self.step_finished.emit(
            step, StepResult(step, True, f"DoVi RPU injecté — {mode_label}", duration)
        )

    # ------------------------------------------------------------------
    # Étape 5 — Injection HDR10+
    # ------------------------------------------------------------------

    def _step_inject_hdr10plus(
        self,
        paths: _WorkflowPaths,
        flags: HDRFlags,
    ) -> None:
        step = WorkflowStep.INJECT_HDR10PLUS
        t0   = time.monotonic()
        self.step_started.emit(step)
        self.step_progress.emit(step, "Injection HDR10+…")

        # Si DoVi a déjà été injecté → partir de film1_with_dovi.
        # Sinon (HDR10+ seul) → film1_hevc_input résout MKV extrait vs HEVC direct.
        hevc_input = paths.film1_with_dovi if flags.has_dovi else paths.film1_hevc_input
        if hevc_input.exists():
            self._ensure_free_space(step, paths.work_dir, hevc_input.stat().st_size, "l'injection HDR10+")

        self._run_cmd([
            self._bins["hdr10plus_tool"],
            "inject",
            "-i", str(hevc_input),
            "-j", str(paths.film2_hdr10plus),
            "-o", str(paths.film1_final),
        ], step)

        self.step_progress.emit(step, f"HDR10+ injecté → {paths.film1_final.name}")
        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, "HDR10+ injecté", duration)
        )

    # ------------------------------------------------------------------
    # Étape 6 — Injection SEI HDR10 statiques (master_display + max_cll)
    # ------------------------------------------------------------------

    def _step_inject_static_hdr(
        self,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        static_film1: StaticHdrMetadata,
        static_film2: StaticHdrMetadata,
    ) -> bool:
        """Injecte les SEI HDR10 statiques (Mastering Display 137 + CLL 144)
        dans le flux final si Film 1 ne les a pas. Priorité d'origine :
        Film 1 (déjà présents → no-op géré par inject_static_hdr_sei_file)
        sinon Film 2 (fallback). Retourne True si l'injection a produit un
        nouveau fichier ``film1_with_static_hdr.hevc`` à muxer."""
        step = WorkflowStep.INJECT_STATIC_HDR
        t0   = time.monotonic()
        self.step_started.emit(step)

        # Si Film 1 a déjà ses SEI statiques complets, on ne touche à rien
        # (dovi_tool inject-rpu et hdr10plus_tool inject les préservent).
        if static_film1.is_complete:
            self.step_progress.emit(
                step, "Film 1 a déjà ses SEI HDR10 statiques — aucune injection.",
            )
            duration = time.monotonic() - t0
            self.step_finished.emit(
                step, StepResult(step, True, "SEI HDR10 préservés", duration),
            )
            return False

        # Source des valeurs : Film 2 si dispo, sinon Film 1 partiel.
        chosen = static_film2 if static_film2.has_any else static_film1
        if not chosen.has_any:
            self.step_progress.emit(
                step,
                "[WARN] Aucune métadonnée HDR10 statique disponible — étape ignorée.",
            )
            duration = time.monotonic() - t0
            self.step_finished.emit(
                step, StepResult(step, True, "Pas de SEI à injecter", duration),
            )
            return False

        # Source HEVC à patcher = sortie de la chaîne d'injection RPU/HDR10+.
        source_hevc = paths.injection_chain_final(flags, static_hdr_applied=False)
        if not source_hevc.exists():
            raise WorkflowError(
                step,
                f"Source HEVC introuvable pour patch SEI statiques : {source_hevc.name}",
            )

        self._ensure_free_space(step, paths.work_dir, source_hevc.stat().st_size, "l'injection SEI HDR10")
        self.step_progress.emit(
            step,
            f"Injection SEI HDR10 statiques (master_display="
            f"{'oui' if chosen.master_display else 'non'}, "
            f"max_cll={'oui' if chosen.max_cll else 'non'}, "
            f"source : Film {'2' if chosen is static_film2 else '1'})…",
        )

        try:
            result = inject_static_hdr_sei_file(
                source_hevc,
                paths.film1_with_static_hdr,
                master_display=chosen.master_display,
                max_cll=chosen.max_cll,
            )
        except ValueError as exc:
            # Format invalide → on log et on continue sans injection.
            self.step_progress.emit(
                step, f"[WARN] Injection SEI ignorée ({exc}).",
            )
            duration = time.monotonic() - t0
            self.step_finished.emit(
                step, StepResult(step, True, "SEI invalides — ignoré", duration),
            )
            return False

        if result.applied:
            self.step_progress.emit(
                step,
                f"SEI HDR10 statiques injectés sur "
                f"{result.injected_access_units} access unit(s).",
            )
        else:
            # inject_static_hdr_sei_file a fait shutil.copyfile (pas d'AU
            # ciblé). On nettoie le doublon et on retourne False.
            paths.film1_with_static_hdr.unlink(missing_ok=True)
            self.step_progress.emit(
                step, "SEI HDR10 déjà présents dans le flux — aucune modification.",
            )

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(
                step, True,
                f"SEI HDR10 statiques : {'injectés' if result.applied else 'préservés'}",
                duration,
            ),
        )
        return result.applied

    # ------------------------------------------------------------------
    # Étape 7 — Vérification alignement frame count (FrameCountGuard)
    # ------------------------------------------------------------------

    def _step_verify(
        self,
        film1: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        *,
        static_hdr_applied: bool = False,
        film1_frames: int | None = None,
    ) -> None:
        """Relit le flux HEVC final : trames vidéo, RPU et HDR10+ réellement injectés.

        ``film1_frames`` : compte de Film 1 établi à FRAME_COUNT (évite de
        relire un Film 1 HEVC brut en entier).

        Égalité stricte avec Film 1, sans aucun ajustement (les éventuels
        ajustements explicites ont lieu avant l'injection). Contrôle de
        comptage : l'alignement temporel n'est pas prouvé.
        """
        step = WorkflowStep.VERIFY
        t0   = time.monotonic()
        self.step_started.emit(step)

        final = paths.injection_chain_final(flags, static_hdr_applied=static_hdr_applied)
        if not final.exists():
            raise WorkflowError(step, f"Flux injecté introuvable : {final.name}")

        final_frames = self._get_framecount(final)
        if final_frames is None or final_frames <= 0:
            raise WorkflowError(step, "Nombre exact de trames du flux final illisible : vérification impossible.")
        guard = self._frame_count_guard()
        audit = guard.audit(
            source=film1, encoded=final, known_source_frames=film1_frames,
            known_encoded_frames=final_frames,
        )
        if audit.source is None or audit.encoded is None:
            raise WorkflowError(
                step,
                "Nombre de trames illisible (Film 1 ou flux final) : vérification impossible.",
            )
        if audit.encoded != audit.source:
            raise WorkflowError(
                step,
                f"Flux final : {audit.encoded} trames contre {audit.source} pour Film 1 "
                "(frame count divergent après injection).",
            )

        def _emit(msg: str) -> None:
            self.step_progress.emit(step, msg)

        tasks: dict[str, Callable] = {}
        if flags.has_dovi:
            tasks["RPU Dolby Vision"] = lambda: self._run_raw([
                self._bins["dovi_tool"], "extract-rpu",
                "-i", str(final), "-o", str(paths.verify_rpu),
            ], step=step)
        if flags.has_hdr10plus:
            tasks["HDR10+"] = lambda: self._run_raw([
                self._bins["hdr10plus_tool"], "extract",
                str(final), "-o", str(paths.verify_hdr10plus),
            ], step=step)

        injected: dict[str, int] = {}
        try:
            if tasks:
                _emit(f"Relecture des métadonnées injectées ({', '.join(tasks)}) dans {final.name}…")
                errors: list[str] = []
                self._run_pool(tasks, errors)
                if errors:
                    raise WorkflowError(
                        step, "Relecture des métadonnées du flux final échouée :\n" + "\n".join(errors),
                    )
            readers = (
                ("RPU Dolby Vision", flags.has_dovi, paths.verify_rpu, guard.rpu_frame_count),
                ("HDR10+", flags.has_hdr10plus, paths.verify_hdr10plus, guard.hdr10p_frame_count),
            )
            for label, active, path, count_frames in readers:
                if not active:
                    continue
                count = count_frames(path) if path.is_file() else None
                if count is None:
                    raise WorkflowError(step, f"{label} du flux final illisible.")
                if count != audit.source:
                    raise WorkflowError(
                        step,
                        f"{label} du flux final : {count} trames contre {audit.source} pour Film 1.",
                    )
                injected[label] = count
        finally:
            paths.verify_rpu.unlink(missing_ok=True)
            paths.verify_hdr10plus.unlink(missing_ok=True)

        detail = (
            f"Film 1 : {audit.source}  |  Flux final : {audit.encoded}"
            + "".join(f"  |  {label} injecté : {count}" for label, count in injected.items())
            + "  —  comptages identiques (alignement temporel non vérifié)"
        )
        self.step_progress.emit(step, detail)

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, detail, duration)
        )

    # ------------------------------------------------------------------
    # Étape 7 — Remuxage final
    # ------------------------------------------------------------------

    def _read_video_track_props(self, film: Path) -> dict[str, Any]:
        """Extrait la résolution, le format d'affichage et la langue de la piste vidéo."""
        if film.is_file() and film.suffix.lower() == ".mkv":
            try:
                reader = MatroskaReader(film)
                video_tracks = [t for t in reader.tracks() if t.track_type == TRACK_TYPE_VIDEO]
                if video_tracks:
                    vt = video_tracks[0]
                    v_dict = vt.video or {}
                    return {
                        "pixel_width": v_dict.get("pixel_width", 3840),
                        "pixel_height": v_dict.get("pixel_height", 2160),
                        "display_width": v_dict.get("display_width"),
                        "display_height": v_dict.get("display_height"),
                        "language": vt.language_bcp47 or vt.language or "und",
                        "name": vt.name or "",
                    }
            except (OSError, ValueError) as exc:
                self.step_progress.emit(
                    WorkflowStep.REMUX,
                    f"Lecture des propriétés vidéo Matroska impossible : {exc}. Repli MediaInfo.",
                )

        # Fallback mediainfo
        width_str = self._mediainfo(film, "Video;%Width%").strip()
        height_str = self._mediainfo(film, "Video;%Height%").strip()
        lang_str = self._mediainfo(film, "Video;%Language%").strip()
        title_str = self._mediainfo(film, "Video;%Title%").strip()
        try:
            width = int(width_str) if width_str else 3840
        except ValueError:
            width = 3840
        try:
            height = int(height_str) if height_str else 2160
        except ValueError:
            height = 2160
        return {
            "pixel_width": width,
            "pixel_height": height,
            "display_width": None,
            "display_height": None,
            "language": lang_str or "und",
            "name": title_str or "",
        }

    def _mux_native_video(
        self,
        final_hevc: Path,
        film1: Path,
        paths: _WorkflowPaths,
        video_props: dict[str, Any],
        dovi_record: DolbyVisionConfigRecord | None,
        colour_element: bytes,
        *,
        allow_frame_count_mismatch: bool = False,
    ) -> None:
        """``allow_frame_count_mismatch`` : contrôle final ignoré par l'utilisateur."""
        muxer = MatroskaNativeMuxer(
            ffprobe_bin=self._bins["ffprobe"],
        )
        result = muxer.mux(
            hevc_input=final_hevc,
            source_for_timestamps=film1,
            output=paths.film1_wrapped_video,
            pixel_width=video_props["pixel_width"],
            pixel_height=video_props["pixel_height"],
            display_width=video_props.get("display_width"),
            display_height=video_props.get("display_height"),
            dovi_record=dovi_record,
            language=video_props.get("language") or "und",
            colour_element=colour_element,
            timestamp_order="packet",
            allow_frame_count_mismatch=allow_frame_count_mismatch,
        )
        written, available = result.frames_written, result.source_timestamps
        if result.dropped_frames:
            self.step_progress.emit(
                WorkflowStep.REMUX,
                f"[WARN] {result.dropped_frames} image(s) sans horodatage Film 1 écartée(s) "
                f"en fin de flux ({written} conservées).",
            )
        elif written != available:
            self.step_progress.emit(
                WorkflowStep.REMUX,
                f"[WARN] {written} images pour {available} horodatages Film 1 : association par "
                "position, durée ramenée aux images écrites (désynchronisation si des images "
                "manquent ailleurs qu'en fin de flux).",
            )

    def _assemble_final_mkv(
        self,
        plan: MatroskaAssemblyPlan,
        dovi_record: DolbyVisionConfigRecord | None,
    ) -> None:
        contract = assembly_output_contract(
            plan,
            require_block_addition_mapping=dovi_record is not None,
        )
        plan_with_contract = replace(plan, expected_output_contract=contract)
        mux_plan = compile_assembly_plan(plan_with_contract)

        def validate(path, packet_validation):
            validate_final_output(
                path, validate_matroska_output(path, contract, packet_validation=packet_validation),
                lambda: self._run_raw([
                    self._bins["ffprobe"], "-v", "error", "-show_entries",
                    "format=format_name", "-of", "json", str(path),
                ], step=WorkflowStep.REMUX),
                message_prefix="Validation de la sortie Merge DoVi échouée : ",
                override=self._validation_override, cancelled=lambda: self._cancelled,
                warn=lambda msg: self.step_progress.emit(WorkflowStep.REMUX, f"[WARN] {msg}"),
            )

        MatroskaWriter().write(
            mux_plan,
            cancel_cb=lambda: self._cancelled,
            external_validator=validate,
            validation_error_handler=lambda path, message: accept_validation_override(
                self._validation_override, path, message, lambda: self._cancelled,
                lambda msg: self.step_progress.emit(WorkflowStep.REMUX, f"[WARN] {msg}"),
            ),
        )

    def _step_remux(
        self,
        film1: Path,
        paths: _WorkflowPaths,
        flags: HDRFlags,
        *,
        film2: Path | None = None,
        static_hdr_applied: bool = False,
        dovi_profile: DoviProfile = DoviProfile.P8_1,
        static_hdr_metadata: StaticHdrMetadata | None = None,
        extra_subtitle_files: tuple[Path, ...] = (),
        preserve_film1_dovi: bool = False,
        allow_frame_count_mismatch: bool = False,
    ) -> None:
        step = WorkflowStep.REMUX
        t0   = time.monotonic()
        self.step_started.emit(step)

        final_hevc = paths.injection_chain_final(flags, static_hdr_applied=static_hdr_applied)
        if not final_hevc.exists():
            raise WorkflowError(step, f"Flux injecté introuvable : {final_hevc.name}")

        dovi_record = None
        if flags.has_dovi and paths.film2_rpu.exists():
            dovi_record = self._build_dovi_record_from_rpu(
                paths.film2_rpu,
                forced_compat_id=1 if dovi_profile == DoviProfile.P8_1 else None,
            )
        elif preserve_film1_dovi:
            dovi_record = self._film1_dovi_record(film1, final_hevc)
            if dovi_record is None:
                self.step_progress.emit(
                    step,
                    "[WARN] Configuration Dolby Vision de Film 1 illisible : "
                    "piste non signalée Dolby Vision au niveau Matroska.",
                )
            else:
                self.step_progress.emit(
                    step,
                    f"Signal Dolby Vision de Film 1 conservé (profil "
                    f"{dovi_record.profile}.{dovi_record.bl_signal_compat_id}, "
                    f"niveau {dovi_record.level}).",
                )

        colour_element = b""
        if static_hdr_metadata and (static_hdr_metadata.master_display or static_hdr_metadata.max_cll):
            colour_element = _build_colour_element_from_static_hdr(static_hdr_metadata)

        # 1. Propriétés vidéo de film1 (résolution, format d'affichage, langue)
        video_props = self._read_video_track_props(film1)

        # 2. Encapsulation native de la vidéo injectée
        self._ensure_free_space(step, paths.work_dir, final_hevc.stat().st_size, "l'encapsulation vidéo")
        self.step_progress.emit(
            step,
            f"Encapsulation vidéo native HEVC → {paths.film1_wrapped_video.name}…",
        )
        try:
            self._mux_native_video(
                final_hevc,
                film1,
                paths,
                video_props,
                dovi_record,
                colour_element,
                allow_frame_count_mismatch=allow_frame_count_mismatch,
            )
        except Exception as exc:
            raise WorkflowError(step, f"Encapsulation vidéo native échouée : {exc}") from exc
        # Le flux HEVC final vit désormais dans l'encapsulation MKV.
        self._discard_consumed(step, paths, final_hevc)

        # 3. Préparation du conteneur pour pistes audio / sous-titres
        if film1.is_file() and film1.suffix.lower() == ".mkv":
            source_mkv = film1
        else:
            canonical_mkv = paths.work_dir / "film1_canonical.mkv"
            self._ensure_free_space(step, paths.work_dir, film1.stat().st_size, "la canonicalisation MKV")
            self.step_progress.emit(
                step,
                f"Canonicalisation conteneur source ({film1.suffix}) → {canonical_mkv.name}…",
            )
            subs_codec_args = self._subtitle_codec_args_for(film1)
            self._run_cmd([
                self._bins["ffmpeg"],
                "-hide_banner",
                "-y",
                "-nostdin",
                "-i", str(film1),
                "-map", "0",
                "-c", "copy",
                *subs_codec_args,
                str(canonical_mkv),
            ], step)
            source_mkv = canonical_mkv

        source_reader = MatroskaReader(source_mkv)

        # 4. Pistes d'assemblage
        assembly_tracks: list[MatroskaAssemblyTrack] = [
            MatroskaAssemblyTrack(
                artifact=paths.film1_wrapped_video,
                artifact_track_index=0,
                source_identity=deterministic_source_identity(paths.film1_wrapped_video),
                language_value=video_props.get("language") or "und",
                name=video_props.get("name") or "",
                flags=MatroskaTrackFlags(enabled=True, default=True),
            )
        ]

        # Audio et sous-titres depuis source_mkv
        for track_idx, track in enumerate(source_reader.tracks()):
            if track.track_type == TRACK_TYPE_VIDEO:
                continue
            assembly_tracks.append(
                MatroskaAssemblyTrack(
                    artifact=source_mkv,
                    artifact_track_index=track_idx,
                    source_identity=deterministic_source_identity(source_mkv),
                    language_value=None,  # préserve le TrackEntry source tel quel
                    name=None,
                    flags=None,
                )
            )

        # Sous-titres supplémentaires optionnels
        for extra_idx, extra_path in enumerate(extra_subtitle_files):
            extra_p = Path(extra_path)
            if not extra_p.is_file():
                continue
            if extra_p.suffix.lower() == ".srt":
                sub_artifact = paths.work_dir / f"extra_sub_{extra_idx}.mkv"
                self._run_cmd([
                    self._bins["ffmpeg"],
                    "-hide_banner",
                    "-y",
                    "-nostdin",
                    "-i", str(extra_p),
                    "-c:s", "srt",
                    "-f", "matroska",
                    str(sub_artifact),
                ], step)
            elif extra_p.suffix.lower() == ".mkv":
                sub_artifact = extra_p
            else:
                continue

            stem_lower = extra_p.name.lower()
            is_sdh = "sdh" in stem_lower or "hi" in stem_lower or "cc" in stem_lower
            is_forced = "forced" in stem_lower
            lang = "fre" if ("fr" in stem_lower or "fra" in stem_lower) else ("eng" if "en" in stem_lower else "und")
            title = "Français (SDH)" if (is_sdh and lang == "fre") else ("Français" if lang == "fre" else extra_p.stem)

            assembly_tracks.append(
                MatroskaAssemblyTrack(
                    artifact=sub_artifact,
                    artifact_track_index=0,
                    source_identity=deterministic_source_identity(sub_artifact),
                    language_value=lang,
                    name=title,
                    flags=MatroskaTrackFlags(
                        enabled=True,
                        default=False,
                        forced=is_forced,
                        hearing_impaired=is_sdh,
                    ),
                )
            )

        # 5. Chapitres : source_mkv si présent, sinon repli vers film2 si disponible
        chapter_source: Path | None = None
        if source_reader.raw_top_level(CHAPTERS_ID):
            chapter_source = source_mkv
        elif film2 is not None and film2.is_file() and film2.suffix.lower() == ".mkv":
            if MatroskaReader(film2).raw_top_level(CHAPTERS_ID):
                chapter_source = film2
                self.step_progress.emit(step, f"Chapitres importés depuis {film2.name}.")

        # 6. Attachments
        attachments = tuple(
            MatroskaAssemblyAttachment(
                artifact=source_mkv,
                local_index=idx,
                source_identity=deterministic_source_identity(source_mkv),
            )
            for idx in range(len(source_reader.attachment_headers()))
        )

        # 7. Tags et titre
        tag_copy_sources = (source_mkv,) if source_reader.raw_top_level(TAGS_ID) else ()
        segment_title = source_reader.segment_title()

        # 8. Assemblage et écriture native
        self.step_progress.emit(
            step,
            f"Assemblage Matroska natif multi-pistes ({len(assembly_tracks)} pistes) → {paths.output_mkv.name}…",
        )
        plan = MatroskaAssemblyPlan(
            output=paths.output_mkv,
            ordered_tracks=tuple(assembly_tracks),
            attachments=attachments,
            chapter_source=chapter_source,
            tag_copy_sources=tag_copy_sources,
            segment_title=segment_title,
        )

        # Sortie ≈ vidéo encapsulée + pistes non vidéo de Film 1 (+ sous-titres ajoutés).
        output_estimate = paths.film1_wrapped_video.stat().st_size + max(
            0, film1.stat().st_size - self._video_stream_bytes(film1)[0],
        ) + sum(Path(p).stat().st_size for p in extra_subtitle_files if Path(p).is_file())
        self._ensure_free_space(step, paths.output_mkv.parent, output_estimate, "le fichier final")
        try:
            self._assemble_final_mkv(plan, dovi_record)
        except Exception as exc:
            raise WorkflowError(step, f"Écriture Matroska native échouée : {exc}") from exc

        size_mb = paths.output_mkv.stat().st_size / (1024 ** 2)
        duration = time.monotonic() - t0
        self.step_finished.emit(
            step,
            StepResult(
                step, True,
                f"{paths.output_mkv.name}  ({size_mb:.0f} Mo)",
                duration,
            ),
        )

    def _patch_video_timecodes(
        self,
        target_mkv: Path,
        source_for_timestamps: Path,
        step: WorkflowStep,
    ) -> None:
        """Réapplique les PTS source packet-order sur les blocs vidéo."""
        self.step_progress.emit(
            step,
            "Correction timecodes vidéo depuis la source (ordre packet)…",
        )
        try:
            result = MatroskaVideoTimecodePatcher(
                ffprobe_bin=self._bins["ffprobe"],
            ).patch(
                target_mkv=target_mkv,
                source_for_timestamps=source_for_timestamps,
            )
        except Exception as exc:
            raise WorkflowError(step, f"Patch timecodes vidéo échoué : {exc}") from exc
        self.step_progress.emit(
            step,
            f"Timecodes vidéo corrigés : {result.patched_blocks} blocs "
            f"(dernier PTS {result.last_pts_ms} ms).",
        )

    def _patch_dovi_block_addition(
        self,
        paths: _WorkflowPaths,
        *,
        dovi_profile: DoviProfile,
        step: WorkflowStep,
        dovi_record: DolbyVisionConfigRecord | None = None,
    ) -> None:
        """Ajoute la signalisation Dolby Vision Matroska sur la piste HEVC."""
        self.step_progress.emit(step, "Injection signal Dolby Vision au niveau Matroska…")
        record = dovi_record or self._build_dovi_record_from_rpu(
            paths.film2_rpu,
            forced_compat_id=1 if dovi_profile == DoviProfile.P8_1 else None,
        )
        if record is None:
            self.step_progress.emit(
                step,
                "[WARN] Impossible d'extraire le profil DOVI du RPU "
                "pour le signal Matroska.",
            )
            return

        try:
            patch_result = MatroskaDoviBlockAdditionEditor().patch(
                paths.output_mkv,
                record=record,
            )
        except Exception as exc:
            self.step_progress.emit(
                step,
                f"[WARN] Patch BlockAdditionMapping DOVI échoué : {exc}",
            )
            return

        if patch_result.applied:
            self.step_progress.emit(
                step,
                f"BlockAdditionMapping DOVI ajouté "
                f"(track #{patch_result.patched_track_number}, "
                f"Δ {patch_result.bytes_delta:+d} octets).",
            )
        elif patch_result.skipped:
            self.step_progress.emit(
                step,
                f"Signal DOVI Matroska non modifié : {patch_result.reason}",
            )

    def _build_dovi_record_from_rpu(
        self,
        rpu_bin: Path,
        *,
        forced_compat_id: int | None = None,
    ) -> DolbyVisionConfigRecord | None:
        """Construit le record ``dvcC`` Matroska depuis le résumé dovi_tool."""
        try:
            result = self._run_probe(
                [
                    self._bins["dovi_tool"],
                    "info",
                    "-i",
                    str(rpu_bin),
                    "--summary",
                ],
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        text = (result.stdout or "") + (result.stderr or "")

        profile_match = re.search(r"Profile\s*:\s*(\d+)(?:\.(\d+))?", text)
        if not profile_match:
            return None
        profile = int(profile_match.group(1))
        sub_profile = int(profile_match.group(2) or 0)

        if forced_compat_id is not None:
            compat_id = int(forced_compat_id)
        else:
            compat_match = re.search(
                r"compatibility\s*id\s*:\s*(\d+)",
                text,
                re.IGNORECASE,
            )
            if compat_match:
                compat_id = int(compat_match.group(1))
            elif profile == 8 and sub_profile > 0:
                compat_id = sub_profile
            else:
                # Profile 8 nu dans dovi_tool == P8.x ; pour une base HDR10,
                # signaler P8.1 évite le fallback DV/HDR10 ambigu côté players.
                compat_id = 1 if profile == 8 else 0

        level_match = re.search(r"DV\s+Level\s*:\s*(\d+)", text, re.IGNORECASE)
        level = int(level_match.group(1)) if level_match else 6

        return DolbyVisionConfigRecord(
            profile=profile,
            level=level,
            rpu_present=True,
            el_present=False,
            bl_present=True,
            bl_signal_compat_id=max(0, min(15, compat_id)),
        )

    # ------------------------------------------------------------------
    # Étape 8 — Nettoyage
    # ------------------------------------------------------------------

    def _step_cleanup(self, paths: _WorkflowPaths) -> None:
        step = WorkflowStep.CLEANUP
        t0   = time.monotonic()
        self.step_started.emit(step)

        for path in paths.intermediates():
            if path.exists():
                path.unlink()
                self.step_progress.emit(step, f"Supprimé : {path.name}")

        for extra_temp in paths.work_dir.glob("extra_sub_*.mkv"):
            extra_temp.unlink(missing_ok=True)

        # Dossier process créé par start() : suppression prouvée par jeton
        # (marqueur compris) ; sinon seulement s'il est vide.
        owned = paths.owned_dir
        try:
            if owned is None or not owned.remove():
                paths.work_dir.rmdir()
        except OSError:
            pass

        duration = time.monotonic() - t0
        self.step_finished.emit(
            step, StepResult(step, True, "Fichiers intermédiaires supprimés", duration)
        )

    # ------------------------------------------------------------------
    # Tâches d'extraction (utilisées dans le pool)
    # ------------------------------------------------------------------

    def _extract_hevc(
        self, source: Path, dest: Path, emit: Callable[[str], None]
    ) -> str:
        emit(f"Extraction HEVC : {source.name} → {dest.name}…")
        # hevc_mp4toannexb est OBLIGATOIRE pour MP4/MOV/TS/M2TS : convertit
        # le HEVC length-prefixed en annexB (start codes) consommable par
        # dovi_tool et hdr10plus_tool. Inoffensif pour MKV (start codes déjà
        # présents → BSF no-op).
        self._run_raw([
            self._bins["ffmpeg"],
            "-hide_banner",
            "-y",
            "-i", str(source),
            "-map", "0:v:0",
            "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-an",
            "-sn",
            "-dn",
            "-f", "hevc",
            str(dest),
        ])
        return f"HEVC extrait → {dest.name}"

    def _extract_rpu(
        self, source: Path, dest: Path, emit: Callable[[str], None]
    ) -> str:
        emit(f"Extraction RPU DoVi : {source.name}…")
        self._run_raw([
            self._bins["dovi_tool"], "extract-rpu",
            "-i", str(source), "-o", str(dest),
        ], step=WorkflowStep.EXTRACT_PARALLEL)
        return f"RPU extrait → {dest.name}"

    def _extract_hdr10plus(
        self, source: Path, dest: Path, emit: Callable[[str], None]
    ) -> str:
        emit(f"Extraction HDR10+ : {source.name}…")
        self._run_raw([
            self._bins["hdr10plus_tool"], "extract",
            str(source), "-o", str(dest),
        ], step=WorkflowStep.EXTRACT_PARALLEL)
        return f"HDR10+ extrait → {dest.name}"

    # ------------------------------------------------------------------
    # Pool d'exécution parallèle
    # ------------------------------------------------------------------

    def _run_pool(
        self,
        tasks: dict[str, Callable],
        errors: list[str],
    ) -> None:
        """
        Exécute les callables en parallèle via ThreadPoolExecutor.
        Collecte les erreurs dans `errors`. Vérifie le flag d'annulation
        entre chaque future complétée pour permettre un arrêt propre.
        Ne fait rien si `tasks` est vide (ThreadPoolExecutor(max_workers=0)
        lèverait ValueError).
        """
        if not tasks:
            return
        with ThreadPoolExecutor(max_workers=min(len(tasks), self._max_workers)) as executor:
            futures: dict[Future, str] = {
                executor.submit(fn): label
                for label, fn in tasks.items()
            }
            for future in as_completed(futures):
                label = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    errors.append(f"[{label}] {exc}")
                # Vérifier l'annulation après chaque tâche terminée
                if self._cancelled:
                    break

    # ------------------------------------------------------------------
    # Helpers subprocess
    # ------------------------------------------------------------------

    def _run_cmd(self, cmd: list[str], step: WorkflowStep) -> str:
        """
        Lance une commande, émet des lignes de progression, lève WorkflowError
        si le code de retour est non nul.

        Pour `dovi_tool` / `hdr10plus_tool` sous Linux/macOS, la commande est
        exécutée sous pty pour que l'outil émette sa barre de progression.
        Les lignes XX% alimentent `step_progress_pct` (barre globale) et NE
        sont PAS émises en `step_progress` (le LogPanel reste lisible).
        """
        binary = Path(cmd[0]).name
        use_pty = sys.platform != "win32" and binary in _PTY_PROGRESS_TOOLS
        try:
            if use_pty:
                return self._run_cmd_pty(cmd, step)
            with subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **subprocess_text_kwargs(),
            ) as proc:
                self._track_proc(proc)
                try:
                    lines: list[str] = []
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        stripped = line.rstrip("\n")
                        if stripped:
                            lines.append(stripped)
                            self.step_progress.emit(step, stripped)
                    proc.wait()
                except BaseException:
                    # Tuer avant Popen.__exit__, qui attendrait sinon la fin de l'outil.
                    kill_process_tree(proc, timeout=0.2)
                    raise
                finally:
                    self._untrack_proc(proc)
                output = "\n".join(lines)
                if proc.returncode != 0:
                    raise WorkflowError(
                        step,
                        f"Commande échouée (code {format_returncode(proc.returncode)}) : {' '.join(cmd[:2])}\n"
                        + output[-1000:],
                    )
                return output
        except WorkflowError:
            raise
        except FileNotFoundError:
            raise WorkflowError(step, f"Outil introuvable : {cmd[0]}")

    def _run_cmd_pty(self, cmd: list[str], step: WorkflowStep) -> str:
        """
        Variante pty pour dovi_tool / hdr10plus_tool (Linux/macOS).

        Lignes XX% → `step_progress_pct.emit(step, pct)` au changement.
        Autres lignes → `step_progress.emit(step, line)` comme d'habitude.
        """
        import fcntl
        import pty
        import struct
        import termios

        master_fd, slave_fd = pty.openpty()
        # Taille de terminal nécessaire pour que `indicatif` (dovi_tool /
        # hdr10plus_tool) accepte d'afficher la barre de progression.
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 120, 0, 0))
        except OSError:
            pass
        # `indicatif` désactive la barre quand TERM=dumb (cas via .desktop /
        # distrobox-export) ou non défini. Forcer un TERM réel.
        env = os.environ.copy()
        if env.get("TERM", "dumb") in ("", "dumb"):
            env["TERM"] = "xterm-256color"
        proc = subprocess.Popen(
            cmd,
            stdout=slave_fd,
            stderr=slave_fd,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            env=env,
        )
        os.close(slave_fd)
        self._track_proc(proc)

        lines: list[str] = []
        last_pct: int = -1
        buffer = ""
        try:
            while True:
                try:
                    data = os.read(master_fd, 1024)
                except OSError:
                    break
                if not data:
                    break

                buffer += data.decode("utf-8", errors="replace")
                if "\r" not in buffer and "\n" not in buffer:
                    continue

                parts = re.split(r"[\r\n]+", buffer)
                buffer = parts[-1]
                for raw in parts[:-1]:
                    line = _ANSI_RE.sub("", raw).strip()
                    if not line:
                        continue
                    lines.append(line)

                    m = _PERCENT_RE.search(line)
                    if m:
                        try:
                            pct = int(m.group(1))
                        except ValueError:
                            pct = -1
                        if 0 <= pct <= 100 and pct != last_pct:
                            last_pct = pct
                            self.step_progress_pct.emit(step, pct)
                        # Lignes de progression : pas de log
                        continue
                    self.step_progress.emit(step, line)

            if buffer.strip():
                line = _ANSI_RE.sub("", buffer).strip()
                lines.append(line)
                if not _PERCENT_RE.search(line):
                    self.step_progress.emit(step, line)

            proc.wait()
            output = "\n".join(lines)
            if proc.returncode != 0:
                raise WorkflowError(
                    step,
                    f"Commande échouée (code {format_returncode(proc.returncode)}) : {' '.join(cmd[:2])}\n"
                    + output[-1000:],
                )
            return output
        except BaseException:
            kill_process_tree(proc, timeout=0.2)
            raise
        finally:
            self._untrack_proc(proc)
            try:
                os.close(master_fd)
            except OSError:
                pass

    def _run_raw(self, cmd: list[str], step: WorkflowStep | None = None) -> str:
        """
        Lance une commande dans un worker. Si `step` est fourni et le binaire
        est dovi_tool/hdr10plus_tool sous Linux/macOS, exécute sous pty pour
        capturer le pourcentage de progression (alimente `step_progress_pct`).
        """
        binary = Path(cmd[0]).name
        if (
            step is not None
            and sys.platform != "win32"
            and binary in _PTY_PROGRESS_TOOLS
        ):
            return self._run_cmd_pty(cmd, step)

        # Popen (et non run) : le processus doit rester tuable par cancel().
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **subprocess_text_kwargs(),
        ) as proc:
            self._track_proc(proc)
            try:
                stdout, stderr = proc.communicate()
            except BaseException:
                kill_process_tree(proc, timeout=0.2)
                raise
            finally:
                self._untrack_proc(proc)
        if proc.returncode != 0:
            raise RuntimeError(
                f"Commande échouée (code {format_returncode(proc.returncode)}) : {' '.join(cmd[:2])}\n"
                + ((stdout or "") + (stderr or ""))[-500:]
            )
        return stdout or ""

    # ------------------------------------------------------------------
    # Helpers mediainfo
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_frame_rate_expr(value: object) -> str | None:
        raw = str(value or "").strip()
        if raw in {"", "0", "0/0", "N/A"}:
            return None
        if re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return raw
        if re.fullmatch(r"\d+/\d+", raw):
            return raw
        return None

    def _source_video_fps_expr(self, source: Path) -> str:
        cmd = [
            self._bins["ffprobe"],
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(source),
        ]
        try:
            result = self._run_probe(
                cmd,
                capture_output=True,
                check=False,
                **subprocess_text_kwargs(),
            )
        except _CancelledError:
            raise
        except Exception:
            return _FALLBACK_HEVC_FRAME_RATE
        if result.returncode != 0:
            return _FALLBACK_HEVC_FRAME_RATE
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return _FALLBACK_HEVC_FRAME_RATE
        streams = payload.get("streams")
        if not isinstance(streams, list):
            return _FALLBACK_HEVC_FRAME_RATE
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if stream.get("codec_type") != "video":
                continue
            for key in ("avg_frame_rate", "r_frame_rate"):
                fps_expr = self._normalize_frame_rate_expr(stream.get(key))
                if fps_expr is not None:
                    return fps_expr
            break
        return _FALLBACK_HEVC_FRAME_RATE

    def _subtitle_codec_args_for(self, source: Path) -> list[str]:
        """Args ``-c:s …`` pour le muxage MKV final selon les subs du source.

        Route chaque piste : copy quand MKV l'accepte, srt sinon (mov_text,
        eia_608, …). Si aucune sub ou probing impossible → ``-c:s copy``.
        """
        cmd = [
            self._bins["ffprobe"],
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-select_streams", "s",
            str(source),
        ]
        try:
            result = self._run_probe(
                cmd, capture_output=True, check=False,
                **subprocess_text_kwargs(),
            )
        except _CancelledError:
            raise
        except Exception:
            return ["-c:s", "copy"]
        if result.returncode != 0:
            return ["-c:s", "copy"]
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return ["-c:s", "copy"]

        streams = payload.get("streams") or []
        per_index: list[str] = []
        any_convert = False
        # Les streams renvoyés par -select_streams s sont ordonnés selon
        # leur apparition dans le fichier : c'est l'ordre que ffmpeg utilisera
        # pour -map 1:s? (out index 0, 1, 2, …).
        for out_idx, stream in enumerate(streams):
            if not isinstance(stream, dict):
                continue
            codec = str(stream.get("codec_name", "") or "")
            try:
                codec_arg, _ = plan_subtitle_codec(codec)
            except ValueError:
                # Codec non supporté : on force srt, ffmpeg refusera s'il ne
                # sait pas convertir et l'utilisateur verra l'erreur.
                codec_arg = "srt"
            if codec_arg != "copy":
                any_convert = True
                per_index.extend([f"-c:s:{out_idx}", codec_arg])
        if not any_convert:
            return ["-c:s", "copy"]
        return ["-c:s", "copy", *per_index]

    def _has_dovi_rpu(self, path: Path) -> bool:
        """Repli de mediainfo, aveugle au RPU d'un HEVC brut ou d'un MKV sans dvcC."""
        if path.suffix.lower() not in {".hevc", ".h265", ".265", ".x265", ".mkv"}:
            return False
        with tempfile.TemporaryDirectory(prefix="dovi_probe_") as tmp:
            return self._extract_first_rpu(path, Path(tmp) / "rpu.bin")

    def _extract_first_rpu(self, path: Path, rpu: Path) -> bool:
        """Extrait le premier RPU de ``path`` (``extract-rpu -l 1``) ; True si obtenu.

        MKV dont Tracks échappe au premier SeekHead (lecteur strict de dovi_tool) :
        premières images copiées en Annex B par FFmpeg, puis lues par dovi_tool.
        """
        source = path
        try:
            if path.suffix.lower() == ".mkv" and not strict_demuxer_reads_tracks(path):
                source = rpu.with_suffix(".hevc")
                # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
                self._run_probe(
                    [self._bins["ffmpeg"], "-nostdin", "-v", "error", "-y", "-i", str(path), "-map", "0:v:0",
                     "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-frames:v", "8", "-f", "hevc", str(source)],
                    capture_output=True, check=False, timeout=120, **subprocess_text_kwargs(),
                )
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit
            self._run_probe(
                [self._bins["dovi_tool"], "extract-rpu", "-i", str(source), "-l", "1", "-o", str(rpu)],
                capture_output=True, check=False, timeout=120, **subprocess_text_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            if source != path:
                source.unlink(missing_ok=True)
        return rpu.is_file() and rpu.stat().st_size > 0

    def _film1_dovi_record(self, film1: Path, hevc: Path) -> DolbyVisionConfigRecord | None:
        """Configuration Dolby Vision existante de Film 1, conservée quand seul
        le HDR10+ est injecté. Priorité au record du conteneur (ffprobe, exact :
        niveau, couche EL) ; repli sur le premier RPU du flux HEVC injecté."""
        record = self._probe_dovi_config_record(film1)
        if record is not None:
            return record
        with tempfile.TemporaryDirectory(prefix="dovi_probe_") as tmp:
            rpu = Path(tmp) / "rpu.bin"
            if not self._extract_first_rpu(hevc, rpu):
                return None
            return self._build_dovi_record_from_rpu(rpu)

    def _probe_dovi_config_record(self, path: Path) -> DolbyVisionConfigRecord | None:
        """Lit le « DOVI configuration record » de la première piste vidéo (ffprobe)."""
        try:
            result = self._run_probe(
                [
                    self._bins["ffprobe"], "-v", "error", "-select_streams", "v:0",
                    "-show_streams", "-of", "json", str(path),
                ],
                capture_output=True, check=False, **subprocess_text_kwargs(),
            )
            streams = json.loads(result.stdout or "{}").get("streams") or []
        except (OSError, json.JSONDecodeError, AttributeError):
            return None
        for stream in streams[:1]:
            for side_data in stream.get("side_data_list") or []:
                if side_data.get("side_data_type") != "DOVI configuration record":
                    continue
                try:
                    return DolbyVisionConfigRecord(
                        profile=int(side_data["dv_profile"]),
                        level=int(side_data["dv_level"]),
                        rpu_present=bool(int(side_data.get("rpu_present_flag", 1))),
                        el_present=bool(int(side_data.get("el_present_flag", 0))),
                        bl_present=bool(int(side_data.get("bl_present_flag", 1))),
                        bl_signal_compat_id=int(side_data.get("dv_bl_signal_compatibility_id", 0)),
                    )
                except (KeyError, TypeError, ValueError):
                    return None
        return None

    def _mediainfo(self, path: Path, inform: str) -> str:
        """Lance mediainfo --Inform et retourne la sortie brute."""
        result = self._run_probe(
            [self._bins["mediainfo"], f"--Inform={inform}", str(path)],
            capture_output=True, check=False, **subprocess_text_kwargs(),
        )
        return result.stdout

    def _get_framecount(self, path: Path) -> int | None:
        """Compte exact, sans statistiques périmées, avec scan annulable."""
        return reliable_frame_count(
            path, mediainfo_bin=self._bins["mediainfo"], ffprobe_bin=self._bins["ffprobe"],
            exact=True, run_command=self._run_probe,
        )

    def _load_mediainfo_video(self, path: Path) -> dict | None:
        """Charge le track Video du JSON mediainfo (None si indisponible)."""
        try:
            result = self._run_probe(
                [self._bins["mediainfo"], "--Output=JSON", str(path)],
                capture_output=True, check=False, **subprocess_text_kwargs(),
            )
        except (FileNotFoundError, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return None
        media = data.get("media") or {}
        for track in media.get("track") or []:
            if isinstance(track, dict) and track.get("@type") == "Video":
                return track
        return None

    def _read_static_hdr_metadata(self, path: Path) -> StaticHdrMetadata:
        """Lit MasteringDisplay + MaxCLL/MaxFALL via mediainfo et reformate
        en chaînes attendues par ``inject_static_hdr_sei_file`` :
          - master_display : ``G(x,y)B(x,y)R(x,y)WP(x,y)L(max,min)``
          - max_cll        : ``"MaxCLL,MaxFALL"``

        Toute partie absente / non parsable → chaîne vide (no-op côté
        injection)."""
        track = self._load_mediainfo_video(path)
        if track is None:
            return StaticHdrMetadata()
        return StaticHdrMetadata(
            master_display=_format_master_display_from_mediainfo(track),
            max_cll=_format_max_cll_from_mediainfo(track),
        )

    # ------------------------------------------------------------------
    # Utilitaire statique
    # ------------------------------------------------------------------

    @staticmethod
    def required_tools() -> list[str]:
        return ["mediainfo", "ffmpeg", "ffprobe", "dovi_tool", "hdr10plus_tool"]


class _CancelledError(Exception):
    """Levée en interne pour signaler une annulation."""
    pass
