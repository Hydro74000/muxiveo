"""Validation merge_dovi : acceptation des conteneurs vidéo étendus
(MKV/MP4/MOV/TS/M2TS/VOB/…) et des streams HEVC bruts, rejet du reste.

Pas de test end-to-end : l'exécution complète du workflow exige des
sources DoVi/HDR10+ réelles qu'on ne peut pas synthétiser avec ffmpeg.
Ce fichier couvre donc uniquement la couche de validation + routage
d'extraction HEVC annexB.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.workflows.merge_dovi import (
    _MERGE_DOVI_ACCEPTED,
    _MERGE_DOVI_CONTAINERS,
    _RAW_HEVC_EXTENSIONS,
    _is_raw_hevc,
    _needs_hevc_extraction,
)


# ---------------------------------------------------------------------------
# Extensions supportées en entrée merge_dovi
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ext", [
    # Conteneurs standard
    ".mkv", ".mk3d", ".mks",
    ".mp4", ".m4v",
    ".mov",
    ".ts", ".m2ts", ".mts",
    ".mpg", ".mpeg", ".m2v", ".mpv", ".evo", ".evob", ".vob",
    ".avi",
    ".webm",
    ".flv", ".f4v",
    # Streams HEVC bruts
    ".hevc", ".h265", ".265", ".x265",
])
def test_merge_dovi_accepts_extension(ext: str) -> None:
    assert ext in _MERGE_DOVI_ACCEPTED, f"{ext} devrait être accepté par merge_dovi"


@pytest.mark.parametrize("ext", [
    # Audio uniquement
    ".ac3", ".dts", ".flac", ".aac", ".thd", ".wav",
    # Subs
    ".srt", ".ass", ".sup", ".idx",
    # Images
    ".jpg", ".png",
    # Non-média
    ".txt", ".zip",
])
def test_merge_dovi_rejects_extension(ext: str) -> None:
    assert ext not in _MERGE_DOVI_ACCEPTED, f"{ext} ne devrait pas être accepté"


# ---------------------------------------------------------------------------
# Routage : HEVC brut vs conteneur
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ext", [".hevc", ".h265", ".265", ".x265"])
def test_raw_hevc_detected(ext: str) -> None:
    assert _is_raw_hevc(Path(f"/tmp/x{ext}"))
    assert not _needs_hevc_extraction(Path(f"/tmp/x{ext}"))


@pytest.mark.parametrize("ext", [
    ".mkv", ".mp4", ".mov", ".ts", ".m2ts", ".vob", ".webm", ".avi",
])
def test_container_needs_extraction(ext: str) -> None:
    """Les conteneurs doivent être extraits en HEVC annexB avant passage
    aux outils dovi_tool/hdr10plus_tool (qui ne gèrent que MKV + HEVC brut)."""
    p = Path(f"/tmp/x{ext}")
    assert not _is_raw_hevc(p)
    assert _needs_hevc_extraction(p)


def test_raw_and_containers_are_disjoint() -> None:
    assert not (_RAW_HEVC_EXTENSIONS & _MERGE_DOVI_CONTAINERS)


def test_accepted_is_union() -> None:
    assert _MERGE_DOVI_ACCEPTED == (_RAW_HEVC_EXTENSIONS | _MERGE_DOVI_CONTAINERS)


# ---------------------------------------------------------------------------
# Intégration ffmpeg : le BSF hevc_mp4toannexb est nécessaire pour MP4/TS
# ---------------------------------------------------------------------------

def test_extract_hevc_command_includes_annexb_bsf() -> None:
    """Le code source doit appliquer hevc_mp4toannexb dans _extract_hevc.

    Test anti-régression : cette BSF est indispensable pour que les sorties
    HEVC extraites de MP4/MOV/TS soient consommables par dovi_tool et
    hdr10plus_tool. MKV n'en a pas besoin mais la BSF y est no-op.
    """
    src = Path(__file__).resolve().parents[2] / "core" / "workflows" / "merge_dovi.py"
    content = src.read_text(encoding="utf-8")
    # Cherche le bloc ffmpeg d'extraction HEVC
    assert "hevc_mp4toannexb" in content, (
        "BSF hevc_mp4toannexb absente de merge_dovi.py : "
        "l'extraction HEVC depuis MP4/TS échouera en aval."
    )
