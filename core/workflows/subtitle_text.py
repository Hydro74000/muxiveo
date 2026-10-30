"""Texte des pistes de sous-titres pour la détection des forcés et des SDH.

Matroska : blocs lus directement par l'index (Cues) quand il couvre chaque
bloc de la piste, soit quelques kilo-octets au lieu du fichier entier. Sinon
une seule passe FFmpeg par fichier extrait toutes les pistes demandées.
"""
from __future__ import annotations

import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from core.subprocess_utils import ProbeCancelledError, run_probe
from core.workflows.preparation_cache import PreparationCache, SubtitleTextSummary, file_identity
from core.workflows.subtitle_sync import SOUND_DESCRIPTION

#: Codecs FFmpeg dont le texte est lisible (les sous-titres image ne le sont pas).
TEXT_SUBTITLE_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"})

_SRT_CODEC_IDS = frozenset({"S_TEXT/UTF8", "S_TEXT/ASCII"})
_ASS_CODEC_IDS = frozenset({"S_TEXT/ASS", "S_TEXT/SSA"})
#: Codec FFmpeg → CodecID Matroska attendu pour la même piste.
_MATROSKA_CODEC_IDS = {
    "subrip": _SRT_CODEC_IDS, "srt": _SRT_CODEC_IDS, "text": _SRT_CODEC_IDS,
    "ass": _ASS_CODEC_IDS, "ssa": _ASS_CODEC_IDS,
    "webvtt": frozenset({"S_TEXT/WEBVTT", "D_WEBVTT/SUBTITLES", "D_WEBVTT/CAPTIONS"}),
}
_ASS_OVERRIDE = re.compile(r"\{([^}]*)\}")
_ASS_STYLE_TAG = re.compile(r"\\([ibu])([01])")
#: Lecture FFmpeg complète : délai d'au moins 120 s, étendu à 50 Mo/s pour les gros fichiers.
_FFMPEG_MIN_TIMEOUT_S = 120.0
_FFMPEG_MIN_BYTES_PER_S = 50 * 1024 * 1024


@dataclass(frozen=True)
class SubtitleTextRequest:
    """Piste à lire : index FFmpeg, codec FFmpeg et nombre de blocs annoncé."""

    stream_index: int
    codec: str
    #: Statistiques de l'inspection ; sans elles l'index ne peut être vérifié.
    element_count: int | None = None


@dataclass(frozen=True)
class SubtitleText:
    text: str
    count: int


