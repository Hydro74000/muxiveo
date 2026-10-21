"""Classification opt-in, à partir de métadonnées et de texte extrait."""
from __future__ import annotations

from core.i18n import translate_text
from core.workflows.subtitle_sync import subtitle_hints
from core.workflows.subtitle_text import TEXT_SUBTITLE_CODECS, SubtitleTextRequest, subtitle_text_summaries


def classify_subtitles(sources, infos, *, ffmpeg, forced=False, sdh=False, threshold=50, all_audio_languages=False,
                       cache=None, cancel_event=None):
    """Flags forcés / SDH ; le texte de toutes les pistes d'un fichier est lu en une fois."""
    if not (forced or sdh):
        return
    if threshold <= 0:
        raise ValueError("Le seuil des sous-titres forcés doit être positif.")
    audios = [t for s in sources for t in s.tracks if t.enabled and t.track_type == "audio"]
    default = next((t for t in audios if t.flag_default), audios[0] if audios else None)
    if default is None:
        # Sans audio, les forcés n'ont pas de langue de référence ; les SDH restent détectables.
        forced = False
        if not sdh:
            return
    audio_language = default.language if default is not None else ""
    # Avant un profil, la langue audio par défaut n'est pas encore décidée.
    # Détecter les forcés de chaque langue candidate ; le profil tranchera.
    languages = list(dict.fromkeys(t.language for t in audios)) if all_audio_languages else [audio_language]
    for source, info in zip(sources, infos):
        subtitles = {t.index: t for t in info.subtitle_tracks}
        candidates = [track for track in source.tracks if track.enabled and track.track_type == "subtitle"]
        summaries = subtitle_text_summaries(source.path, [
            SubtitleTextRequest(track.mkv_tid, track.codec, getattr(subtitles.get(track.mkv_tid), "element_count", None))
            for track in candidates if track.codec.casefold() in TEXT_SUBTITLE_CODECS
        ], ffmpeg=str(ffmpeg), cache=cache, cancel_event=cancel_event)
        for track in candidates:
            details = subtitles.get(track.mkv_tid)
            count = getattr(details, "element_count", None)
            text = ""
            summary = summaries.get(track.mkv_tid)
            if summary is not None:
                # La première description sonore suffit à la détection SDH.
                text, count = summary.sound_description, summary.count
            hints = subtitle_hints(text, count=count, language=track.language,
                audio_language=audio_language, threshold=threshold, title=track.title)
            forced_hint = forced and (hints["forced"] or (all_audio_languages and any(
                subtitle_hints(text, count=count, language=track.language,
                    audio_language=language, threshold=threshold)["forced"] for language in languages
            )))
            if forced_hint:
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
