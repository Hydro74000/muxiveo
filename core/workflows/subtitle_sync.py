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


def _number(value: float) -> str:
    """Nombre d'une balise ASS : entier si possible, sinon deux décimales au plus."""
    rounded = round(value, 2)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:g}"


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


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _retime_ass_override(text: str, ratio: float, *, elapsed_ms: float = 0.0, duration_ms: float = 0.0,
                         split: bool = False, last: bool = True) -> str:
    """Temps internes des balises (relatifs au début de l'événement, ms ; karaoké en centièmes) d'un
    morceau d'événement : étirés par la conversion de cadence (`ratio`) et, pour un morceau qui ne
    commence pas au début de l'événement (`elapsed_ms` déjà écoulées, coupe ou début rogné), repris
    là où l'animation en était. `split` : événement découpé (animations sur toute sa durée rendues
    explicites, fondu de sortie sur le dernier morceau seulement)."""
    if (abs(ratio - 1.0) < 1e-12 and not split) or "{" not in text:
        return text

    def out(source_ms: float) -> int:
        """Temps source (depuis le début de l'événement) → temps du morceau, étiré."""
        return round((source_ms - elapsed_ms) * ratio)

    karaoke_source = [0.0]
    karaoke_out = [0]

    def karaoke(match: re.Match) -> str:
        # Bornes cumulées étirées puis différences : aucune dérive d'arrondi syllabe après syllabe.
        karaoke_source[0] += float(match[2]) * 10
        end = round(max(0.0, karaoke_source[0] - elapsed_ms) * ratio / 10)
        value, karaoke_out[0] = end - karaoke_out[0], end
        return match[1] + str(value)

    text = _ASS_KARAOKE.sub(karaoke, text)
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
        name, args = match[1], _split_args(text[start:end - 1])
        values = [_float(arg) for arg in args]
        replacement = None
        if name == "move" and len(args) in {4, 6} and all(value is not None for value in values):
            x1, y1, x2, y2 = (float(value or 0.0) for value in values[:4])
            t1, t2 = (float(values[4] or 0.0), float(values[5] or 0.0)) if len(args) == 6 else (0.0, 0.0)
            if not t1 and not t2:
                t1, t2 = 0.0, duration_ms  # mouvement sur tout l'événement
            if not split:
                replacement = (f"\\move({','.join(args[:4])},{out(t1)},{out(t2)})" if len(args) == 6
                               else text[match.start():end])
            elif elapsed_ms >= t2:
                replacement = f"\\pos({_number(x2)},{_number(y2)})"
            elif elapsed_ms > t1:
                k = (elapsed_ms - t1) / (t2 - t1)
                replacement = (f"\\move({_number(x1 + (x2 - x1) * k)},{_number(y1 + (y2 - y1) * k)},"
                               f"{_number(x2)},{_number(y2)},0,{out(t2)})")
            else:
                replacement = f"\\move({','.join(args[:4])},{out(t1)},{out(t2)})"
        elif name == "fad" and len(args) == 2 and None not in values:
            fade_in, fade_out = float(values[0] or 0.0), float(values[1] or 0.0)
            fade_in = max(0.0, fade_in - elapsed_ms)
            fade_out = fade_out if last else 0.0
            replacement = f"\\fad({round(fade_in * ratio)},{round(fade_out * ratio)})"
        elif name == "fade" and len(args) == 7 and None not in values:
            times = ",".join(str(out(float(value or 0.0))) for value in values[3:])
            replacement = f"\\fade({','.join(args[:3])},{times})"
        elif name == "t":
            timed = len(args) >= 3 and values[0] is not None and values[1] is not None
            if timed or split:
                t1, t2 = (float(values[0] or 0.0), float(values[1] or 0.0)) if timed else (0.0, duration_ms)
                if not t2:
                    t2 = duration_ms  # fin 0 : jusqu'à la fin de l'événement (libass, VSFilter)
                rest = args[2:] if timed else args
                o1, o2 = out(t1), out(t2)
                if o2 <= 0:
                    # Transformation déjà achevée : jamais une fin à 0, qui la rejouerait sur le morceau.
                    o1, o2 = -2, -1
                replacement = f"\\t({o1},{o2},{','.join(rest)})"
        result.append(text[position:match.start()] + (replacement or text[match.start():end]))
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
                event_start, event_end = parse_time(values[a]), parse_time(values[b])
                pieces = list(calibration.source_intervals(event_start, event_end))
                # Fin rognée (coupe négative) : le dernier morceau s'arrête avant la fin de l'événement.
                tail_clipped = bool(pieces) and (
                    pieces[-1][2] + (pieces[-1][1] - pieces[-1][0]) / ratio < event_end - 0.5)
                split = (len(pieces) > 1 or (bool(pieces) and pieces[0][2] > event_start + 0.5)
                         or tail_clipped)
                text_index = fields.index("text") if "text" in fields else None
                for piece, (start, end, source_start) in enumerate(pieces):
                    clone = list(values)
                    if text_index is not None:
                        clone[text_index] = _retime_ass_override(
                            values[text_index], ratio, elapsed_ms=max(0.0, source_start - event_start),
                            duration_ms=event_end - event_start, split=split,
                            last=piece == len(pieces) - 1 and not tail_clipped,
                        )
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
