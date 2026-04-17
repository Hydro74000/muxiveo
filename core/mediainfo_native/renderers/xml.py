"""XML renderer (report-driven)."""

from __future__ import annotations

from typing import Any, Callable, Protocol


class _XmlReport(Protocol):
    source: str
    tracks: list[Any]


def render_xml(
    report: _XmlReport,
    *,
    structured_order_for_track: Callable[[Any], list[str]] | None = None,
    xml_escape: Callable[[str], str] | None = None,
    string: Callable[[Any], str] | None = None,
) -> str:
    if (
        structured_order_for_track is None
        or xml_escape is None
        or string is None
    ):
        raise ValueError("Renderer dependencies missing for xml renderer")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<MediaInfo",
        '    xmlns="https://mediaarea.net/mediainfo"',
        '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '    xsi:schemaLocation="https://mediaarea.net/mediainfo https://mediaarea.net/mediainfo/mediainfo_2_0.xsd"',
        '    version="2.0">',
        '<creatingLibrary version="26.01" url="https://mediaarea.net/MediaInfo">MediaInfoLib</creatingLibrary>',
        f'<media ref="{xml_escape(report.source)}">',
    ]
    counters: dict[str, int] = {}
    totals: dict[str, int] = {}
    for track in report.tracks:
        totals[track.kind] = totals.get(track.kind, 0) + 1
    for track in report.tracks:
        counters[track.kind] = counters.get(track.kind, 0) + 1
        if track.kind != "General" and totals.get(track.kind, 0) > 1:
            lines.append(
                f'<track type="{xml_escape(track.kind)}" typeorder="{xml_escape(string(counters[track.kind]))}">'
            )
        else:
            lines.append(f'<track type="{xml_escape(track.kind)}">')
        ordered_keys = structured_order_for_track(track)
        for key in ordered_keys:
            value = track.fields.get(key, "")
            if value == "" or key.startswith("_"):
                continue
            lines.append(f"<{key}>{xml_escape(value)}</{key}>")
        extra_items = [
            (k.split(".", 1)[1], v)
            for k, v in track.fields.items()
            if k.startswith("extra.") and v != ""
        ]
        if extra_items:
            lines.append("<extra>")
            for ekey, eval_ in extra_items:
                lines.append(f"<{ekey}>{xml_escape(eval_)}</{ekey}>")
            lines.append("</extra>")
        lines.append("</track>")
    lines.extend(["</media>", "</MediaInfo>", ""])
    return "\n".join(lines) + "\n"
