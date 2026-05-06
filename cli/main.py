"""Headless remux CLI for Mediarecode."""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QEventLoop

from core.config import AppConfig
from core.inspector import (
    AttachmentInfo,
    ChapterEntry,
    FileInfo,
    FileInspector,
    fmt_timecode_display,
)
from core.lang_tags import Rfc5646LanguageTags
from core.media_info_fetcher import (
    TmdbError,
    TmdbFetcher,
    clean_filename_for_search,
    extract_year_from_filename,
)
from core.runner import ToolNotFoundError
from core.workflows.remux import RemuxWorkflow
from core.workflows.remux_models import RemuxConfig, SourceInput, TrackEntry, tracks_from_file_info


EXIT_OK = 0
EXIT_ARGS = 2
EXIT_VALIDATION = 3
EXIT_TOOL = 4
EXIT_EXISTS = 5
EXIT_WORKFLOW = 6
EXIT_PARTIAL = 7

TRACK_TYPES = {"video", "audio", "subtitle"}
FLAG_NAMES = (
    "enabled",
    "default",
    "forced",
    "hearing_impaired",
    "visual_impaired",
    "original",
    "commentary",
)


class CliError(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_WORKFLOW) -> None:
        self.exit_code = exit_code
        super().__init__(message)


class ContractError(CliError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("Contrat JSON invalide :\n" + "\n".join(f"- {err}" for err in errors), EXIT_ARGS)
        self.errors = errors


class Logger:
    def __init__(self, *, fmt: str = "text", stream=None) -> None:
        self.fmt = fmt
        self.stream = stream or sys.stderr

    def emit(self, level: str, message: str, **fields: Any) -> None:
        if self.fmt == "jsonl":
            payload = {"level": level.lower(), "message": message, **fields}
            print(json.dumps(payload, ensure_ascii=False, default=_json_default), file=self.stream)
            return
        print(f"[{level.upper()}] {message}", file=self.stream)

    def workflow_log(self, level: str, message: str) -> None:
        self.emit(level, message)


def _ensure_qcore_app(argv: list[str] | None = None) -> QCoreApplication:
    app = QCoreApplication.instance()
    if isinstance(app, QCoreApplication):
        return app
    return QCoreApplication(argv or [sys.argv[0]])


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.name
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CliError(f"JSON introuvable : {path}", EXIT_ARGS) from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"JSON invalide {path}:{exc.lineno}:{exc.colno} : {exc.msg}", EXIT_ARGS) from exc
    if not isinstance(data, dict):
        raise CliError("Le fichier JSON racine doit être un objet.", EXIT_ARGS)
    return data


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _expect(errors: list[str], path: str, value: Any, expected: str, predicate) -> None:
    if not predicate(value):
        errors.append(f"{path}: attendu {expected}, reçu {_type_name(value)}")


