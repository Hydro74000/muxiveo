"""Recalage SRT, WebVTT et ASS sans modifier le contenu des répliques."""
from __future__ import annotations

import re
from pathlib import Path

from core.workflows.sync_calibration import SyncCalibration

_TIMING = re.compile(r"(?P<a>(?:\d+:)?\d{2}:\d{2}[,.]\d{3})\s+-->\s+(?P<b>(?:\d+:)?\d{2}:\d{2}[,.]\d{3})(?P<settings>[^\r\n]*)")


def parse_time(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    return sum(float(v) * 60 ** i for i, v in enumerate(reversed(parts))) * 1000


def format_time(value: float, *, ass=False, vtt=False) -> str:
    units = max(0, round(value / (10 if ass else 1)))
    seconds, fraction = divmod(units, 100 if ass else 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if ass:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{'.' if vtt else ','}{fraction:03d}"


def shift_text(text: str, calibration: SyncCalibration, *, suffix=".srt") -> str:
    suffix = suffix.lower()
    if suffix not in {".srt", ".ass", ".ssa", ".vtt"}:
        raise ValueError("Sous-titres pris en charge : SRT, ASS, SSA, VTT.")
    newline = "\r\n" if "\r\n" in text else "\n"
    if suffix in {".ass", ".ssa"}:
        result, fields, events = [], [], False
        for line in text.splitlines():
            if line.startswith("["):
                events = line.strip().lower() == "[events]"
            if events and line.lower().startswith("format:"):
                fields = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
            if events and line.lower().startswith("dialogue:"):
                if "start" not in fields or "end" not in fields:
                    raise ValueError("ASS : Format des événements manquant.")
                values = line.split(":", 1)[1].split(",", len(fields) - 1)
                if len(values) != len(fields):
                    raise ValueError("ASS : événement invalide.")
                a, b = fields.index("start"), fields.index("end")
                for start, end in calibration.intervals(parse_time(values[a]), parse_time(values[b])):
                    clone = list(values)
                    clone[a], clone[b] = format_time(start, ass=True), format_time(end, ass=True)
                    result.append("Dialogue:" + ",".join(clone))
            else:
                result.append(line)
        return newline.join(result) + newline
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    result, counter = [], 0
    for block in blocks:
        match = _TIMING.search(block)
        if not match:
            if "-->" in block:
                raise ValueError("Horodatage de sous-titre invalide.")
            if suffix == ".vtt":
                result.append(block)
            elif block.strip():
                raise ValueError("Bloc SRT invalide.")
            continue
        for start, end in calibration.intervals(parse_time(match["a"]), parse_time(match["b"])):
            counter += 1
            timing = f"{format_time(start, vtt=suffix == '.vtt')} --> {format_time(end, vtt=suffix == '.vtt')}{match['settings']}"
            prefix = f"{counter}{newline}" if suffix == ".srt" else block[:match.start()]
            result.append(prefix + timing + block[match.end():])
    return (newline * 2).join(result) + newline


def shift_file(source: Path, output: Path, calibration: SyncCalibration):
    from core.workflows.workflow_store import atomic_write_text
    with source.open(encoding="utf-8-sig", newline="") as stream:
        content = stream.read()
    atomic_write_text(output, shift_text(content, calibration, suffix=source.suffix))


def subtitle_hints(text: str, *, count: int | None, language: str,
                   audio_language: str, threshold=50, title="") -> dict[str, bool]:
    from core.lang_tags import Rfc5646LanguageTags
    def canonical(value):
        return Rfc5646LanguageTags.to_iso639_2(value) or value.casefold()
    forced = (count is not None and 0 < count < threshold and bool(language)
              and canonical(language) == canonical(audio_language))
    sdh = bool(re.search(r"\bSDH\b|\b(?:hearing impaired|malentendant)\b", title, re.I)
               or re.search(r"[\[(](?:music|musique|rires|laughter|applause|applaudissements|cris|screams|gunshots|tirs)[^\])]*[\])]", text, re.I))
    return {"forced": forced, "hearing_impaired": sdh}
