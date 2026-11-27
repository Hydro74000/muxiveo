"""Recalage SRT, WebVTT et ASS sans modifier le contenu des répliques."""
from __future__ import annotations

import re
from pathlib import Path

from core.workflows.sync_calibration import SyncCalibration

#: Description sonore entre crochets ou parenthèses, marque des sous-titres SDH.
SOUND_DESCRIPTION = re.compile(
    r"[\[(](?:music|musique|rires|laughter|applause|applaudissements|cris|screams|gunshots|tirs)[^\])]*[\])]", re.I)
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


_ASS_KARAOKE = re.compile(r"(\\(?:kf|ko|k|K))(\d+(?:\.\d+)?)")
_ASS_TIMED_TAG = re.compile(r"\\(move|fade|fad|t)\(")


def _scaled_number(value: str, ratio: float) -> str:
    """Temps d'une balise ASS mis à l'échelle (entier conservé entier)."""
    try:
        number = float(value)
    except ValueError:
        return value
    scaled = number * ratio
    return str(round(scaled)) if "." not in value.strip() else f"{scaled:g}"


def _split_args(inner: str) -> list[str]:
    """Arguments d'une balise, séparés au premier niveau de parenthèses."""
    args: list[str] = []
    current: list[str] = []
    depth = 0
    for char in inner:
        if char == "," and depth == 0:
            args.append("".join(current))
            current = []
            continue
        depth += char == "("
        depth -= char == ")"
        current.append(char)
    args.append("".join(current))
    return args


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _scale_ass_override_times(text: str, ratio: float) -> str:
    """Conversion de cadence : temps internes des balises (relatifs au début de l'événement, ms ;
    karaoké en centièmes) étirés comme la chronologie, l'animation reste calée sur l'image.
    \\move(…, t1, t2), \\t(t1, t2, …), \\fad(entrée, sortie), \\fade(…, t1, t2, t3, t4), \\k."""
    if abs(ratio - 1.0) < 1e-12 or "{" not in text:
        return text
    text = _ASS_KARAOKE.sub(lambda match: match[1] + _scaled_number(match[2], ratio), text)
    result: list[str] = []
    position = 0
    while (match := _ASS_TIMED_TAG.search(text, position)) is not None:
        start = match.end()
        depth, end = 1, start
        while end < len(text) and depth:
            depth += text[end] == "("
            depth -= text[end] == ")"
            end += 1
        if depth:
            break  # parenthèse non fermée : balise laissée telle quelle
        args = _split_args(text[start:end - 1])
        name = match[1]
        timed: tuple[int, ...]
        if name == "move" and len(args) == 6:
            timed = (4, 5)
        elif name == "fad" and len(args) == 2:
            timed = (0, 1)
        elif name == "fade" and len(args) == 7:
            timed = (3, 4, 5, 6)
        elif name == "t" and len(args) >= 3 and _is_number(args[0]) and _is_number(args[1]):
            timed = (0, 1)
        else:
            timed = ()
        for index in timed:
            args[index] = _scaled_number(args[index], ratio)
        result.append(text[position:start] + ",".join(args) + ")")
        position = end
    result.append(text[position:])
    return "".join(result)


def shift_text(text: str, calibration: SyncCalibration, *, suffix=".srt") -> str:
    suffix = suffix.lower()
    if suffix not in {".srt", ".ass", ".ssa", ".vtt"}:
        raise ValueError("Sous-titres pris en charge : SRT, ASS, SSA, VTT.")
    newline = "\r\n" if "\r\n" in text else "\n"
    if suffix in {".ass", ".ssa"}:
        ratio = calibration.cadence_mismatch.time_stretch_ratio if calibration.cadence_mismatch else 1.0
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
                if "text" in fields:
                    values[fields.index("text")] = _scale_ass_override_times(values[fields.index("text")], ratio)
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
               or SOUND_DESCRIPTION.search(text))
    return {"forced": forced, "hearing_impaired": sdh}
