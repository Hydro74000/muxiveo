"""JSON schema for the public CLI job contracts."""

from __future__ import annotations

from typing import Any

from core.jobs.constants import (
    FLAG_NAMES,
    MUX_BACKEND_CHOICES,
    SYNC_MODES,
    SYNC_REWRITE_MODES,
    SYNC_SUBTITLE_MODES,
    TMDB_KINDS,
    TRACK_TYPES,
)
from core.version import APP_NAME, APP_SCHEMA_BASE_URL


def _flag_properties() -> dict[str, dict[str, str]]:
    return {name: {"type": "boolean"} for name in FLAG_NAMES}


def _selector_schema(flag_properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "source": {"type": "integer"},
            "source_index": {"type": "integer"},
            "type": {"enum": sorted(TRACK_TYPES)},
            "track_type": {"enum": sorted(TRACK_TYPES)},
            "position": {"type": "integer"},
            "type_index": {"type": "integer"},
            "position_order": {"enum": ["source"]},
            "id": {"type": "integer"},
            "mkv_tid": {"type": "integer"},
            "stream": {"type": "integer"},
            "codec": {"type": "string"},
            "codecs": {"type": "array", "items": {"type": "string"}},
            "language": {"type": "string"},
            "lang": {"type": "string", "description": "Alias de language."},
            "languages": {"type": "array", "items": {"type": "string"}},
            "channels": {"type": "string"},
            "atmos": {"type": "boolean"},
            "audio_object": {"type": "string"},
            "title": {"type": "string"},
            "title_contains": {"type": "string"},
            "display_contains": {"type": "string"},
            "entry_id": {"type": "string"},
            "resolution": {
                "oneOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
                            "bucket": {"type": "string"},
                        },
                    },
                ]
            },
            "video_flags_hex": {"type": "string"},
            "video_flags": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
        },
    }


def _track_edit_schema(flag_properties: dict[str, Any]) -> dict[str, Any]:
    return {
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
            "remove_closed_captions": {"type": "boolean"},
            "language": {"type": "string"},
            "title": {"type": "string"},
            "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
            "time_shift_ms": {"type": "integer"},
            "sync_calibration": {"type": ["object", "null"]},
            "sync_rewrite_mode": {"type": "string", "enum": list(SYNC_REWRITE_MODES)},
            "sync": sync_spec_schema(track_level=True),
        },
    }


def sync_spec_schema(*, track_level: bool = False) -> dict[str, Any]:
    """``sources[i].sync`` / ``tracks[i].sync`` : synchro face à la piste d'une autre source."""
    selector = {"oneOf": [{"type": "integer"}, {"type": "string", "pattern": "^[asv]:[0-9]+$"}]}
    properties: dict[str, Any] = {
        "reference": {"type": "integer", "minimum": 0, "default": 0,
                      "description": "Index de la source de référence."},
        "reference_track": {**selector, "description": "Piste de référence (mkv_tid ou a:N / s:N ; défaut : 1re audio)."},
        "mode": {"enum": ["auto", "quick", "deep"], "default": "auto",
                 "description": "Mesure : réglage [sync] quick_audio_check, synchro rapide ou approfondie."},
        "type": {"enum": ["auto", "audio", "subtitle"], "default": "auto"},
        "calibration": {"oneOf": [{"type": "object"}, {"type": "string"}],
                        "description": "Calibration relative donnée (objet ou fichier JSON) : aucune mesure."},
        "offset_ms": {"type": "integer", "description": "Décalage relatif constant donné : aucune mesure."},
    }
    if not track_level:
        properties["track"] = {**selector, "description": "Piste mesurée (défaut : audio dans la langue de la référence)."}
    return {"type": "object", "additionalProperties": False, "properties": properties}


def _variables_schema() -> dict[str, Any]:
    alias_section = {"type": "object", "additionalProperties": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "aliases": {
                "type": "object",
                "additionalProperties": alias_section,
                "properties": {
                    "*": alias_section,
                },
            },
            "codec_names": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        },
    }


