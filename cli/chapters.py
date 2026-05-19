"""Chapter import helpers for CLI remux jobs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.inspector import ChapterEntry, FileInfo

from cli.constants import EXIT_ARGS
from cli.errors import CliError
from cli.json_io import load_json


def parse_timecode(value: Any) -> float:
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
    data = load_json(path)
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
        ChapterEntry(timecode_s=parse_timecode(times[index]), name=names.get(index, ""))
        for index in sorted(times)
    ]


def _chapter_from_mapping(item: dict[str, Any]) -> ChapterEntry:
    raw_time = item.get("timestamp", item.get("timecode", item.get("time", item.get("timecode_s"))))
    if raw_time is None:
        raise CliError("Chapitre sans timestamp/timecode.", EXIT_ARGS)
    name = item.get("chaptername", item.get("name", item.get("title", "")))
    return ChapterEntry(timecode_s=parse_timecode(raw_time), name=str(name or ""))


def chapter_entries(job: dict[str, Any], infos: list[FileInfo]) -> tuple[bool, list[ChapterEntry] | None, int | None]:
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
        if 0 <= idx < len(infos):
            source_chapters = infos[idx].chapters
            if source_chapters is not None:
                entries.extend(source_chapters.entries)

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
