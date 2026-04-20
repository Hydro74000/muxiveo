"""Tests unitaires de core/file_types — filtrage des extensions source.

Couverture :
- ACCEPTED_EXTENSIONS contient les formats supportés (MP4/M2TS/VOB/DTS/AAC/…)
- VIDEO_CONTAINER_EXTENSIONS exclut les pistes audio pures
- build_qt_filter() produit une chaîne Qt valide avec tous les groupes
- is_accepted() route correctement selon video_only
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.file_types import (
    ACCEPTED_EXTENSIONS,
    VIDEO_CONTAINER_EXTENSIONS,
    SUPPORTED_FILE_TYPES,
    build_qt_filter,
    is_accepted,
)


# ---------------------------------------------------------------------------
# Matrice d'extensions à accepter en entrée
# ---------------------------------------------------------------------------

_EXPECTED_ACCEPTED = [
    # conteneurs majeurs
    ".mkv", ".mk3d", ".mks",
    ".mp4", ".m4v",
    ".mov",
    ".avi",
    ".ts", ".m2ts", ".mts",
    ".mpg", ".mpeg", ".m2v", ".mpv", ".m1v", ".evo", ".evob", ".vob",
    ".webm", ".webma", ".webmv", ".weba",
    ".flv", ".f4v",
    ".ogg", ".ogm", ".ogv",
    ".rm", ".rmvb", ".rv", ".ra", ".ram",
    ".mpls", ".bdmv",
    # streams élémentaires vidéo
    ".hevc", ".h265", ".265", ".x265",
    ".h264", ".264", ".avc", ".x264",
    ".av1", ".obu", ".ivf",
    ".vc1",
    ".drc",
    # audio
    ".ac3", ".eac3", ".eb3", ".ec3",
    ".dts", ".dtshd", ".dts-hd", ".dtsma",
    ".aac", ".m4a",
    ".mp2", ".mp3",
    ".flac",
    ".wav",
    ".wv",
    ".tta",
    ".opus",
    ".thd", ".mlp", ".truehd", ".true-hd", ".thd+ac3",
    # subs
    ".srt",
    ".ass", ".ssa",
    ".sup",
    ".idx",
    ".vtt", ".webvtt",
    ".textst",
    ".usf", ".xml",
    # divers
    ".btn",
    ".caf",
]


@pytest.mark.parametrize("ext", _EXPECTED_ACCEPTED)
def test_extension_accepted(ext: str) -> None:
    """Toutes les extensions supportées sont acceptées en entrée globale."""
    assert ext in ACCEPTED_EXTENSIONS, f"Extension manquante : {ext}"


def test_accepted_extensions_lowercase_dotted() -> None:
    """Toutes les extensions sont en minuscule, préfixées d'un point."""
    for ext in ACCEPTED_EXTENSIONS:
        assert ext.startswith("."), f"'{ext}' : point manquant"
        assert ext == ext.lower(), f"'{ext}' : casse incorrecte"


def test_accepted_extensions_count() -> None:
    """Un volume raisonnable d'extensions (sanity check)."""
    # Environ 80 extensions uniques supportées
    assert len(ACCEPTED_EXTENSIONS) >= 70


# ---------------------------------------------------------------------------
# Sous-ensemble vidéo/conteneur : exclut audio-only et subs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ext", [
    ".mkv", ".mp4", ".mov", ".ts", ".m2ts", ".webm",
    ".hevc", ".h264", ".av1", ".ivf",
    ".vob", ".mpls", ".bdmv",
])
def test_video_container_includes_video(ext: str) -> None:
    assert ext in VIDEO_CONTAINER_EXTENSIONS


@pytest.mark.parametrize("ext", [
    ".ac3", ".eac3", ".dts", ".aac", ".flac", ".wav", ".opus",
    ".srt", ".ass", ".sup", ".idx",
    ".thd", ".mlp", ".tta",
])
def test_video_container_excludes_audio_and_subs(ext: str) -> None:
    """Les pistes purement audio et sous-titres ne doivent pas passer le
    filtre vidéo (le panneau d'inspection / encode refuse ces sources)."""
    assert ext not in VIDEO_CONTAINER_EXTENSIONS


# ---------------------------------------------------------------------------
# is_accepted
# ---------------------------------------------------------------------------

def test_is_accepted_global() -> None:
    assert is_accepted("/tmp/a.mkv")
    assert is_accepted("/tmp/a.mp4")
    assert is_accepted("/tmp/a.m2ts")
    assert is_accepted("/tmp/a.dts")        # audio accepté en mode global
    assert is_accepted("/tmp/a.SRT")        # casse tolérée via Path.suffix.lower
    assert is_accepted(Path("/tmp/a.vob"))
    assert not is_accepted("/tmp/a.txt")
    assert not is_accepted("/tmp/noext")


def test_is_accepted_video_only() -> None:
    assert is_accepted("/tmp/a.mkv", video_only=True)
    assert is_accepted("/tmp/a.m2ts", video_only=True)
    assert is_accepted("/tmp/a.hevc", video_only=True)
    # Audio et subs refusés en mode video_only
    assert not is_accepted("/tmp/a.dts", video_only=True)
    assert not is_accepted("/tmp/a.srt", video_only=True)
    assert not is_accepted("/tmp/a.flac", video_only=True)


# ---------------------------------------------------------------------------
# build_qt_filter
# ---------------------------------------------------------------------------

def test_build_qt_filter_global_contains_all_and_types() -> None:
    flt = build_qt_filter()
    # Sépare les groupes par ";;"
    groups = flt.split(";;")
    assert len(groups) >= len(SUPPORTED_FILE_TYPES) + 2  # "all supported" + "all"
    # Le premier groupe liste toutes les extensions
    assert "*.mkv" in groups[0]
    assert "*.mp4" in groups[0]
    assert "*.m2ts" in groups[0]
    assert "*.dts" in groups[0]
    # Deuxième groupe = "tous" universel
    assert groups[1] == "Tous les fichiers (*)"


def test_build_qt_filter_video_only_excludes_audio_subs() -> None:
    flt = build_qt_filter(video_only=True)
    groups = flt.split(";;")
    assert len(groups) == 2
    assert "*.mkv" in groups[0]
    assert "*.m2ts" in groups[0]
    assert "*.hevc" in groups[0]
    assert "*.dts" not in groups[0]
    assert "*.srt" not in groups[0]
    assert "*.flac" not in groups[0]


def test_build_qt_filter_no_duplicate_extensions() -> None:
    """Les extensions partagées entre plusieurs types (ogg, m4a, mp4) ne
    doivent pas apparaître en double dans le groupe 'all supported'."""
    flt = build_qt_filter()
    all_group = flt.split(";;")[0]
    # Extrait les globs *.ext
    import re
    globs = re.findall(r"\*\.[a-z0-9\-+]+", all_group)
    assert len(globs) == len(set(globs)), f"Doublons : {[g for g in globs if globs.count(g) > 1]}"
