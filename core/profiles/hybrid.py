"""Hybrid GUI/CLI profile primitives.

This module intentionally lives in ``core`` so both the GUI and the headless CLI
can share the same selector semantics.  Selectors describe tracks with stable
media characteristics instead of transient UI ids.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from core.lang_tags import Rfc5646LanguageTags
from core.workflows.remux_models import RemuxConfig, TrackEntry, clone_track_entry


FLAG_NAMES = (
    "enabled",
    "default",
    "forced",
    "hearing_impaired",
    "visual_impaired",
    "original",
    "commentary",
)

VIDEO_FLAG_RES_SD = 0x00000001
VIDEO_FLAG_RES_HD = 0x00000002
VIDEO_FLAG_RES_FHD = 0x00000004
VIDEO_FLAG_RES_UHD = 0x00000008
VIDEO_FLAG_HDR = 0x00000010
VIDEO_FLAG_HDR10 = 0x00000020
VIDEO_FLAG_HDR10PLUS = 0x00000040
VIDEO_FLAG_DOLBY_VISION = 0x00000080
VIDEO_FLAG_HLG = 0x00000100
VIDEO_FLAG_SDR = 0x00000200
VIDEO_FLAG_BIT_DEPTH_8 = 0x00001000
VIDEO_FLAG_BIT_DEPTH_10 = 0x00002000
VIDEO_FLAG_BIT_DEPTH_12 = 0x00004000

VIDEO_RESOLUTION_MASK = (
    VIDEO_FLAG_RES_SD
    | VIDEO_FLAG_RES_HD
    | VIDEO_FLAG_RES_FHD
    | VIDEO_FLAG_RES_UHD
)
VIDEO_HDR_MASK = (
    VIDEO_FLAG_HDR
    | VIDEO_FLAG_HDR10
    | VIDEO_FLAG_HDR10PLUS
    | VIDEO_FLAG_DOLBY_VISION
    | VIDEO_FLAG_HLG
    | VIDEO_FLAG_SDR
)
VIDEO_BIT_DEPTH_MASK = VIDEO_FLAG_BIT_DEPTH_8 | VIDEO_FLAG_BIT_DEPTH_10 | VIDEO_FLAG_BIT_DEPTH_12
VIDEO_FLAG_HEX_WIDTH = 8


class HybridResolutionError(RuntimeError):
    """Raised when an exact hybrid template cannot resolve a stable selector."""

    def __init__(
        self,
        message: str,
        *,
        report: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.report = report or {"valid": False, "errors": [message]}


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _safe_profile_filename(name: str) -> str:
    safe = re.sub(r"[^\w\-]+", "_", str(name or "").strip())
    return safe.strip("_") or "profile"


class HybridProfileManager:
    """JSON persistence for hybrid decision profiles."""

    def __init__(self, profiles_dir: Path) -> None:
        self._dir = Path(profiles_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._dir

    def path_for_name(self, name: str) -> Path:
        return self._dir / f"{_safe_profile_filename(name)}.json"

    def save(self, profile: Mapping[str, Any]) -> Path:
        data = dict(profile)
        name = str(data.get("name") or data.get("profile_name") or "").strip()
        if not name:
            raise ValueError("Hybrid profile requires a non-empty name.")
        data.setdefault("version", 2)
        data.setdefault("kind", "hybrid-profile")
        if data.get("kind") == "hybrid-profile" and data.get("profile_mode") == "decision":
            data.pop("sources", None)
            data.pop("output", None)
        data["name"] = name
        path = self.path_for_name(name)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )
        return path

    def load(self, name: str) -> dict[str, Any] | None:
        path = self.path_for_name(name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def load_all(self) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                profiles.append(data)
        return profiles

    def names(self) -> list[str]:
        names: list[str] = []
        for profile in self.load_all():
            name = str(profile.get("name") or "").strip()
            if name:
                names.append(name)
        return names

    def delete(self, name: str) -> None:
        self.path_for_name(name).unlink(missing_ok=True)


def normalize_lang(tag: str | None, title: str | None = None) -> str:
    if not tag:
        return ""
    regionalized = Rfc5646LanguageTags.regionalize_track_language(str(tag), title)
    if regionalized:
        return regionalized
    canonical = Rfc5646LanguageTags.normalize(str(tag))
    return canonical or str(tag).strip()


def _channels_from_display(display_info: str) -> str:
    text = str(display_info or "")
    match = re.search(r"\b(?:mono|stereo|[1-9](?:\.[0-9])?)\b", text, flags=re.IGNORECASE)
    return match.group(0) if match else ""


def _audio_object_from_display(display_info: str) -> str:
    text = str(display_info or "").lower()
    if "atmos" in text:
        return "Atmos"
    if "dts:x" in text or "dtsx" in text:
        return "DTS:X"
    return ""


def _source_index_for_track(
    track: TrackEntry,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> int | None:
    if source_index_by_file_id and track.file_id in source_index_by_file_id:
        return int(source_index_by_file_id[track.file_id])
    file_id = str(track.file_id or "")
    if file_id.startswith("src"):
        try:
            return int(file_id[3:])
        except ValueError:
            return None
    return None


def _flag_value(track: TrackEntry, name: str, *, original: bool = True) -> bool:
    attr = f"{'orig_' if original else ''}flag_{name}"
    if name == "enabled":
        attr = f"{'orig_' if original else ''}flag_enabled"
    if not hasattr(track, attr):
        attr = f"flag_{name}"
    return bool(getattr(track, attr, False))


def _track_position(
    track: TrackEntry,
    tracks: list[TrackEntry],
    *,
    source_index: int | None,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> int:
    position = 0
    for candidate in tracks:
        candidate_source = _source_index_for_track(candidate, source_index_by_file_id)
        if candidate is track:
            return position
        if candidate.track_type == track.track_type and candidate_source == source_index:
            position += 1
    return position


def track_summary(
    track: TrackEntry,
    *,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    summary = {
        "source": _source_index_for_track(track, source_index_by_file_id),
        "id": track.mkv_tid,
        "type": track.track_type,
        "codec": track.codec,
        "language": normalize_lang(track.language, track.title),
        "title": track.title,
        "display_info": track.display_info,
        "flags": {name: _flag_value(track, name, original=False) for name in FLAG_NAMES},
    }
    if track.track_type == "video":
        resolution = _video_resolution_payload(track)
        if resolution:
            summary["resolution"] = resolution
        summary["video_flags_hex"] = _video_characteristic_flags_hex(track)
    return summary


def track_selector_for_entry(
    track: TrackEntry,
    *,
    source_index: int | None = None,
    tracks: list[TrackEntry] | None = None,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    resolved_source = (
        source_index
        if source_index is not None
        else _source_index_for_track(track, source_index_by_file_id)
    )
    all_tracks = tracks or [track]
    selector: dict[str, Any] = {
        "source": resolved_source,
        "type": track.track_type,
        "position": _track_position(
            track,
            all_tracks,
            source_index=resolved_source,
            source_index_by_file_id=source_index_by_file_id,
        ),
        "codec": track.orig_codec or track.codec,
        "language": normalize_lang(track.orig_language or track.language, track.orig_title or track.title),
    }
    channels = _channels_from_display(track.orig_display_info or track.display_info)
    if channels:
        selector["channels"] = channels
    audio_object = _audio_object_from_display(track.orig_display_info or track.display_info)
    if audio_object:
        selector["audio_object"] = audio_object
    if track.orig_title:
        selector["title"] = track.orig_title
    selector["flags"] = {
        name: _flag_value(track, name, original=True)
        for name in FLAG_NAMES
        if name != "enabled" and _flag_value(track, name, original=True)
    }
    return {key: value for key, value in selector.items() if value not in (None, "", {})}


def _selector_source(selector: Mapping[str, Any]) -> int | None:
    raw = selector.get("source", selector.get("source_index"))
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _selector_languages(selector: Mapping[str, Any]) -> set[str]:
    raw = selector.get("languages")
    if isinstance(raw, list):
        return {normalize_lang(str(lang)) for lang in raw if str(lang).strip()}
    raw_lang = selector.get("language", selector.get("lang"))
    if raw_lang is None:
        return set()
    return {normalize_lang(str(raw_lang))}


def _selector_codecs(selector: Mapping[str, Any]) -> set[str]:
    raw = selector.get("codecs")
    if isinstance(raw, list):
        return {str(codec).strip().upper() for codec in raw if str(codec).strip()}
    raw_codec = selector.get("codec")
    if raw_codec is None:
        return set()
    return {str(raw_codec).strip().upper()}


def _matches_selector(
    track: TrackEntry,
    selector: Mapping[str, Any],
    tracks: list[TrackEntry],
    *,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> bool:
    source = _selector_source(selector)
    track_source = _source_index_for_track(track, source_index_by_file_id)
    if source is not None and track_source != source:
        return False

    track_type = selector.get("type", selector.get("track_type"))
    if track_type and str(track_type) != track.track_type:
        return False

    if "position" in selector or "type_index" in selector:
        raw_position = selector.get("position", selector.get("type_index"))
        try:
            expected_position = int(raw_position)
        except (TypeError, ValueError):
            expected_position = -1
        position = _track_position(
            track,
            tracks,
            source_index=track_source,
            source_index_by_file_id=source_index_by_file_id,
        )
        if position != expected_position:
            return False

    raw_id = selector.get("id", selector.get("mkv_tid", selector.get("stream")))
    if raw_id is not None:
        try:
            if int(raw_id) != int(track.mkv_tid):
                return False
        except (TypeError, ValueError):
            return False

    codecs = _selector_codecs(selector)
    if codecs and str(track.orig_codec or track.codec).upper() not in codecs and track.codec.upper() not in codecs:
        return False

    languages = _selector_languages(selector)
    if languages:
        track_lang = normalize_lang(track.orig_language or track.language, track.orig_title or track.title)
        if track_lang not in languages and normalize_lang(track.language, track.title) not in languages:
            return False

    if "channels" in selector:
        expected = str(selector.get("channels") or "").strip().lower()
        actual = _channels_from_display(track.orig_display_info or track.display_info).lower()
        if expected and actual != expected:
            return False

    if "atmos" in selector:
        has_object = bool(_audio_object_from_display(track.orig_display_info or track.display_info))
        if bool(selector["atmos"]) != has_object:
            return False

    if "audio_object" in selector:
        expected = str(selector.get("audio_object") or "").strip().lower()
        actual = _audio_object_from_display(track.orig_display_info or track.display_info).lower()
        if expected and expected != actual:
            return False

    if "title" in selector:
        expected_title = str(selector.get("title") or "")
        if expected_title not in {track.orig_title, track.title}:
            return False

    if "title_contains" in selector:
        needle = str(selector.get("title_contains") or "").lower()
        haystack = f"{track.orig_title} {track.title}".lower()
        if needle and needle not in haystack:
            return False

    if "display_contains" in selector:
        needle = str(selector.get("display_contains") or "").lower()
        haystack = f"{track.orig_display_info} {track.display_info}".lower()
        if needle and needle not in haystack:
            return False

    flags = selector.get("flags")
    if isinstance(flags, Mapping):
        for name, expected in flags.items():
            if name in FLAG_NAMES and _flag_value(track, str(name), original=True) != bool(expected):
                return False

    entry_id = selector.get("entry_id")
    if entry_id and str(track.entry_id) != str(entry_id):
        return False

    return True


def match_track_selector(
    selector: Mapping[str, Any],
    tracks: list[TrackEntry],
    *,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> list[TrackEntry]:
    if not isinstance(selector, Mapping):
        return []
    return [
        track
        for track in tracks
        if _matches_selector(
            track,
            selector,
            tracks,
            source_index_by_file_id=source_index_by_file_id,
        )
    ]


def resolve_track_selector(
    selector: Mapping[str, Any],
    tracks: list[TrackEntry],
    *,
    context: str = "selector",
    strict: bool = True,
    suggested_profile: str | None = None,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> TrackEntry | None:
    matches = match_track_selector(
        selector,
        tracks,
        source_index_by_file_id=source_index_by_file_id,
    )
    if len(matches) == 1:
        return matches[0]
    if not strict:
        return matches[0] if matches else None
    error = "track_selector_unmatched" if not matches else "track_selector_ambiguous"
    report = {
        "valid": False,
        "error": error,
        "context": context,
        "selector": dict(selector),
        "match_count": len(matches),
        "matches": [track_summary(match, source_index_by_file_id=source_index_by_file_id) for match in matches],
    }
    if suggested_profile:
        report["suggested_profile"] = suggested_profile
    message = (
        f"Sélecteur de piste introuvable pour {context}."
        if not matches
        else f"Sélecteur de piste ambigu pour {context}: {len(matches)} correspondances."
    )
    raise HybridResolutionError(message, report=report)


def apply_track_spec(track: TrackEntry, spec: Mapping[str, Any]) -> None:
    if "enabled" in spec:
        track.enabled = bool(spec["enabled"])
    if "language" in spec:
        track.language = normalize_lang(str(spec["language"]), track.title)
    if "title" in spec:
        track.title = str(spec["title"])
    flags = spec.get("flags")
    if isinstance(flags, Mapping):
        for name, value in flags.items():
            if name in FLAG_NAMES:
                setattr(track, f"flag_{name}", bool(value))
    if "time_shift_ms" in spec:
        track.time_shift_ms = int(spec["time_shift_ms"] or 0)


def _flags_payload(track: TrackEntry) -> dict[str, bool]:
    return {
        name: _flag_value(track, name, original=False)
        for name in FLAG_NAMES
        if name != "enabled"
    }


@dataclass
class DecisionProfileResult:
    tracks: list[TrackEntry]
    report: dict[str, Any]


def _truthy_flags(track: TrackEntry) -> dict[str, bool]:
    return {
        name: True
        for name in FLAG_NAMES
        if name != "enabled" and _flag_value(track, name, original=True)
    }


def _channels_value(track: TrackEntry) -> str:
    return _channels_from_display(track.orig_display_info or track.display_info)


def _audio_object_value(track: TrackEntry) -> str:
    return _audio_object_from_display(track.orig_display_info or track.display_info)


def _codec_value(track: TrackEntry) -> str:
    return str(track.orig_codec or track.codec or "").strip().upper()


def _lang_value(track: TrackEntry) -> str:
    return normalize_lang(track.orig_language or track.language, track.orig_title or track.title)


def _display_resolution(track: TrackEntry) -> str:
    resolution = _video_resolution(track)
    if not resolution:
        return ""
    return f"{resolution[0]}x{resolution[1]}"


def _video_text(track: TrackEntry) -> str:
    return " ".join(
        str(part or "")
        for part in (
            track.orig_display_info,
            track.display_info,
            track.orig_title,
            track.title,
            track.orig_codec,
            track.codec,
        )
    )


def _video_resolution(track: TrackEntry) -> tuple[int, int] | None:
    match = re.search(r"\b(\d{3,5})\s*[xX\u00d7]\s*(\d{3,5})\b", _video_text(track))
    if not match:
        match = re.search(r"\b(720|1080|1440|2160|4320)p\b", _video_text(track), flags=re.IGNORECASE)
        if not match:
            return None
        height = int(match.group(1))
        width = {
            720: 1280,
            1080: 1920,
            1440: 2560,
            2160: 3840,
            4320: 7680,
        }.get(height, 0)
        return (width, height) if width else None
    try:
        width = int(match.group(1))
        height = int(match.group(2))
    except ValueError:
        return None
    return (width, height) if width > 0 and height > 0 else None


def _resolution_bucket(width: int, height: int) -> str:
    longest = max(width, height)
    shortest = min(width, height)
    if longest >= 7000 or shortest >= 4000:
        return "8k"
    if longest >= 3000 or shortest >= 1800:
        return "uhd"
    if shortest >= 1000:
        return "fhd"
    if shortest >= 650:
        return "hd"
    return "sd"


def _resolution_flag(width: int, height: int) -> int:
    bucket = _resolution_bucket(width, height)
    if bucket in {"8k", "uhd"}:
        return VIDEO_FLAG_RES_UHD
    if bucket == "fhd":
        return VIDEO_FLAG_RES_FHD
    if bucket == "hd":
        return VIDEO_FLAG_RES_HD
    return VIDEO_FLAG_RES_SD


def _video_resolution_payload(track: TrackEntry) -> dict[str, Any]:
    resolution = _video_resolution(track)
    if not resolution:
        return {}
    width, height = resolution
    return {
        "width": width,
        "height": height,
        "bucket": _resolution_bucket(width, height),
    }


def _video_characteristic_flags(track: TrackEntry) -> int:
    if track.track_type != "video":
        return 0
    flags = 0
    resolution = _video_resolution(track)
    if resolution:
        flags |= _resolution_flag(*resolution)

    text = _video_text(track).lower()
    has_dv = "dolby vision" in text or "dovi" in text
    has_hdr10plus = "hdr10+" in text or "hdr10plus" in text
    has_hdr10 = bool(re.search(r"\bhdr\s*10\b", text)) or "hdr10" in text
    has_hlg = bool(re.search(r"\bhlg\b", text))
    has_hdr = has_dv or has_hdr10plus or has_hdr10 or has_hlg or bool(re.search(r"\bhdr\b", text))

    if has_hdr:
        flags |= VIDEO_FLAG_HDR
    if has_dv:
        flags |= VIDEO_FLAG_DOLBY_VISION
    if has_hdr10plus:
        flags |= VIDEO_FLAG_HDR10PLUS
    elif has_hdr10:
        flags |= VIDEO_FLAG_HDR10
    if has_hlg:
        flags |= VIDEO_FLAG_HLG
    if not has_hdr:
        flags |= VIDEO_FLAG_SDR

    if re.search(r"\b(?:8[\s-]*bit|8\s*bits|yuv\d*p8)\b", text):
        flags |= VIDEO_FLAG_BIT_DEPTH_8
    if re.search(r"\b(?:10[\s-]*bit|10\s*bits|main\s*10|yuv\d*p10)\b", text):
        flags |= VIDEO_FLAG_BIT_DEPTH_10
    if re.search(r"\b(?:12[\s-]*bit|12\s*bits|yuv\d*p12)\b", text):
        flags |= VIDEO_FLAG_BIT_DEPTH_12
    return flags


def _video_characteristic_flags_hex(track: TrackEntry) -> str:
    return f"0x{_video_characteristic_flags(track):0{VIDEO_FLAG_HEX_WIDTH}X}"


def _parse_video_flags(raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        return int(text, 16 if text.lower().startswith("0x") else 10)
    except ValueError:
        return 0


def _match_resolution(match: Mapping[str, Any]) -> tuple[int, int] | None:
    raw = match.get("resolution")
    if isinstance(raw, Mapping):
        try:
            width = int(raw.get("width") or 0)
            height = int(raw.get("height") or 0)
        except (TypeError, ValueError):
            return None
        return (width, height) if width > 0 and height > 0 else None
    if isinstance(raw, str):
        fake = TrackEntry(0, "video", "", raw, "", "")
        return _video_resolution(fake)
    display_contains = match.get("display_contains")
    if isinstance(display_contains, str):
        fake = TrackEntry(0, "video", "", display_contains, "", "")
        return _video_resolution(fake)
    return None


def _resolution_similarity_score(track: TrackEntry, match: Mapping[str, Any]) -> int:
    expected = _match_resolution(match)
    if not expected:
        return 0
    actual = _video_resolution(track)
    if not actual:
        return 0
    expected_width, expected_height = expected
    actual_width, actual_height = actual
    if (actual_width, actual_height) == (expected_width, expected_height):
        return 24
    if _resolution_bucket(actual_width, actual_height) == _resolution_bucket(expected_width, expected_height):
        return 18
    expected_pixels = expected_width * expected_height
    actual_pixels = actual_width * actual_height
    if expected_pixels <= 0:
        return 0
    relative_delta = abs(actual_pixels - expected_pixels) / expected_pixels
    return max(0, 16 - int(relative_delta * 16))


def _video_flag_similarity_score(track: TrackEntry, match: Mapping[str, Any]) -> int:
    expected = _parse_video_flags(match.get("video_flags_hex", match.get("video_flags")))
    if expected <= 0:
        return 0
    actual = _video_characteristic_flags(track)
    score = 0

    expected_resolution = expected & VIDEO_RESOLUTION_MASK
    if expected_resolution:
        score += 8 if actual & expected_resolution else 0

    expected_hdr = expected & VIDEO_HDR_MASK
    if expected_hdr:
        if actual & expected_hdr:
            score += 12
        if (expected & VIDEO_FLAG_DOLBY_VISION) and (actual & VIDEO_FLAG_DOLBY_VISION):
            score += 8
        if (expected & VIDEO_FLAG_HDR10PLUS) and (actual & VIDEO_FLAG_HDR10PLUS):
            score += 6
        if (expected & VIDEO_FLAG_HDR10) and (actual & VIDEO_FLAG_HDR10):
            score += 4
        if (expected & VIDEO_FLAG_HLG) and (actual & VIDEO_FLAG_HLG):
            score += 4
        if (expected & VIDEO_FLAG_SDR) and (actual & VIDEO_FLAG_SDR):
            score += 6

    expected_depth = expected & VIDEO_BIT_DEPTH_MASK
    if expected_depth and actual & expected_depth:
        score += 4

    return score


def _decision_sort_key(
    track: TrackEntry,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> tuple[int, int, str]:
    source_index = _source_index_for_track(track, source_index_by_file_id)
    try:
        mkv_tid = int(track.mkv_tid)
    except (TypeError, ValueError):
        mkv_tid = 0
    return (
        source_index if source_index is not None else 1_000_000,
        mkv_tid,
        str(track.entry_id or ""),
    )


def _match_is_video(match: Mapping[str, Any]) -> bool:
    track_type = match.get("type", match.get("track_type"))
    return str(track_type or "").lower() == "video"


def _base_decision_match(track: TrackEntry) -> dict[str, Any]:
    match: dict[str, Any] = {"type": track.track_type}
    codec = _codec_value(track)
    if codec:
        match["codec"] = codec
    lang = _lang_value(track)
    if lang:
        match["language"] = lang
    if track.track_type == "audio":
        channels = _channels_value(track)
        if channels:
            match["channels"] = channels
        audio_object = _audio_object_value(track)
        if audio_object:
            match["audio_object"] = audio_object
    if track.track_type == "video":
        resolution = _video_resolution_payload(track)
        if resolution:
            match["resolution"] = resolution
        flags_hex = _video_characteristic_flags_hex(track)
        if flags_hex != "0x00000000":
            match["video_flags_hex"] = flags_hex
        match["tie_break"] = "first_source_index"
    return match


def _decision_match_for_entry(
    track: TrackEntry,
    tracks: list[TrackEntry],
    *,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Build a reusable matcher, adding specificity only when useful."""
    candidates = [candidate for candidate in tracks if not candidate.is_new]
    match = _base_decision_match(track)

    def _count(raw_match: Mapping[str, Any]) -> int:
        return len(_decision_candidates(raw_match, candidates, source_index_by_file_id=source_index_by_file_id))

    if _count(match) <= 1:
        return match

    flags = _truthy_flags(track)
    if flags:
        with_flags = {**match, "flags": flags}
        if _count(with_flags) <= 1:
            return with_flags
        match = with_flags

    source_title = str(track.orig_title or "").strip()
    if source_title:
        with_title = {**match, "title_contains": source_title}
        if _count(with_title) <= 1:
            return with_title
        match = with_title

    return match


