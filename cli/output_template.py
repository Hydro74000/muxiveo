"""Output filename template rendering for the CLI.

Templates mix metadata placeholders (TMDB/source name) with track-aware
keywords computed from the final remux track list.
"""

from __future__ import annotations

import re
import string
import unicodedata
from pathlib import Path
from typing import Any

from core.media_info_fetcher import MediaDetails
from core.profiles import keywords as keyword_registry
from core.workflows.remux_models import TrackEntry


_FORBIDDEN_FS_CHARS = re.compile(r"[/\\:*?\"<>|]")
_TRAILING_GROUP_RE = re.compile(r"[-.]([A-Za-z0-9]{2,})$")
_KNOWN_VIDEO_EXTS = frozenset({".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".mka", ".m4a", ".ts", ".m2ts"})
_FORMATTER = string.Formatter()
_TRACK_MODES = {"best", "first", "all"}

_AUDIO_CODEC_RANK = (
    "truehd",
    "dtsx",
    "dtshd",
    "dtshdhra",
    "dts",
    "eac3",
    "ac3",
    "aac",
    "flac",
    "lpcm",
    "mp3",
    "ape",
)
_AUDIO_CODEC_RELEASE_LABEL = {
    "truehd": "TrueHD",
    "dtsx": "DTS-X",
    "dtshd": "DTSHD-MA",
    "dtshdhra": "DTSHD-HRA",
    "dts": "DTS",
    "eac3": "DDP",
    "ac3": "AC3",
    "aac": "AAC",
    "flac": "FLAC",
    "lpcm": "PCM",
    "mp3": "MP3",
    "ape": "APE",
}
_VIDEO_CODEC_RELEASE_LABEL = {
    "hevc": "x265",
    "h265": "x265",
    "h.265": "x265",
    "x265": "x265",
    "avc": "x264",
    "h264": "x264",
    "h.264": "x264",
    "x264": "x264",
    "av1": "AV1",
    "vp9": "VP9",
    "xvid": "Xvid",
    "divx": "DivX",
    "mpeg": "MPEG",
}
_VIDEO_SOURCE_PATTERNS = (
    (re.compile(r"(?<![A-Za-z0-9])(?:blu[\.\-]?ray|bdrip|brrip)(?![A-Za-z0-9])", re.I), "BluRay"),
    (re.compile(r"(?<![A-Za-z0-9])web[\.\-]?(?:dl|rip)?(?![A-Za-z0-9])", re.I), "WEB"),
    (re.compile(r"(?<![A-Za-z0-9])hdtv(?![A-Za-z0-9])", re.I), "HDTV"),
    (re.compile(r"(?<![A-Za-z0-9])tvrip(?![A-Za-z0-9])", re.I), "TVRip"),
    (re.compile(r"(?<![A-Za-z0-9])(?:dvdrip|dvd)(?![A-Za-z0-9])", re.I), "DVD"),
)


def sanitize_token(value: str) -> str:
    """Remplace les caractères interdits filesystem par '.'."""
    cleaned = _FORBIDDEN_FS_CHARS.sub(".", str(value or ""))
    return cleaned.strip()


def sanitize_release_title(value: str) -> str:
    """Normalize a title to scene-style dotted ASCII text."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^\w\s.\-]", "", text, flags=re.ASCII)
    text = re.sub(r"[\s\-]+", ".", text.strip())
    text = re.sub(r"\.{2,}", ".", text)
    return text.strip(".")


def extract_release_group(stem: str) -> str:
    """Extrait le tag de scene-group en fin de nom (ex: 'RARBG', 'NTb')."""
    match = _TRAILING_GROUP_RE.search((stem or "").strip())
    return match.group(1) if match else ""


def build_output_context(
    source_path: Path,
    details: MediaDetails | None,
    *,
    tracks: list[TrackEntry] | None = None,
    track_order: list[tuple[int, int, str]] | None = None,
    output_all: bool = False,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
        "__source_path__": source_path,
        "__tracks__": tracks or [],
        "__track_order__": track_order or [],
        "__output_all__": bool(output_all),
        "__variables__": variables if isinstance(variables, dict) else {},
    }


def render_output_template(
    template: str,
    context: dict[str, Any],
    *,
    default_ext: str = ".mkv",
) -> str:
    """Rend le template avec context (clés manquantes -> '') et ajoute .mkv au besoin."""
    rendered_parts: list[str] = []
    for literal, field_name, format_spec, conversion in _FORMATTER.parse(str(template or "")):
        rendered_parts.append(literal)
        if field_name is None:
            continue
        rendered_parts.append(_render_field(field_name, format_spec or "", conversion, context))
    rendered = _clean_rendered_template("".join(rendered_parts))
    if Path(rendered).suffix.lower() not in _KNOWN_VIDEO_EXTS:
        rendered = rendered + default_ext
    return rendered


def _render_field(field_name: str, format_spec: str, conversion: str | None, context: dict[str, Any]) -> str:
    key = str(field_name or "").strip()
    if not key:
        return ""
    track_value = _render_track_keyword(key, format_spec, context)
    if track_value is not None:
        return sanitize_token(track_value)
    if key not in context:
        return ""
    value = context.get(key)
    if key == "title" and format_spec == "release":
        return sanitize_release_title(str(value or ""))
    if conversion:
        value = _FORMATTER.convert_field(value, conversion)
    if format_spec:
        try:
            return sanitize_token(_FORMATTER.format_field(value, format_spec))
        except (TypeError, ValueError):
            return ""
    return sanitize_token(str(value or ""))


def _render_track_keyword(field_name: str, format_spec: str, context: dict[str, Any]) -> str | None:
    parsed = _parse_track_keyword(field_name)
    if parsed is None:
        return None
    group, keyword = parsed
    tracks = _ordered_final_tracks(context)
    variables = context.get("__variables__")
    if not isinstance(variables, dict):
        variables = {}
    audio_tracks = [track for track in tracks if track.track_type == "audio"]
    group_tracks = [track for track in tracks if _track_group(track) == group]
    mode = "all" if context.get("__output_all__") else (format_spec if format_spec in _TRACK_MODES else "best")

    if group == "audio" and keyword == "multi":
        return "MULTi" if len(_audio_language_families(audio_tracks)) > 1 else ""
    if group == "audio" and keyword == "fr-tag":
        return _audio_fr_tag(audio_tracks)
    if group == "sub" and keyword == "vostfr":
        return _subtitle_vostfr(audio_tracks, [track for track in tracks if track.track_type == "subtitle"])
    if group == "audio" and keyword == "immersive":
        return _all_or_any_label(group_tracks, mode, _audio_immersive_label, keyword="audio_object", variables=variables)
    if group == "video" and keyword == "source":
        return _video_source_from_name(Path(context.get("__source_path__", "")))
    if group == "video" and keyword == "10bit":
        return _all_or_any_label(group_tracks, mode, _video_10bit_label, keyword="video_10bit", variables=variables)
    if group == "video" and keyword == "dolby-vision":
        return _all_or_any_label(group_tracks, mode, _video_dolby_label, keyword="video_dolby_vision", variables=variables)
    if keyword == "codec-release":
        if group == "audio":
            return _render_ranked_track_value(group_tracks, mode, _audio_codec_score, _audio_codec_release_label, keyword="codec_release", variables=variables)
        if group == "video":
            return _render_ranked_track_value(group_tracks, mode, _video_codec_score, _video_codec_release_label, keyword="codec_release", variables=variables)
    if group == "audio" and keyword == "channels":
        return _render_ranked_track_value(group_tracks, mode, _audio_channels_score, _audio_channels_label, keyword="channels", variables=variables)
    if group == "video" and keyword == "resolution":
        return _render_ranked_track_value(group_tracks, mode, _video_resolution_score, keyword_registry.video_release_resolution, keyword="resolution", variables=variables)
    if group == "video" and keyword == "hdr":
        return _render_ranked_track_value(group_tracks, mode, _video_hdr_score, _video_hdr_release_label, keyword="hdr", variables=variables)
    if group == "audio" and keyword == "codec":
        return _render_ranked_track_value(group_tracks, mode, _audio_codec_score, lambda track: str(track.codec or "").upper(), keyword="codec", variables=variables)
    if group == "video" and keyword == "codec":
        return _render_ranked_track_value(group_tracks, mode, _video_codec_score, lambda track: str(track.codec or "").upper(), keyword="codec", variables=variables)
    return _render_generic_track_value(group_tracks, keyword, mode, variables)


def _parse_track_keyword(field_name: str) -> tuple[str, str] | None:
    normalized = str(field_name or "").strip().replace("_", "-").lower()
    for prefix, group in (("subtitle-", "sub"), ("sub-", "sub"), ("audio-", "audio"), ("video-", "video")):
        if normalized.startswith(prefix):
            keyword = normalized[len(prefix):].strip("-")
            return (group, keyword) if keyword else None
    return None


def _track_group(track: TrackEntry) -> str:
    if track.track_type == "subtitle":
        return "sub"
    return track.track_type


def _ordered_final_tracks(context: dict[str, Any]) -> list[TrackEntry]:
    raw_tracks = [track for track in context.get("__tracks__", []) if isinstance(track, TrackEntry) and track.enabled]
    if not raw_tracks:
        return []
    by_entry_id = {track.entry_id: track for track in raw_tracks}
    by_key = {(keyword_registry.source_index_for_track(track), int(track.mkv_tid)): track for track in raw_tracks}
    ordered: list[TrackEntry] = []
    seen: set[str] = set()
    for item in context.get("__track_order__", []) or []:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        entry_id = str(item[2]) if len(item) > 2 else ""
        track = by_entry_id.get(entry_id) if entry_id else None
        if track is None:
            track = by_key.get((int(item[0]), int(item[1])))
        if track is not None and track.entry_id not in seen:
            ordered.append(track)
            seen.add(track.entry_id)
    remaining = [track for track in raw_tracks if track.entry_id not in seen]
    remaining.sort(key=lambda track: (keyword_registry.source_index_for_track(track) or 0, int(track.mkv_tid), track.entry_id))
    ordered.extend(remaining)
    return ordered


def _render_generic_track_value(tracks: list[TrackEntry], keyword: str, mode: str, variables: dict[str, Any]) -> str:
    field = keyword.replace("-", "_")

    def value_for(track: TrackEntry) -> str:
        values = keyword_registry.track_field_values(track, variables=variables)
        rendered = _stringify_track_value(field, values.get(field, ""))
        return keyword_registry.render_alias(field, rendered, variables)

    if mode == "all":
        return _join_unique(value_for(track) for track in tracks)
    if mode == "first":
        return next((value for value in (value_for(track) for track in tracks) if value), "")
    return next((value for value in (value_for(track) for track in tracks) if value), "")


def _stringify_track_value(field: str, value: Any) -> str:
    if isinstance(value, bool):
        return _bool_keyword_label(field) if value else ""
    if isinstance(value, list):
        return _join_unique(str(item) for item in value)
    return str(value or "")


def _bool_keyword_label(field: str) -> str:
    labels = {
        "atmos": "Atmos",
        "codec_atmos": "Atmos",
        "dtsx": "DTS-X",
        "codec_dtsx": "DTS-X",
        "video_hdr": "HDR",
        "video_hdr10": "HDR",
        "video_hdr10plus": "HDR10P",
        "video_dolby_vision": "DV",
        "video_hlg": "HLG",
        "video_sdr": "SDR",
        "flag_enabled": "Enabled",
        "flag_default": "Default",
        "flag_forced": "Forced",
        "flag_hearing_impaired": "Hearing.Impaired",
        "flag_visual_impaired": "Visual.Impaired",
        "flag_original": "Original",
        "flag_commentary": "Commentary",
    }
    return labels.get(field, field.replace("_", ".").title())


def _join_unique(values: Any) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return "+".join(out)


def _all_or_any_label(
    tracks: list[TrackEntry],
    mode: str,
    label_func: Any,
    *,
    keyword: str,
    variables: dict[str, Any],
) -> str:
    labels = [keyword_registry.render_alias(keyword, label_func(track), variables) for track in tracks]
    if mode == "all":
        return _join_unique(labels)
    return next((label for label in labels if label), "")


def _render_ranked_track_value(
    tracks: list[TrackEntry],
    mode: str,
    score_func: Any,
    label_func: Any,
    *,
    keyword: str,
    variables: dict[str, Any],
) -> str:
    candidates = [(score_func(track), track, keyword_registry.render_alias(keyword, label_func(track), variables)) for track in tracks]
    candidates = [(score, track, label) for score, track, label in candidates if score > 0 and str(label or "").strip()]
    if mode == "all":
        return _join_unique(label for _score, _track, label in candidates)
    if mode == "first":
        return next((label for _score, _track, label in candidates if label), "")
    if not candidates:
        return ""
    _score, _track, label = max(candidates, key=lambda item: item[0])
    return str(label or "")


def _audio_language_families(tracks: list[TrackEntry]) -> set[str]:
    families = set()
    for track in tracks:
        lang = keyword_registry.normalize_lang(track.language, track.title)
        base = lang.split("-", 1)[0].lower()
        if base:
            families.add(base)
    return families


def _audio_fr_tag(tracks: list[TrackEntry]) -> str:
    langs = {keyword_registry.normalize_lang(track.language, track.title).lower() for track in tracks}
    has_vff = any(lang in {"fr", "fr-fr"} for lang in langs)
    has_vfq = "fr-ca" in langs
    if has_vff and has_vfq:
        return "VF2"
    if has_vfq:
        return "VFQ"
    if has_vff:
        return "VFF"
    return ""


def _subtitle_vostfr(audio_tracks: list[TrackEntry], subtitle_tracks: list[TrackEntry]) -> str:
    has_fr_audio = any(keyword_registry.normalize_lang(track.language, track.title).lower().split("-", 1)[0] == "fr" for track in audio_tracks)
    has_fr_sub = any(keyword_registry.normalize_lang(track.language, track.title).lower().split("-", 1)[0] == "fr" for track in subtitle_tracks)
    return "VOSTFR" if has_fr_sub and not has_fr_audio else ""


def _audio_codec_key(track: TrackEntry) -> str:
    codec = str(track.codec or track.orig_codec or "").lower()
    blob = " ".join(str(part or "").lower() for part in (track.codec, track.orig_codec, track.display_info, track.orig_display_info, track.title, track.orig_title))
    if "truehd" in blob or "mlp fba" in blob:
        return "truehd"
    if _audio_immersive_label(track) == "Atmos" and "dts" in blob:
        return "dtsx"
    if "dts-hd hra" in blob or "dts hd hra" in blob or "hra" in blob:
        return "dtshdhra"
    if "dts-hd ma" in blob or "dts hd ma" in blob or "dts-hd" in blob or "dts hd" in blob or "xll" in blob:
        return "dtshd"
    if codec == "dts" or " dts " in f" {blob} ":
        return "dts"
    if codec in {"eac3", "e-ac-3"} or "e-ac-3" in blob or "eac3" in blob:
        return "eac3"
    if codec in {"ac3", "ac-3"} or "ac-3" in blob:
        return "ac3"
    if codec in {"aac", "flac", "mp3", "ape"}:
        return codec
    if codec in {"pcm", "lpcm", "wav"}:
        return "lpcm"
    return codec


def _audio_codec_score(track: TrackEntry) -> int:
    key = _audio_codec_key(track)
    try:
        return len(_AUDIO_CODEC_RANK) - _AUDIO_CODEC_RANK.index(key)
    except ValueError:
        return 0


def _audio_codec_release_label(track: TrackEntry) -> str:
    key = _audio_codec_key(track)
    return _AUDIO_CODEC_RELEASE_LABEL.get(key, str(track.codec or "").upper())


def _audio_channels_label(track: TrackEntry) -> str:
    channels = keyword_registry.channels_from_display(track.orig_display_info or track.display_info)
    lower = channels.lower()
    if lower == "mono":
        return "1.0"
    if lower == "stereo":
        return "2.0"
    return channels


def _audio_channels_score(track: TrackEntry) -> int:
    label = _audio_channels_label(track)
    if label == "7.1":
        return 8
    if label == "5.1":
        return 6
    if label == "2.0":
        return 2
    if label == "1.0":
        return 1
    try:
        return int(float(label) * 10)
    except ValueError:
        return 0


def _audio_immersive_label(track: TrackEntry) -> str:
    audio_object = keyword_registry.audio_object_from_display(track.orig_display_info or track.display_info)
    return "Atmos" if audio_object in {"Atmos", "DTS:X"} else ""


def _video_codec_key(track: TrackEntry) -> str:
    codec = str(track.codec or track.orig_codec or "").strip().lower()
    return codec.replace("_", "-")


def _video_codec_score(track: TrackEntry) -> int:
    label = _video_codec_release_label(track)
    order = ["x265", "x264", "AV1", "VP9", "Xvid", "DivX", "MPEG"]
    try:
        return len(order) - order.index(label)
    except ValueError:
        return 1 if label else 0


def _video_codec_release_label(track: TrackEntry) -> str:
    key = _video_codec_key(track)
    return _VIDEO_CODEC_RELEASE_LABEL.get(key, str(track.codec or "").upper())


def _video_resolution_score(track: TrackEntry) -> int:
    resolution = keyword_registry.video_resolution(track)
    return resolution[0] * resolution[1] if resolution else 0


def _video_hdr_score(track: TrackEntry) -> int:
    flags = keyword_registry.video_characteristic_flags(track)
    if flags & keyword_registry.VIDEO_FLAG_HDR10PLUS:
        return 40
    if flags & keyword_registry.VIDEO_FLAG_HDR10:
        return 30
    if flags & keyword_registry.VIDEO_FLAG_HLG:
        return 20
    if flags & keyword_registry.VIDEO_FLAG_HDR:
        return 10
    return 0


def _video_hdr_release_label(track: TrackEntry) -> str:
    flags = keyword_registry.video_characteristic_flags(track)
    if flags & keyword_registry.VIDEO_FLAG_HDR10PLUS:
        return "HDR10P"
    if flags & keyword_registry.VIDEO_FLAG_HDR10:
        return "HDR"
    if flags & keyword_registry.VIDEO_FLAG_HLG:
        return "HLG"
    return ""


def _video_10bit_label(track: TrackEntry) -> str:
    flags = keyword_registry.video_characteristic_flags(track)
    if flags & (keyword_registry.VIDEO_FLAG_BIT_DEPTH_10 | keyword_registry.VIDEO_FLAG_BIT_DEPTH_12):
        return "10Bits"
    if flags & (keyword_registry.VIDEO_FLAG_HDR10PLUS | keyword_registry.VIDEO_FLAG_DOLBY_VISION):
        return "10Bits"
    return ""


def _video_dolby_label(track: TrackEntry) -> str:
    flags = keyword_registry.video_characteristic_flags(track)
    return "DV" if flags & keyword_registry.VIDEO_FLAG_DOLBY_VISION else ""


def _video_source_from_name(source_path: Path) -> str:
    stem = str(source_path.stem if source_path else "")
    for pattern, label in _VIDEO_SOURCE_PATTERNS:
        if pattern.search(stem):
            return label
    return ""


def _clean_rendered_template(value: str) -> str:
    parts = re.split(r"([/\\])", str(value or ""))
    cleaned_parts: list[str] = []
    for part in parts:
        if part in {"/", "\\"}:
            cleaned_parts.append(part)
            continue
        cleaned_parts.append(_clean_path_component(part))
    return "".join(cleaned_parts).strip()


def _clean_path_component(component: str) -> str:
    text = str(component or "")
    if text in {".", ".."}:
        return text
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"\.-", "-", text)
    text = re.sub(r"-\.", "-", text)
    text = re.sub(r"\s+", " ", text)
    if "." in text:
        stem, dot, suffix = text.rpartition(".")
        stem = stem.strip(" ._-")
        suffix = suffix.strip(" ._-")
        return f"{stem}{dot}{suffix}" if suffix else stem
    return text.strip(" ._-")
