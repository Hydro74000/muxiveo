"""Classification opt-in, à partir de métadonnées et de texte extrait."""
from __future__ import annotations

import subprocess

from core.i18n import translate_text
from core.subprocess_utils import subprocess_text_kwargs
from core.workflows.subtitle_sync import subtitle_hints


def classify_subtitles(sources, infos, *, ffmpeg, forced=False, sdh=False, threshold=50):
    if not (forced or sdh):
        return
    if threshold <= 0:
        raise ValueError("Le seuil des sous-titres forcés doit être positif.")
    audios = [t for s in sources for t in s.tracks if t.enabled and t.track_type == "audio"]
    default = next((t for t in audios if t.flag_default), audios[0] if audios else None)
    if default is None:
        return
    for source, info in zip(sources, infos):
        subtitles = {t.index: t for t in info.subtitle_tracks}
        for track in source.tracks:
            if not track.enabled or track.track_type != "subtitle":
                continue
            details = subtitles.get(track.mkv_tid)
            count = getattr(details, "element_count", None)
            text = ""
            if track.codec.casefold() in {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}:
                result = subprocess.run([str(ffmpeg), "-nostdin", "-v", "error", "-i", str(source.path),
                    "-map", f"0:{track.mkv_tid}", "-c:s", "srt", "-f", "srt", "pipe:1"],
                    capture_output=True, timeout=120, **subprocess_text_kwargs())
                if result.returncode:
                    raise ValueError(result.stderr or "Extraction des sous-titres impossible.")
                text = result.stdout
                count = text.count(" --> ")
            hints = subtitle_hints(text, count=count, language=track.language,
                audio_language=default.language, threshold=threshold, title=track.title)
            if forced and hints["forced"]:
                track.flag_forced = track.flag_default = True
                track.title = f"{track.language} ({translate_text('Forcé')})"
            if sdh and hints["hearing_impaired"]:
                track.flag_hearing_impaired = True
                track.title = f"{track.language} (SDH)"


def forced_first(order, sources):
    """Place les forcés avant les complets de même langue, sans trier les autres pistes."""
    tracks = {(s.file_index, t.mkv_tid, t.entry_id): t for s in sources for t in s.tracks}
    def get(item):
        return tracks.get(tuple(item)) if len(item) > 2 else next((t for k, t in tracks.items() if k[:2] == tuple(item)), None)
    result = list(order)
    for item in list(order):
        track = get(item)
        if track is None or track.track_type != "subtitle" or not track.flag_forced:
            continue
        result.remove(item)
        target = next((index for index, other in enumerate(result)
                       if (candidate := get(other)) is not None and candidate.track_type == "subtitle"
                       and candidate.language == track.language and not candidate.flag_forced), len(result))
        result.insert(target, item)
    return result
