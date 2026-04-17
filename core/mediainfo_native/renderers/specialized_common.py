"""Shared helpers for specialized renderers (EBUCore/PBCore/MPEG-7)."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Mapping


def public_fields(
    track: Any,
    *,
    structured_field_order: Mapping[str, list[str]],
) -> dict[str, str]:
    ordered: OrderedDict[str, str] = OrderedDict()
    for key in structured_field_order.get(track.kind, list(track.fields.keys())):
        value = track.fields.get(key, "")
        if value == "" or key.startswith("_"):
            continue
        ordered[key] = value
    for key, value in track.fields.items():
        if not key.startswith("extra.") or value == "":
            continue
        ordered[key] = value
    return dict(ordered)


def duration_iso8601_from_ms(duration_ms: int | None) -> str:
    if duration_ms is None:
        return ""
    total_seconds = duration_ms / 1000.0
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = total_seconds % 60
    if hours > 0:
        return f"PT{hours}H{minutes}M{seconds:.3f}S"
    if minutes > 0:
        return f"PT{minutes}M{seconds:.3f}S"
    return f"PT{seconds:.3f}S"
