"""Appariement déterministe des saisons, sans dépendance à l'interface."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

# Export des composants multi-sources matriciels
from core.workflows.hybrid_matrix import (
    HybridMatrix,
    HybridRecipe,
    MatrixEpisode,
    MatrixSource,
    SourceRole,
    parse_episode_key,
    prepare_matrix_episode,
)

# S01E02, S01.E02, S01 E02, S01_E02 ; 1x02. Plusieurs épisodes dans un nom : refusé (ambigu).
_EPISODE = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,3})[\s._-]?e(\d{1,3})(?!\d)")
_EPISODE_X = re.compile(r"(?i)(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?![a-z0-9])")
# Second marqueur explicite (E02, 1x02) : suffixe accolé admis (« E02v2 », « E02REPACK ») ;
# numéro nu : seul un suffixe de version, « S01E02-1080p » / « 1x02-2160p » restent un épisode.
_MULTI_EPISODE = re.compile(
    r"(?i)s\d+[\s._-]?e\d+(?:[\s._+-]?e\d{1,3}(?!\d)|[-+]\d{1,3}(?:v\d{1,2})?(?![a-z0-9]))"
    r"|(?<![a-z0-9])\d{1,2}x\d{2,3}[-+](?:\d{1,2}x\d{2,3}(?!\d)|\d{2,3}(?:v\d{1,2})?(?![a-z0-9]))"
)
_VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm"}


@dataclass(frozen=True)
class HybridPair:
    reference: Path
    donor: Path
    season: int = 0
    episode: int = 0


def episode_key(path):
    stem = Path(path).stem
    matches = [*_EPISODE.finditer(stem), *_EPISODE_X.finditer(stem)]
    if len(matches) != 1 or _MULTI_EPISODE.search(stem):
        raise ValueError(f"Épisode absent ou ambigu : {Path(path).name}")
    return tuple(map(int, matches[0].groups()))


def pair_directories(reference: Path, donor: Path) -> list[HybridPair]:
    def index(directory):
        if not directory.is_dir():
            raise ValueError(f"Dossier introuvable : {directory}")
        result = {}
        for path in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
            if not path.is_file() or path.suffix.lower() not in _VIDEO_EXTENSIONS:
                continue
            key = episode_key(path)
            if key in result:
                raise ValueError(f"Épisode en double : {path.name}")
            result[key] = path.resolve()
        return result
    refs, donors = index(reference), index(donor)
    if not refs or refs.keys() != donors.keys():
        missing = sorted(refs.keys() ^ donors.keys())
        raise ValueError(f"Appariement incomplet : {missing}")
    return [HybridPair(refs[key], donors[key], *key) for key in sorted(refs)]


__all__ = [
    "HybridPair",
    "episode_key",
    "pair_directories",
    "HybridMatrix",
    "HybridRecipe",
    "MatrixEpisode",
    "MatrixSource",
    "SourceRole",
    "parse_episode_key",
    "prepare_matrix_episode",
]