def subtitle_text_summaries(
    path: Path,
    requests: list[SubtitleTextRequest],
    *,
    ffmpeg: str,
    cache: PreparationCache | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[int, SubtitleTextSummary]:
    """Résumés par index FFmpeg ; seules les pistes absentes du cache sont lues."""
    identity = file_identity(path) if cache is not None else None
    summaries: dict[int, SubtitleTextSummary] = {}
    missing: list[SubtitleTextRequest] = []
    for request in {request.stream_index: request for request in requests}.values():
        cached = cache.subtitle_summary(identity, request.stream_index) if cache is not None and identity else None
        if cached is None:
            missing.append(request)
        else:
            summaries[request.stream_index] = cached
    if not missing:
        return summaries
    texts = extract_subtitle_texts(path, missing, ffmpeg=ffmpeg, cancel_event=cancel_event)
    unchanged = identity is not None and file_identity(path) == identity
    for index, text in texts.items():
        match = SOUND_DESCRIPTION.search(text.text)
        summary = SubtitleTextSummary(text.count, match.group(0) if match else "")
        summaries[index] = summary
        if cache is not None and identity is not None and unchanged:
            cache.store_subtitle_summary(identity, index, summary)
    return summaries


def extract_subtitle_texts(
    path: Path,
    requests: list[SubtitleTextRequest],
    *,
    ffmpeg: str,
    cancel_event: threading.Event | None = None,
) -> dict[int, SubtitleText]:
    """Texte de chaque piste demandée : index Matroska, puis FFmpeg en une passe pour le reste."""
    texts = _indexed_texts(path, requests, cancel_event)
    remaining = sorted({request.stream_index for request in requests} - set(texts))
    if remaining:
        texts.update(_ffmpeg_texts(path, remaining, ffmpeg=ffmpeg, cancel_event=cancel_event))
    return texts


def _indexed_texts(
    path: Path, requests: list[SubtitleTextRequest], cancel_event: threading.Event | None,
) -> dict[int, SubtitleText]:
    """Pistes dont les Cues désignent exactement chaque bloc ; les autres sont omises.

    L'index ne fait foi que si son nombre d'entrées égale le nombre de blocs des
    statistiques : Cues partiels, statistiques périmées, lacing, compression ou
    chiffrement renvoient la piste à FFmpeg.
    """
    from core.matroska.reader import MatroskaReader

    eligible = [request for request in requests
                if request.element_count and request.codec.casefold() in _MATROSKA_CODEC_IDS]
    if not eligible:
        return {}
    try:
        reader = MatroskaReader(path)
        tracks = reader.tracks()
        encodings = reader.content_encodings_by_track()
    except (OSError, ValueError):
        return {}
    # Index FFmpeg = rang du TrackEntry (même correspondance que l'inspection).
    by_number: dict[int, SubtitleTextRequest] = {}
    for request in eligible:
        if request.stream_index >= len(tracks):
            continue
        track = tracks[request.stream_index]
        if track.codec_id in _MATROSKA_CODEC_IDS[request.codec.casefold()] and not any(encodings[request.stream_index]):
            by_number[track.number] = request
    if not by_number:
        return {}
    try:
        positions = reader.cue_block_positions(set(by_number))
    except (OSError, ValueError):
        return {}
    if positions is None:
        return {}
    by_number = {number: request for number, request in by_number.items()
                 if len(positions[number]) == request.element_count}
    owners = {position: number for number in by_number for position in positions[number]}
    frames: dict[int, list[bytes]] = {number: [] for number in by_number}
    try:
        for position, number, payloads in reader.blocks_at(owners):
            if cancel_event is not None and cancel_event.is_set():
                raise ProbeCancelledError("Lecture des sous-titres annulée.")
            if number != owners[position]:
                return {}
            frames[number].extend(payloads)
    except (OSError, ValueError):
        return {}
    texts: dict[int, SubtitleText] = {}
    for number, request in by_number.items():
        # Comme FFmpeg, qui n'écrit aucune entrée SRT pour un bloc sans texte.
        events = [text for text in (_event_text(request.codec.casefold(), payload) for payload in frames[number])
                  if text.strip()]
        texts[request.stream_index] = SubtitleText("\n\n".join(events), len(events))
    return texts


def _event_text(codec: str, payload: bytes) -> str:
    """Texte d'un bloc, comme dans le SRT produit par FFmpeg (balises i/b/u conservées)."""
    text = payload.decode("utf-8", "replace")
    if codec in {"ass", "ssa"}:
        # ReadOrder, Layer, Style, Name, MarginL, MarginR, MarginV, Effect, Text
        fields = text.split(",", 8)
        text = fields[8] if len(fields) == 9 else text
        text = _ASS_OVERRIDE.sub(
            lambda block: "".join(f"<{'/' if state == '0' else ''}{tag}>"
                                  for tag, state in _ASS_STYLE_TAG.findall(block.group(1))),
            text,
        )
        text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    return text


def _ffmpeg_texts(
    path: Path, stream_indexes: list[int], *, ffmpeg: str, cancel_event: threading.Event | None,
) -> dict[int, SubtitleText]:
    """Une seule lecture du fichier pour toutes les pistes (une sortie SRT par piste)."""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    timeout = max(_FFMPEG_MIN_TIMEOUT_S, size / _FFMPEG_MIN_BYTES_PER_S)
    with tempfile.TemporaryDirectory(prefix="muxiveo-subtitles-") as directory:
        outputs = {index: Path(directory) / f"{index}.srt" for index in stream_indexes}
        command = [str(ffmpeg), "-nostdin", "-v", "error", "-y", "-i", str(path)]
        for index, output in outputs.items():
            command += ["-map", f"0:{index}", "-c:s", "srt", "-f", "srt", str(output)]
        result = run_probe(command, timeout=timeout, cancel_event=cancel_event)
        if result.returncode:
            raise ValueError(result.stderr or "Extraction des sous-titres impossible.")
        texts: dict[int, SubtitleText] = {}
        for index, output in outputs.items():
            text = output.read_text(encoding="utf-8", errors="replace") if output.exists() else ""
            texts[index] = SubtitleText(text, text.count(" --> "))
        return texts
