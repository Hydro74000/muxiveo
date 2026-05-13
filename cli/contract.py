"""Validation of the public CLI JSON contract."""

from __future__ import annotations

from typing import Any

from cli.constants import FLAG_NAMES
from cli.errors import ContractError


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


def _validate_flags(errors: list[str], path: str, flags: Any) -> None:
    if not isinstance(flags, dict):
        errors.append(f"{path}: attendu object, reçu {_type_name(flags)}")
        return
    for flag, expected in flags.items():
        flag_path = f"{path}.{flag}"
        if flag not in FLAG_NAMES:
            errors.append(f"{flag_path}: flag inconnu")
        elif not isinstance(expected, bool):
            errors.append(f"{flag_path}: attendu bool, reçu {_type_name(expected)}")

def _validate_track_edit(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: attendu object, reçu {_type_name(value)}")
        return
    if "selector" in value:
        _validate_selector(errors, f"{path}.selector", value["selector"])
    for key in ("source", "source_index", "id", "mkv_tid", "stream", "time_shift_ms"):
        if key in value:
            _expect(errors, f"{path}.{key}", value[key], "integer", _is_int)
    if "enabled" in value:
        _expect(errors, f"{path}.enabled", value["enabled"], "bool", _is_bool)
    for key in ("language", "title"):
        if key in value:
            _expect(errors, f"{path}.{key}", value[key], "string", _is_string)
    if "flags" in value:
        _validate_flags(errors, f"{path}.flags", value["flags"])


def _validate_selector(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: attendu object, reçu {_type_name(value)}")
        return
    for key in ("source", "source_index", "position", "type_index", "id", "mkv_tid", "stream"):
        if key in value:
            _expect(errors, f"{path}.{key}", value[key], "integer", _is_int)
    for key in ("type", "track_type", "codec", "language", "lang", "channels", "audio_object", "title", "title_contains", "display_contains", "entry_id"):
        if key in value:
            _expect(errors, f"{path}.{key}", value[key], "string", _is_string)
    if "codecs" in value:
        _validate_string_list(errors, f"{path}.codecs", value["codecs"])
    if "languages" in value:
        _validate_string_list(errors, f"{path}.languages", value["languages"])
    if "atmos" in value:
        _expect(errors, f"{path}.atmos", value["atmos"], "bool", _is_bool)
    if "flags" in value:
        _validate_flags(errors, f"{path}.flags", value["flags"])


def _validate_audio_variant(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, dict):
        errors.append(f"{path}: attendu object, reçu {_type_name(value)}")
        return
    selector = value.get("source_selector", value.get("selector"))
    if selector is None:
        errors.append(f"{path}.selector: champ requis")
    else:
        _validate_selector(errors, f"{path}.selector", selector)
    for key in ("codec", "target_codec", "language", "title", "entry_id"):
        if key in value:
            _expect(errors, f"{path}.{key}", value[key], "string", _is_string)
    if "bitrate_kbps" in value:
        _expect(errors, f"{path}.bitrate_kbps", value["bitrate_kbps"], "integer", _is_int)
    if "enabled" in value:
        _expect(errors, f"{path}.enabled", value["enabled"], "bool", _is_bool)
    if "flags" in value:
        _validate_flags(errors, f"{path}.flags", value["flags"])


def _validate_track_order(errors: list[str], path: str, value: Any) -> None:
    if not isinstance(value, list):
        errors.append(f"{path}: attendu array, reçu {_type_name(value)}")
        return
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if isinstance(item, dict):
            if "selector" in item:
                _validate_selector(errors, f"{item_path}.selector", item["selector"])
                continue
            for key in ("source", "source_index"):
                if key in item:
                    _expect(errors, f"{item_path}.{key}", item[key], "integer", _is_int)
            track_keys = [key for key in ("id", "mkv_tid", "stream") if key in item]
            if not track_keys:
                errors.append(f"{item_path}.id: champ requis")
            for key in track_keys:
                _expect(errors, f"{item_path}.{key}", item[key], "integer", _is_int)
            continue
        if not isinstance(item, list):
            errors.append(f"{item_path}: attendu object ou array, reçu {_type_name(item)}")
            continue
        if len(item) < 2:
            errors.append(f"{item_path}: attendu au moins 2 éléments")
            continue
        for part_index, expected in ((0, "source"), (1, "id")):
            _expect(errors, f"{item_path}[{part_index}]", item[part_index], f"{expected} integer", _is_int)
        if len(item) > 2:
            _expect(errors, f"{item_path}[2]", item[2], "entry_id string", _is_string)


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
    if "cover" in value:
        _expect(errors, f"{path}.cover", value["cover"], "bool", _is_bool)
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
    if "output_template" in job:
        _expect(errors, f"{path}.output_template", job["output_template"], "string", _is_string)
    if "output_all" in job:
        _expect(errors, f"{path}.output_all", job["output_all"], "boolean", lambda value: isinstance(value, bool))
    if "work_dir" in job:
        _expect(errors, f"{path}.work_dir", job["work_dir"], "string", _is_string)
    if "file_title" in job:
        _expect(errors, f"{path}.file_title", job["file_title"], "string", _is_string)
    if "tracks" in job:
        tracks = job["tracks"]
        if not isinstance(tracks, list):
            errors.append(f"{path}.tracks: attendu array[object], reçu {_type_name(tracks)}")
        else:
            for index, item in enumerate(tracks):
                _validate_track_edit(errors, f"{path}.tracks[{index}]", item)
    if "track_order" in job:
        _validate_track_order(errors, f"{path}.track_order", job["track_order"])
    if "audio_variants" in job:
        variants = job["audio_variants"]
        if not isinstance(variants, list):
            errors.append(f"{path}.audio_variants: attendu array[object], reçu {_type_name(variants)}")
        else:
            for index, item in enumerate(variants):
                _validate_audio_variant(errors, f"{path}.audio_variants[{index}]", item)
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


def validate_batch_contract(batch: dict[str, Any], *, path: str = "$") -> None:
    errors: list[str] = []
    if not isinstance(batch, dict):
        raise ContractError([f"{path}: attendu object, reçu {_type_name(batch)}"])

    raw_jobs = batch.get("jobs", batch.get("inputs"))
    raw_key = "jobs" if "jobs" in batch else "inputs"
    if raw_jobs is None:
        errors.append(f"{path}.jobs: champ requis")
    elif not isinstance(raw_jobs, list):
        errors.append(f"{path}.{raw_key}: attendu array, reçu {_type_name(raw_jobs)}")
    elif not raw_jobs:
        errors.append(f"{path}.{raw_key}: ne doit pas être vide")
    else:
        for index, item in enumerate(raw_jobs):
            item_path = f"{path}.{raw_key}[{index}]"
            if isinstance(item, dict):
                try:
                    validate_job_contract(item, path=item_path, require_version=False)
                except ContractError as exc:
                    errors.extend(exc.errors)
            elif not isinstance(item, str):
                errors.append(f"{item_path}: attendu string ou object, reçu {_type_name(item)}")

    if errors:
        raise ContractError(errors)
