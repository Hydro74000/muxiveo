"""Output filename template rendering for the CLI.

Permet de composer le nom du fichier de sortie avec des placeholders
peuplés par TMDB et par parsing du nom de fichier source.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.media_info_fetcher import MediaDetails


_FORBIDDEN_FS_CHARS = re.compile(r"[/\\:*?\"<>|]")

_TRAILING_GROUP_RE = re.compile(r"[-.]([A-Za-z0-9]{2,})$")

_KNOWN_VIDEO_EXTS = frozenset({".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".mka", ".m4a", ".ts", ".m2ts"})


def sanitize_token(value: str) -> str:
    """Remplace les caractères interdits filesystem par '.'."""
    cleaned = _FORBIDDEN_FS_CHARS.sub(".", str(value or ""))
    return cleaned.strip()


def extract_release_group(stem: str) -> str:
    """Extrait le tag de scene-group en fin de nom (ex: 'RARBG', 'NTb')."""
    match = _TRAILING_GROUP_RE.search((stem or "").strip())
    return match.group(1) if match else ""


def build_output_context(source_path: Path, details: MediaDetails | None) -> dict[str, Any]:
    """Construit le dictionnaire de placeholders pour render_output_template."""
    season_raw = (details.season if details else "") or ""
    episode_raw = (details.episode if details else "") or ""
    season_int = int(season_raw) if season_raw.isdigit() else 0
    episode_int = int(episode_raw) if episode_raw.isdigit() else 0
    season_pad = f"{season_int:02d}" if season_int > 0 else ""
    episode_pad = f"{episode_int:02d}" if episode_int > 0 else ""
    se_code = details._season_episode_code() if details else ""
    return {
        "source_name": sanitize_token(source_path.stem),
        "title": sanitize_token(details.title if details else ""),
        "year": sanitize_token(details.year if details else ""),
        "episode_title": sanitize_token(details.episode_title if details else ""),
        "season": season_pad,
        "episode": episode_pad,
        "season_num": season_int,
        "episode_num": episode_int,
        "season_episode": se_code,
        "group": sanitize_token(extract_release_group(source_path.stem)),
    }


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def render_output_template(
    template: str,
    context: dict[str, Any],
    *,
    default_ext: str = ".mkv",
) -> str:
    """Rend le template avec context (clés manquantes → '') et ajoute .mkv au besoin."""
    rendered = str(template or "").format_map(_SafeDict(context))
    if Path(rendered).suffix.lower() not in _KNOWN_VIDEO_EXTS:
        rendered = rendered + default_ext
    return rendered