def _encode_schema() -> dict[str, Any]:
    """Bloc ``encode`` (contrat strict de la couche de service)."""
    from core.service.schemas.encode import EncodeBlock

    return EncodeBlock.json_schema()


def build_cli_json_schema() -> dict[str, Any]:
    flag_properties = _flag_properties()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{APP_SCHEMA_BASE_URL}/cli-job-v1.json",
        "title": f"{APP_NAME} CLI job v1",
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
            "output_template": {"type": "string"},
            "output_all": {"type": "boolean"},
            "work_dir": {"type": "string"},
            "sync_mode": {"enum": list(SYNC_MODES), "default": "container"},
            "sync_subtitles": {"enum": list(SYNC_SUBTITLE_MODES), "default": "mirror"},
            "crossfade_ms": {"type": "integer", "minimum": 0, "maximum": 1000},
            "clean_nfo": {"type": "boolean", "default": True},
            "sync_calibrations": {"type": "object", "additionalProperties": {"type": "object"}},
            "auto_sync": {"type": "boolean"},
            "auto_sync_all": {"type": "boolean"},
            "calibration": {"type": "string"},
            "detect_cuts": {"type": "boolean"},
            "drift_threshold_ms": {"type": "integer", "minimum": 1},
            "tmdb_cover": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
            "mux_backend": {"enum": list(MUX_BACKEND_CHOICES)},
            "file_title": {"type": "string"},
            "name": {"type": "string", "description": "Nom du workflow (exact-job enregistré)."},
            "fallback_profile": {
                "oneOf": [{"type": "string"},
                          {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}}}],
                "description": "Profil de repli du workflow (nom, ou objet {\"name\": …}).",
            },
            "variables": _variables_schema(),
            "tracks": {"type": "array", "items": {"$ref": "#/$defs/track_edit"}},
            "track_order": {"type": "array", "items": {"$ref": "#/$defs/track_order_item"}},
            "audio_variants": {"type": "array", "items": {"$ref": "#/$defs/audio_variant"}},
            "encode": _encode_schema(),
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
                            "playlist": {"type": "integer"},
                            "attachments": {
                                "oneOf": [
                                    {"type": "boolean"},
                                    {"enum": ["all", "none"]},
                                    {"type": "array", "items": {"oneOf": [{"type": "string"}, {"type": "integer"}]}},
                                ]
                            },
                            "copy_tags": {"type": "boolean"},
                            "sync": sync_spec_schema(),
                        },
                    },
                ]
            },
            "selector": _selector_schema(flag_properties),
            "track_edit": _track_edit_schema(flag_properties),
            "track_order_item": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {
                            "selector": {"$ref": "#/$defs/selector"},
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
            "audio_variant": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "selector": {"$ref": "#/$defs/selector"},
                    "source_selector": {"$ref": "#/$defs/selector"},
                    "entry_id": {"type": "string"},
                    "codec": {"type": "string"},
                    "target_codec": {"type": "string"},
                    "bitrate_kbps": {"type": "integer"},
                    "enabled": {"type": "boolean"},
                    "language": {"type": "string"},
                    "title": {"type": "string"},
                    "flags": {"type": "object", "additionalProperties": False, "properties": flag_properties},
                    "time_shift_ms": {"type": "integer"},
                    "sync_calibration": {"type": ["object", "null"]},
                    "sync_rewrite_mode": {"type": "string", "enum": list(SYNC_REWRITE_MODES)},
                },
            },
            "chapters": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "source_index": {"type": ["integer", "null"]},
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
                    "kind": {"enum": list(TMDB_KINDS)},
                    "query": {"type": "string"},
                    "title": {"type": "string"},
                    "year": {"type": "string"},
                    "season": {"type": "string"},
                    "episode": {"type": "string"},
                    "language": {"type": "string"},
                    "api_key": {"type": "string"},
                    "bearer_token": {"type": "string"},
                    "cover": {"type": "boolean"},
                    "auto_detect_episode": {"type": "boolean"},
                    "auto_metadata": {"type": "boolean"},
                    "id": {"type": "integer"},
                    "tmdb_id": {"type": "integer"},
                },
            },
        },
    }


