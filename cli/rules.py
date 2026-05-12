"""Track selection, language normalization and rename rules for CLI jobs."""

from __future__ import annotations

import re
from typing import Any

from core.lang_tags import Rfc5646LanguageTags
from core.workflows.remux_models import TrackEntry

from cli.constants import FLAG_NAMES, TRACK_TYPES
from cli.json_io import deep_merge


def normalize_lang(tag: str | None, title: str | None = None) -> str:
    if not tag:
        return ""
    normalized = Rfc5646LanguageTags.regionalize_track_language(str(tag), title)
    if normalized:
        return normalized
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


def flag_value(track: TrackEntry, flag: str, *, original: bool = False) -> bool:
    attr = f"{'orig_' if original else ''}flag_{flag}"
    if flag == "enabled":
        attr = f"{'orig_' if original else ''}flag_enabled"
    return bool(getattr(track, attr, False))


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


def _flag_label(track: TrackEntry, flag: str) -> str:
    labels = {
        "default": "Default",
        "forced": "Forced",
        "hearing_impaired": "Malentendant",
        "visual_impaired": "Malvoyant",
        "original": "Original",
        "commentary": "Commentaire",
        "enabled": "Enabled",
    }
    return labels[flag] if flag_value(track, flag) else ""


def _render_pattern(pattern: str, track: TrackEntry) -> str:
    lang = track.language or ""
    values = {
        "lang": lang,
        "Lang": lang,
        "langname": _lang_name(lang),
        "LangName": _lang_name(lang),
        "codec": track.codec,
        "Codec": track.codec,
        "canaux": _channels_from_display(track.display_info),
        "channels": _channels_from_display(track.display_info),
        "atmos": _audio_object_from_display(track.display_info),
        "audio_object": _audio_object_from_display(track.display_info),
        "title": track.orig_title or track.title,
        "source_title": track.orig_title,
        "type": track.track_type,
        "tag_default": _flag_label(track, "default"),
        "tag_forced": _flag_label(track, "forced"),
        "tag_malentendant": _flag_label(track, "hearing_impaired"),
        "tag_malvoyant": _flag_label(track, "visual_impaired"),
        "tag_original": _flag_label(track, "original"),
        "tag_commentary": _flag_label(track, "commentary"),
        "flags": track.flags_label,
    }
    rendered = pattern
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value or ""))
        rendered = rendered.replace("<" + key + ">", str(value or ""))
    return " ".join(rendered.split()).strip()


def _track_rule(rules: dict[str, Any], track_type: str) -> dict[str, Any]:
    tracks = rules.get("tracks", {})
    if not isinstance(tracks, dict):
        return {}
    specific = tracks.get(track_type, {})
    return specific if isinstance(specific, dict) else {}


def _matches_flag_filters(track: TrackEntry, rule: dict[str, Any]) -> bool:
    filters = rule.get("flags")
    if not isinstance(filters, dict):
        return True
    for name, expected in filters.items():
        if name not in FLAG_NAMES:
            continue
        if flag_value(track, name, original=True) != bool(expected):
            return False
    return True


def _condition_matches(track: TrackEntry, condition: Any) -> bool:
    if not isinstance(condition, dict):
        return True
    if "all" in condition:
        items = condition["all"]
        return isinstance(items, list) and all(_condition_matches(track, item) for item in items)
    if "any" in condition:
        items = condition["any"]
        return isinstance(items, list) and any(_condition_matches(track, item) for item in items)
    if "not" in condition:
        return not _condition_matches(track, condition["not"])
    if "languages" in condition:
        langs = {normalize_lang(lang) for lang in condition.get("languages", [])}
        if normalize_lang(track.language, track.title) not in langs:
            return False
    if "language" in condition:
        if normalize_lang(track.language, track.title) != normalize_lang(condition.get("language")):
            return False
    if "codecs" in condition:
        codecs = {str(codec).upper() for codec in condition.get("codecs", [])}
        if track.codec.upper() not in codecs:
            return False
    if "codec" in condition and track.codec.upper() != str(condition.get("codec")).upper():
        return False
    if "flags" in condition:
        flags = condition["flags"]
        if not isinstance(flags, dict):
            return False
        for name, expected in flags.items():
            if name not in FLAG_NAMES or flag_value(track, name, original=True) != bool(expected):
                return False
    if "title_contains" in condition:
        needle = str(condition.get("title_contains") or "").lower()
        title = f"{track.orig_title} {track.title}".lower()
        if needle and needle not in title:
            return False
    if "channels" in condition:
        if _channels_from_display(track.display_info).lower() != str(condition.get("channels") or "").lower():
            return False
    if "atmos" in condition:
        has_object = bool(_audio_object_from_display(track.display_info))
        if has_object != bool(condition["atmos"]):
            return False
    return True


