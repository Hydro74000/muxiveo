"""JSON schema for the public CLI job contracts."""

from __future__ import annotations

from typing import Any

from cli.constants import FLAG_NAMES, TRACK_TYPES


def _flag_properties() -> dict[str, dict[str, str]]:
    return {name: {"type": "boolean"} for name in FLAG_NAMES}


def _condition_schema(flag_properties: dict[str, Any]) -> dict[str, Any]:
    return {
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


def _track_rule_schema(flag_properties: dict[str, Any]) -> dict[str, Any]:
    return {
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


def build_cli_json_schema() -> dict[str, Any]:
    flag_properties = _flag_properties()
    condition_schema = _condition_schema(flag_properties)
    track_rule_schema = _track_rule_schema(flag_properties)
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


def build_cli_json_schema_v2() -> dict[str, Any]:
    """Schema for the hybrid GUI/CLI v2 job contract."""
    v1_schema = build_cli_json_schema()
    v1_defs = v1_schema["$defs"]
    flag_properties = _flag_properties()
    selector_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "source": {"type": "integer"},
            "source_index": {"type": "integer"},
            "type": {"enum": sorted(TRACK_TYPES)},
            "track_type": {"enum": sorted(TRACK_TYPES)},
            "position": {"type": "integer"},
            "type_index": {"type": "integer"},
            "id": {"type": "integer"},
            "mkv_tid": {"type": "integer"},
            "stream": {"type": "integer"},
            "codec": {"type": "string"},
            "codecs": {"type": "array", "items": {"type": "string"}},
            "language": {"type": "string"},
            "languages": {"type": "array", "items": {"type": "string"}},
            "channels": {"type": "string"},
            "atmos": {"type": "boolean"},
            "audio_object": {"type": "string"},
            "title": {"type": "string"},
            "title_contains": {"type": "string"},
            "display_contains": {"type": "string"},
            "entry_id": {"type": "string"},
            "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
        },
    }
    track_edit_schema = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "selector": {"$ref": "#/$defs/selector"},
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
    }
    encode_track_schema = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "selector": {"$ref": "#/$defs/selector"},
            "codec": {"type": "string"},
            "quality_mode": {"enum": ["crf", "cq", "bitrate", "size"]},
            "crf": {"type": "integer"},
            "cq": {"type": "integer"},
            "bitrate_kbps": {"type": "integer"},
            "target_size_mb": {"type": "integer"},
            "preset": {"type": "string"},
            "extra_params": {"type": "string"},
            "force_8bit": {"type": "boolean"},
            "force_10bit": {"type": "boolean"},
            "inject_hdr_meta": {"type": "boolean"},
            "master_display": {"type": "string"},
            "max_cll": {"type": "string"},
            "copy_dv": {"type": "boolean"},
            "copy_hdr10plus": {"type": "boolean"},
            "dovi_profile": {"type": "string"},
            "tonemap_to_sdr": {"type": "boolean"},
            "tonemap_algorithm": {"type": "string"},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://mediarecode.local/schema/hybrid-job-v2.json",
        "title": "Mediarecode hybrid GUI/CLI job v2",
        "type": "object",
        "additionalProperties": True,
        "required": ["version"],
        "properties": {
            "version": {"const": 2},
            "kind": {"type": "string"},
            "name": {"type": "string"},
            "fallback_profile": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "object", "additionalProperties": True, "properties": {"name": {"type": "string"}}},
                ]
            },
            "sources": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/source"}},
                ]
            },
            "input": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            "output": {"type": "string"},
            "work_dir": {"type": "string"},
            "file_title": {"type": "string"},
            "rules": v1_defs["rules"],
            "tracks": {"type": "array", "items": track_edit_schema},
            "track_order": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {"type": "object", "additionalProperties": True, "properties": {"selector": {"$ref": "#/$defs/selector"}}},
                        v1_defs["track_order_item"],
                    ]
                },
            },
            "audio_variants": {"type": "array", "items": {"$ref": "#/$defs/audio_variant"}},
            "encode": {"$ref": "#/$defs/encode"},
            "encoding": {"$ref": "#/$defs/encode"},
            "chapters": v1_defs["chapters"],
            "tmdb": v1_defs["tmdb"],
            "extra_attachments": {"type": "array", "items": {"type": "string"}},
            "tag_overrides": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "$defs": {
            **v1_defs,
            "selector": selector_schema,
            "audio_variant": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "selector": {"$ref": "#/$defs/selector"},
                    "source_selector": {"$ref": "#/$defs/selector"},
                    "codec": {"type": "string"},
                    "target_codec": {"type": "string"},
                    "bitrate_kbps": {"type": "integer"},
                    "enabled": {"type": "boolean"},
                    "language": {"type": "string"},
                    "title": {"type": "string"},
                    "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
                },
            },
            "encode": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "video": encode_track_schema,
                    "video_tracks": {"type": "array", "items": encode_track_schema},
                    "audio_tracks": {"type": "array", "items": encode_track_schema},
                },
            },
        },
    }


