from __future__ import annotations

from collections.abc import Mapping

from core.lang_tags import Rfc5646LanguageTags as LangTags

STREAM_SPEC_BY_TRACK_TYPE: dict[str, str] = {
    "video": "v",
    "audio": "a",
    "subtitle": "s",
}


def resolve_global_tags(
    tag_overrides: Mapping[str, str] | None,
    file_title: str = "",
) -> dict[str, str]:
    tags: dict[str, str] = {}
    if tag_overrides is not None:
        for key, value in tag_overrides.items():
            key_s = str(key).strip()
            value_s = str(value).strip()
            if not key_s or not value_s:
                continue
            tags[key_s] = value_s
    if file_title.strip():
        tags["title"] = file_title.strip()
    return tags


def normalize_track_language(
    language: str,
    title: str | None = None,
    *,
    default_und: bool = False,
) -> str | None:
    raw = (language or "").strip()
    if not raw:
        return "und" if default_und else None

    canonical = LangTags.normalize(raw) or raw
    if canonical.lower() == "und":
        return "und"

    regional = LangTags.regionalize_track_language(canonical, title) or canonical
    if LangTags.is_valid(regional):
        return regional
    if LangTags.is_valid(canonical):
        return canonical
    return "und" if default_und else None


def normalize_track_language_from_track(track) -> str:
    normalized = normalize_track_language(
        getattr(track, "language", ""),
        getattr(track, "title", None),
        default_und=True,
    )
    return normalized or "und"


def disposition_value(
    *,
    flag_default: bool | None,
    flag_forced: bool | None,
    flag_hearing_impaired: bool | None,
    flag_visual_impaired: bool | None,
    flag_original: bool | None,
    flag_commentary: bool | None,
    allow_partial: bool = False,
) -> str | None:
    values = (
        flag_default,
        flag_forced,
        flag_hearing_impaired,
        flag_visual_impaired,
        flag_original,
        flag_commentary,
    )
    if all(v is None for v in values):
        return None
    if (not allow_partial) and any(v is None for v in values):
        return None

    flags: list[str] = []
    if flag_default:
        flags.append("default")
    if flag_forced:
        flags.append("forced")
    if flag_hearing_impaired:
        flags.append("hearing_impaired")
    if flag_visual_impaired:
        flags.append("visual_impaired")
    if flag_original:
        flags.append("original")
    if flag_commentary:
        flags.append("comment")
    return "+".join(flags) if flags else "0"