def _resolved_rules(rules: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(rules, dict):
        return {}
    presets = rules.get("presets", {})
    use_presets = rules.get("use_presets", [])
    if not isinstance(presets, dict) or not isinstance(use_presets, list):
        return rules
    merged: dict[str, Any] = {}
    for name in use_presets:
        preset = presets.get(str(name))
        if isinstance(preset, dict):
            merged = deep_merge(merged, preset)
    current = {key: value for key, value in rules.items() if key not in {"presets", "use_presets"}}
    return deep_merge(merged, current)


def _apply_fallback_languages(tracks: list[TrackEntry], rules: dict[str, Any]) -> None:
    for track_type in TRACK_TYPES:
        rule = _track_rule(rules, track_type)
        fallback = [normalize_lang(lang) for lang in rule.get("fallback_languages", []) if str(lang).strip()]
        if not fallback or any(track.enabled for track in tracks if track.track_type == track_type):
            continue
        for track in tracks:
            if track.track_type == track_type and normalize_lang(track.language, track.title) in fallback:
                track.enabled = True


def _apply_limits(tracks: list[TrackEntry], rules: dict[str, Any]) -> None:
    for track_type in TRACK_TYPES:
        rule = _track_rule(rules, track_type)
        raw_limit = rule.get("limit_per_language")
        if raw_limit is None:
            continue
        try:
            limit = max(0, int(raw_limit))
        except (TypeError, ValueError):
            continue
        seen: dict[str, int] = {}
        for track in tracks:
            if track.track_type != track_type or not track.enabled:
                continue
            lang = normalize_lang(track.language, track.title) or "und"
            count = seen.get(lang, 0)
            if count >= limit:
                track.enabled = False
            else:
                seen[lang] = count + 1


def _apply_auto_defaults(tracks: list[TrackEntry], rules: dict[str, Any]) -> None:
    for track_type in TRACK_TYPES:
        rule = _track_rule(rules, track_type)
        default_policy = str(rule.get("default") or "").strip().lower()
        if default_policy not in {"first", "first_per_language"}:
            continue
        enabled = [track for track in tracks if track.track_type == track_type and track.enabled]
        for track in enabled:
            track.flag_default = False
        if default_policy == "first" and enabled:
            enabled[0].flag_default = True
            continue
        if default_policy == "first_per_language":
            seen: set[str] = set()
            for track in enabled:
                lang = normalize_lang(track.language, track.title) or "und"
                if lang in seen:
                    continue
                track.flag_default = True
                seen.add(lang)


def _priority_index(track: TrackEntry, rule: dict[str, Any]) -> int:
    priority = rule.get("priority")
    if not isinstance(priority, list):
        return 0
    for index, condition in enumerate(priority):
        if _condition_matches(track, condition):
            return index
    return len(priority)


def _sort_by_priority(tracks: list[TrackEntry], rules: dict[str, Any]) -> list[TrackEntry]:
    buckets: dict[str, list[tuple[int, TrackEntry]]] = {}
    for index, track in enumerate(tracks):
        buckets.setdefault(track.track_type, []).append((index, track))
    sorted_by_type: dict[str, list[TrackEntry]] = {}
    for track_type, items in buckets.items():
        rule = _track_rule(rules, track_type)
        sorted_items = sorted(items, key=lambda item: (_priority_index(item[1], rule), item[0]))
        sorted_by_type[track_type] = [track for _, track in sorted_items]
    counters: dict[str, int] = {}
    reordered: list[TrackEntry] = []
    for track in tracks:
        track_type = track.track_type
        position = counters.get(track_type, 0)
        reordered.append(sorted_by_type[track_type][position])
        counters[track_type] = position + 1
    return reordered


def apply_track_rules(tracks: list[TrackEntry], rules: dict[str, Any]) -> list[TrackEntry]:
    rules = _resolved_rules(rules)
    rename_patterns = rules.get("rename_patterns", {})
    if not isinstance(rename_patterns, dict):
        rename_patterns = {}
    normalize_languages = bool(rules.get("normalize_languages", True))

    out: list[TrackEntry] = []
    for track in tracks:
        rule = _track_rule(rules, track.track_type)
        include = bool(rule.get("include", True))
        allowed_languages = [
            normalize_lang(lang)
            for lang in rule.get("languages", [])
            if str(lang).strip()
        ]
        if allowed_languages:
            track_lang = normalize_lang(track.language, track.title)
            include = include and track_lang in allowed_languages
        include = include and _matches_flag_filters(track, rule)
        include = include and _condition_matches(track, rule.get("conditions", {}))

        track.enabled = include
        if normalize_languages:
            track.language = normalize_lang(track.language, track.title)

        pattern = str(rule.get("rename_pattern") or rename_patterns.get(track.track_type) or "").strip()
        if pattern:
            track.title = _render_pattern(pattern, track)
        out.append(track)
    _apply_fallback_languages(out, rules)
    _apply_limits(out, rules)
    _apply_auto_defaults(out, rules)
    return _sort_by_priority(out, rules)