def _decision_action(
    track: TrackEntry,
    *,
    include_selection: bool,
    include_metadata: bool,
    include_flags: bool,
) -> dict[str, Any]:
    action: dict[str, Any] = {}
    if include_selection:
        action["enabled"] = bool(track.enabled)
    if include_metadata:
        action["language"] = track.language
        action["title"] = track.title
        if int(track.time_shift_ms or 0) != 0:
            action["time_shift_ms"] = int(track.time_shift_ms or 0)
    if include_flags:
        action["flags"] = _flags_payload(track)
    return action


def _bitrate_from_display(display_info: str) -> int:
    match = re.search(r"\b(\d+)\s*kbps\b", str(display_info or ""), flags=re.IGNORECASE)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def _variant_codec(track: TrackEntry, source: TrackEntry | None) -> str:
    codec = str(track.codec or "").strip()
    if not codec:
        return "copy"
    if source is not None and codec.upper() == str(source.orig_codec or source.codec or "").upper():
        return "copy"
    return codec.lower()


def remux_config_to_decision_profile(
    config: RemuxConfig,
    *,
    name: str = "",
    include_selection: bool = True,
    include_metadata: bool = True,
    include_flags: bool = True,
    include_order: bool = True,
    include_audio_variants: bool = True,
) -> dict[str, Any]:
    """Serialize a remux state as a reusable GUI decision profile."""
    source_index_by_file_id: dict[str, int] = {}
    for source in config.sources:
        for track in source.tracks:
            source_index_by_file_id[track.file_id] = source.file_index

    all_tracks = [track for source in config.sources for track in source.tracks]
    original_tracks = [track for track in all_tracks if not track.is_new]
    track_by_order_key = {
        (source.file_index, track.mkv_tid, track.entry_id): track
        for source in config.sources
        for track in source.tracks
    }
    track_by_basic_key = {
        (source.file_index, track.mkv_tid): track
        for source in config.sources
        for track in source.tracks
        if not track.is_new
    }

    track_rules: list[dict[str, Any]] = []
    for track in original_tracks:
        action = _decision_action(
            track,
            include_selection=include_selection,
            include_metadata=include_metadata,
            include_flags=include_flags,
        )
        if not action:
            continue
        track_rules.append(
            {
                "match": _decision_match_for_entry(
                    track,
                    original_tracks,
                    source_index_by_file_id=source_index_by_file_id,
                ),
                "action": action,
            }
        )

    audio_variants: list[dict[str, Any]] = []
    variant_key_by_entry_id: dict[str, str] = {}
    if include_audio_variants:
        for track in all_tracks:
            if not track.is_new or track.track_type != "audio":
                continue
            source_track = next(
                (candidate for candidate in original_tracks if candidate.entry_id == track.source_entry_id),
                None,
            )
            if source_track is None:
                continue
            variant_key = f"audio_variant_{len(audio_variants) + 1}"
            variant_key_by_entry_id[track.entry_id] = variant_key
            action = _decision_action(
                track,
                include_selection=include_selection,
                include_metadata=include_metadata,
                include_flags=include_flags,
            )
            action["codec"] = _variant_codec(track, source_track)
            bitrate = _bitrate_from_display(track.display_info)
            if bitrate > 0:
                action["bitrate_kbps"] = bitrate
            audio_variants.append(
                {
                    "variant_key": variant_key,
                    "source_match": _decision_match_for_entry(
                        source_track,
                        original_tracks,
                        source_index_by_file_id=source_index_by_file_id,
                    ),
                    "action": action,
                }
            )

    order: list[dict[str, Any]] = []
    if include_order:
        for item in config.track_order:
            source_index = int(item[0])
            mkv_tid = int(item[1])
            entry_id = str(item[2]) if len(item) > 2 else ""
            track = (
                track_by_order_key.get((source_index, mkv_tid, entry_id))
                if entry_id
                else track_by_basic_key.get((source_index, mkv_tid))
            )
            if track is None:
                continue
            if track.entry_id in variant_key_by_entry_id:
                order.append({"variant_key": variant_key_by_entry_id[track.entry_id]})
            elif not track.is_new:
                order.append(
                    {
                        "match": _decision_match_for_entry(
                            track,
                            original_tracks,
                            source_index_by_file_id=source_index_by_file_id,
                        )
                    }
                )

    profile: dict[str, Any] = {
        "version": 2,
        "kind": "hybrid-profile",
        "profile_mode": "decision",
        "match_policy": {"missing": "report", "ambiguous": "skip"},
        "selection_policy": {
            "disable_unmatched_types": ["video", "audio", "subtitle"] if include_selection else []
        },
        "track_rules": track_rules,
        "order": order,
        "audio_variants": audio_variants,
    }
    if name:
        profile["name"] = name
    save_options = {
        "selection": include_selection,
        "metadata": include_metadata,
        "flags": include_flags,
        "order": include_order,
        "audio_variants": include_audio_variants,
    }
    profile["save_options"] = save_options
    return {key: value for key, value in profile.items() if value not in ({}, [], None, "")}


