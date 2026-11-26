"""Chapitres de Film 2 projetés sur Film 1 par la carte d'alignement (plan § 4.2).

Temps de Film 2 → image de Film 2 (PTS de sa table) → image de Film 1 (carte) → temps de Film 1. Début dans une
plage retirée : première image conservée qui suit ; aucune après (plage finale) : chapitre supprimé ; fin
bornée à la fin de la dernière image conservée ; chapitre vide ou inversé après projection : supprimé.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from fractions import Fraction

from core.i18n import translate_text

from .align import AlignmentMap
from .frames import FrameTable


@dataclass(frozen=True)
class ProjectedChapter:
    """Chapitre réécrit (attributs lus par ``core.matroska.writer.build_chapters_element``)."""

    timecode_s: float
    end_s: float | None
    name: str
    language: str
    uid: int
    displays: tuple[tuple[str, str], ...]
    children: tuple["ProjectedChapter", ...] = ()
    #: Drapeaux simples recopiés (décision DD) : ChapterFlagHidden / ChapterFlagEnabled.
    hidden: bool = False
    enabled: bool = True


def unsupported_reason(editions: tuple) -> str | None:
    """Motif de non-import des chapitres de Film 2 (décision DD), ``None`` s'ils sont projetables : une seule
    édition, non ordonnée, sans chapitre lié à un autre segment ni ciblant des pistes (ces références ne
    survivent pas à la projection sur Film 1)."""
    if len(editions) != 1:
        return translate_text("plusieurs éditions")
    edition = editions[0]
    if getattr(edition, "ordered", False):
        return translate_text("édition ordonnée")

    def walk(chapters: tuple) -> str | None:
        for chapter in chapters:
            if getattr(chapter, "segment_uid", None) is not None or getattr(chapter, "segment_edition_uid", None):
                return translate_text("chapitres liés à un autre segment")
            if getattr(chapter, "tracks", ()):
                return translate_text("chapitres ciblant des pistes (ChapterTrack)")
            if (reason := walk(getattr(chapter, "children", ()))) is not None:
                return reason
        return None

    return walk(edition.chapters)


def edition_flags(editions: tuple) -> tuple[bool, bool]:
    """(EditionFlagHidden, EditionFlagDefault) de l'édition projetée."""
    edition = editions[0] if editions else None
    return bool(getattr(edition, "hidden", False)), bool(getattr(edition, "default", False))


def _seconds(table: FrameTable) -> list[Fraction]:
    return [Fraction(value) * table.props.time_base for value in table.pts]


def _film1_frame(alignment: AlignmentMap, j: int) -> int | None:
    """Image de Film 1 montrant l'image ``j`` de Film 2, ou la première conservée qui la suit ; None sinon."""
    for seg in alignment.segments:
        if seg.film2_start <= j < seg.film2_end:
            return j - seg.offset
        if seg.film2_start > j:
            return seg.film1_start
    return None


def project_chapters(editions: tuple, alignment: AlignmentMap, film1: FrameTable, film2: FrameTable, *,
                     shift_s: float = 0.0) -> tuple[tuple[ProjectedChapter, ...] | None, int]:
    """(chapitres projetés de l'édition unique, nombre de chapitres supprimés) ; (None, 0) si la projection est
    impossible (plusieurs éditions, film non indexable). ``shift_s`` : décalage appliqué à la vidéo de la
    sortie (canonicalisation), reporté sur les débuts et fins, enfants compris."""
    if len(editions) != 1 or not film1.indexable or not film2.indexable or not film1.pts or not film2.pts:
        return None, 0
    times2 = _seconds(film2)
    times1 = _seconds(film1)
    end_of_film1 = times1[-1] + Fraction(film1.frame_interval) * film1.props.time_base
    dropped = 0

    def to_film1(seconds: Fraction, *, end: bool) -> Fraction | None:
        if end:
            j = bisect.bisect_left(times2, seconds)          # première image qui commence à la fin ou après
            if j >= len(times2):
                return end_of_film1
        else:
            j = max(0, bisect.bisect_right(times2, seconds) - 1)   # image affichée à cet instant
        i = _film1_frame(alignment, j)
        if i is None:
            return end_of_film1 if end else None
        return times1[i] if i < len(times1) else end_of_film1

    def project(chapter) -> ProjectedChapter | None:
        nonlocal dropped
        start = to_film1(Fraction(int(chapter.start_ns), 1_000_000_000), end=False)
        if start is None:
            dropped += 1
            return None
        end = None
        if chapter.end_ns is not None:
            end = to_film1(Fraction(int(chapter.end_ns), 1_000_000_000), end=True)
            if end is None or end <= start:
                dropped += 1
                return None
        children = tuple(child for child in (project(sub) for sub in chapter.children) if child is not None)
        name, language = (chapter.displays[0] if chapter.displays else ("", "und"))
        return ProjectedChapter(max(0.0, float(start) + shift_s),
                                max(0.0, float(end) + shift_s) if end is not None else None, name, language,
                                int(chapter.uid), tuple(chapter.displays), children,
                                bool(getattr(chapter, "hidden", False)), bool(getattr(chapter, "enabled", True)))

    projected = tuple(chapter for chapter in (project(c) for c in editions[0].chapters) if chapter is not None)
    return projected, dropped
