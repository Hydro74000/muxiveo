"""Native text/subtitle parser family."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SubripParseResult:
    duration_end_ms: int
    events_total: int
    events_min_duration_ms: int
    lines_count: int
    lines_max_count_per_event: int


_TIMECODE_RE = re.compile(
    r"^\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _parse_subrip(path: Path) -> SubripParseResult:
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = raw.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]

    events_total = 0
    duration_end_ms = 0
    min_duration_ms: int | None = None
    lines_count = 0
    lines_max_count_per_event = 0

    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip() != ""]
        if not lines:
            continue

        tc_index = 0
        if tc_index < len(lines) and lines[tc_index].strip().isdigit():
            tc_index += 1
        if tc_index >= len(lines):
            continue

        match = _TIMECODE_RE.match(lines[tc_index].strip())
        if not match:
            continue

        start_ms = (
            int(match.group(1)) * 3_600_000
            + int(match.group(2)) * 60_000
            + int(match.group(3)) * 1000
            + int(match.group(4).ljust(3, "0")[:3])
        )
        end_ms = (
            int(match.group(5)) * 3_600_000
            + int(match.group(6)) * 60_000
            + int(match.group(7)) * 1000
            + int(match.group(8).ljust(3, "0")[:3])
        )

        events_total += 1
        duration_end_ms = max(duration_end_ms, end_ms)
        event_duration = max(0, end_ms - start_ms)
        if min_duration_ms is None:
            min_duration_ms = event_duration
        else:
            min_duration_ms = min(min_duration_ms, event_duration)

        event_text_lines = [line for line in lines[tc_index + 1:] if line.strip() != ""]
        line_count = len(event_text_lines)
        lines_count += line_count
        lines_max_count_per_event = max(lines_max_count_per_event, line_count)

    return SubripParseResult(
        duration_end_ms=duration_end_ms,
        events_total=events_total,
        events_min_duration_ms=min_duration_ms if min_duration_ms is not None else 0,
        lines_count=lines_count,
        lines_max_count_per_event=lines_max_count_per_event,
    )


def parse_text(source: str) -> dict[str, object]:
    path = Path(source)
    suffix = path.suffix.lower()
    if suffix == ".srt":
        stats = _parse_subrip(path)
        return {
            "container": "text",
            "format": "subrip",
            "stats": stats,
        }
    return {
        "container": "text",
        "format": suffix.lstrip(".") or "text",
    }
