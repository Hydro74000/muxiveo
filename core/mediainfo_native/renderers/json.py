"""JSON renderer (report-driven)."""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, Callable, Protocol


class _JsonReport(Protocol):
    source: str
    tracks: list[Any]


def render_json(
    report: _JsonReport,
    *,
    structured_order_for_track: Callable[[Any], list[str]] | None = None,
    render_oracle_json_track: Callable[[dict[str, Any]], str] | None = None,
    string: Callable[[Any], str] | None = None,
) -> str:
    if (
        structured_order_for_track is None
        or render_oracle_json_track is None
        or string is None
    ):
        raise ValueError("Renderer dependencies missing for json renderer")
    track_payloads: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    totals: dict[str, int] = {}
    for track in report.tracks:
        totals[track.kind] = totals.get(track.kind, 0) + 1
    for track in report.tracks:
        normal_fields: OrderedDict[str, str] = OrderedDict()
        extra_fields: OrderedDict[str, str] = OrderedDict()
        ordered_keys = structured_order_for_track(track)
        for key in ordered_keys:
            value = track.fields.get(key, "")
            if value == "" or key.startswith("_"):
                continue
            normal_fields[key] = value
        for key, value in track.fields.items():
            if key.startswith("extra.") and value != "":
                extra_fields[key.split(".", 1)[1]] = value
        counters[track.kind] = counters.get(track.kind, 0) + 1
        payload: dict[str, Any] = {"@type": track.kind}
        if track.kind != "General" and totals.get(track.kind, 0) > 1:
            payload["@typeorder"] = string(counters[track.kind])
        payload.update(normal_fields)
        if extra_fields:
            payload["extra"] = dict(extra_fields)
        track_payloads.append(payload)

    creating = {"name": "MediaInfoLib", "version": "26.01", "url": "https://mediaarea.net/MediaInfo"}
    tracks_rendered = ",".join(render_oracle_json_track(track) for track in track_payloads)
    lines = [
        "{",
        f'"creatingLibrary":{json.dumps(creating, ensure_ascii=False, separators=(",", ":"))},',
        f'"media":{{"@ref":{json.dumps(report.source, ensure_ascii=False, separators=(",", ":"))},"track":[{tracks_rendered}]}}',
        "}",
    ]
    return "\n".join(lines) + "\n\n"