def _generalized_match_from_selector(selector: Mapping[str, Any]) -> dict[str, Any]:
    match: dict[str, Any] = {}
    for key in (
        "type",
        "track_type",
        "codec",
        "codecs",
        "language",
        "languages",
        "channels",
        "audio_object",
        "atmos",
        "flags",
        "display_contains",
        "resolution",
        "video_flags_hex",
        "video_flags",
    ):
        if key in selector:
            target_key = "type" if key == "track_type" else key
            match[target_key] = selector[key]
    if "title_contains" in selector:
        match["title_contains"] = selector["title_contains"]
    elif selector.get("title"):
        match["title_contains"] = selector["title"]
    if _match_is_video(match):
        match["tie_break"] = "first_source_index"
    return match


def _action_from_legacy_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    action: dict[str, Any] = {}
    for key in ("enabled", "language", "title", "time_shift_ms"):
        if key in spec:
            action[key] = spec[key]
    flags = spec.get("flags")
    if isinstance(flags, Mapping):
        action["flags"] = dict(flags)
    for key in ("codec", "bitrate_kbps"):
        if key in spec:
            action[key] = spec[key]
    return action


def decision_profile_from_legacy(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Best-effort conversion of older exact GUI profiles to decision profiles."""
    converted: dict[str, Any] = {
        "version": 2,
        "kind": "hybrid-profile",
        "profile_mode": "decision",
        "legacy_converted": True,
        "name": profile.get("name", ""),
        "match_policy": {"missing": "report", "ambiguous": "skip"},
        "selection_policy": {"disable_unmatched_types": []},
    }
    track_rules: list[dict[str, Any]] = []
    for spec in profile.get("tracks", []) if isinstance(profile.get("tracks"), list) else []:
        if not isinstance(spec, Mapping) or not isinstance(spec.get("selector"), Mapping):
            continue
        action = _action_from_legacy_spec(spec)
        if not action:
            continue
        track_rules.append({"match": _generalized_match_from_selector(spec["selector"]), "action": action})
    order: list[dict[str, Any]] = []
    for item in profile.get("track_order", []) if isinstance(profile.get("track_order"), list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("selector"), Mapping):
            continue
        order.append({"match": _generalized_match_from_selector(item["selector"])})
    variants: list[dict[str, Any]] = []
    for spec in profile.get("audio_variants", []) if isinstance(profile.get("audio_variants"), list) else []:
        if not isinstance(spec, Mapping):
            continue
        selector = spec.get("source_selector", spec.get("selector"))
        if not isinstance(selector, Mapping):
            continue
        variants.append(
            {
                "variant_key": f"legacy_audio_variant_{len(variants) + 1}",
                "source_match": _generalized_match_from_selector(selector),
                "action": _action_from_legacy_spec(spec),
            }
        )
    converted["track_rules"] = track_rules
    converted["order"] = order
    converted["audio_variants"] = variants
    if track_rules or variants:
        converted["selection_policy"] = {"disable_unmatched_types": ["video", "audio", "subtitle"]}
    return {key: value for key, value in converted.items() if value not in ({}, [], None, "")}


def _decision_candidates(
    match: Mapping[str, Any],
    tracks: list[TrackEntry],
    *,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> list[TrackEntry]:
    scored: list[tuple[int, TrackEntry]] = []
    for track in tracks:
        score = _decision_match_score(track, match, source_index_by_file_id=source_index_by_file_id)
        if score is not None:
            scored.append((score, track))
    if not scored:
        return []
    best_score = max(score for score, _track in scored)
    best_tracks = [track for score, track in scored if score == best_score]
    tie_break = str(match.get("tie_break") or "first_source_index").strip().lower()
    if len(best_tracks) > 1 and _match_is_video(match) and tie_break in {"first_source_index", "first_index"}:
        return [min(best_tracks, key=lambda track: _decision_sort_key(track, source_index_by_file_id))]
    return best_tracks


def _decision_match_score(
    track: TrackEntry,
    match: Mapping[str, Any],
    *,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> int | None:
    track_type = match.get("type", match.get("track_type"))
    if track_type and str(track_type) != track.track_type:
        return None
    score = 20 if track_type else 0
    is_video = track.track_type == "video" and (not track_type or str(track_type) == "video")

    source = _selector_source(match)
    if source is not None:
        if _source_index_for_track(track, source_index_by_file_id) != source:
            return None
        score += 1

    codecs = _selector_codecs(match)
    if codecs:
        codec_matches = _codec_value(track) in codecs or str(track.codec or "").strip().upper() in codecs
        if not codec_matches and not is_video:
            return None
        score += 6 if codec_matches else 0

    languages = _selector_languages(match)
    if languages:
        language_matches = _lang_value(track) in languages or normalize_lang(track.language, track.title) in languages
        if not language_matches and not is_video:
            return None
        score += 10 if language_matches else 0

    if is_video:
        score += _resolution_similarity_score(track, match)
        score += _video_flag_similarity_score(track, match)

    if "channels" in match:
        expected = str(match.get("channels") or "").strip().lower()
        if expected and _channels_value(track).lower() != expected:
            return None
        score += 5

    if "audio_object" in match:
        expected = str(match.get("audio_object") or "").strip().lower()
        if expected and _audio_object_value(track).lower() != expected:
            return None
        score += 5

    if "atmos" in match:
        if bool(match["atmos"]) != bool(_audio_object_value(track)):
            return None
        score += 4

    if "title_contains" in match:
        needle = str(match.get("title_contains") or "").lower()
        haystack = f"{track.orig_title} {track.title}".lower()
        if needle and needle not in haystack and not is_video:
            return None
        score += 3 if needle and needle in haystack else 0

    if "display_contains" in match:
        needle = str(match.get("display_contains") or "").lower()
        haystack = f"{track.orig_display_info} {track.display_info}".lower()
        if needle and needle not in haystack and not is_video:
            return None
        score += 3 if needle and needle in haystack else 0

    flags = match.get("flags")
    if isinstance(flags, Mapping):
        for name, expected in flags.items():
            if name not in FLAG_NAMES:
                continue
            flag_matches = _flag_value(track, str(name), original=True) == bool(expected)
            if not flag_matches and not is_video:
                return None
            score += 2 if flag_matches else 0

    return score


def _apply_decision_action(track: TrackEntry, action: Mapping[str, Any]) -> None:
    apply_track_spec(track, action)


def _codec_label(codec: str) -> str:
    normalized = str(codec or "copy").strip().lower()
    if normalized == "copy":
        return "copy"
    return {
        "aac": "AAC",
        "ac3": "AC3",
        "eac3": "EAC3",
        "flac": "FLAC",
    }.get(normalized, normalized.upper())


def _variant_display_info(source_display_info: str, codec: str, bitrate_kbps: int) -> str:
    if str(codec or "copy").strip().lower() == "copy":
        return source_display_info
    parts = [
        part.strip()
        for part in str(source_display_info or "").replace("·", "  ").split("  ")
        if part.strip() and "kbps" not in part.lower()
    ]
    if bitrate_kbps > 0:
        parts.append(f"{int(bitrate_kbps)} kbps")
    return "  ".join(parts)


def _find_existing_variant(
    tracks: list[TrackEntry],
    source_track: TrackEntry,
    action: Mapping[str, Any],
) -> TrackEntry | None:
    codec = _codec_label(str(action.get("codec") or "copy"))
    title = str(action.get("title") or "")
    language = normalize_lang(str(action.get("language") or source_track.language), title)
    for track in tracks:
        if not track.is_new or track.source_entry_id != source_track.entry_id:
            continue
        if str(track.codec or "").upper() != codec.upper() and codec != "copy":
            continue
        if title and track.title != title:
            continue
        if language and normalize_lang(track.language, track.title) != language:
            continue
        return track
    return None


def apply_decision_profile(
    profile: Mapping[str, Any],
    tracks: list[TrackEntry],
    *,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> DecisionProfileResult:
    """Apply a reusable GUI profile without requiring exact source identity."""
    if profile.get("profile_mode") != "decision":
        profile = decision_profile_from_legacy(profile)

    working = list(tracks)
    original_tracks = [track for track in working if not track.is_new]
    report: dict[str, Any] = {
        "valid": True,
        "legacy_converted": bool(profile.get("legacy_converted", False)),
        "applied_rules": 0,
        "missing_rules": [],
        "ambiguous_rules": [],
        "created_variants": [],
        "reused_variants": [],
        "order_changed": False,
    }

    selection_policy = profile.get("selection_policy")
    disable_types: set[str] = set()
    if isinstance(selection_policy, Mapping):
        raw_disable = selection_policy.get("disable_unmatched_types", [])
        if isinstance(raw_disable, list):
            disable_types = {str(item) for item in raw_disable}
    if disable_types:
        for track in working:
            if track.track_type in disable_types:
                track.enabled = False

    for index, rule in enumerate(profile.get("track_rules", []) if isinstance(profile.get("track_rules"), list) else []):
        if not isinstance(rule, Mapping) or not isinstance(rule.get("match"), Mapping):
            continue
        matches = _decision_candidates(
            rule["match"],
            original_tracks,
            source_index_by_file_id=source_index_by_file_id,
        )
        if not matches:
            report["missing_rules"].append({"index": index, "match": dict(rule["match"])})
            continue
        if len(matches) > 1:
            report["ambiguous_rules"].append(
                {
                    "index": index,
                    "match": dict(rule["match"]),
                    "matches": [track_summary(track, source_index_by_file_id=source_index_by_file_id) for track in matches],
                }
            )
            continue
        action = rule.get("action", {})
        if isinstance(action, Mapping):
            _apply_decision_action(matches[0], action)
            report["applied_rules"] += 1

    created_by_key: dict[str, TrackEntry] = {}
    for index, spec in enumerate(profile.get("audio_variants", []) if isinstance(profile.get("audio_variants"), list) else []):
        if not isinstance(spec, Mapping) or not isinstance(spec.get("source_match"), Mapping):
            continue
        matches = _decision_candidates(
            spec["source_match"],
            original_tracks,
            source_index_by_file_id=source_index_by_file_id,
        )
        if not matches:
            report["missing_rules"].append({"index": index, "kind": "audio_variant", "match": dict(spec["source_match"])})
            continue
        if len(matches) > 1:
            report["ambiguous_rules"].append(
                {
                    "index": index,
                    "kind": "audio_variant",
                    "match": dict(spec["source_match"]),
                    "matches": [track_summary(track, source_index_by_file_id=source_index_by_file_id) for track in matches],
                }
            )
            continue
        source_track = matches[0]
        action = spec.get("action", {})
        if not isinstance(action, Mapping):
            action = {}
        variant = _find_existing_variant(working, source_track, action)
        if variant is None:
            variant = clone_track_entry(source_track)
            working.append(variant)
            report["created_variants"].append(track_summary(variant, source_index_by_file_id=source_index_by_file_id))
        else:
            report["reused_variants"].append(track_summary(variant, source_index_by_file_id=source_index_by_file_id))
        codec = str(action.get("codec") or "copy").strip().lower()
        bitrate = int(action.get("bitrate_kbps") or 0)
        if codec and codec != "copy":
            variant.codec = _codec_label(codec)
            variant.display_info = _variant_display_info(source_track.orig_display_info or source_track.display_info, codec, bitrate)
        else:
            variant.codec = source_track.orig_codec or source_track.codec
            variant.display_info = source_track.orig_display_info or source_track.display_info
        _apply_decision_action(variant, action)
        variant_key = str(spec.get("variant_key") or "").strip()
        if variant_key:
            created_by_key[variant_key] = variant

    raw_order = profile.get("order", [])
    ordered_tracks: list[TrackEntry] = []
    seen_ids: set[str] = set()
    if isinstance(raw_order, list):
        for index, item in enumerate(raw_order):
            if not isinstance(item, Mapping):
                continue
            variant_key = str(item.get("variant_key") or "").strip()
            if variant_key:
                track = created_by_key.get(variant_key)
                if track is not None and track.entry_id not in seen_ids:
                    ordered_tracks.append(track)
                    seen_ids.add(track.entry_id)
                continue
            match = item.get("match")
            if not isinstance(match, Mapping):
                continue
            matches = _decision_candidates(
                match,
                [track for track in working if not track.is_new],
                source_index_by_file_id=source_index_by_file_id,
            )
            enabled_matches = [track for track in matches if track.enabled]
            if not enabled_matches:
                report["missing_rules"].append({"index": index, "kind": "order", "match": dict(match)})
                continue
            if len(enabled_matches) > 1:
                report["ambiguous_rules"].append(
                    {
                        "index": index,
                        "kind": "order",
                        "match": dict(match),
                        "matches": [track_summary(track, source_index_by_file_id=source_index_by_file_id) for track in enabled_matches],
                    }
                )
                continue
            track = enabled_matches[0]
            if track.entry_id not in seen_ids:
                ordered_tracks.append(track)
                seen_ids.add(track.entry_id)

    ordered_tracks.extend(track for track in working if track.entry_id not in seen_ids)
    report["order_changed"] = [track.entry_id for track in tracks] != [track.entry_id for track in ordered_tracks]
    report["valid"] = not report["ambiguous_rules"]
    return DecisionProfileResult(tracks=ordered_tracks, report=report)


def remux_config_to_hybrid_job(
    config: RemuxConfig,
    *,
    name: str = "",
    fallback_profile: str = "",
) -> dict[str, Any]:
    """Serialize a remux state as an exact hybrid v2 job."""
    source_index_by_file_id: dict[str, int] = {}
    for source in config.sources:
        for track in source.tracks:
            source_index_by_file_id[track.file_id] = source.file_index

    all_tracks = [track for source in config.sources for track in source.tracks]
    track_by_order_key = {
        (source.file_index, track.mkv_tid, track.entry_id): track
        for source in config.sources
        for track in source.tracks
    }
    track_by_basic_key = {
        (source.file_index, track.mkv_tid): track
        for source in config.sources
        for track in source.tracks
        if not track.is_new
    }

    def _track_selector(track: TrackEntry) -> dict[str, Any]:
        source_idx = source_index_by_file_id.get(track.file_id)
        source_tracks = next(
            (source.tracks for source in config.sources if source.file_index == source_idx),
            all_tracks,
        )
        return track_selector_for_entry(
            track,
            source_index=source_idx,
            tracks=list(source_tracks),
            source_index_by_file_id=source_index_by_file_id,
        )

    tracks_payload: list[dict[str, Any]] = []
    audio_variants: list[dict[str, Any]] = []
    for track in all_tracks:
        payload = {
            "selector": _track_selector(track),
            "enabled": bool(track.enabled),
            "language": track.language,
            "title": track.title,
            "flags": _flags_payload(track),
            "time_shift_ms": int(track.time_shift_ms or 0),
        }
        if track.is_new:
            payload["codec"] = track.codec
            source_track = next(
                (
                    candidate
                    for candidate in all_tracks
                    if candidate.entry_id == track.source_entry_id
                ),
                None,
            )
            if source_track is not None:
                payload["source_selector"] = _track_selector(source_track)
            audio_variants.append(payload)
        else:
            tracks_payload.append(payload)

    order_payload: list[dict[str, Any]] = []
    for item in config.track_order:
        source_index = int(item[0])
        mkv_tid = int(item[1])
        entry_id = str(item[2]) if len(item) > 2 else ""
        track = (
            track_by_order_key.get((source_index, mkv_tid, entry_id))
            if entry_id
            else track_by_basic_key.get((source_index, mkv_tid))
        )
        if track is not None:
            order_payload.append({"selector": _track_selector(track)})

    sources_payload = []
    for source in config.sources:
        attachment_names = [att.filename for att in source.selected_attachments]
        sources_payload.append(
            {
                "path": str(source.path),
                "attachments": attachment_names if attachment_names else "none",
                "copy_tags": bool(source.copy_tags),
            }
        )

    chapters: dict[str, Any] | bool
    if not config.keep_chapters:
        chapters = False
    elif config.chapter_overrides is not None:
        chapters = {
            "source_index": config.chapter_source_index,
            "include_source": False,
            "add": [
                {
                    "timestamp": getattr(chapter, "timecode_s", 0),
                    "chaptername": getattr(chapter, "name", ""),
                }
                for chapter in config.chapter_overrides
            ],
        }
    else:
        chapters = {"source_index": config.chapter_source_index} if config.chapter_source_index is not None else {}

    job: dict[str, Any] = {
        "version": 1,
        "kind": "exact-job",
        "sources": sources_payload,
        "output": str(config.output),
        "tracks": tracks_payload,
        "track_order": order_payload,
        "chapters": chapters,
        "extra_attachments": [str(path) for path in config.extra_attachments],
        "file_title": config.file_title,
        "tag_overrides": config.tag_overrides,
    }
    if name:
        job["name"] = name
    if fallback_profile:
        job["fallback_profile"] = fallback_profile
    if audio_variants:
        job["audio_variants"] = audio_variants
    return {key: value for key, value in job.items() if value not in ({}, [], None, "")}
