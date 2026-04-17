"""Text renderer (report-driven)."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol, Sequence


class _TextReport(Protocol):
    tracks: list[Any]

    def first_track(self, kind: str) -> Any: ...


def render_text(
    report: _TextReport,
    *,
    raw: bool = False,
    text_field_order: Mapping[str, Sequence[str]] | None = None,
    text_labels: Mapping[str, str] | None = None,
    text_raw_field_map: Mapping[str, Sequence[tuple[str, str]]] | None = None,
    int_or_none: Callable[[Any], int | None] | None = None,
    should_display_text_field: Callable[[Any, str], bool] | None = None,
    format_text_field_value: Callable[..., str] | None = None,
    format_text_raw_value: Callable[..., str] | None = None,
) -> str:
    if (
        text_field_order is None
        or text_labels is None
        or text_raw_field_map is None
        or int_or_none is None
        or should_display_text_field is None
        or format_text_field_value is None
        or format_text_raw_value is None
    ):
        raise ValueError("Renderer dependencies missing for text renderer")

    if raw:
        return _render_text_raw_impl(
            report=report,
            text_raw_field_map=text_raw_field_map,
            int_or_none=int_or_none,
            should_display_text_field=should_display_text_field,
            format_text_raw_value=format_text_raw_value,
        )
    return _render_text_impl(
        report=report,
        text_field_order=text_field_order,
        text_labels=text_labels,
        int_or_none=int_or_none,
        should_display_text_field=should_display_text_field,
        format_text_field_value=format_text_field_value,
    )


def _render_text_impl(
    *,
    report: _TextReport,
    text_field_order: Mapping[str, Sequence[str]],
    text_labels: Mapping[str, str],
    int_or_none: Callable[[Any], int | None],
    should_display_text_field: Callable[[Any, str], bool],
    format_text_field_value: Callable[..., str],
) -> str:
    chunks: list[str] = []
    counters: dict[str, int] = {}
    totals: dict[str, int] = {}
    for track in report.tracks:
        totals[track.kind] = totals.get(track.kind, 0) + 1
    general = report.first_track("General")
    general_size = int_or_none(general.fields.get("FileSize")) if general else None
    for track in report.tracks:
        counters[track.kind] = counters.get(track.kind, 0) + 1
        idx = counters[track.kind]
        if track.kind == "General":
            title = track.kind
        else:
            title = f"{track.kind} #{idx}" if totals.get(track.kind, 0) > 1 else track.kind
        chunks.append(title)

        ordered = text_field_order.get(track.kind, [])
        rendered_keys: set[str] = set()

        for key in ordered:
            raw = track.fields.get(key, "")
            if raw == "" and key == "extra.dialnorm_Maximum":
                raw = track.fields.get("extra.dialnorm_Minimum", "")
            if raw == "":
                continue
            if not should_display_text_field(track, key):
                continue
            display = text_labels.get(key, key.replace("_", " "))
            value = format_text_field_value(
                key=key,
                value=raw,
                track=track,
                general_file_size=general_size,
            )
            if value == "":
                continue
            chunks.append(f"{display:<40} : {value}")
            rendered_keys.add(key)

        if not ordered:
            for key, value in track.fields.items():
                if key in rendered_keys:
                    continue
                if key.startswith("_") or key.startswith("extra."):
                    continue
                if value == "":
                    continue
                display = text_labels.get(key, key.replace("_", " "))
                text_value = format_text_field_value(
                    key=key,
                    value=value,
                    track=track,
                    general_file_size=general_size,
                )
                if text_value == "":
                    continue
                chunks.append(f"{display:<40} : {text_value}")
        if track.kind == "General":
            for key, value in track.fields.items():
                if key in rendered_keys or not key.startswith("extra.") or value == "":
                    continue
                display = key.split(".", 1)[1]
                text_value = format_text_field_value(
                    key=key,
                    value=value,
                    track=track,
                    general_file_size=general_size,
                )
                if text_value == "":
                    continue
                chunks.append(f"{display:<40} : {text_value}")
        chunks.append("")
    return "\n".join(chunks) + "\n\n"


def _render_text_raw_impl(
    *,
    report: _TextReport,
    text_raw_field_map: Mapping[str, Sequence[tuple[str, str]]],
    int_or_none: Callable[[Any], int | None],
    should_display_text_field: Callable[[Any, str], bool],
    format_text_raw_value: Callable[..., str],
) -> str:
    chunks: list[str] = []
    counters: dict[str, int] = {}
    totals: dict[str, int] = {}
    for track in report.tracks:
        totals[track.kind] = totals.get(track.kind, 0) + 1
    general = report.first_track("General")
    general_size = int_or_none(general.fields.get("FileSize")) if general else None
    for track in report.tracks:
        counters[track.kind] = counters.get(track.kind, 0) + 1
        idx = counters[track.kind]
        if track.kind == "General":
            title = track.kind
        else:
            title = f"{track.kind} #{idx}" if totals.get(track.kind, 0) > 1 else track.kind
        chunks.append(title)
        pairs = text_raw_field_map.get(track.kind, [])
        rendered_keys: set[str] = set()
        for display, key in pairs:
            raw = track.fields.get(key, "")
            if raw == "" and key == "extra.dialnorm_Maximum":
                raw = track.fields.get("extra.dialnorm_Minimum", "")
            if raw == "":
                continue
            if not should_display_text_field(track, key):
                continue
            value = format_text_raw_value(
                display_key=display,
                source_key=key,
                value=raw,
                track=track,
                general_file_size=general_size,
            )
            if value == "":
                continue
            chunks.append(f"{display:<32} : {value}")
            rendered_keys.add(key)
        if track.kind == "General":
            for key, raw in track.fields.items():
                if key in rendered_keys or not key.startswith("extra.") or raw == "":
                    continue
                display = key.split(".", 1)[1]
                value = format_text_raw_value(
                    display_key=display,
                    source_key=key,
                    value=raw,
                    track=track,
                    general_file_size=general_size,
                )
                if value == "":
                    continue
                chunks.append(f"{display:<32} : {value}")
        elif not pairs:
            for key, raw in track.fields.items():
                if key in rendered_keys or key.startswith("_") or key.startswith("extra.") or raw == "":
                    continue
                value = format_text_raw_value(
                    display_key=key,
                    source_key=key,
                    value=raw,
                    track=track,
                    general_file_size=general_size,
                )
                if value == "":
                    continue
                chunks.append(f"{key:<32} : {value}")
        chunks.append("")
    return "\n".join(chunks) + "\n\n"