def build_cli_json_schema_bundle() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Muxiveo CLI schemas",
        "oneOf": [
            build_cli_json_schema(),
            build_exact_job_schema_v1(),
            build_decision_profile_schema_v1(),
        ],
    }


def build_exact_job_schema_v1() -> dict[str, Any]:
    """Schema for exact remux jobs used as strict reusable templates."""
    base = build_cli_json_schema()
    schema = dict(base)
    schema["$id"] = f"{APP_SCHEMA_BASE_URL}/exact-job-v1.json"
    schema["title"] = f"{APP_NAME} exact job v1"
    schema["properties"] = dict(base["properties"])
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
            "expr": {"type": "string"},
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
                    "remove_track_tags",
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
            "clear": {"type": "array", "items": {"enum": ["title", "language"]}, "uniqueItems": True},
            "order_priority": {"type": "integer", "minimum": 1},
            "match_bitrate": {"type": "boolean"},
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
        "$id": f"{APP_SCHEMA_BASE_URL}/decision-profile-v1.json",
        "title": f"{APP_NAME} decision profile v1",
        "type": "object",
        "additionalProperties": True,
        "required": ["version", "kind", "name"],
        "properties": {
            "version": {"const": 1},
            "kind": {"const": "decision-profile"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "auto_sync": {"type": "boolean"},
            "sync": {"type": "boolean"},
            "detect_cuts": {"type": "boolean"},
            "drift_threshold_ms": {"type": "integer", "minimum": 1},
            "output_template": {"type": "string"},
            "variables": {
                **_variables_schema(),
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


def unknown_job_keys(job: Any, *, path: str = "job") -> list[str]:
    """Chemins des clés absentes du contrat (job v1 ou exact-job) ; clés internes ``_…`` et ``$…`` ignorées.

    Objets fermés compris (``sources[i].sync``, ``flags``…). Libres : ``variables`` (noms choisis par
    l'utilisateur) et les tables (``tag_overrides``, ``sync_calibrations``). Le bloc ``encode`` (contrat
    strict propre) est vérifié par sa propre lecture.
    """
    if not isinstance(job, dict):
        return []
    schema = build_exact_job_schema_v1() if job.get("kind") == "exact-job" else build_cli_json_schema()
    found: list[str] = []
    _walk(job, schema, schema.get("$defs", {}), path, found, skip={"encode", "variables"})
    return found


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        return defs.get(ref.removeprefix("#/$defs/"), {})
    return node


def _walk(value: Any, node: dict[str, Any], defs: dict[str, Any], path: str, found: list[str],
          skip: set[str] = frozenset()) -> None:  # type: ignore[assignment]
    node = _resolve(node, defs)
    variants = node.get("oneOf") or node.get("anyOf")
    if variants:
        for variant in variants:
            variant = _resolve(variant, defs)
            if (isinstance(value, dict) and variant.get("type") == "object") or (
                    isinstance(value, list) and variant.get("type") == "array"):
                _walk(value, variant, defs, path, found)
                return
        return
    if isinstance(value, dict) and "properties" in node:
        properties = node["properties"]
        extra = node.get("additionalProperties", True)
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in skip or str(key).startswith(("_", "$")):
                continue
            if key in properties:
                _walk(item, properties[key], defs, child, found)
            elif not isinstance(extra, dict):  # table (schéma de valeur) : noms de clés libres
                found.append(child)
    elif isinstance(value, list) and isinstance(node.get("items"), dict):
        for index, item in enumerate(value):
            _walk(item, node["items"], defs, f"{path}[{index}]", found)
