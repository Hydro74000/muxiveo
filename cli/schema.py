"""JSON schema for the public CLI job contract."""

from __future__ import annotations

from typing import Any

from cli.constants import FLAG_NAMES, TRACK_TYPES


def build_cli_json_schema() -> dict[str, Any]:
    flag_properties = {name: {"type": "boolean"} for name in FLAG_NAMES}
    condition_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "all": {"type": "array", "items": {"$ref": "#/$defs/condition"}},
            "any": {"type": "array", "items": {"$ref": "#/$defs/condition"}},
            "not": {"$ref": "#/$defs/condition"},
            "language": {"type": "string"},
            "languages": {"type": "array", "items": {"type": "string"}},
            "codec": {"type": "string"},
            "codecs": {"type": "array", "items": {"type": "string"}},
            "channels": {"type": "string"},
            "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
            "title_contains": {"type": "string"},
            "atmos": {"type": "boolean"},
        },
    }
    track_rule_schema = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "include": {"type": "boolean"},
            "languages": {"type": "array", "items": {"type": "string"}},
            "fallback_languages": {"type": "array", "items": {"type": "string"}},
            "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
            "rename_pattern": {"type": "string"},
            "conditions": {"$ref": "#/$defs/condition"},
            "priority": {"type": "array", "items": {"$ref": "#/$defs/condition"}},
            "limit_per_language": {"type": "integer", "minimum": 0},
            "default": {"enum": ["", "first", "first_per_language"]},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://mediarecode.local/schema/cli-job-v1.json",
        "title": "Mediarecode CLI job v1",
        "type": "object",
        "additionalProperties": True,
        "required": ["version"],
        "properties": {
            "version": {"const": 1},
            "sources": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/source"}},
                ]
            },
            "input": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ]
            },
            "output": {"type": "string"},
            "work_dir": {"type": "string"},
            "file_title": {"type": "string"},
            "rules": {"$ref": "#/$defs/rules"},
            "tracks": {"type": "array", "items": {"$ref": "#/$defs/track_edit"}},
            "track_order": {"type": "array", "items": {"$ref": "#/$defs/track_order_item"}},
            "chapters": {"oneOf": [{"const": False}, {"$ref": "#/$defs/chapters"}]},
            "tmdb": {"oneOf": [{"type": "boolean"}, {"$ref": "#/$defs/tmdb"}]},
            "extra_attachments": {"type": "array", "items": {"type": "string"}},
            "tag_overrides": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "$defs": {
            "source": {
                "oneOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "additionalProperties": True,
                        "required": ["path"],
                        "properties": {
                            "path": {"type": "string"},
                            "attachments": {
                                "oneOf": [
                                    {"type": "boolean"},
                                    {"enum": ["all", "none"]},
                                    {"type": "array", "items": {"oneOf": [{"type": "string"}, {"type": "integer"}]}},
                                ]
                            },
                            "copy_tags": {"type": "boolean"},
                        },
                    },
                ]
            },
            "rules": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "normalize_languages": {"type": "boolean"},
                    "rename_patterns": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {track_type: {"type": "string"} for track_type in sorted(TRACK_TYPES)},
                    },
                    "presets": {"type": "object", "additionalProperties": {"$ref": "#/$defs/rules"}},
                    "use_presets": {"type": "array", "items": {"type": "string"}},
                    "tracks": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {track_type: track_rule_schema for track_type in sorted(TRACK_TYPES)},
                    },
                },
            },
            "condition": condition_schema,
            "track_edit": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "source": {"type": "integer"},
                    "source_index": {"type": "integer"},
                    "id": {"type": "integer"},
                    "mkv_tid": {"type": "integer"},
                    "stream": {"type": "integer"},
                    "enabled": {"type": "boolean"},
                    "language": {"type": "string"},
                    "title": {"type": "string"},
                    "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
                    "time_shift_ms": {"type": "integer"},
                },
            },
            "track_order_item": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "source": {"type": "integer"},
                            "source_index": {"type": "integer"},
                            "id": {"type": "integer"},
                            "mkv_tid": {"type": "integer"},
                            "stream": {"type": "integer"},
                        },
                    },
                    {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 3,
                        "prefixItems": [{"type": "integer"}, {"type": "integer"}, {"type": "string"}],
                    },
                ]
            },
            "chapters": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "source_index": {"type": "integer"},
                    "include_source": {"type": "boolean"},
                    "import": {"type": "string"},
                    "add": {"type": "array", "items": {"$ref": "#/$defs/chapter"}},
                },
            },
            "chapter": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "timestamp": {"oneOf": [{"type": "string"}, {"type": "number"}]},
                    "timecode": {"oneOf": [{"type": "string"}, {"type": "number"}]},
                    "time": {"oneOf": [{"type": "string"}, {"type": "number"}]},
                    "timecode_s": {"oneOf": [{"type": "string"}, {"type": "number"}]},
                    "chaptername": {"type": "string"},
                    "name": {"type": "string"},
                    "title": {"type": "string"},
                },
            },
            "tmdb": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "kind": {"enum": ["all", "movie", "tv"]},
                    "query": {"type": "string"},
                    "title": {"type": "string"},
                    "year": {"type": "string"},
                    "season": {"type": "string"},
                    "episode": {"type": "string"},
                    "language": {"type": "string"},
                    "api_key": {"type": "string"},
                    "bearer_token": {"type": "string"},
                    "id": {"type": "integer"},
                    "tmdb_id": {"type": "integer"},
                },
            },
        },
    }
