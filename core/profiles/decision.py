"""Decision profile v1 engine for reusable low-code track automapping."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from core.lang_tags import Rfc5646LanguageTags
from core.workflows.remux_models import RemuxConfig, TrackEntry, clone_track_entry


DECISION_PROFILE_KIND = "decision-profile"
DECISION_PROFILE_VERSION = 1

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

DECISION_KEYWORDS = (
    "type",
    "source_index",
    "track_index",
    "language",
    "lang",
    "lang_name",
    "title",
    "source_title",
    "codec",
    "codec_raw",
    "codec_name",
    "channels",
    "channel_layout",
    "audio_object",
    "atmos",
    "dtsx",
    "codec_atmos",
    "codec_dtsx",
    "resolution",
    "width",
    "height",
    "hdr",
    "video_hdr",
    "video_hdr10",
    "video_hdr10plus",
    "video_dolby_vision",
    "video_hlg",
    "video_sdr",
    "video_flags_hex",
    "flags",
    "flag_enabled",
    "flag_default",
    "flag_forced",
    "flag_hearing_impaired",
    "flag_visual_impaired",
    "flag_original",
    "flag_commentary",
)
TITLE_KEYWORDS = DECISION_KEYWORDS

_FIELD_WEIGHTS = {
    "type": 40,
    "language": 18,
    "lang": 18,
    "codec": 8,
    "codec_raw": 8,
    "codec_name": 8,
    "channels": 8,
    "channel_layout": 8,
    "audio_object": 10,
    "atmos": 10,
    "dtsx": 10,
    "codec_atmos": 10,
    "codec_dtsx": 10,
    "title": 4,
    "source_title": 4,
    "flags": 6,
    "flag_enabled": 4,
    "flag_default": 4,
    "flag_forced": 8,
    "flag_hearing_impaired": 6,
    "flag_visual_impaired": 6,
    "flag_original": 4,
    "flag_commentary": 8,
    "resolution": 16,
    "width": 8,
    "height": 8,
    "video_flags_hex": 18,
    "hdr": 12,
    "video_hdr": 12,
    "video_hdr10": 10,
    "video_hdr10plus": 12,
    "video_dolby_vision": 12,
    "video_hlg": 10,
    "video_sdr": 8,
    "track_tags": 14,
}


class DecisionProfileError(ValueError):
    """Raised when a decision profile is structurally invalid."""


@dataclass
class ConditionResult:
    eligible: bool
    matched: bool
    score: int = 0


@dataclass
class DecisionProfileResult:
    tracks: list[TrackEntry]
    report: dict[str, Any]
    profile: dict[str, Any] | None = None


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


class DecisionProfileManager:
    """JSON persistence for decision profile v1 files."""

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
        data.pop("sources", None)
        data.pop("output", None)
        data["version"] = DECISION_PROFILE_VERSION
        data["kind"] = DECISION_PROFILE_KIND
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("Decision profile requires a non-empty name.")
        validate_decision_profile(data)
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


def validate_decision_profile(profile: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(profile.get("version", 0) or 0) != DECISION_PROFILE_VERSION:
        errors.append("version: expected 1")
    if profile.get("kind") != DECISION_PROFILE_KIND:
        errors.append("kind: expected decision-profile")
    if not str(profile.get("name") or "").strip():
        errors.append("name: required")
    rules = profile.get("rules", [])
    if rules is not None and not isinstance(rules, list):
        errors.append("rules: expected array")
    if isinstance(rules, list):
        seen: set[str] = set()
        for index, rule in enumerate(rules):
            if not isinstance(rule, Mapping):
                errors.append(f"rules[{index}]: expected object")
                continue
            rule_id = str(rule.get("id") or "").strip()
            if not rule_id:
                errors.append(f"rules[{index}].id: required")
            elif rule_id in seen:
                errors.append(f"rules[{index}].id: duplicate {rule_id}")
            seen.add(rule_id)
            if "match" not in rule:
                errors.append(f"rules[{index}].match: required")
            if "actions" in rule and not isinstance(rule["actions"], list):
                errors.append(f"rules[{index}].actions: expected array")
    if errors:
        raise DecisionProfileError("; ".join(errors))
    return errors


def normalize_lang(tag: str | None, title: str | None = None) -> str:
    if not tag:
        return ""
    regionalized = Rfc5646LanguageTags.regionalize_track_language(str(tag), title)
    if regionalized:
        return regionalized
    canonical = Rfc5646LanguageTags.normalize(str(tag))
    return canonical or str(tag).strip()


def _lang_name(tag: str) -> str:
    if not tag:
        return ""
    canonical = Rfc5646LanguageTags.normalize(tag) or tag
    if canonical in Rfc5646LanguageTags.TAGS:
        return Rfc5646LanguageTags.TAGS[canonical].split(" (", 1)[0]
    base = canonical.split("-", 1)[0]
    return Rfc5646LanguageTags.TAGS.get(base, canonical).split(" (", 1)[0]


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


def _flag_value(track: TrackEntry, name: str, *, original: bool = False) -> bool:
    attr = f"{'orig_' if original else ''}flag_{name}"
    if name == "enabled":
        attr = f"{'orig_' if original else ''}flag_enabled"
    if not hasattr(track, attr):
        attr = f"flag_{name}"
    return bool(getattr(track, attr, False))


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


def _track_sort_key(
    track: TrackEntry,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> tuple[int, int, str]:
    source_index = _source_index_for_track(track, source_index_by_file_id)
    return (
        source_index if source_index is not None else 1_000_000,
        int(track.mkv_tid),
        str(track.entry_id or ""),
    )


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
    text = _video_text(track)
    match = re.search(r"\b(\d{3,5})\s*[xX\u00d7]\s*(\d{3,5})\b", text)
    if not match:
        match = re.search(r"\b(720|1080|1440|2160|4320)p\b", text, flags=re.IGNORECASE)
        if not match:
            return None
        height = int(match.group(1))
        width = {720: 1280, 1080: 1920, 1440: 2560, 2160: 3840, 4320: 7680}.get(height, 0)
        return (width, height) if width else None
    width = int(match.group(1))
    height = int(match.group(2))
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


def build_video_flags_hex(
    *,
    width: int = 0,
    height: int = 0,
    hdr: bool = False,
    hdr10: bool = False,
    hdr10plus: bool = False,
    dolby_vision: bool = False,
    hlg: bool = False,
    sdr: bool = False,
    bit_depth: int = 0,
) -> str:
    """Build the reusable hexadecimal signature used by video decision rules."""
    flags = 0
    if width > 0 and height > 0:
        flags |= _resolution_flag(width, height)
    if hdr or hdr10 or hdr10plus or dolby_vision or hlg:
        flags |= VIDEO_FLAG_HDR
    if hdr10:
        flags |= VIDEO_FLAG_HDR10
    if hdr10plus:
        flags |= VIDEO_FLAG_HDR10PLUS
    if dolby_vision:
        flags |= VIDEO_FLAG_DOLBY_VISION
    if hlg:
        flags |= VIDEO_FLAG_HLG
    if sdr:
        flags |= VIDEO_FLAG_SDR
    if bit_depth == 8:
        flags |= VIDEO_FLAG_BIT_DEPTH_8
    elif bit_depth == 10:
        flags |= VIDEO_FLAG_BIT_DEPTH_10
    elif bit_depth == 12:
        flags |= VIDEO_FLAG_BIT_DEPTH_12
    return f"0x{flags:08X}"


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


def video_flags_hex(track: TrackEntry) -> str:
    return f"0x{_video_characteristic_flags(track):08X}"


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


def _video_hdr_label(track: TrackEntry) -> str:
    flags = _video_characteristic_flags(track)
    parts: list[str] = []
    if flags & VIDEO_FLAG_DOLBY_VISION:
        parts.append("Dolby Vision")
    if flags & VIDEO_FLAG_HDR10PLUS:
        parts.append("HDR10+")
    elif flags & VIDEO_FLAG_HDR10:
        parts.append("HDR10")
    if flags & VIDEO_FLAG_HLG:
        parts.append("HLG")
    if not parts and flags & VIDEO_FLAG_SDR:
        return "SDR"
    return " + ".join(parts)


def _profile_variables(variables: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return variables if isinstance(variables, Mapping) else {}


def _codec_alias_key(codec: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(codec or "").upper())


def _codec_name(codec: str, variables: Mapping[str, Any] | None = None) -> str:
    raw_codec = str(codec or "").strip().upper()
    profile_variables = _profile_variables(variables)
    codec_names = profile_variables.get("codec_names", {})
    if isinstance(codec_names, Mapping):
        expected = _codec_alias_key(raw_codec)
        for key, value in codec_names.items():
            if _codec_alias_key(key) == expected:
                text = str(value or "").strip()
                if text:
                    return text
    return raw_codec


def _track_field_values(
    track: TrackEntry,
    *,
    temporary_tags: set[str] | None = None,
    source_index_by_file_id: Mapping[str, int] | None = None,
    variables: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    lang = normalize_lang(track.language, track.title)
    source_lang = normalize_lang(track.orig_language or track.language, track.orig_title or track.title)
    channels = _channels_from_display(track.orig_display_info or track.display_info)
    audio_object = _audio_object_from_display(track.orig_display_info or track.display_info)
    resolution = _video_resolution(track)
    width, height = resolution or (0, 0)
    resolution_text = f"{resolution[0]}x{resolution[1]}" if resolution else ""
    video_flags = _video_characteristic_flags(track)
    flags = {name: _flag_value(track, name, original=True) for name in FLAG_NAMES}
    codec = str(track.orig_codec or track.codec or "").strip().upper()
    values: dict[str, Any] = {
        "type": track.track_type,
        "source_index": _source_index_for_track(track, source_index_by_file_id),
        "track_index": int(track.mkv_tid),
        "language": lang,
        "lang": lang,
        "lang_name": _lang_name(lang),
        "source_language": source_lang,
        "title": track.title,
        "source_title": track.orig_title or track.title,
        "codec": codec,
        "codec_raw": codec,
        "codec_name": _codec_name(codec, variables),
        "channels": channels,
        "channel_layout": channels,
        "audio_object": audio_object,
        "atmos": audio_object == "Atmos",
        "dtsx": audio_object == "DTS:X",
        "codec_atmos": audio_object == "Atmos",
        "codec_dtsx": audio_object == "DTS:X",
        "resolution": resolution_text,
        "width": width,
        "height": height,
        "hdr": _video_hdr_label(track),
        "video_hdr": bool(video_flags & VIDEO_FLAG_HDR),
        "video_hdr10": bool(video_flags & VIDEO_FLAG_HDR10),
        "video_hdr10plus": bool(video_flags & VIDEO_FLAG_HDR10PLUS),
        "video_dolby_vision": bool(video_flags & VIDEO_FLAG_DOLBY_VISION),
        "video_hlg": bool(video_flags & VIDEO_FLAG_HLG),
        "video_sdr": bool(video_flags & VIDEO_FLAG_SDR),
        "video_flags_hex": video_flags_hex(track),
        "flags": _flags_label(track),
        "track_tags": sorted(temporary_tags or set()),
    }
    for name, value in flags.items():
        values[f"flag_{name}"] = bool(value)
    return values


def _flags_label(track: TrackEntry) -> str:
    labels = []
    mapping = {
        "default": "Default",
        "forced": "Forced",
        "hearing_impaired": "Malentendant",
        "visual_impaired": "Malvoyant",
        "original": "Original",
        "commentary": "Commentaire",
    }
    for name, label in mapping.items():
        if _flag_value(track, name, original=False):
            labels.append(label)
    return " ".join(labels)


def _clean_rendered_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = re.sub(r"\s+([,.;:/)\]])", r"\1", cleaned)
    cleaned = re.sub(r"([(\[])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", cleaned)
    cleaned = re.sub(r"(?:\s*[-/|]\s*){2,}", " - ", cleaned)
    cleaned = re.sub(r"\s+-\s+$", "", cleaned).strip(" -/|")
    return re.sub(r"\s+", " ", cleaned).strip()


def render_title_pattern(
    pattern: str,
    track: TrackEntry,
    *,
    temporary_tags: set[str] | None = None,
    source_index_by_file_id: Mapping[str, int] | None = None,
    variables: Mapping[str, Any] | None = None,
) -> str:
    values = _track_field_values(
        track,
        temporary_tags=temporary_tags,
        source_index_by_file_id=source_index_by_file_id,
        variables=variables,
    )

    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key == "codec":
            return _codec_name(values.get("codec_raw") or values.get("codec"), variables)
        value = values.get(key, "")
        if isinstance(value, bool):
            return key.removeprefix("flag_").replace("_", " ").title() if value else ""
        if isinstance(value, list):
            return " ".join(str(item) for item in value if str(item).strip())
        return str(value or "")

    return _clean_rendered_title(re.sub(r"\{([^{}]+)\}", repl, str(pattern or "")))


def _condition_leaf_result(
    condition: Mapping[str, Any],
    track: TrackEntry,
    *,
    temporary_tags: set[str],
    source_index_by_file_id: Mapping[str, int] | None,
    variables: Mapping[str, Any] | None,
) -> ConditionResult:
    field = str(condition.get("field") or "").strip()
    if not field:
        return ConditionResult(True, True, 0)
    op = str(condition.get("op") or "is").strip().lower()
    expected = condition.get("value")
    required = bool(condition.get("required", True))
    values = _track_field_values(
        track,
        temporary_tags=temporary_tags,
        source_index_by_file_id=source_index_by_file_id,
        variables=variables,
    )
    actual = values.get(field)
    if field == "resolution":
        similarity = _resolution_similarity_bonus(track, expected)
        matched = similarity > 0 and op not in {"not_is", "ne", "!=", "not_contains", "not_in"}
    elif field in {"width", "height"}:
        similarity = _dimension_similarity_bonus(track, field, expected)
        matched = _compare_value(actual, op, expected) or (
            similarity > 0 and op not in {"not_is", "ne", "!=", "not_contains", "not_in"}
        )
    elif field == "video_flags_hex":
        similarity = _video_flag_similarity_bonus(track, expected)
        matched = _compare_value(actual, op, expected) or (
            similarity > 0 and op not in {"not_is", "ne", "!=", "not_contains", "not_in"}
        )
    else:
        similarity = 0
        matched = _compare_value(actual, op, expected)
    if not matched and required:
        return ConditionResult(False, False, 0)
    if not matched:
        return ConditionResult(True, False, 0)
    weight = int(condition.get("weight") or _FIELD_WEIGHTS.get(field, 3))
    if field == "resolution":
        weight += similarity
    elif field in {"width", "height"}:
        weight += similarity
    elif field == "video_flags_hex":
        weight += similarity
    return ConditionResult(True, True, weight)


def _condition_result(
    condition: Any,
    track: TrackEntry,
    *,
    temporary_tags: set[str],
    source_index_by_file_id: Mapping[str, int] | None,
    variables: Mapping[str, Any] | None,
) -> ConditionResult:
    if not isinstance(condition, Mapping):
        return ConditionResult(True, True, 0)
    if "all" in condition:
        items = condition.get("all")
        if not isinstance(items, list):
            return ConditionResult(False, False, 0)
        score = 0
        matched = True
        for item in items:
            result = _condition_result(
                item,
                track,
                temporary_tags=temporary_tags,
                source_index_by_file_id=source_index_by_file_id,
                variables=variables,
            )
            if not result.eligible:
                return ConditionResult(False, False, score)
            score += result.score
            matched = matched and result.matched
        return ConditionResult(True, matched, score)
    if "any" in condition:
        items = condition.get("any")
        if not isinstance(items, list):
            return ConditionResult(False, False, 0)
        results = [
            _condition_result(
                item,
                track,
                temporary_tags=temporary_tags,
                source_index_by_file_id=source_index_by_file_id,
                variables=variables,
            )
            for item in items
        ]
        eligible = [result for result in results if result.eligible]
        matched = [result for result in eligible if result.matched]
        if matched:
            return ConditionResult(True, True, max(result.score for result in matched))
        return ConditionResult(bool(eligible), False, 0)
    if "not" in condition:
        result = _condition_result(
            condition["not"],
            track,
            temporary_tags=temporary_tags,
            source_index_by_file_id=source_index_by_file_id,
            variables=variables,
        )
        return ConditionResult(not result.matched, not result.matched, 0)
    return _condition_leaf_result(
        condition,
        track,
        temporary_tags=temporary_tags,
        source_index_by_file_id=source_index_by_file_id,
        variables=variables,
    )


def _compare_value(actual: Any, op: str, expected: Any) -> bool:
    if op in {"exists", "present"}:
        return actual not in (None, "", [], {})
    if op in {"missing", "not_exists"}:
        return actual in (None, "", [], {})
    if op in {"in", "not_in"}:
        if not isinstance(expected, list):
            expected_values = [expected]
        else:
            expected_values = expected
        if isinstance(actual, list):
            matched = any(_scalar_equals(item, expected_item) for item in actual for expected_item in expected_values)
        else:
            matched = any(_scalar_equals(actual, expected_item) for expected_item in expected_values)
        return not matched if op == "not_in" else matched
    if op in {"contains", "not_contains"}:
        needle = str(expected or "").lower()
        if isinstance(actual, list):
            haystack = " ".join(str(item) for item in actual).lower()
        else:
            haystack = str(actual or "").lower()
        matched = bool(needle and needle in haystack)
        return not matched if op == "not_contains" else matched
    if op in {"regex", "matches"}:
        try:
            return re.search(str(expected or ""), str(actual or ""), flags=re.IGNORECASE) is not None
        except re.error:
            return False
    if op in {"not_is", "ne", "!="}:
        return not _scalar_equals(actual, expected)
    return _scalar_equals(actual, expected)


def _scalar_equals(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return bool(actual) == bool(expected)
    if actual is None:
        actual = ""
    if expected is None:
        expected = ""
    actual_text = str(actual).strip()
    expected_text = str(expected).strip()
    if "-" in actual_text or "-" in expected_text:
        return normalize_lang(actual_text) == normalize_lang(expected_text)
    return actual_text.lower() == expected_text.lower()


def _resolution_similarity_bonus(track: TrackEntry, expected: Any) -> int:
    actual_resolution = _video_resolution(track)
    if not actual_resolution:
        return 0
    if isinstance(expected, Mapping):
        try:
            expected_resolution = (int(expected.get("width") or 0), int(expected.get("height") or 0))
        except (TypeError, ValueError):
            return 0
    else:
        fake = TrackEntry(0, "video", "", str(expected or ""), "", "")
        expected_resolution = _video_resolution(fake) or (0, 0)
    if expected_resolution == actual_resolution:
        return 12
    if not all(expected_resolution):
        return 0
    if _resolution_bucket(*actual_resolution) == _resolution_bucket(*expected_resolution):
        return 8
    expected_pixels = expected_resolution[0] * expected_resolution[1]
    actual_pixels = actual_resolution[0] * actual_resolution[1]
    if expected_pixels <= 0:
        return 0
    relative_delta = abs(actual_pixels - expected_pixels) / expected_pixels
    return max(0, 6 - int(relative_delta * 6))


def _dimension_similarity_bonus(track: TrackEntry, field: str, expected: Any) -> int:
    actual_resolution = _video_resolution(track)
    if not actual_resolution:
        return 0
    actual = actual_resolution[0] if field == "width" else actual_resolution[1]
    try:
        expected_value = int(expected or 0)
    except (TypeError, ValueError):
        return 0
    if expected_value <= 0 or actual <= 0:
        return 0
    if actual == expected_value:
        return 8
    relative_delta = abs(actual - expected_value) / expected_value
    return max(0, 5 - int(relative_delta * 5))


def _video_flag_similarity_bonus(track: TrackEntry, expected: Any) -> int:
    expected_flags = _parse_video_flags(expected)
    if expected_flags <= 0:
        return 0
    actual = _video_characteristic_flags(track)
    score = 0
    if actual & (expected_flags & VIDEO_RESOLUTION_MASK):
        score += 4
    expected_hdr = expected_flags & VIDEO_HDR_MASK
    if expected_hdr and actual & expected_hdr:
        score += 6
    if (expected_flags & VIDEO_FLAG_DOLBY_VISION) and (actual & VIDEO_FLAG_DOLBY_VISION):
        score += 5
    if (expected_flags & VIDEO_FLAG_HDR10PLUS) and (actual & VIDEO_FLAG_HDR10PLUS):
        score += 4
    if (expected_flags & VIDEO_BIT_DEPTH_MASK) and (actual & (expected_flags & VIDEO_BIT_DEPTH_MASK)):
        score += 3
    return score


def _rule_matches(
    rule: Mapping[str, Any],
    tracks: list[TrackEntry],
    *,
    temporary_tags_by_entry_id: Mapping[str, set[str]],
    source_index_by_file_id: Mapping[str, int] | None,
    variables: Mapping[str, Any] | None,
) -> tuple[list[tuple[int, TrackEntry]], list[TrackEntry]]:
    scored: list[tuple[int, TrackEntry]] = []
    for track in tracks:
        tags = temporary_tags_by_entry_id.get(track.entry_id, set())
        result = _condition_result(
            rule.get("match", {}),
            track,
            temporary_tags=tags,
            source_index_by_file_id=source_index_by_file_id,
            variables=variables,
        )
        if result.eligible:
            scored.append((result.score, track))
    if not scored:
        return [], []
    scope = str(rule.get("scope") or "best").strip().lower()
    if scope == "all":
        return scored, []
    if scope == "first":
        return [min(scored, key=lambda item: _track_sort_key(item[1], source_index_by_file_id))], []
    best_score = max(score for score, _track in scored)
    best = [(score, track) for score, track in scored if score == best_score]
    if len(best) == 1:
        return best, []
    if _candidate_type(best) == "video" and str(rule.get("tie_break") or "first_source_index") == "first_source_index":
        return [min(best, key=lambda item: _track_sort_key(item[1], source_index_by_file_id))], []
    return [], [track for _score, track in best]


def _candidate_type(scored: list[tuple[int, TrackEntry]]) -> str:
    types = {track.track_type for _score, track in scored}
    return next(iter(types)) if len(types) == 1 else ""


def _action_type(action: Mapping[str, Any]) -> str:
    return str(action.get("type") or action.get("action") or "").strip()


def _action_value(action: Mapping[str, Any], key: str = "value", default: Any = None) -> Any:
    return action.get(key, action.get("pattern", default))


def _rule_write_mode(rule: Mapping[str, Any], action: Mapping[str, Any] | None = None) -> str:
    raw = ""
    if action is not None:
        raw = str(action.get("write_mode") or action.get("mode") or "").strip().lower()
    if not raw:
        raw = str(rule.get("write_mode") or rule.get("mode") or "priority").strip().lower()
    aliases = {
        "replace": "override",
        "overwrite": "override",
        "force": "override",
        "fill": "add",
        "merge": "add",
        "append": "add",
        "complement": "add",
        "complementary": "add",
    }
    return aliases.get(raw, raw if raw in {"priority", "override", "add"} else "priority")


def _rule_resolution_key(rule: Mapping[str, Any], group_priority: int = 0) -> tuple[int, int]:
    return (int(group_priority or 0), int(rule.get("priority") or 0))


def _append_clean_value(left: Any, right: Any) -> str:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text:
        return right_text
    if not right_text:
        return left_text
    if right_text.lower() in left_text.lower().split(" | "):
        return left_text
    return _clean_rendered_title(f"{left_text} {right_text}")


def _field_change(
    track: TrackEntry,
    field: str,
    value: Any,
    *,
    rule: Mapping[str, Any],
    mode: str,
    priority_key: tuple[int, int],
    proposed: dict[tuple[str, str], dict[str, Any]],
    conflicts: list[dict[str, Any]],
    resolved_writes: list[dict[str, Any]],
    skipped_writes: list[dict[str, Any]],
    conflict_choices: Mapping[str, str] | None,
) -> bool:
    key = (track.entry_id, field)
    rule_id = str(rule.get("id") or "")
    previous = proposed.get(key)
    if previous is None or previous.get("value") == value or previous.get("rule_id") == rule_id:
        proposed[key] = {"rule_id": rule_id, "value": value, "priority_key": priority_key}
        _assign_track_field(track, field, value)
        return True

    conflict_id = f"{track.entry_id}:{field}:{previous.get('rule_id')}:{rule_id}"

    if mode == "add":
        if field == "title":
            merged = _append_clean_value(previous.get("value"), value)
            proposed[key] = {
                "rule_id": f"{previous.get('rule_id')}+{rule_id}",
                "value": merged,
                "priority_key": max(tuple(previous.get("priority_key") or (0, 0)), priority_key),
            }
            _assign_track_field(track, field, merged)
            resolved_writes.append(
                {
                    "id": conflict_id,
                    "track": track_summary(track),
                    "field": field,
                    "mode": "add",
                    "kept_rule_id": previous.get("rule_id"),
                    "added_rule_id": rule_id,
                    "value": merged,
                }
            )
            return True
        skipped_writes.append(
            {
                "id": conflict_id,
                "track": track_summary(track),
                "field": field,
                "mode": "add",
                "kept_rule_id": previous.get("rule_id"),
                "skipped_rule_id": rule_id,
                "kept_value": previous.get("value"),
                "skipped_value": value,
            }
        )
        return False

    if mode == "override":
        proposed[key] = {"rule_id": rule_id, "value": value, "priority_key": priority_key}
        _assign_track_field(track, field, value)
        resolved_writes.append(
            {
                "id": conflict_id,
                "track": track_summary(track),
                "field": field,
                "mode": "override",
                "previous_rule_id": previous.get("rule_id"),
                "previous_value": previous.get("value"),
                "new_rule_id": rule_id,
                "new_value": value,
            }
        )
        return True

    previous_priority = tuple(previous.get("priority_key") or (0, 0))
    if priority_key > previous_priority:
        proposed[key] = {"rule_id": rule_id, "value": value, "priority_key": priority_key}
        _assign_track_field(track, field, value)
        resolved_writes.append(
            {
                "id": conflict_id,
                "track": track_summary(track),
                "field": field,
                "mode": "priority",
                "previous_rule_id": previous.get("rule_id"),
                "previous_value": previous.get("value"),
                "winner_rule_id": rule_id,
                "winner_value": value,
            }
        )
        return True
    if priority_key < previous_priority:
        skipped_writes.append(
            {
                "id": conflict_id,
                "track": track_summary(track),
                "field": field,
                "mode": "priority",
                "kept_rule_id": previous.get("rule_id"),
                "kept_value": previous.get("value"),
                "skipped_rule_id": rule_id,
                "skipped_value": value,
            }
        )
        return False

    conflict_id = f"{track.entry_id}:{field}:{previous.get('rule_id')}:{rule_id}"
    choice = str((conflict_choices or {}).get(conflict_id) or "")
    conflict = {
        "id": conflict_id,
        "track": track_summary(track),
        "field": field,
        "current_rule_id": previous.get("rule_id"),
        "current_value": previous.get("value"),
        "new_rule_id": rule_id,
        "new_value": value,
    }
    conflicts.append(conflict)
    if choice == rule_id:
        proposed[key] = {"rule_id": rule_id, "value": value, "priority_key": priority_key}
        _assign_track_field(track, field, value)
        return True
    return False


def _assign_track_field(track: TrackEntry, field: str, value: Any) -> None:
    if field == "enabled":
        track.enabled = bool(value)
    elif field == "language":
        track.language = normalize_lang(str(value or ""), track.title)
    elif field == "title":
        track.title = str(value or "")
    elif field == "time_shift_ms":
        track.time_shift_ms = int(value or 0)
    elif field.startswith("flag_"):
        name = field.removeprefix("flag_")
        if name in FLAG_NAMES:
            setattr(track, f"flag_{name}", bool(value))


def _apply_rule_action(
    action: Mapping[str, Any],
    track: TrackEntry,
    *,
    rule: Mapping[str, Any],
    priority_key: tuple[int, int],
    tracks: list[TrackEntry],
    temporary_tags_by_entry_id: dict[str, set[str]],
    order_priorities: dict[str, int],
    proposed: dict[tuple[str, str], dict[str, Any]],
    conflicts: list[dict[str, Any]],
    resolved_writes: list[dict[str, Any]],
    skipped_writes: list[dict[str, Any]],
    report: dict[str, Any],
    conflict_choices: Mapping[str, str] | None,
    source_index_by_file_id: Mapping[str, int] | None,
    variables: Mapping[str, Any] | None,
) -> None:
    action_name = _action_type(action)
    tags = temporary_tags_by_entry_id.setdefault(track.entry_id, set())
    write_mode = _rule_write_mode(rule, action)
    if action_name == "set_enabled":
        _field_change(
            track,
            "enabled",
            bool(action.get("value", True)),
            rule=rule,
            mode=write_mode,
            priority_key=priority_key,
            proposed=proposed,
            conflicts=conflicts,
            resolved_writes=resolved_writes,
            skipped_writes=skipped_writes,
            conflict_choices=conflict_choices,
        )
    elif action_name == "set_language":
        value = str(action.get("value") or "")
        if "{" in value and "}" in value:
            value = render_title_pattern(
                value,
                track,
                temporary_tags=tags,
                source_index_by_file_id=source_index_by_file_id,
                variables=variables,
            )
        _field_change(
            track,
            "language",
            value,
            rule=rule,
            mode=write_mode,
            priority_key=priority_key,
            proposed=proposed,
            conflicts=conflicts,
            resolved_writes=resolved_writes,
            skipped_writes=skipped_writes,
            conflict_choices=conflict_choices,
        )
    elif action_name == "set_title":
        if "pattern" in action:
            value = render_title_pattern(
                str(action.get("pattern") or ""),
                track,
                temporary_tags=tags,
                source_index_by_file_id=source_index_by_file_id,
                variables=variables,
            )
        else:
            value = str(action.get("value") or "")
        _field_change(
            track,
            "title",
            value,
            rule=rule,
            mode=write_mode,
            priority_key=priority_key,
            proposed=proposed,
            conflicts=conflicts,
            resolved_writes=resolved_writes,
            skipped_writes=skipped_writes,
            conflict_choices=conflict_choices,
        )
    elif action_name == "set_time_shift_ms":
        _field_change(
            track,
            "time_shift_ms",
            int(action.get("value") or 0),
            rule=rule,
            mode=write_mode,
            priority_key=priority_key,
            proposed=proposed,
            conflicts=conflicts,
            resolved_writes=resolved_writes,
            skipped_writes=skipped_writes,
            conflict_choices=conflict_choices,
        )
    elif action_name == "set_flags":
        flags = action.get("value", action.get("flags", {}))
        if isinstance(flags, Mapping):
            for name, value in flags.items():
                if name in FLAG_NAMES:
                    _field_change(
                        track,
                        f"flag_{name}",
                        bool(value),
                        rule=rule,
                        mode=write_mode,
                        priority_key=priority_key,
                        proposed=proposed,
                        conflicts=conflicts,
                        resolved_writes=resolved_writes,
                        skipped_writes=skipped_writes,
                        conflict_choices=conflict_choices,
                    )
    elif action_name == "add_track_tags":
        values = action.get("value", [])
        if isinstance(values, str):
            values = [values]
        if isinstance(values, Sequence):
            for value in values:
                text = str(value or "").strip()
                if "{" in text and "}" in text:
                    text = render_title_pattern(
                        text,
                        track,
                        temporary_tags=tags,
                        source_index_by_file_id=source_index_by_file_id,
                        variables=variables,
                    )
                if text:
                    tags.add(text)
    elif action_name == "set_order_priority":
        order_priorities[track.entry_id] = int(action.get("value") or 0)
    elif action_name == "create_audio_variant" and track.track_type == "audio":
        _apply_audio_variant_action(
            action,
            track,
            tracks=tracks,
            rule=rule,
            temporary_tags_by_entry_id=temporary_tags_by_entry_id,
            report=report,
            source_index_by_file_id=source_index_by_file_id,
            variables=variables,
        )


def _apply_audio_variant_action(
    action: Mapping[str, Any],
    source_track: TrackEntry,
    *,
    tracks: list[TrackEntry],
    rule: Mapping[str, Any],
    temporary_tags_by_entry_id: dict[str, set[str]],
    report: dict[str, Any],
    source_index_by_file_id: Mapping[str, int] | None,
    variables: Mapping[str, Any] | None,
) -> None:
    codec = str(action.get("codec") or action.get("target_codec") or "copy").strip()
    title_pattern = str(action.get("title_pattern") or action.get("pattern") or "")
    title = (
        render_title_pattern(
            title_pattern,
            source_track,
            temporary_tags=temporary_tags_by_entry_id.get(source_track.entry_id, set()),
            source_index_by_file_id=source_index_by_file_id,
            variables=variables,
        )
        if title_pattern
        else str(action.get("title") or "")
    )
    raw_language = str(action.get("language") or source_track.language)
    if "{" in raw_language and "}" in raw_language:
        raw_language = render_title_pattern(
            raw_language,
            source_track,
            temporary_tags=temporary_tags_by_entry_id.get(source_track.entry_id, set()),
            source_index_by_file_id=source_index_by_file_id,
            variables=variables,
        )
    language = normalize_lang(raw_language, title)
    existing = _find_existing_variant(tracks, source_track, codec=codec, title=title, language=language)
    if existing is None:
        variant = clone_track_entry(source_track)
        tracks.append(variant)
        report["created_variants"].append(track_summary(variant, source_index_by_file_id=source_index_by_file_id))
    else:
        variant = existing
        report["reused_variants"].append(track_summary(variant, source_index_by_file_id=source_index_by_file_id))
    if codec and codec.lower() != "copy":
        variant.codec = codec.upper()
        bitrate = int(action.get("bitrate_kbps") or 0)
        parts = [
            part.strip()
            for part in str(source_track.orig_display_info or source_track.display_info or "").replace("·", "  ").split("  ")
            if part.strip() and "kbps" not in part.lower()
        ]
        if bitrate > 0:
            parts.append(f"{bitrate} kbps")
        variant.display_info = "  ".join(parts)
    if title:
        variant.title = title
    if language:
        variant.language = language
    variant.enabled = bool(action.get("enabled", True))
    flags = action.get("flags")
    if isinstance(flags, Mapping):
        for name, value in flags.items():
            if name in FLAG_NAMES and name != "enabled":
                setattr(variant, f"flag_{name}", bool(value))
    tags = temporary_tags_by_entry_id.setdefault(variant.entry_id, set())
    tags.add(str(rule.get("id") or "variant"))


def _find_existing_variant(
    tracks: list[TrackEntry],
    source_track: TrackEntry,
    *,
    codec: str,
    title: str,
    language: str,
) -> TrackEntry | None:
    normalized_codec = source_track.codec if not codec or codec.lower() == "copy" else codec.upper()
    for track in tracks:
        if not track.is_new or track.source_entry_id != source_track.entry_id:
            continue
        if normalized_codec and str(track.codec or "").upper() != normalized_codec.upper():
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
    conflict_choices: Mapping[str, str] | None = None,
) -> DecisionProfileResult:
    """Apply a decision profile v1 to an in-memory track list."""
    validate_decision_profile(profile)
    working = list(tracks)
    original_tracks = [track for track in working if not track.is_new]
    variables = _profile_variables(profile.get("variables"))
    temporary_tags_by_entry_id: dict[str, set[str]] = {track.entry_id: set() for track in working}
    order_priorities: dict[str, int] = {}
    proposed: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    resolved_writes: list[dict[str, Any]] = []
    skipped_writes: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "valid": True,
        "profile": profile.get("name", ""),
        "applied_rules": 0,
        "missing_rules": [],
        "ambiguous_matches": [],
        "conflicts": conflicts,
        "resolved_writes": resolved_writes,
        "skipped_writes": skipped_writes,
        "created_variants": [],
        "reused_variants": [],
        "track_tags": {},
        "order_changed": False,
    }

    groups = profile.get("groups", [])
    group_enabled: dict[str, bool] = {}
    group_priority: dict[str, int] = {}
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            group_id = str(group.get("id") or "").strip()
            if not group_id:
                continue
            group_enabled[group_id] = bool(group.get("enabled", True))
            group_priority[group_id] = int(group.get("priority") or 0)

    rules = [rule for rule in profile.get("rules", []) if isinstance(rule, Mapping)]
    rules.sort(
        key=lambda rule: (
            group_priority.get(str(rule.get("group_id") or ""), 0),
            int(rule.get("priority") or 0),
        ),
        reverse=True,
    )

    for rule in rules:
        group_id = str(rule.get("group_id") or "").strip()
        if group_id and group_enabled.get(group_id) is False:
            continue
        if not bool(rule.get("enabled", True)):
            continue
        matches, ambiguous = _rule_matches(
            rule,
            original_tracks,
            temporary_tags_by_entry_id=temporary_tags_by_entry_id,
            source_index_by_file_id=source_index_by_file_id,
            variables=variables,
        )
        if ambiguous:
            report["ambiguous_matches"].append(
                {
                    "rule_id": rule.get("id", ""),
                    "rule_label": rule.get("label", ""),
                    "candidates": [track_summary(track, source_index_by_file_id=source_index_by_file_id) for track in ambiguous],
                }
            )
            continue
        if not matches:
            report["missing_rules"].append({"rule_id": rule.get("id", ""), "rule_label": rule.get("label", "")})
            continue
        priority_key = _rule_resolution_key(rule, group_priority.get(group_id, 0))
        for _score, track in matches:
            for action in rule.get("actions", []) if isinstance(rule.get("actions"), list) else []:
                if isinstance(action, Mapping):
                    _apply_rule_action(
                        action,
                        track,
                        rule=rule,
                        priority_key=priority_key,
                        tracks=working,
                        temporary_tags_by_entry_id=temporary_tags_by_entry_id,
                        order_priorities=order_priorities,
                        proposed=proposed,
                        conflicts=conflicts,
                        resolved_writes=resolved_writes,
                        skipped_writes=skipped_writes,
                        report=report,
                        conflict_choices=conflict_choices,
                        source_index_by_file_id=source_index_by_file_id,
                        variables=variables,
                    )
            report["applied_rules"] += 1

    selection_policy = profile.get("selection_policy", {})
    disable_types: set[str] = set()
    if isinstance(selection_policy, Mapping):
        raw_disable = selection_policy.get("disable_unmatched_types", [])
        if isinstance(raw_disable, list):
            disable_types = {str(item) for item in raw_disable}
    if disable_types:
        for track in working:
            if not track.is_new and track.track_type in disable_types and (track.entry_id, "enabled") not in proposed:
                track.enabled = False

    if order_priorities:
        before_order = [track.entry_id for track in working]
        stable_index = {track.entry_id: index for index, track in enumerate(working)}
        working.sort(key=lambda track: (-order_priorities.get(track.entry_id, 0), stable_index.get(track.entry_id, 0)))
        report["order_changed"] = before_order != [track.entry_id for track in working]

    report["track_tags"] = {
        entry_id: sorted(tags)
        for entry_id, tags in temporary_tags_by_entry_id.items()
        if tags
    }
    report["valid"] = not report["conflicts"] and not report["ambiguous_matches"]
    return DecisionProfileResult(tracks=working, report=report, profile=dict(profile))


def track_summary(
    track: TrackEntry,
    *,
    source_index_by_file_id: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    payload = {
        "source": _source_index_for_track(track, source_index_by_file_id),
        "id": track.mkv_tid,
        "entry_id": track.entry_id,
        "type": track.track_type,
        "codec": track.codec,
        "language": normalize_lang(track.language, track.title),
        "title": track.title,
        "display_info": track.display_info,
        "enabled": track.enabled,
        "flags": {name: _flag_value(track, name, original=False) for name in FLAG_NAMES},
    }
    if track.track_type == "video":
        resolution = _video_resolution(track)
        if resolution:
            payload["resolution"] = {"width": resolution[0], "height": resolution[1], "bucket": _resolution_bucket(*resolution)}
        payload["video_flags_hex"] = video_flags_hex(track)
    return payload


def _condition(field: str, op: str, value: Any, *, required: bool = True, weight: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"field": field, "op": op, "value": value, "required": required}
    if weight is not None:
        item["weight"] = weight
    return item


def _match_for_track(track: TrackEntry) -> dict[str, Any]:
    items: list[dict[str, Any]] = [_condition("type", "is", track.track_type, required=True)]
    lang = normalize_lang(track.orig_language or track.language, track.orig_title or track.title)
    codec = str(track.orig_codec or track.codec or "").strip().upper()
    if track.track_type == "video":
        if codec:
            items.append(_condition("codec", "is", codec, required=False))
        resolution = _video_resolution(track)
        if resolution:
            items.append(
                _condition(
                    "resolution",
                    "is",
                    {"width": resolution[0], "height": resolution[1], "bucket": _resolution_bucket(*resolution)},
                    required=False,
                )
            )
        items.append(_condition("video_flags_hex", "is", video_flags_hex(track), required=False))
    elif track.track_type == "audio":
        if lang:
            items.append(_condition("language", "is", lang, required=True))
        if codec:
            items.append(_condition("codec", "is", codec, required=False))
        channels = _channels_from_display(track.orig_display_info or track.display_info)
        if channels:
            items.append(_condition("channels", "is", channels, required=False))
        audio_object = _audio_object_from_display(track.orig_display_info or track.display_info)
        if audio_object:
            items.append(_condition("audio_object", "is", audio_object, required=False))
    elif track.track_type == "subtitle":
        if lang:
            items.append(_condition("language", "is", lang, required=True))
        if codec:
            items.append(_condition("codec", "is", codec, required=False))
        if track.orig_title:
            items.append(_condition("source_title", "contains", track.orig_title, required=False))
    return {"all": items}


def _flag_action_payload(track: TrackEntry) -> dict[str, bool]:
    return {
        name: _flag_value(track, name, original=False)
        for name in FLAG_NAMES
    }


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
    """Capture the current remux table as an editable decision-profile v1."""
    all_tracks = [track for source in config.sources for track in source.tracks]
    original_tracks = [track for track in all_tracks if not track.is_new]
    groups = [
        {"id": "video", "label": "Video", "enabled": True, "priority": 300},
        {"id": "audio", "label": "Audio", "enabled": True, "priority": 200},
        {"id": "subtitle", "label": "Subtitles", "enabled": True, "priority": 100},
        {"id": "order", "label": "Order", "enabled": True, "priority": 10},
    ]
    rules: list[dict[str, Any]] = []
    for index, track in enumerate(original_tracks):
        actions: list[dict[str, Any]] = []
        if include_selection:
            actions.append({"type": "set_enabled", "value": bool(track.enabled)})
        if include_metadata:
            if track.language:
                actions.append({"type": "set_language", "value": normalize_lang(track.language, track.title)})
            if track.title:
                actions.append({"type": "set_title", "value": track.title})
            if int(track.time_shift_ms or 0) != 0:
                actions.append({"type": "set_time_shift_ms", "value": int(track.time_shift_ms or 0)})
        if include_flags:
            actions.append({"type": "set_flags", "value": _flag_action_payload(track)})
        if include_order and track.enabled:
            actions.append({"type": "set_order_priority", "value": len(original_tracks) - index})
        if not actions:
            continue
        rule_id = f"{track.track_type}_{index + 1}"
        rules.append(
            {
                "id": rule_id,
                "label": f"{track.type_long} {track.codec} {track.language}".strip(),
                "group_id": track.track_type if track.track_type in {"video", "audio", "subtitle"} else "",
                "tags": [track.track_type],
                "enabled": True,
                "priority": 1000 - index,
                "scope": "best",
                "tie_break": "first_source_index" if track.track_type == "video" else "ambiguous",
                "match": _match_for_track(track),
                "actions": actions,
            }
        )

    if include_audio_variants:
        by_entry_id = {track.entry_id: track for track in original_tracks}
        for index, track in enumerate(track for track in all_tracks if track.is_new and track.track_type == "audio"):
            source_track = by_entry_id.get(track.source_entry_id)
            if source_track is None:
                continue
            codec = track.codec.lower() if track.codec.upper() != source_track.codec.upper() else "copy"
            action: dict[str, Any] = {
                "type": "create_audio_variant",
                "codec": codec,
                "enabled": bool(track.enabled),
                "language": normalize_lang(track.language or source_track.language, track.title),
                "title": track.title,
                "flags": _flag_action_payload(track),
            }
            bitrate = _bitrate_from_display(track.display_info)
            if bitrate:
                action["bitrate_kbps"] = bitrate
            rules.append(
                {
                    "id": f"audio_variant_{index + 1}",
                    "label": f"Variante audio {track.codec}",
                    "group_id": "audio",
                    "tags": ["audio", "variant"],
                    "enabled": True,
                    "priority": 500 - index,
                    "scope": "best",
                    "match": _match_for_track(source_track),
                    "actions": [action],
                }
            )

    profile: dict[str, Any] = {
        "version": DECISION_PROFILE_VERSION,
        "kind": DECISION_PROFILE_KIND,
        "name": name or "Profil decisionnel",
        "description": "",
        "tags": [],
        "variables": {"codec_names": {}},
        "groups": groups,
        "selection_policy": {
            "disable_unmatched_types": ["video", "audio", "subtitle"] if include_selection else []
        },
        "rules": rules,
        "save_options": {
            "selection": include_selection,
            "metadata": include_metadata,
            "flags": include_flags,
            "order": include_order,
            "audio_variants": include_audio_variants,
        },
    }
    return profile


def _bitrate_from_display(display_info: str) -> int:
    match = re.search(r"\b(\d+)\s*kbps\b", str(display_info or ""), flags=re.IGNORECASE)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0
