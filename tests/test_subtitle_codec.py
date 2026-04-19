"""Tests unitaires de core/subtitle_codec — routage codec sub pour sortie MKV.

Couvre les trois branches :
- copy direct (subrip, ass, webvtt, PGS, VobSub, TextST)
- conversion vers srt (mov_text, eia_608, cea_708, microdvd, …)
- refus explicite (dvb_subtitle, dvb_teletext)
- codec inconnu → tentative copy + warning
"""
from __future__ import annotations

import pytest

from core.subtitle_codec import (
    CONVERT_TO_SRT,
    MKV_COPY_SAFE,
    UNSUPPORTED,
    plan_subtitle_codec,
)


# ---------------------------------------------------------------------------
# Copy safe : muxage direct en MKV
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("codec", [
    "subrip", "srt",
    "ass", "ssa",
    "webvtt",
    "hdmv_pgs_subtitle",
    "dvd_subtitle",
    "hdmv_text_subtitle",
])
def test_copy_safe_codecs(codec: str) -> None:
    arg, warn = plan_subtitle_codec(codec)
    assert arg == "copy"
    assert warn is None


# ---------------------------------------------------------------------------
# Conversion vers srt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("codec", [
    "mov_text",         # MP4/MOV → MKV nécessite conversion
    "eia_608", "cea_608", "cea_708",  # closed captions TS broadcast
    "microdvd",
    "jacosub",
    "mpl2",
    "pjs",
    "realtext",
    "sami",
    "stl",
    "subviewer", "subviewer1",
    "vplayer",
])
def test_convert_to_srt_codecs(codec: str) -> None:
    arg, warn = plan_subtitle_codec(codec)
    assert arg == "srt"
    assert warn is None


# ---------------------------------------------------------------------------
# Refus explicite
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("codec", [
    "dvb_subtitle",
    "dvb_teletext",
    "arib_caption",
])
def test_unsupported_codecs_raise(codec: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        plan_subtitle_codec(codec)
    assert codec in str(exc_info.value) or "non supporté" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Codec inconnu / vide
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("codec", ["unknown_codec", "foo_bar", "", "  "])
def test_unknown_codec_defaults_to_copy_with_warning(codec: str) -> None:
    arg, warn = plan_subtitle_codec(codec)
    assert arg == "copy"
    assert warn is not None


def test_case_insensitive() -> None:
    """Le routage normalise en minuscule."""
    assert plan_subtitle_codec("MOV_TEXT") == ("srt", None)
    assert plan_subtitle_codec("SubRip") == ("copy", None)
    assert plan_subtitle_codec("  mov_text  ") == ("srt", None)


# ---------------------------------------------------------------------------
# Invariants des listes
# ---------------------------------------------------------------------------

def test_lists_are_disjoint() -> None:
    """Chaque codec n'appartient qu'à une seule catégorie."""
    assert not (MKV_COPY_SAFE & CONVERT_TO_SRT)
    assert not (MKV_COPY_SAFE & UNSUPPORTED)
    assert not (CONVERT_TO_SRT & UNSUPPORTED)


def test_all_lists_lowercase() -> None:
    for s in (MKV_COPY_SAFE, CONVERT_TO_SRT, UNSUPPORTED):
        for codec in s:
            assert codec == codec.lower(), f"Codec non-minuscule : {codec}"
