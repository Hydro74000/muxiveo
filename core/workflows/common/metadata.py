from __future__ import annotations

from collections.abc import Iterable, Mapping

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


# Tags de codec acceptés par le muxer Matroska de FFmpeg : un tag MP4 Dolby
# Vision (dvh1/dvhe/dva1/dvav) recopié tel quel fait échouer l'écriture de
# l'en-tête. Forcer le tag générique conserve le dvcC (side data).
_MATROSKA_SAFE_VIDEO_TAGS = {"HEVC": "hvc1", "H264": "avc1"}


def matroska_video_tag_args(video_codecs: list[str]) -> list[str]:
    """Arguments ``-tag:v:N`` neutralisant les tags MP4 incompatibles Matroska."""
    args: list[str] = []
    for out_index, codec in enumerate(video_codecs):
        tag = _MATROSKA_SAFE_VIDEO_TAGS.get(str(codec or "").upper())
        if tag:
            args.extend([f"-tag:v:{out_index}", tag])
    return args


_DEFAULT_TRACK_KINDS = {"video": "vidéo", "audio": "audio", "subtitle": "sous-titres"}


def duplicate_default_track_warnings(tracks: Iterable[tuple[str, bool, str]]) -> list[str]:
    """Avertissement par type de piste ayant plusieurs pistes marquées par défaut.

    ``tracks`` : ``(type, défaut, libellé)`` de chaque piste de sortie, dans
    l'ordre. Les flags ne sont jamais modifiés : le lecteur choisit parmi elles
    selon ses préférences de langue, sinon la première.
    """
    defaults: dict[str, list[str]] = {}
    for track_type, is_default, label in tracks:
        if is_default:
            defaults.setdefault(track_type, []).append(label)
    return [
        f"Plusieurs pistes {_DEFAULT_TRACK_KINDS.get(kind, kind)} marquées par défaut : {', '.join(labels)}. "
        "Le lecteur choisit selon ses préférences de langue, sinon la première."
        for kind, labels in defaults.items()
        if len(labels) > 1
    ]


def output_track_label(position: int, language: str | None, name: str | None) -> str:
    """Libellé court d'une piste de sortie (position, langue, nom) pour les messages."""
    details = " ".join(part for part in ((language or "").strip(),) if part and part != "und")
    if name:
        details = f"{details} « {name} »".strip()
    return f"#{position}" + (f" ({details})" if details else "")
