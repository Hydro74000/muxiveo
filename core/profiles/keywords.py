"""Decision-profile keyword registry and shared track value helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from core.lang_tags import Rfc5646LanguageTags
from core.workflows.remux_models import TrackEntry


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
    "source_language",
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
    "track_tags",
    "none",
)
TITLE_KEYWORDS = DECISION_KEYWORDS

KEYWORD_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Piste", ("type", "source_index", "track_index", "track_tags")),
    ("Langue", ("language", "lang", "lang_name", "source_language")),
    ("Source", ("title", "source_title")),
    (
        "Audio",
        (
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
        ),
    ),
    (
        "Video",
        (
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
        ),
    ),
    (
        "Flags",
        (
            "flags",
            "flag_enabled",
            "flag_default",
            "flag_forced",
            "flag_hearing_impaired",
            "flag_visual_impaired",
            "flag_original",
            "flag_commentary",
        ),
    ),
    ("Divers", ("none",)),
)


def is_none_keyword(keyword: str) -> bool:
    return str(keyword or "").strip().strip("{}").lower() == "none"


def normalize_lang(tag: str | None, title: str | None = None) -> str:
    if not tag:
        return ""
    regionalized = Rfc5646LanguageTags.regionalize_track_language(str(tag), title)
    if regionalized:
        return regionalized
    canonical = Rfc5646LanguageTags.normalize(str(tag))
    return canonical or str(tag).strip()


def lang_name(tag: str) -> str:
    if not tag:
        return ""
    canonical = Rfc5646LanguageTags.normalize(tag) or tag
    base = canonical.split("-", 1)[0]
    label = Rfc5646LanguageTags.TAGS.get(canonical) or Rfc5646LanguageTags.TAGS.get(base, canonical)
    base_label = Rfc5646LanguageTags.TAGS.get(base, label).split(" (", 1)[0]
    default_regional = Rfc5646LanguageTags.from_ietf_short_regional(base)
    if "-" in canonical and default_regional and canonical != default_regional:
        return label
    return base_label


def channels_from_display(display_info: str) -> str:
    text = str(display_info or "")
    match = re.search(r"\b(?:mono|stereo|[1-9](?:\.[0-9])?)\b", text, flags=re.IGNORECASE)
    return match.group(0) if match else ""


def audio_object_from_display(display_info: str) -> str:
    text = str(display_info or "").lower()
    if "atmos" in text:
        return "Atmos"
    if "dts:x" in text or "dtsx" in text:
        return "DTS:X"
    return ""


def flag_value(track: TrackEntry, name: str, *, original: bool = False) -> bool:
    attr = f"{'orig_' if original else ''}flag_{name}"
    if name == "enabled":
        attr = f"{'orig_' if original else ''}flag_enabled"
    if not hasattr(track, attr):
        attr = f"flag_{name}"
    return bool(getattr(track, attr, False))


def source_index_for_track(
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


def video_text(track: TrackEntry) -> str:
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


def video_resolution(track: TrackEntry) -> tuple[int, int] | None:
    text = video_text(track)
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


def resolution_bucket(width: int, height: int) -> str:
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


def video_release_resolution(track: TrackEntry) -> str:
    resolution = video_resolution(track)
    if not resolution:
        return ""
    longest = max(resolution)
    if longest > 7000:
        return "8K"
    if longest >= 3800:
        return "2160p"
    if longest >= 1900:
        return "1080p"
    if longest >= 1250:
        return "720p"
    return "SD"


def resolution_flag(width: int, height: int) -> int:
    bucket = resolution_bucket(width, height)
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
        flags |= resolution_flag(width, height)
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


def video_characteristic_flags(track: TrackEntry) -> int:
    if track.track_type != "video":
        return 0
    flags = 0
    resolution = video_resolution(track)
    if resolution:
        flags |= resolution_flag(*resolution)
    text = video_text(track).lower()
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
    return f"0x{video_characteristic_flags(track):08X}"


def parse_video_flags(raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    text = str(raw or "").strip()
    if not text:
        return 0
    try:
        return int(text, 16 if text.lower().startswith("0x") else 10)
    except ValueError:
        return 0


def video_hdr_label(track: TrackEntry) -> str:
    flags = video_characteristic_flags(track)
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


def profile_variables(variables: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return variables if isinstance(variables, Mapping) else {}


def alias_match_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def render_alias(keyword: str, value: Any, variables: Mapping[str, Any] | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    variables_map = profile_variables(variables)
    aliases = variables_map.get("aliases", {})
    if not isinstance(aliases, Mapping):
        return text

    needle = alias_match_key(text)
    scopes = (str(keyword or "").strip().strip("{}"), "*")
    for scope in scopes:
        section = aliases.get(scope)
        if not isinstance(section, Mapping):
            continue
        for source, replacement in section.items():
            if alias_match_key(source) != needle:
                continue
            rendered = str(replacement or "").strip()
            if rendered:
                return rendered
    return text


def keyword_to_match_field(keyword: str) -> str:
    key = str(keyword or "").strip().strip("{}")
    if is_none_keyword(key):
        return ""
    aliases = {
        "hdr": "video_hdr",
        "dolby_vision": "video_dolby_vision",
        "dovi": "video_dolby_vision",
        "hdr10plus": "video_hdr10plus",
        "atmos": "codec_atmos",
        "dtsx": "codec_dtsx",
    }
    key = aliases.get(key, key)
    if key.startswith("flag_"):
        return key
    if key in {
        "codec_atmos",
        "codec_dtsx",
        "video_hdr",
        "video_hdr10",
        "video_hdr10plus",
        "video_dolby_vision",
        "video_hlg",
        "video_sdr",
    }:
        return key
    return ""


def codec_alias_key(codec: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(codec or "").upper())


def codec_name(codec: str, variables: Mapping[str, Any] | None = None) -> str:
    raw_codec = str(codec or "").strip().upper()
    variables_map = profile_variables(variables)
    codec_names = variables_map.get("codec_names", {})
    if isinstance(codec_names, Mapping):
        expected = codec_alias_key(raw_codec)
        for key, value in codec_names.items():
            if codec_alias_key(key) == expected:
                text = str(value or "").strip()
                if text:
                    return text
    return raw_codec


def flags_label(track: TrackEntry) -> str:
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
        if flag_value(track, name, original=False):
            labels.append(label)
    return " ".join(labels)


def _prefixed_aliases(track_type: str, values: Mapping[str, Any]) -> dict[str, Any]:
    primary = {"video": "video", "audio": "audio", "subtitle": "sub"}.get(track_type)
    if not primary:
        return {}
    prefixes = [primary]
    if track_type == "subtitle":
        prefixes.append("subtitle")
    aliases: dict[str, Any] = {}
    for key, value in values.items():
        dashed_key = key.replace("_", "-")
        for prefix in prefixes:
            aliases[f"{prefix}-{dashed_key}"] = value
            aliases[f"{prefix}_{key}"] = value
    return aliases


def track_field_values(
    track: TrackEntry,
    *,
    temporary_tags: set[str] | None = None,
    source_index_by_file_id: Mapping[str, int] | None = None,
    variables: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    lang = normalize_lang(track.language, track.title)
    source_lang = normalize_lang(track.orig_language or track.language, track.orig_title or track.title)
    channels = channels_from_display(track.orig_display_info or track.display_info)
    audio_object = audio_object_from_display(track.orig_display_info or track.display_info)
    resolution = video_resolution(track)
    width, height = resolution or (0, 0)
    resolution_text = f"{resolution[0]}x{resolution[1]}" if resolution else ""
    video_flags = video_characteristic_flags(track)
    flags = {name: flag_value(track, name, original=True) for name in FLAG_NAMES}
    codec = str(track.orig_codec or track.codec or "").strip().upper()
    values: dict[str, Any] = {
        "type": track.track_type,
        "source_index": source_index_for_track(track, source_index_by_file_id),
        "track_index": int(track.mkv_tid),
        "language": lang,
        "lang": lang,
        "lang_name": lang_name(lang),
        "source_language": source_lang,
        "title": track.title,
        "source_title": track.orig_title or track.title,
        "codec": codec,
        "codec_raw": codec,
        "codec_name": codec_name(codec, variables),
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
        "hdr": video_hdr_label(track),
        "video_hdr": bool(video_flags & VIDEO_FLAG_HDR),
        "video_hdr10": bool(video_flags & VIDEO_FLAG_HDR10),
        "video_hdr10plus": bool(video_flags & VIDEO_FLAG_HDR10PLUS),
        "video_dolby_vision": bool(video_flags & VIDEO_FLAG_DOLBY_VISION),
        "video_hlg": bool(video_flags & VIDEO_FLAG_HLG),
        "video_sdr": bool(video_flags & VIDEO_FLAG_SDR),
        "video_flags_hex": video_flags_hex(track),
        "flags": flags_label(track),
        "track_tags": sorted(temporary_tags or set()),
        "none": "",
    }
    for name, value in flags.items():
        values[f"flag_{name}"] = bool(value)
    values.update(_prefixed_aliases(track.track_type, values))
    return values