def build_cli_json_schema_bundle() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Mediarecode CLI schemas",
        "oneOf": [
            build_cli_json_schema(),
            build_cli_json_schema_v2(),
            build_exact_job_schema_v1(),
            build_decision_profile_schema_v1(),
        ],
    }


def build_exact_job_schema_v1() -> dict[str, Any]:
    """Schema for exact remux jobs used as strict reusable templates."""
    v2 = build_cli_json_schema_v2()
    schema = dict(v2)
    schema["$id"] = "https://mediarecode.local/schema/exact-job-v1.json"
    schema["title"] = "Mediarecode exact job v1"
    schema["properties"] = dict(v2["properties"])
    schema["properties"]["version"] = {"const": 1}
    schema["properties"]["kind"] = {"const": "exact-job"}
    schema["required"] = ["version", "kind"]
    return schema


def build_decision_profile_schema_v1() -> dict[str, Any]:
    """Schema for decision-profile v1 low-code automapping profiles."""
    flag_properties = _flag_properties()
    condition_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "all": {"type": "array", "items": {"$ref": "#/$defs/condition"}},
            "any": {"type": "array", "items": {"$ref": "#/$defs/condition"}},
            "not": {"$ref": "#/$defs/condition"},
            "field": {"type": "string"},
            "op": {"type": "string"},
            "value": {},
            "required": {"type": "boolean"},
            "weight": {"type": "integer"},
        },
    }
    action_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "required": ["type"],
        "properties": {
            "type": {
                "enum": [
                    "set_enabled",
                    "set_language",
                    "set_title",
                    "set_time_shift_ms",
                    "set_flags",
                    "add_track_tags",
                    "set_order_priority",
                    "create_audio_variant",
                ]
            },
            "value": {},
            "mode": {"enum": ["priority", "override", "add"]},
            "write_mode": {"enum": ["priority", "override", "add"]},
            "pattern": {"type": "string"},
            "codec": {"type": "string"},
            "target_codec": {"type": "string"},
            "bitrate_kbps": {"type": "integer"},
            "language": {"type": "string"},
            "title": {"type": "string"},
            "title_pattern": {"type": "string"},
            "enabled": {"type": "boolean"},
            "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
        },
    }
    rule_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "required": ["id", "match", "actions"],
        "properties": {
            "id": {"type": "string"},
            "label": {"type": "string"},
            "group_id": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "enabled": {"type": "boolean"},
            "priority": {"type": "integer"},
            "mode": {"enum": ["priority", "override", "add"]},
            "write_mode": {"enum": ["priority", "override", "add"]},
            "scope": {"enum": ["all", "first", "best"]},
            "tie_break": {"type": "string"},
            "match": {"$ref": "#/$defs/condition"},
            "actions": {"type": "array", "items": {"$ref": "#/$defs/action"}},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://mediarecode.local/schema/decision-profile-v1.json",
        "title": "Mediarecode decision profile v1",
        "type": "object",
        "additionalProperties": True,
        "required": ["version", "kind", "name"],
        "properties": {
            "version": {"const": 1},
            "kind": {"const": "decision-profile"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "variables": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "codec_names": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
            },
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "enabled": {"type": "boolean"},
                        "priority": {"type": "integer"},
                    },
                },
            },
            "selection_policy": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "disable_unmatched_types": {
                        "type": "array",
                        "items": {"enum": sorted(TRACK_TYPES)},
                    }
                },
            },
            "rules": {"type": "array", "items": {"$ref": "#/$defs/rule"}},
        },
        "$defs": {
            "condition": condition_schema,
            "action": action_schema,
            "rule": rule_schema,
        },
    }