def _is_string(value: Any) -> bool:
    return isinstance(value, str)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_string_or_number(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _validate_string_list(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, list):
        errors.append(f"{path}: attendu array[string], reçu {_type_name(value)}")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{path}[{index}]: attendu string, reçu {_type_name(item)}")


def _validate_source(errors: list[str], path: str, value: Any) -> None:
    if isinstance(value, str):
        return
    if not isinstance(value, dict):
        errors.append(f"{path}: attendu string ou object, reçu {_type_name(value)}")
        return
    if "path" not in value:
        errors.append(f"{path}.path: champ requis")
    elif not isinstance(value["path"], str):
        errors.append(f"{path}.path: attendu string, reçu {_type_name(value['path'])}")
    if "attachments" in value:
        attachments = value["attachments"]
        if attachments not in (True, False, None, "all", "none") and not isinstance(attachments, list):
            errors.append(f"{path}.attachments: attendu bool, 'all', 'none' ou array, reçu {_type_name(attachments)}")
        elif isinstance(attachments, list):
            for index, item in enumerate(attachments):
                if not isinstance(item, (str, int)) or isinstance(item, bool):
                    errors.append(f"{path}.attachments[{index}]: attendu string ou integer, reçu {_type_name(item)}")
    if "copy_tags" in value:
        _expect(errors, f"{path}.copy_tags", value["copy_tags"], "bool", _is_bool)


def _validate_rules(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: attendu object, reçu {_type_name(value)}")
        return
    if "normalize_languages" in value:
        _expect(errors, f"{path}.normalize_languages", value["normalize_languages"], "bool", _is_bool)
    if "rename_patterns" in value:
        rename_patterns = value["rename_patterns"]
        if not isinstance(rename_patterns, dict):
            errors.append(f"{path}.rename_patterns: attendu object, reçu {_type_name(rename_patterns)}")
        else:
            for track_type, pattern in rename_patterns.items():
                if track_type not in TRACK_TYPES:
                    errors.append(f"{path}.rename_patterns.{track_type}: type de piste inconnu")
                elif not isinstance(pattern, str):
                    errors.append(f"{path}.rename_patterns.{track_type}: attendu string, reçu {_type_name(pattern)}")
    if "use_presets" in value:
        _validate_string_list(errors, f"{path}.use_presets", value["use_presets"])
    if "presets" in value:
        presets = value["presets"]
        if not isinstance(presets, dict):
            errors.append(f"{path}.presets: attendu object, reçu {_type_name(presets)}")
        else:
            for name, preset in presets.items():
                _validate_rules(errors, f"{path}.presets.{name}", preset)
    if "tracks" not in value:
        return
    tracks = value["tracks"]
    if not isinstance(tracks, dict):
        errors.append(f"{path}.tracks: attendu object, reçu {_type_name(tracks)}")
        return
    for track_type, rule in tracks.items():
        rule_path = f"{path}.tracks.{track_type}"
        if track_type not in TRACK_TYPES:
            errors.append(f"{rule_path}: type de piste inconnu")
            continue
        if not isinstance(rule, dict):
            errors.append(f"{rule_path}: attendu object, reçu {_type_name(rule)}")
            continue
        if "include" in rule:
            _expect(errors, f"{rule_path}.include", rule["include"], "bool", _is_bool)
        if "languages" in rule:
            _validate_string_list(errors, f"{rule_path}.languages", rule["languages"])
        if "rename_pattern" in rule:
            _expect(errors, f"{rule_path}.rename_pattern", rule["rename_pattern"], "string", _is_string)
        if "fallback_languages" in rule:
            _validate_string_list(errors, f"{rule_path}.fallback_languages", rule["fallback_languages"])
        if "limit_per_language" in rule:
            _expect(errors, f"{rule_path}.limit_per_language", rule["limit_per_language"], "integer", _is_int)
        if "default" in rule and rule["default"] not in {"", "first", "first_per_language"}:
            errors.append(f"{rule_path}.default: attendu 'first' ou 'first_per_language'")
        if "conditions" in rule:
            _validate_condition(errors, f"{rule_path}.conditions", rule["conditions"])
        if "priority" in rule:
            priority = rule["priority"]
            if not isinstance(priority, list):
                errors.append(f"{rule_path}.priority: attendu array[object], reçu {_type_name(priority)}")
            else:
                for index, condition in enumerate(priority):
                    _validate_condition(errors, f"{rule_path}.priority[{index}]", condition)
        if "flags" in rule:
            flags = rule["flags"]
            if not isinstance(flags, dict):
                errors.append(f"{rule_path}.flags: attendu object, reçu {_type_name(flags)}")
            else:
                for flag, expected in flags.items():
                    flag_path = f"{rule_path}.flags.{flag}"
                    if flag not in FLAG_NAMES:
                        errors.append(f"{flag_path}: flag inconnu")
                    elif not isinstance(expected, bool):
                        errors.append(f"{flag_path}: attendu bool, reçu {_type_name(expected)}")


def _validate_condition(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: attendu object, reçu {_type_name(value)}")
        return
    for key in ("all", "any"):
        if key in value:
            items = value[key]
            if not isinstance(items, list):
                errors.append(f"{path}.{key}: attendu array[object], reçu {_type_name(items)}")
            else:
                for index, item in enumerate(items):
                    _validate_condition(errors, f"{path}.{key}[{index}]", item)
    if "not" in value:
        _validate_condition(errors, f"{path}.not", value["not"])
    if "languages" in value:
        _validate_string_list(errors, f"{path}.languages", value["languages"])
    if "language" in value:
        _expect(errors, f"{path}.language", value["language"], "string", _is_string)
    if "codecs" in value:
        _validate_string_list(errors, f"{path}.codecs", value["codecs"])
    if "codec" in value:
        _expect(errors, f"{path}.codec", value["codec"], "string", _is_string)
    if "flags" in value:
        flags = value["flags"]
        if not isinstance(flags, dict):
            errors.append(f"{path}.flags: attendu object, reçu {_type_name(flags)}")
        else:
            for flag, expected in flags.items():
                flag_path = f"{path}.flags.{flag}"
                if flag not in FLAG_NAMES:
                    errors.append(f"{flag_path}: flag inconnu")
                elif not isinstance(expected, bool):
                    errors.append(f"{flag_path}: attendu bool, reçu {_type_name(expected)}")
    if "title_contains" in value:
        _expect(errors, f"{path}.title_contains", value["title_contains"], "string", _is_string)
    if "channels" in value:
        _expect(errors, f"{path}.channels", value["channels"], "string", _is_string)
    if "atmos" in value:
        _expect(errors, f"{path}.atmos", value["atmos"], "bool", _is_bool)


def _validate_track_edit(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: attendu object, reçu {_type_name(value)}")
        return
    for key in ("source", "source_index", "id", "mkv_tid", "stream", "time_shift_ms"):
        if key in value:
            _expect(errors, f"{path}.{key}", value[key], "integer", _is_int)
    if "enabled" in value:
        _expect(errors, f"{path}.enabled", value["enabled"], "bool", _is_bool)
    for key in ("language", "title"):
        if key in value:
            _expect(errors, f"{path}.{key}", value[key], "string", _is_string)
    if "flags" in value:
        flags = value["flags"]
        if not isinstance(flags, dict):
            errors.append(f"{path}.flags: attendu object, reçu {_type_name(flags)}")
        else:
            for flag, expected in flags.items():
                flag_path = f"{path}.flags.{flag}"
                if flag not in FLAG_NAMES:
                    errors.append(f"{flag_path}: flag inconnu")
                elif not isinstance(expected, bool):
                    errors.append(f"{flag_path}: attendu bool, reçu {_type_name(expected)}")


def _validate_chapter(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: attendu object, reçu {_type_name(value)}")
        return
    if not any(key in value for key in ("timestamp", "timecode", "time", "timecode_s")):
        errors.append(f"{path}.timestamp: champ requis")
    for key in ("timestamp", "timecode", "time", "timecode_s"):
        if key in value:
            _expect(errors, f"{path}.{key}", value[key], "string ou number", _is_string_or_number)
    for key in ("chaptername", "name", "title"):
        if key in value:
            _expect(errors, f"{path}.{key}", value[key], "string", _is_string)


def _validate_chapters(errors: list[str], path: str, value: Any) -> None:
    if value is False:
        return
    if not isinstance(value, dict):
        errors.append(f"{path}: attendu false ou object, reçu {_type_name(value)}")
        return
    if "source_index" in value:
        _expect(errors, f"{path}.source_index", value["source_index"], "integer", _is_int)
    if "include_source" in value:
        _expect(errors, f"{path}.include_source", value["include_source"], "bool", _is_bool)
    if "import" in value:
        _expect(errors, f"{path}.import", value["import"], "string", _is_string)
    if "add" in value:
        add = value["add"]
        if not isinstance(add, list):
            errors.append(f"{path}.add: attendu array[object], reçu {_type_name(add)}")
        else:
            for index, item in enumerate(add):
                _validate_chapter(errors, f"{path}.add[{index}]", item)


def _validate_tmdb(errors: list[str], path: str, value: Any) -> None:
    if value in (False, None):
        return
    if value is True:
        return
    if not isinstance(value, dict):
        errors.append(f"{path}: attendu bool ou object, reçu {_type_name(value)}")
        return
    if "enabled" in value:
        _expect(errors, f"{path}.enabled", value["enabled"], "bool", _is_bool)
    if "kind" in value and value["kind"] not in {"all", "movie", "tv"}:
        errors.append(f"{path}.kind: attendu 'all', 'movie' ou 'tv'")
    for key in ("query", "title", "year", "season", "episode", "language", "api_key", "bearer_token"):
        if key in value:
            _expect(errors, f"{path}.{key}", value[key], "string", _is_string)
    for key in ("id", "tmdb_id"):
        if key in value:
            _expect(errors, f"{path}.{key}", value[key], "integer", _is_int)


def validate_job_contract(job: dict[str, Any], *, path: str = "$", require_version: bool = False) -> None:
    errors: list[str] = []
    if not isinstance(job, dict):
        raise ContractError([f"{path}: attendu object, reçu {_type_name(job)}"])
    if "version" not in job:
        if require_version:
            errors.append(f"{path}.version: champ requis")
    elif job["version"] != 1:
        errors.append(f"{path}.version: attendu 1, reçu {job['version']!r}")
    if "sources" in job:
        sources = job["sources"]
        if isinstance(sources, str):
            pass
        elif isinstance(sources, list):
            if not sources:
                errors.append(f"{path}.sources: ne doit pas être vide")
            for index, source in enumerate(sources):
                _validate_source(errors, f"{path}.sources[{index}]", source)
        else:
            errors.append(f"{path}.sources: attendu string ou array, reçu {_type_name(sources)}")
    if "input" in job and not isinstance(job["input"], (str, list)):
        errors.append(f"{path}.input: attendu string ou array, reçu {_type_name(job['input'])}")
    if "output" in job:
        _expect(errors, f"{path}.output", job["output"], "string", _is_string)
    if "work_dir" in job:
        _expect(errors, f"{path}.work_dir", job["work_dir"], "string", _is_string)
    if "file_title" in job:
        _expect(errors, f"{path}.file_title", job["file_title"], "string", _is_string)
    if "rules" in job:
        _validate_rules(errors, f"{path}.rules", job["rules"])
    if "tracks" in job:
        tracks = job["tracks"]
        if not isinstance(tracks, list):
            errors.append(f"{path}.tracks: attendu array[object], reçu {_type_name(tracks)}")
        else:
            for index, item in enumerate(tracks):
                _validate_track_edit(errors, f"{path}.tracks[{index}]", item)
    if "track_order" in job and not isinstance(job["track_order"], list):
        errors.append(f"{path}.track_order: attendu array, reçu {_type_name(job['track_order'])}")
    if "chapters" in job:
        _validate_chapters(errors, f"{path}.chapters", job["chapters"])
    if "tmdb" in job:
        _validate_tmdb(errors, f"{path}.tmdb", job["tmdb"])
    if "extra_attachments" in job:
        _validate_string_list(errors, f"{path}.extra_attachments", job["extra_attachments"])
    if "tag_overrides" in job:
        tags = job["tag_overrides"]
        if not isinstance(tags, dict):
            errors.append(f"{path}.tag_overrides: attendu object, reçu {_type_name(tags)}")
        else:
            for key, value in tags.items():
                if not isinstance(value, str):
                    errors.append(f"{path}.tag_overrides.{key}: attendu string, reçu {_type_name(value)}")
    if errors:
        raise ContractError(errors)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _csv_values(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _normalize_lang(tag: str | None, title: str | None = None) -> str:
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


def _flag_value(track: TrackEntry, flag: str, *, original: bool = False) -> bool:
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
    return labels[flag] if _flag_value(track, flag) else ""


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
        if _flag_value(track, name, original=True) != bool(expected):
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
        langs = {_normalize_lang(lang) for lang in condition.get("languages", [])}
        if _normalize_lang(track.language, track.title) not in langs:
            return False
    if "language" in condition:
        if _normalize_lang(track.language, track.title) != _normalize_lang(condition.get("language")):
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
            if name not in FLAG_NAMES or _flag_value(track, name, original=True) != bool(expected):
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


def _merge_rule_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    return _deep_merge(base, override)


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
            merged = _merge_rule_dicts(merged, preset)
    current = {key: value for key, value in rules.items() if key not in {"presets", "use_presets"}}
    return _merge_rule_dicts(merged, current)


def _apply_fallback_languages(tracks: list[TrackEntry], rules: dict[str, Any]) -> None:
    for track_type in TRACK_TYPES:
        rule = _track_rule(rules, track_type)
        fallback = [_normalize_lang(lang) for lang in rule.get("fallback_languages", []) if str(lang).strip()]
        if not fallback or any(track.enabled for track in tracks if track.track_type == track_type):
            continue
        for track in tracks:
            if track.track_type == track_type and _normalize_lang(track.language, track.title) in fallback:
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
            lang = _normalize_lang(track.language, track.title) or "und"
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
                lang = _normalize_lang(track.language, track.title) or "und"
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
            _normalize_lang(lang)
            for lang in rule.get("languages", [])
            if str(lang).strip()
        ]
        if allowed_languages:
            track_lang = _normalize_lang(track.language, track.title)
            include = include and track_lang in allowed_languages
        include = include and _matches_flag_filters(track, rule)
        include = include and _condition_matches(track, rule.get("conditions", {}))

        track.enabled = include
        if normalize_languages:
            track.language = _normalize_lang(track.language, track.title)

        pattern = str(rule.get("rename_pattern") or rename_patterns.get(track.track_type) or "").strip()
        if pattern:
            track.title = _render_pattern(pattern, track)
        out.append(track)
    _apply_fallback_languages(out, rules)
    _apply_limits(out, rules)
    _apply_auto_defaults(out, rules)
    return _sort_by_priority(out, rules)


def _source_path_items(job: dict[str, Any], cli_inputs: list[str] | None = None) -> list[dict[str, Any]]:
    if cli_inputs:
        return [{"path": value} for value in cli_inputs]
    raw_sources = job.get("sources")
    if raw_sources is None and job.get("input"):
        raw_sources = [job["input"]]
    if isinstance(raw_sources, (str, Path)):
        raw_sources = [raw_sources]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise CliError("Au moins une source est requise.", EXIT_ARGS)
    items: list[dict[str, Any]] = []
    for item in raw_sources:
        if isinstance(item, dict):
            if not item.get("path"):
                raise CliError("Chaque source JSON doit contenir `path`.", EXIT_ARGS)
            items.append(item)
        else:
            items.append({"path": item})
    return items


def _inspect_sources(
    job: dict[str, Any],
    config: AppConfig,
    args: argparse.Namespace,
    logger: Logger,
    *,
    cli_inputs: list[str] | None = None,
) -> tuple[list[SourceInput], list[FileInfo], list[TrackEntry]]:
    ffprobe = args.ffprobe or job.get("ffprobe") or config.tool_ffprobe
    mediainfo = args.mediainfo or job.get("mediainfo") or config.tool_mediainfo
    inspector = FileInspector(
        ffprobe_bin=str(ffprobe),
        mediainfo_bin=str(mediainfo),
        verbose_output=(lambda line: logger.emit("debug", line)) if getattr(args, "verbose", False) else None,
    )
    source_items = _source_path_items(job, cli_inputs)
    sources: list[SourceInput] = []
    infos: list[FileInfo] = []
    all_tracks: list[TrackEntry] = []

    for source_index, item in enumerate(source_items):
        path = Path(str(item["path"])).expanduser()
        if not path.exists():
            raise CliError(f"Source introuvable : {path}", EXIT_VALIDATION)
        info = inspector.inspect(path)
        file_id = f"src{source_index}"
        tracks = tracks_from_file_info(info, file_id=file_id)
        infos.append(info)
        attachment_selection = _selected_attachments(info, item)
        source = SourceInput(
            path=path,
            file_index=source_index,
            tracks=tracks,
            selected_attachments=attachment_selection,
            attachment_count=len(info.attachments),
            copy_tags=bool(item.get("copy_tags", False)),
            has_chapters=bool(info.chapters and info.chapters.entries),
        )
        sources.append(source)
        all_tracks.extend(tracks)
    return sources, infos, all_tracks


def _selected_attachments(info: FileInfo, item: dict[str, Any]) -> list[AttachmentInfo]:
    selection = item.get("attachments", "none")
    if selection is True or selection == "all":
        return list(info.attachments)
    if selection in (False, None, "none"):
        return []
    names: set[str] = set()
    indices: set[int] = set()
    if isinstance(selection, list):
        for entry in selection:
            if isinstance(entry, int):
                indices.add(entry)
            else:
                names.add(str(entry))
    return [
        att
        for att in info.attachments
        if att.local_index in indices or att.index in indices or att.filename in names
    ]


def _apply_explicit_track_edits(job: dict[str, Any], tracks: list[TrackEntry]) -> None:
    specs = job.get("tracks", [])
    if not isinstance(specs, list):
        return
    lookup = {(t.file_id, t.mkv_tid): t for t in tracks}
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        source_index = int(spec.get("source", spec.get("source_index", 0)))
        file_id = f"src{source_index}"
        tid = spec.get("id", spec.get("mkv_tid", spec.get("stream")))
        if tid is None:
            continue
        track = lookup.get((file_id, int(tid)))
        if track is None:
            continue
        if "enabled" in spec:
            track.enabled = bool(spec["enabled"])
        if "language" in spec:
            track.language = _normalize_lang(str(spec["language"]), track.title)
        if "title" in spec:
            track.title = str(spec["title"])
        flags = spec.get("flags")
        if isinstance(flags, dict):
            for name, value in flags.items():
                if name in FLAG_NAMES:
                    setattr(track, f"flag_{name}", bool(value))
        if "time_shift_ms" in spec:
            track.time_shift_ms = int(spec["time_shift_ms"])


def _track_order(job: dict[str, Any], tracks: list[TrackEntry]) -> list[tuple[int, int, str]]:
    explicit = job.get("track_order")
    if isinstance(explicit, list):
        order: list[tuple[int, int, str]] = []
        by_key = {(t.file_id, t.mkv_tid): t for t in tracks}
        for item in explicit:
            if isinstance(item, dict):
                source_index = int(item.get("source", item.get("source_index", 0)))
                tid = int(item.get("id", item.get("mkv_tid", item.get("stream"))))
            else:
                source_index = int(item[0])
                tid = int(item[1])
            track = by_key.get((f"src{source_index}", tid))
            if track is not None and track.enabled:
                order.append((source_index, tid, track.entry_id))
        return order
    return [
        (int(track.file_id.removeprefix("src")), track.mkv_tid, track.entry_id)
        for track in tracks
        if track.enabled
    ]


def _parse_timecode(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        raise ValueError("timecode vide")
    parts = text.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"timecode invalide: {value}")


def _parse_chapters_json(path: Path) -> list[ChapterEntry]:
    data = _load_json(path)
    raw_entries = data.get("chapters", data.get("entries", []))
    if not isinstance(raw_entries, list):
        raise CliError(f"Chapitres JSON invalides : {path}", EXIT_ARGS)
    return [_chapter_from_mapping(item) for item in raw_entries if isinstance(item, dict)]


def _parse_chapters_ffmetadata(path: Path) -> list[ChapterEntry]:
    entries: list[ChapterEntry] = []
    current: dict[str, str] = {}
    in_chapter = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[CHAPTER]":
            if current:
                entries.append(_chapter_from_ffmetadata(current))
            current = {}
            in_chapter = True
            continue
        if not in_chapter or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        current[key.strip().upper()] = value.strip()
    if current:
        entries.append(_chapter_from_ffmetadata(current))
    return entries


def _chapter_from_ffmetadata(data: dict[str, str]) -> ChapterEntry:
    timebase = data.get("TIMEBASE", "1/1000")
    start = float(data.get("START", "0"))
    if timebase == "1/1000":
        seconds = start / 1000
    else:
        try:
            num, den = timebase.split("/", 1)
            seconds = start * float(num) / float(den)
        except Exception:
            seconds = start / 1000
    return ChapterEntry(timecode_s=seconds, name=data.get("TITLE", ""))


def _parse_chapters_ogm(path: Path) -> list[ChapterEntry]:
    times: dict[int, str] = {}
    names: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        match = re.fullmatch(r"CHAPTER(\d{2,})", key.strip(), flags=re.IGNORECASE)
        if match:
            times[int(match.group(1))] = value.strip()
            continue
        match = re.fullmatch(r"CHAPTER(\d{2,})NAME", key.strip(), flags=re.IGNORECASE)
        if match:
            names[int(match.group(1))] = value.strip()
    return [
        ChapterEntry(timecode_s=_parse_timecode(times[index]), name=names.get(index, ""))
        for index in sorted(times)
    ]


def _chapter_from_mapping(item: dict[str, Any]) -> ChapterEntry:
    raw_time = item.get("timestamp", item.get("timecode", item.get("time", item.get("timecode_s"))))
    if raw_time is None:
        raise CliError("Chapitre sans timestamp/timecode.", EXIT_ARGS)
    name = item.get("chaptername", item.get("name", item.get("title", "")))
    return ChapterEntry(timecode_s=_parse_timecode(raw_time), name=str(name or ""))


def _chapter_entries(job: dict[str, Any], infos: list[FileInfo]) -> tuple[bool, list[ChapterEntry] | None, int | None]:
    chapters = job.get("chapters", {})
    if chapters is False:
        return False, None, None
    if not isinstance(chapters, dict):
        return True, None, None

    source_index = chapters.get("source_index")
    selected_source = int(source_index) if source_index is not None else None
    entries: list[ChapterEntry] = []
    if bool(chapters.get("include_source", False)):
        idx = selected_source if selected_source is not None else 0
        if 0 <= idx < len(infos) and infos[idx].chapters is not None:
            entries.extend(infos[idx].chapters.entries)

    import_path = chapters.get("import")
    if import_path:
        path = Path(str(import_path)).expanduser()
        suffix = path.suffix.lower()
        if suffix == ".json":
            entries.extend(_parse_chapters_json(path))
        elif suffix in {".txt", ".ogm"}:
            entries.extend(_parse_chapters_ogm(path))
        else:
            entries.extend(_parse_chapters_ffmetadata(path))

    additions = chapters.get("add", [])
    if isinstance(additions, list):
        entries.extend(_chapter_from_mapping(item) for item in additions if isinstance(item, dict))

    entries = sorted(entries, key=lambda entry: entry.timecode_s)
    return True, entries if entries else None, selected_source


def _resolve_tmdb(job: dict[str, Any], config: AppConfig, first_source: Path, logger: Logger) -> tuple[str, dict[str, str] | None, tuple[str, str] | None]:
    tmdb = job.get("tmdb")
    if not tmdb:
        return "", None, None
    if tmdb is True:
        tmdb = {"enabled": True}
    if not isinstance(tmdb, dict) or not tmdb.get("enabled", True):
        return "", None, None

    fetcher = TmdbFetcher(
        api_key=str(tmdb.get("api_key") or config.tmdb_api_key or ""),
        bearer_token=str(tmdb.get("bearer_token") or config.tmdb_bearer_token or ""),
        language=str(tmdb.get("language") or "fr-FR"),
    )
    kind = str(tmdb.get("kind") or "all")
    season = str(tmdb.get("season") or "")
    episode = str(tmdb.get("episode") or "")
    tmdb_id = tmdb.get("id", tmdb.get("tmdb_id"))
    if tmdb_id:
        title = str(tmdb.get("title") or clean_filename_for_search(first_source) or first_source.stem)
        result = fetcher.search(title, kind=kind, year=str(tmdb.get("year") or ""))[0] if False else None
        from core.media_info_fetcher import MediaSearchResult

        result = MediaSearchResult(
            tmdb_id=int(tmdb_id),
            title=title,
            year=str(tmdb.get("year") or ""),
            kind="movie" if kind not in {"movie", "tv"} else kind,
        )
    else:
        query = str(tmdb.get("query") or clean_filename_for_search(first_source) or first_source.stem)
        year = str(tmdb.get("year") or extract_year_from_filename(first_source) or "")
        results = fetcher.search(query, kind=kind, year=year)
        if not results:
            raise CliError(f"Aucun résultat TMDB pour : {query}", EXIT_VALIDATION)
        result = results[0]
        logger.emit("info", f"TMDB premier résultat retenu : {result.title} ({result.year}) #{result.tmdb_id}")

    details = fetcher.get_details(result, season=season, episode=episode)
    title = details.formatted_container_title()
    cover = (details.cover_url, details.cover_filename) if details.cover_url and details.cover_filename else None
    return title, details.to_mkv_tags(), cover


def build_remux_config(
    job: dict[str, Any],
    config: AppConfig,
    args: argparse.Namespace,
    logger: Logger,
    *,
    cli_inputs: list[str] | None = None,
    cli_output: str | None = None,
) -> RemuxConfig:
    validate_job_contract(job, require_version=False)
    sources, infos, tracks = _inspect_sources(job, config, args, logger, cli_inputs=cli_inputs)
    tracks = apply_track_rules(tracks, job.get("rules", {}))
    _apply_explicit_track_edits(job, tracks)

    output_raw = cli_output or getattr(args, "output", None) or job.get("output")
    if not output_raw:
        raise CliError("Une sortie est requise (`--output` ou `output` JSON).", EXIT_ARGS)
    output = Path(str(output_raw)).expanduser()

    keep_chapters, chapter_overrides, chapter_source_index = _chapter_entries(job, infos)
    tmdb_title = ""
    tmdb_tags = None
    tmdb_cover = None
    try:
        tmdb_title, tmdb_tags, tmdb_cover = _resolve_tmdb(job, config, sources[0].path, logger)
    except TmdbError as exc:
        raise CliError(str(exc), EXIT_VALIDATION) from exc

    tag_overrides = job.get("tag_overrides", None)
    if tmdb_tags:
        tag_overrides = _deep_merge(tmdb_tags, tag_overrides if isinstance(tag_overrides, dict) else {})

    work_dir = Path(str(getattr(args, "work_dir", None) or job.get("work_dir") or config.work_dir)).expanduser()
    return RemuxConfig(
        sources=sources,
        output=output,
        track_order=_track_order(job, tracks),
        keep_chapters=keep_chapters,
        chapter_overrides=chapter_overrides,
        chapter_source_index=chapter_source_index,
        extra_attachments=[Path(str(p)).expanduser() for p in job.get("extra_attachments", [])],
        work_dir=work_dir,
        file_title=str(job.get("file_title") or tmdb_title or ""),
        tag_overrides=tag_overrides if isinstance(tag_overrides, dict) else None,
        tmdb_cover=tmdb_cover,
    )


def _workflow(config: AppConfig, args: argparse.Namespace, logger: Logger) -> RemuxWorkflow:
    return RemuxWorkflow(
        ffmpeg_bin=str(args.ffmpeg or config.tool_ffmpeg),
        ffprobe_bin=str(args.ffprobe or config.tool_ffprobe),
        ffmpeg_threads=args.threads if args.threads is not None else config.ffmpeg_threads,
        writing_application=str(getattr(args, "writing_application", "") or ""),
        generate_nfo=config.generate_nfo if getattr(args, "nfo", None) is None else bool(args.nfo),
        mediainfo_bin=str(args.mediainfo or config.tool_mediainfo),
    )


def _config_to_template(job: dict[str, Any], *, include_output: bool = False) -> dict[str, Any]:
    template = {
        "version": 1,
        "rules": job.get("rules", {}),
        "chapters": job.get("chapters", {}),
        "tmdb": job.get("tmdb", False),
        "extra_attachments": job.get("extra_attachments", []),
        "tag_overrides": job.get("tag_overrides", None),
    }
    if include_output and job.get("output"):
        template["output"] = job["output"]
    return {k: v for k, v in template.items() if v not in ({}, None, False, [])}


def _template_from_info(info: FileInfo, *, output: str = "") -> dict[str, Any]:
    return {
        "version": 1,
        "sources": [{"path": str(info.path), "attachments": "none", "copy_tags": False}],
        "output": output or str(info.path.with_suffix(".remux.mkv")),
        "rules": {
            "normalize_languages": True,
            "tracks": {
                "video": {"include": True},
                "audio": {"include": True},
                "subtitle": {"include": True},
            },
            "rename_patterns": {},
        },
        "chapters": {"source_index": 0},
    }


def serialize_file_info(info: FileInfo) -> dict[str, Any]:
    def track_payload(track: Any, track_type: str) -> dict[str, Any]:
        payload = asdict(track)
        payload.pop("raw", None)
        payload["type"] = track_type
        if hasattr(track, "hdr_type"):
            payload["hdr_type"] = track.hdr_type.label()
        return payload

    return {
        "path": str(info.path),
        "format": info.format,
        "duration_s": info.duration_s,
        "duration": info.duration_human,
        "size_bytes": info.size_bytes,
        "size": info.size_human,
        "bit_rate": info.bit_rate,
        "title": info.title,
        "hdr_type": info.hdr_type.label(),
        "frame_count": info.frame_count,
        "tag_count": info.tag_count,
        "global_tags": info.global_tags,
        "tracks": [
            *(track_payload(track, "video") for track in info.video_tracks),
            *(track_payload(track, "audio") for track in info.audio_tracks),
            *(track_payload(track, "subtitle") for track in info.subtitle_tracks),
        ],
        "attachments": [asdict(att) for att in info.attachments],
        "chapters": [
            {"timecode_s": ch.timecode_s, "timecode": fmt_timecode_display(ch.timecode_s), "name": ch.name}
            for ch in (info.chapters.entries if info.chapters else [])
        ],
    }


def _load_job(args: argparse.Namespace) -> dict[str, Any]:
    job: dict[str, Any] = {}
    require_version = False
    if getattr(args, "config", None):
        loaded = _load_json(Path(args.config).expanduser())
        validate_job_contract(loaded, require_version=True)
        job = _deep_merge(job, loaded)
        require_version = True
    if getattr(args, "template", None):
        template = _load_json(Path(args.template).expanduser())
        validate_job_contract(template, require_version=True)
        job = _deep_merge(template, job)
        require_version = True
    if getattr(args, "input", None):
        job["sources"] = [{"path": item} for item in args.input]
    if getattr(args, "output", None):
        job["output"] = args.output
    if getattr(args, "languages", None):
        langs = _csv_values(args.languages)
        job.setdefault("rules", {}).setdefault("tracks", {}).setdefault("audio", {})["languages"] = langs
        job.setdefault("rules", {}).setdefault("tracks", {}).setdefault("subtitle", {})["languages"] = langs
    if getattr(args, "tmdb", False):
        tmdb = job.setdefault("tmdb", {})
        if not isinstance(tmdb, dict):
            tmdb = {"enabled": True}
            job["tmdb"] = tmdb
        tmdb["enabled"] = True
    if getattr(args, "tmdb_id", None):
        tmdb = job.setdefault("tmdb", {})
        if not isinstance(tmdb, dict):
            tmdb = {"enabled": True}
            job["tmdb"] = tmdb
        tmdb["enabled"] = True
        tmdb["id"] = args.tmdb_id
    validate_job_contract(job, require_version=require_version)
    return job


def cmd_inspect(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    ffprobe = args.ffprobe or config.tool_ffprobe
    mediainfo = args.mediainfo or config.tool_mediainfo
    inspector = FileInspector(ffprobe_bin=str(ffprobe), mediainfo_bin=str(mediainfo))
    infos = [inspector.inspect(Path(value).expanduser()) for value in args.input]
    if args.config_template:
        payload = _template_from_info(infos[0], output=args.output or "")
    else:
        payload = {"files": [serialize_file_info(info) for info in infos]}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return EXIT_OK


def cmd_validate(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    job = _load_job(args)
    remux_config = build_remux_config(job, config, args, logger)
    errors = _workflow(config, args, logger).validate(remux_config)
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    logger.emit("info", "Configuration valide.")
    return EXIT_OK


def cmd_preview(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    job = _load_job(args)
    remux_config = build_remux_config(job, config, args, logger)
    errors = _workflow(config, args, logger).validate(remux_config)
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    print(_workflow(config, args, logger).preview_command(remux_config))
    return EXIT_OK


def _preview_remux_config(args: argparse.Namespace, config: AppConfig, logger: Logger, remux_config: RemuxConfig) -> int:
    errors = _workflow(config, args, logger).validate(remux_config)
    if errors:
        for error in errors:
            logger.emit("error", error)
        return EXIT_VALIDATION
    print(_workflow(config, args, logger).preview_command(remux_config))
    return EXIT_OK


def _run_remux_config(args: argparse.Namespace, config: AppConfig, logger: Logger, remux_config: RemuxConfig) -> int:
    if remux_config.output.exists() and not args.force:
        raise CliError(f"Sortie déjà existante : {remux_config.output} (utiliser --force)", EXIT_EXISTS)
    wf = _workflow(config, args, logger)
    wf.log_message.connect(logger.workflow_log)
    signals = wf.run(remux_config)
    loop = QEventLoop()
    state: dict[str, Any] = {"exit": EXIT_OK, "error": ""}

    def done(message: str = "") -> None:
        if message:
            logger.emit("info", message)
        state["exit"] = EXIT_OK
        loop.quit()

    def failed(message: str, exc: object) -> None:
        logger.emit("error", message, exception=repr(exc))
        state["exit"] = EXIT_WORKFLOW
        state["error"] = message
        loop.quit()

    def cancelled() -> None:
        logger.emit("error", "Opération annulée.")
        state["exit"] = EXIT_WORKFLOW
        loop.quit()

    signals.progress.connect(lambda line: logger.emit("info", line))
    signals.finished.connect(done)
    signals.failed.connect(failed)
    signals.cancelled.connect(cancelled)

    stop = threading.Event()

    def _watch_stdin() -> None:
        # Réservé pour annulation future; garde le CLI non interactif.
        stop.wait()

    watcher = threading.Thread(target=_watch_stdin, daemon=True)
    watcher.start()
    try:
        loop.exec()
    finally:
        stop.set()
    return int(state["exit"])


def cmd_remux(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    job = _load_job(args)
    if args.save:
        _write_json(Path(args.save).expanduser(), _config_to_template(job))
        logger.emit("info", f"Template sauvegardé : {args.save}")
    remux_config = build_remux_config(job, config, args, logger)
    if getattr(args, "dry_run", False):
        return _preview_remux_config(args, config, logger, remux_config)
    return _run_remux_config(args, config, logger, remux_config)


def _batch_jobs(batch: dict[str, Any]) -> list[dict[str, Any]]:
    raw_jobs = batch.get("jobs", batch.get("inputs", []))
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise CliError("Le batch doit contenir `jobs` ou `inputs`.", EXIT_ARGS)
    jobs: list[dict[str, Any]] = []
    for item in raw_jobs:
        if isinstance(item, dict):
            jobs.append(item)
        else:
            jobs.append({"sources": [str(item)]})
    return jobs


def _job_primary_input(job: dict[str, Any]) -> str:
    try:
        first = _source_path_items(job)[0]["path"]
        return str(first)
    except Exception:
        return ""


def cmd_batch(args: argparse.Namespace, config: AppConfig, logger: Logger) -> int:
    template = _load_json(Path(args.template).expanduser())
    validate_job_contract(template, require_version=True)
    batch = _load_json(Path(args.batch).expanduser()) if args.batch else {"inputs": args.input or []}
    failures = 0
    total = 0
    for item in _batch_jobs(batch):
        job_index = total
        total += 1
        job = _deep_merge(template, item)
        validate_job_contract(job, path=f"jobs[{total - 1}]", require_version=True)
        if "output" not in job and args.output_dir:
            first = _source_path_items(job)[0]["path"]
            job["output"] = str(Path(args.output_dir).expanduser() / (Path(str(first)).stem + ".mkv"))
        input_label = _job_primary_input(job)
        output_label = str(job.get("output") or "")
        logger.emit(
            "info",
            f"Batch job {job_index + 1} démarré",
            event="batch_job",
            job_index=job_index,
            input=input_label,
            output=output_label,
            status="started",
        )
        try:
            remux_config = build_remux_config(job, config, args, logger)
            if getattr(args, "dry_run", False):
                rc = _preview_remux_config(args, config, logger, remux_config)
            else:
                rc = _run_remux_config(args, config, logger, remux_config)
            if rc != EXIT_OK:
                failures += 1
                logger.emit(
                    "error",
                    f"Batch job {job_index + 1} échoué",
                    event="batch_job",
                    job_index=job_index,
                    input=input_label,
                    output=str(remux_config.output),
                    status="failed",
                    exit_code=rc,
                )
            else:
                logger.emit(
                    "info",
                    f"Batch job {job_index + 1} terminé",
                    event="batch_job",
                    job_index=job_index,
                    input=input_label,
                    output=str(remux_config.output),
                    status="success",
                )
        except Exception as exc:
            failures += 1
            logger.emit(
                "error",
                f"Job batch échoué : {exc}",
                event="batch_job",
                job_index=job_index,
                input=input_label,
                output=output_label,
                status="failed",
                exception=repr(exc),
            )
            if not args.continue_on_error:
                break
    logger.emit("info", f"Batch terminé : {total - failures}/{total} succès.", event="batch_summary", total=total, failures=failures)
    return EXIT_OK if failures == 0 else EXIT_PARTIAL


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Fichier JSON job/template.")
    parser.add_argument("--ffmpeg", help="Chemin ffmpeg override.")
    parser.add_argument("--ffprobe", help="Chemin ffprobe override.")
    parser.add_argument("--mediainfo", help="Chemin mediainfo override.")
    parser.add_argument("--work-dir", help="Répertoire de travail override.")
    parser.add_argument("--threads", type=int, help="Nombre de threads ffmpeg.")
    parser.add_argument("--log-format", choices=("text", "jsonl"), default="text")
    parser.add_argument("--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mediarecode-cli", description="Mediarecode headless CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="Inspecter une ou plusieurs sources.")
    _add_common_options(inspect)
    inspect.add_argument("input", nargs="+")
    inspect.add_argument("--config-template", action="store_true")
    inspect.add_argument("--output")
    inspect.set_defaults(func=cmd_inspect)

    for name, help_text, func in (
        ("validate", "Valider une config remux.", cmd_validate),
        ("preview", "Afficher la commande ffmpeg prévue.", cmd_preview),
        ("remux", "Exécuter un remux.", cmd_remux),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_common_options(p)
        p.add_argument("-i", "--input", action="append")
        p.add_argument("-o", "--output")
        p.add_argument("--languages", help="Langues audio/sous-titres autorisées, séparées par virgules.")
        p.add_argument("--tmdb", action="store_true", help="Activer TMDB; premier résultat si pas d'ID.")
        p.add_argument("--tmdb-id", type=int, help="ID TMDB explicite.")
        p.add_argument("--force", action="store_true", help="Autoriser l'écrasement de la sortie.")
        p.add_argument("--dry-run", action="store_true", help="Valider et afficher la commande sans exécuter.")
        p.add_argument("--nfo", dest="nfo", action="store_true", default=None)
        p.add_argument("--no-nfo", dest="nfo", action="store_false")
        p.add_argument("--writing-application", default="")
        if name == "remux":
            p.add_argument("--save", help="Sauvegarder les options/règles en template réutilisable.")
        p.set_defaults(func=func)

    batch = sub.add_parser("batch", help="Appliquer un template à plusieurs jobs.")
    _add_common_options(batch)
    batch.add_argument("--template", required=True)
    batch.add_argument("--batch", help="JSON contenant `jobs` ou `inputs`.")
    batch.add_argument("-i", "--input", action="append", help="Entrée batch simple; répétable.")
    batch.add_argument("--output-dir")
    batch.add_argument("--force", action="store_true")
    batch.add_argument("--dry-run", action="store_true", help="Valider et afficher les commandes sans exécuter.")
    batch.add_argument("--continue-on-error", action="store_true")
    batch.add_argument("--nfo", dest="nfo", action="store_true", default=None)
    batch.add_argument("--no-nfo", dest="nfo", action="store_false")
    batch.add_argument("--writing-application", default="")
    batch.set_defaults(func=cmd_batch)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _ensure_qcore_app([sys.argv[0], *argv])
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = Logger(fmt=args.log_format)
    try:
        config = AppConfig()
        return int(args.func(args, config, logger))
    except ToolNotFoundError as exc:
        logger.emit("error", str(exc))
        return EXIT_TOOL
    except CliError as exc:
        logger.emit("error", str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        logger.emit("error", "Interrompu.")
        return EXIT_WORKFLOW
    except Exception as exc:
        logger.emit("error", str(exc), exception=repr(exc))
        return EXIT_WORKFLOW


if __name__ == "__main__":
    raise SystemExit(main())
